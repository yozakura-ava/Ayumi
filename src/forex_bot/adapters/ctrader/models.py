from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from core.types import TradeDirection


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class PositionStatus(Enum):
    ENTRY_PENDING = "entry_pending"  # Order sent, not yet filled
    OPEN = "open"  # Position is open
    TP_HIT = "tp_hit"  # Closed by take profit
    SL_HIT = "sl_hit"  # Closed by stop loss
    TIMEOUT_CLOSE = "timeout_close"  # Closed by time limit
    MANUAL_CLOSE = "manual_close"  # Closed manually
    CLOSED = "closed"  # Generic closed (backward compat)

    @property
    def is_closed(self) -> bool:
        """True for any terminal (closed) status."""
        return self in (
            PositionStatus.TP_HIT,
            PositionStatus.SL_HIT,
            PositionStatus.TIMEOUT_CLOSE,
            PositionStatus.MANUAL_CLOSE,
            PositionStatus.CLOSED,
        )


@dataclass
class Order:
    order_id: str
    symbol: str
    direction: TradeDirection
    order_type: OrderType
    volume: float
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    filled_at: datetime | None = None
    filled_price: float | None = None
    comment: str = ""
    # ── Naked-position fix: cTrader positionId (commit 1ed0cdee, 2026-07-06) ──
    # cTrader ``positionId`` from the fill event — required for downstream
    # ``amend_sl_tp`` calls when SL/TP could not be sent inline on the
    # MARKET order. ``None`` until the fill event arrives.
    position_id: int | None = None


@dataclass
class Position:
    position_id: str
    symbol: str
    direction: TradeDirection
    volume: float
    entry_price: float
    current_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    unrealized_pnl: float = 0.0
    status: PositionStatus = PositionStatus.OPEN
    opened_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: datetime | None = None
    closed_price: float | None = None
    closed_pnl: float = 0.0
    comment: str = ""
    # ── Phase 1D: Position monitoring fields ──────────────────────────────
    max_favorable_excursion: float = 0.0  # MFE — best unrealized PnL reached
    max_adverse_excursion: float = 0.0  # MAE — worst unrealized PnL reached
    time_in_trade_sec: float = 0.0  # Seconds since position opened
    high_water_mark: float = 0.0  # Best price seen (for long: highest, for short: lowest)
    low_water_mark: float = 0.0  # Worst price seen (for long: lowest, for short: highest)
    # ── Multi-TP extension (Sprint Task 1.1, card a7b8e896) ───────────────
    # cTrader Open API only accepts a single TP per position. TP2/TP3 are
    # tracked here for monitoring / partial-close logic; tp_levels_fired
    # is the idempotency key that records which levels have already been
    # actioned (e.g. [1] after TP1 fires, [1, 2] after TP2).
    take_profit_2: float | None = None
    take_profit_3: float | None = None
    tp_levels_fired: list[int] = field(default_factory=list)


@dataclass
class CTraderTradeSignal:
    symbol: str
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    volume: float
    confidence: float
    rationale: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    strategy_id: str = ""


@dataclass
class cTraderCredentials:
    host: str
    port: int
    use_ssl: bool = True
    sender_comp_id: str = ""
    target_comp_id: str = "cServer"
    sender_sub_id: str = "TRADE"
    target_sub_id: str = "TRADE"
    username: str = ""  # Account number for FIX logon (tag 553)
    password: str = ""


@dataclass
class AccountInfo:
    account_id: str
    balance: float
    equity: float
    margin_used: float
    margin_available: float
    unrealized_pnl: float = 0.0
    daily_pnl: float = 0.0
    is_demo: bool = True


@dataclass
class MarketDataSnapshot:
    symbol: str
    bid: float
    ask: float
    last: float
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class SymbolInfo:
    """Metadata for a trading symbol used by OrderManager and RiskGuard.

    Use ``from_live_symbol_info()`` to bridge from the live-trading
    ``market_data_feed.SymbolInfo`` populated by ``_fetch_symbol_details``.

    .. deprecated:: 2026-07-30
        ``pip_size`` should be sourced from :func:`utils.pip_value.pip_value_for_symbol`.
        ``SYMBOL_METADATA`` remains as a compatibility layer for ``pip_value_per_lot``
        and contract sizing, but pip_size is now delegated to the canonical source.
    """

    pip_size: float  # e.g., 0.0001 for EURUSD, 0.1 for XAUUSD
    pip_value_per_lot: float  # USD value of 1 pip per standard lot
    lot_size: int = 100_000  # contract size per lot
    contract_size: float = 100_000.0  # same as lot_size but as float for some calcs

    @classmethod
    def from_live_symbol_info(
        cls,
        live: "SymbolInfo",  # type: ignore[assignment]  # forward ref to market_data_feed.SymbolInfo
        pip_value_per_lot: float = 10.0,
    ) -> "SymbolInfo":
        """Bridge from the live-trading ``market_data_feed.SymbolInfo``.

        Args:
            live: A ``market_data_feed.SymbolInfo`` with populated volume fields.
            pip_value_per_lot: Override for non-FX symbols (default 10.0 USD).

        Returns:
            A ``models.SymbolInfo`` suitable for OrderManager / RiskManager.
        """
        return cls(
            pip_size=live.pip_size,
            pip_value_per_lot=pip_value_per_lot,
            lot_size=live.lot_size,
            contract_size=live.contract_size,
        )


# Canonical symbol metadata — replace hardcoded pip heuristics throughout the codebase.
SYMBOL_METADATA: dict[str, SymbolInfo] = {
    "EURUSD": SymbolInfo(pip_size=0.0001, pip_value_per_lot=10.0),
    "GBPUSD": SymbolInfo(pip_size=0.0001, pip_value_per_lot=10.0),
    "USDJPY": SymbolInfo(pip_size=0.01, pip_value_per_lot=6.5),
    "XAUUSD": SymbolInfo(pip_size=0.1, pip_value_per_lot=10.0, lot_size=100, contract_size=100.0),
    "AUDUSD": SymbolInfo(pip_size=0.0001, pip_value_per_lot=10.0),
    "USDCHF": SymbolInfo(pip_size=0.0001, pip_value_per_lot=10.0),
    "USDCAD": SymbolInfo(pip_size=0.0001, pip_value_per_lot=10.0),
}

# Default for unknown FX pairs
_DEFAULT_SYMBOL_INFO = SymbolInfo(pip_size=0.0001, pip_value_per_lot=10.0)


def get_symbol_info(symbol: str) -> SymbolInfo:
    """Look up symbol metadata with canonical pip_size delegation.

    pip_size is sourced from :func:`utils.pip_value.pip_value_for_symbol`
    (the single source of truth). Other fields (pip_value_per_lot, lot_size,
    contract_size) come from ``SYMBOL_METADATA``.

    For unknown symbols, falls back to FX defaults with a warning.
    """
    import logging

    from utils.pip_value import pip_value_for_symbol

    sym_upper = symbol.upper()
    info = SYMBOL_METADATA.get(sym_upper)
    canonical_pip = pip_value_for_symbol(symbol)

    if info is not None:
        # Override pip_size with canonical source to prevent drift.
        if info.pip_size != canonical_pip:
            logging.getLogger(__name__).warning(
                "SymbolInfo.pip_size=%.5f for '%s' differs from canonical "
                "pip_value_for_symbol()=%.5f — using canonical",
                info.pip_size,
                sym_upper,
                canonical_pip,
            )
            return SymbolInfo(
                pip_size=canonical_pip,
                pip_value_per_lot=info.pip_value_per_lot,
                lot_size=info.lot_size,
                contract_size=info.contract_size,
            )
        return info

    # Unknown symbol — use canonical pip_size with FX defaults.
    logging.getLogger(__name__).warning(
        "Unknown symbol '%s' — falling back to FX defaults (pip_size=%.5f, pip_value=10.0/lot)",
        symbol,
        canonical_pip,
    )
    return SymbolInfo(
        pip_size=canonical_pip,
        pip_value_per_lot=_DEFAULT_SYMBOL_INFO.pip_value_per_lot,
        lot_size=_DEFAULT_SYMBOL_INFO.lot_size,
        contract_size=_DEFAULT_SYMBOL_INFO.contract_size,
    )
