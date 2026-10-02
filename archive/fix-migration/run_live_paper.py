"""Live Paper Trading — Lean Blend (5 strategies, M15).

Connects to cTrader market data feed, aggregates ticks into M15 bars,
evaluates strategies on bar close, and executes real orders on the cTrader
DEMO account via the trade port (5202) with FTMO risk guard.

Strategies:
  - SRM XAUUSD, SRM USDJPY, SRM GBPUSD (session_range_mr)
  - TTC XAUUSD, TTC EURUSD (TTC signal engine)

Usage:
  PYTHONPATH=src/forex_bot:src python -m run_live_paper
"""

import logging
import os
import signal as sig
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Load .env before anything else reads env vars
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not _env_path.exists():
        _env_path = Path(__file__).resolve().parents[3] / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass

from adapters.ctrader.api_client import cTraderAPIClient
from adapters.ctrader.market_data_feed import LiveMarketDataFeed, Tick
from adapters.ctrader.models import (
    OrderType,
    TradeSignal,
    cTraderCredentials,
)
from adapters.ctrader.models import (
    TradeDirection as CTradeDirection,
)
from adapters.ctrader.paper_trader import PaperTrader
from adapters.ctrader.risk_guard import FTMOConfig
from adapters.ctrader.trade_logger import TradeLogger
from backtest.engine import Bar, MarketState, SessionType, TradeDirection
from backtest.parameter_sweep.legacy_optimizer import legacy_strategy_factory
from backtest.parameter_sweep.ttc_optimizer import ttc_strategy_factory
from backtest.strategy_legacy import ISignalStrategy

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────

STARTING_BALANCE = 10_000.0
MAX_LOOKBACK_BARS = 200  # keep more than needed for strategies
STATUS_INTERVAL_S = 60
LOG_DIR = "logs/live_paper"

SYMBOLS = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "XAUUSD": "XAU/USD",
}

# cTrader symbol IDs — will be auto-populated, these are common FTMO IDs
SYMBOL_ID_MAP = {
    1: "EURUSD",
    2: "GBPUSD",
    3: "USDJPY",
    31: "XAUUSD",  # common FTMO ID for XAU/USD
}

STRATEGY_PARAMS = {
    "SRM_XAUUSD": {
        "factory": "srm",
        "symbol": "XAUUSD",
        "params": {
            "min_confidence": 0.4,
            "session_range_min_pips": 35.0,
            "session_range_sl_fraction": 0.3,
        },
    },
    "SRM_USDJPY": {
        "factory": "srm",
        "symbol": "USDJPY",
        "params": {
            "min_confidence": 0.5,
            "session_range_min_pips": 20.0,
            "session_range_sl_fraction": 0.9,
        },
    },
    "SRM_GBPUSD": {
        "factory": "srm",
        "symbol": "GBPUSD",
        "params": {
            "min_confidence": 0.4,
            "session_range_min_pips": 25.0,
            "session_range_sl_fraction": 0.8,
        },
    },
    "TTC_XAUUSD": {
        "factory": "ttc",
        "symbol": "XAUUSD",
        "params": {
            "min_confidence": 0.5,
            "min_quality_score": 0.4,
            "mw_base_confidence": 0.45,
            "rsi_divergence_boost": 0.2,
            "htf_trend_aligned_boost": 0.1,
            "htf_opposing_penalty": -0.05,
            "kill_zone_active_boost": -0.15,
            "negative_weight": 0.25,
            "swing_lookback": 3,
            "history_bars": 50,
        },
    },
    "TTC_EURUSD": {
        "factory": "ttc",
        "symbol": "EURUSD",
        "params": {
            "min_confidence": 0.25,
            "min_quality_score": 0.45,
            "mw_base_confidence": 0.4,
            "rsi_divergence_boost": 0.1,
            "htf_trend_aligned_boost": 0.0,
            "htf_opposing_penalty": -0.25,
            "kill_zone_active_boost": -0.15,
            "negative_weight": 1.75,
            "swing_lookback": 7,
            "history_bars": 80,
        },
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────


def _detect_session(dt_utc: datetime) -> SessionType:
    """Determine FX session from UTC hour."""
    h = dt_utc.hour
    if 0 <= h < 7:
        return SessionType.ASIAN
    elif 7 <= h < 12:
        return SessionType.LONDON
    elif 12 <= h < 17:
        return SessionType.NY_AM
    elif 17 <= h < 21:
        return SessionType.NY_PM
    return SessionType.OUTSIDE


def _m15_bar_start(dt_utc: datetime) -> datetime:
    """Floor a datetime to the start of its M15 bar."""
    return dt_utc.replace(minute=(dt_utc.minute // 15) * 15, second=0, microsecond=0)


def _load_credentials() -> cTraderCredentials:
    """Load cTrader credentials for the QUOTE (market data) port."""
    host = os.environ.get("CTRADER_HOST", "")
    readonly_port = int(os.environ.get("CTRADER_READONLY_SSL_PORT", "5211"))
    sender = os.environ.get("CTRADER_SENDER_COMP_ID", "")
    target = os.environ.get("CTRADER_TARGET_COMP_ID", "cServer")
    username = os.environ.get("CTRADER_ACCOUNT", "")
    password = os.environ.get("CTRADER_PASSWORD", "")
    missing = [
        k
        for k, v in [
            ("CTRADER_HOST", host),
            ("CTRADER_SENDER_COMP_ID", sender),
            ("CTRADER_ACCOUNT", username),
            ("CTRADER_PASSWORD", password),
        ]
        if not v
    ]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")
    return cTraderCredentials(
        host=host,
        port=readonly_port,
        use_ssl=True,
        sender_comp_id=sender,
        target_comp_id=target,
        sender_sub_id="QUOTE",
        target_sub_id="QUOTE",
        username=username,
        password=password,
    )


# ── Main System ──────────────────────────────────────────────────────────


@dataclass
class SymbolTracker:
    """Aggregates ticks into M15 bars for one symbol."""

    internal_name: str  # e.g. "EURUSD"
    cTrader_name: str  # e.g. "EUR/USD"
    symbol_id: int | None = None
    ticks: list = field(default_factory=list)
    bars: list[Bar] = field(default_factory=list)
    current_bar: dict = field(default_factory=dict)  # accumulating current bar
    current_bar_start: datetime | None = None
    last_tick: Tick | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_tick(self, tick: Tick):
        with self.lock:
            self.last_tick = tick
            bar_start = _m15_bar_start(tick.timestamp)

            if self.current_bar_start is None or bar_start > self.current_bar_start:
                # Close the previous bar
                if self.current_bar_start is not None and self.current_bar:
                    closed_bar = Bar(
                        time=self.current_bar_start,
                        open=self.current_bar["open"],
                        high=self.current_bar["high"],
                        low=self.current_bar["low"],
                        close=self.current_bar["close"],
                        volume=self.current_bar["volume"],
                    )
                    self.bars.append(closed_bar)
                    if len(self.bars) > MAX_LOOKBACK_BARS:
                        self.bars = self.bars[-MAX_LOOKBACK_BARS:]
                # Start new bar
                self.current_bar = {
                    "open": tick.mid,
                    "high": tick.high if hasattr(tick, "high") else tick.mid,
                    "low": tick.low if hasattr(tick, "low") else tick.mid,
                    "close": tick.mid,
                    "volume": 0.0,
                }
                self.current_bar_start = bar_start
            else:
                # Update current bar
                mid = tick.mid
                self.current_bar["high"] = max(self.current_bar["high"], mid)
                self.current_bar["low"] = min(self.current_bar["low"], mid)
                self.current_bar["close"] = mid
                self.current_bar["volume"] += 1.0

            self.ticks.append(tick)
            if len(self.ticks) > 5000:
                self.ticks = self.ticks[-2000:]

    def try_close_bar(self) -> Bar | None:
        """Check if a bar just closed (called periodically). Returns the closed bar or None."""
        with self.lock:
            if self.current_bar_start is None:
                return None
            now = _m15_bar_start(datetime.now(timezone.utc))
            # Bar is closed if we've moved past the current bar's start
            if now > self.current_bar_start:
                bar = Bar(
                    time=self.current_bar_start,
                    open=self.current_bar["open"],
                    high=self.current_bar["high"],
                    low=self.current_bar["low"],
                    close=self.current_bar["close"],
                    volume=self.current_bar["volume"],
                )
                self.bars.append(bar)
                if len(self.bars) > MAX_LOOKBACK_BARS:
                    self.bars = self.bars[-MAX_LOOKBACK_BARS:]
                # Reset accumulator — next tick will start a new bar
                self.current_bar = {}
                self.current_bar_start = None
                return bar
            return None

    @property
    def spread(self) -> float:
        if self.last_tick:
            return self.last_tick.spread
        return 0.0


class LivePaperTradingSystem:
    def __init__(self, paper_only: bool = False):
        self._paper_only = paper_only
        self._shutdown = False
        self._tick_count = 0
        self._trackers: dict[str, SymbolTracker] = {}
        self._strategies: dict[str, ISignalStrategy] = {}
        self._last_signal_dir: dict[str, TradeDirection | None] = {}
        self._paper_trader: PaperTrader | None = None
        self._trade_logger: TradeLogger | None = None
        self._feed: LiveMarketDataFeed | None = None
        self._api_client: cTraderAPIClient | None = None
        self._status_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_bar_close: dict[str, datetime] = {}
        # Position ID tracking: strategy+symbol → cTrader position_id (tag 721)
        self._position_ids: dict[str, str] = {}
        # Pending SL/TP to attach after fill: clord_id → {signal, strategy_key}
        self._pending_sl_tp: dict[str, dict] = {}
        # Linked SL/TP order IDs per position_id (for cleanup on close)
        self._linked_orders: dict[str, dict[str, str]] = {}

    def _init_strategies(self):
        """Instantiate all 5 strategies."""
        for name, cfg in STRATEGY_PARAMS.items():
            symbol = cfg["symbol"]
            params = cfg["params"]
            if cfg["factory"] == "srm":
                strat = legacy_strategy_factory("session_range_mr", params, symbol=symbol, timeframe="M15")
            else:
                strat = ttc_strategy_factory(params, symbol=symbol, timeframe="M15")
            self._strategies[name] = strat
            self._last_signal_dir[name] = None
            logger.info(f"Strategy initialized: {name}")

    def _init_paper_trader(self):
        ftmo = FTMOConfig(
            daily_loss_limit_pct=0.04,
            total_drawdown_limit_pct=0.07,
            min_risk_reward=0.0,  # strategies manage exits via signal flips
            max_trades_per_day=10,
            max_positions=5,
        )
        self._paper_trader = PaperTrader(
            ftmo_config=ftmo,
            starting_balance=STARTING_BALANCE,
        )
        os.makedirs(LOG_DIR, exist_ok=True)
        self._trade_logger = TradeLogger(log_dir=LOG_DIR, strategy_name="live_paper")

    def _init_trade_connection(self):
        """Connect to cTrader trade port (5212 SSL) for real order execution on DEMO."""
        host = os.environ.get("CTRADER_HOST", "")
        trade_port = int(os.environ.get("CTRADER_TRADE_SSL_PORT", "5212"))
        sender = os.environ.get("CTRADER_SENDER_COMP_ID", "")
        target = os.environ.get("CTRADER_TARGET_COMP_ID", "cServer")
        sub = os.environ.get("CTRADER_SENDER_SUB_ID", "TRADE")
        username = os.environ.get("CTRADER_ACCOUNT", "")
        password = os.environ.get("CTRADER_PASSWORD", "")

        trade_creds = cTraderCredentials(
            host=host,
            port=trade_port,
            use_ssl=True,  # TRADE port uses SSL
            sender_comp_id=sender,
            target_comp_id=target,
            sender_sub_id=sub,
            target_sub_id=sub,
            username=username,
            password=password,
        )

        self._api_client = cTraderAPIClient(trade_creds)
        self._api_client.set_paper_mode(self._paper_only)  # paper=True = local sim only
        connected = self._api_client.connect()
        if not connected:
            raise RuntimeError(f"Failed to connect to cTrader trade port {trade_port}")

        # Wire API client to PaperTrader for live execution
        self._paper_trader.set_api_client(self._api_client)

        # Register execution report callbacks
        self._api_client._client.register_callback("on_order_filled", self._on_order_filled)
        self._api_client._client.register_callback("on_order_new", self._on_order_new)
        self._api_client._client.register_callback("on_order_rejected", self._on_order_rejected)

        # Request existing positions to restore state
        self._sync_existing_positions()

        logger.info("Trade connection established on port %d (SSL)", trade_port)

    def _on_order_new(self, order, msg):
        """Callback when cTrader acknowledges a new order (pending fill)."""
        if order is None:
            return
        logger.info(f"ORDER NEW: {order.order_id} {order.symbol} {order.direction.value} vol={order.volume}")

    def _on_order_filled(self, order, msg):
        """Callback when cTrader fills an order.

        Extracts position_id from the fill report (tag 721, stored in order.comment
        as 'pos_id:XXXXX' by FIXClient) and updates position tracking + PaperTrader.
        """
        if order is None:
            return

        logger.info(
            f"ORDER FILLED: {order.order_id} {order.symbol} {order.direction.value} vol={order.volume} @ {order.filled_price}"  # noqa: E501
        )

        # Extract position_id from order comment (set by FIXClient as "pos_id:XXXXX")
        position_id = None
        if order.comment and "pos_id:" in order.comment:
            try:
                position_id = order.comment.split("pos_id:")[1].split()[0]
            except (IndexError, ValueError):
                logger.warning("Failed to extract position_id from comment: %s", order.comment)

        # Also check raw FIX message tag 721 directly
        if not position_id and msg:
            raw_pos_id = msg.get_field(721)
            if raw_pos_id:
                position_id = raw_pos_id

        if position_id:
            # Try to match by symbol to find the strategy key
            for strat_name, cfg in STRATEGY_PARAMS.items():
                if cfg["symbol"] == order.symbol:
                    key = f"{strat_name}_{order.symbol}"
                    self._position_ids[key] = position_id
                    logger.info(f"Position ID mapped: {key} → {position_id}")
                    break

            # Update PaperTrader position with broker position_id
            positions = self._paper_trader.get_open_positions()
            for pos in positions:
                if pos.symbol == order.symbol and pos.direction == order.direction:
                    self._paper_trader._order_manager.update_position_id(pos.position_id, position_id)
                    break

        # Check if there's a pending SL/TP to attach
        pending = self._pending_sl_tp.pop(order.order_id, None)
        if pending and position_id:
            self._attach_sl_tp(position_id, pending["signal"], pending["strategy_key"])

    def _on_order_rejected(self, order, reason=""):
        """Callback when cTrader rejects an order."""
        if not reason:
            reason = "(no reason provided)"
        logger.warning(f"ORDER REJECTED: {order.order_id} {order.symbol} reason={reason}")

    def _sync_existing_positions(self):
        """Request existing positions from cTrader and populate PaperTrader state."""
        logger.info("Requesting existing positions from cTrader...")
        positions = self._api_client._client.request_positions(req_id="STARTUP_SYNC")
        if not positions:
            logger.info("No existing positions found on startup")
            return
        for pos_id, pos in positions.items():
            logger.info(f"  Existing position: {pos_id} {pos.symbol} {pos.direction.value} vol={pos.volume}")
        logger.info(f"Found {len(positions)} existing position(s) on startup")

    def _attach_sl_tp(self, position_id: str, signal, strategy_key: str):
        """Send SL and TP as linked stop/limit orders via tag 721."""
        symbol = signal.symbol

        sl_orders = []
        tp_orders = []

        if signal.stop_loss and signal.stop_loss > 0:
            sl_order = self._api_client._client.send_order(
                symbol=symbol,
                direction=signal.direction,
                order_type=OrderType.STOP,
                volume=signal.volume,
                price=signal.stop_loss,
                comment=f"SL_{strategy_key}",
                position_id=position_id,
            )
            if sl_order:
                sl_orders.append(sl_order.order_id)
                logger.info(f"SL order sent: {sl_order.order_id} @ {signal.stop_loss} for pos {position_id}")

        if signal.take_profit_1 and signal.take_profit_1 > 0:
            tp_order = self._api_client._client.send_order(
                symbol=symbol,
                direction=signal.direction,
                order_type=OrderType.LIMIT,
                volume=signal.volume,
                price=signal.take_profit_1,
                comment=f"TP_{strategy_key}",
                position_id=position_id,
            )
            if tp_order:
                tp_orders.append(tp_order.order_id)
                logger.info(f"TP order sent: {tp_order.order_id} @ {signal.take_profit_1} for pos {position_id}")

        self._linked_orders[position_id] = {}
        if sl_orders:
            self._linked_orders[position_id]["sl"] = sl_orders[0]
        if tp_orders:
            self._linked_orders[position_id]["tp"] = tp_orders[0]

    def _cancel_linked_orders(self, position_id: str):
        """Cancel any SL/TP orders linked to a position."""
        linked = self._linked_orders.pop(position_id, {})
        for order_type, order_id in linked.items():
            success = self._api_client._client.cancel_order(order_id)
            if success:
                logger.info(f"Cancelled {order_type} order {order_id} for pos {position_id}")
            else:
                logger.warning(f"Failed to cancel {order_type} order {order_id} for pos {position_id}")

    def _init_feed(self):
        creds = _load_credentials()
        self._feed = LiveMarketDataFeed(creds)

        # Add XAU/USD to feed's symbol map (not in DEFAULT_SYMBOLS)
        from adapters.ctrader.market_data_feed import SymbolInfo

        self._feed._symbols[31] = SymbolInfo(symbol_id=31, name="XAU/USD", pip_size=0.01, digits=2)
        self._feed._name_to_id["XAU/USD"] = 31
        self._feed._id_to_name[31] = "XAU/USD"

        # Initialize trackers
        for internal, cTrader in SYMBOLS.items():
            self._trackers[internal] = SymbolTracker(internal_name=internal, cTrader_name=cTrader)

    def _load_historical_bars(self, max_bars: int = 200):
        """Pre-load M15 bars from historical CSV files to avoid 25-hour cold start.

        Looks for {SYMBOL}_M15_2026.csv or {SYMBOL}_M15.csv in data/forex/historical/.
        Loads the most recent max_bars bars for each symbol.
        """
        data_dir = Path(__file__).resolve().parents[2] / "data" / "forex" / "historical"
        if not data_dir.exists():
            logger.warning(f"Historical data directory not found: {data_dir}")
            return

        for internal_name in SYMBOLS:
            csv_name = f"{internal_name}_M15_2026.csv"
            csv_path = data_dir / csv_name
            if not csv_path.exists():
                csv_name = f"{internal_name}_M15.csv"
                csv_path = data_dir / csv_name
            if not csv_path.exists():
                logger.warning(f"No historical M15 data for {internal_name}")
                continue

            tracker = self._trackers.get(internal_name)
            if not tracker:
                continue

            try:
                import csv as csv_mod

                bars_loaded = 0
                with open(csv_path, "r") as f:
                    reader = csv_mod.DictReader(f)
                    rows = list(reader)
                    # Take the most recent max_bars
                    rows = rows[-max_bars:] if len(rows) > max_bars else rows
                    for row in rows:
                        try:
                            dt = datetime.strptime(row["Date"].strip(), "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            dt = datetime.strptime(row["Date"].strip(), "%Y-%m-%d %H:%M")
                        dt = dt.replace(tzinfo=timezone.utc)
                        bar = Bar(
                            time=dt,
                            open=float(row["Open"]),
                            high=float(row["High"]),
                            low=float(row["Low"]),
                            close=float(row["Close"]),
                            volume=float(row.get("Volume", 0)),
                        )
                        tracker.bars.append(bar)
                        bars_loaded += 1

                # Trim to max lookback
                tracker.bars = tracker.bars[-MAX_LOOKBACK_BARS:]
                logger.info(
                    f"Loaded {bars_loaded} historical M15 bars for {internal_name} "
                    f"({tracker.bars[0].time.strftime('%Y-%m-%d')} to "
                    f"{tracker.bars[-1].time.strftime('%Y-%m-%d %H:%M')})"
                )
            except Exception as e:
                logger.error(f"Failed to load historical data for {internal_name}: {e}")

    def _on_tick(self, tick: Tick):
        """Callback for incoming ticks — route to correct tracker."""
        self._tick_count += 1
        if self._tick_count % 1000 == 0:
            logger.info(f"Tick #{self._tick_count}: {tick.symbol_id} bid={tick.bid}")

        # Map symbol_id to internal name
        internal_name = SYMBOL_ID_MAP.get(tick.symbol_id)
        if internal_name is None:
            # Try to discover — log unknown IDs
            logger.debug(f"Unknown symbol_id={tick.symbol_id}, bid={tick.bid}, ask={tick.ask}")
            return

        tracker = self._trackers.get(internal_name)
        if tracker:
            tracker.add_tick(tick)

        if self._paper_trader is not None and internal_name is not None:
            mid_price = tick.mid
            self._paper_trader.update_market_prices(
                {internal_name: mid_price},
                bids={internal_name: tick.bid},
                asks={internal_name: tick.ask},
            )

    def _check_bar_closes(self):
        """Check if any M15 bar just closed, and if so evaluate strategies."""
        for internal_name, tracker in self._trackers.items():
            closed_bar = tracker.try_close_bar()
            if closed_bar is None:
                continue

            # Avoid duplicate evaluations for the same bar time
            bar_key = f"{internal_name}_{closed_bar.time.isoformat()}"
            if bar_key in self._last_bar_close:
                continue
            self._last_bar_close[bar_key] = datetime.now(timezone.utc)
            # Cleanup old entries
            if len(self._last_bar_close) > 100:
                oldest = sorted(self._last_bar_close.keys())[0]
                del self._last_bar_close[oldest]

            # Evaluate strategies for this symbol
            logger.info(
                f"{internal_name}: M15 bar closed at {closed_bar.time.strftime('%H:%M')} | {len(tracker.bars)} bars total"  # noqa: E501
            )
            self._evaluate_symbol(internal_name, tracker)

    def _evaluate_symbol(self, symbol: str, tracker: SymbolTracker):
        """Run all strategies for a symbol on the latest bar history."""
        if len(tracker.bars) < 100:
            logger.info(f"{symbol}: only {len(tracker.bars)} bars, need 100 — skipping")
            return

        session = _detect_session(tracker.bars[-1].time)
        market_state = MarketState(bars=tracker.bars, current_session=session)
        spread = tracker.spread

        for strat_name, strategy in self._strategies.items():
            cfg = STRATEGY_PARAMS[strat_name]
            if cfg["symbol"] != symbol:
                continue

            try:
                signal = strategy.evaluate(market_state)
            except Exception as e:
                logger.error(f"{strat_name} evaluation error: {e}")
                continue

            last_dir = self._last_signal_dir[strat_name]
            current_dir = None
            if signal is not None:
                current_dir = signal.direction

            if signal is None and last_dir is None:
                continue  # no signal, no position — nothing to do

            # Signal flip or new signal
            if current_dir is not None and current_dir != last_dir:
                # Check confidence threshold
                min_conf = cfg["params"].get("min_confidence", 0.5)
                if signal.confidence < min_conf:
                    logger.debug(f"{strat_name}: signal confidence {signal.confidence:.2f} < {min_conf}, skipping")
                    continue

                # Close existing position if any
                if last_dir is not None:
                    self._close_position_for_strategy(strat_name, symbol, tracker)

                # Open new position
                self._open_position(strat_name, symbol, signal, spread)
                self._last_signal_dir[strat_name] = current_dir
            elif signal is None and last_dir is not None:
                # Signal disappeared — close position
                self._close_position_for_strategy(strat_name, symbol, tracker)
                self._last_signal_dir[strat_name] = None

    def _open_position(self, strat_name: str, symbol: str, signal, spread: float):
        """Send a trade signal to PaperTrader."""
        if self._paper_trader is None:
            return

        # Safety: max 1 open position per symbol across all strategies
        key = f"{strat_name}_{symbol}"
        existing_pos_id = self._position_ids.get(key)
        if existing_pos_id:
            logger.warning(f"{strat_name} {symbol}: already have open position {existing_pos_id}, skipping open")
            return

        # Also check PaperTrader for any open position on this symbol
        open_positions = self._paper_trader.get_open_positions()
        for pos in open_positions:
            if pos.symbol == symbol and pos.status.value == "open":
                logger.warning(f"{strat_name} {symbol}: PaperTrader has open position {pos.position_id}, skipping open")
                return

        c_dir = CTradeDirection.LONG if signal.direction == TradeDirection.LONG else CTradeDirection.SHORT

        trade_signal = TradeSignal(
            symbol=symbol,
            direction=c_dir,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            volume=0.01,  # Minimal size for safety during initial testing
            confidence=signal.confidence,
            rationale=signal.rationale,
        )

        result = self._paper_trader.process_signal(trade_signal, spread=spread)
        if result.success:
            logger.info(
                f"OPEN {strat_name} {symbol} {c_dir.value} @ {signal.entry_price:.5f} "
                f"SL={signal.stop_loss} TP={signal.take_profit_1} conf={signal.confidence:.2f}"
            )
            if result.order and self._trade_logger:
                self._trade_logger.log_trade_opened(result.order, result.position)
            # Track pending SL/TP attachment — will be sent after fill with position_id
            if result.order and self._paper_trader.is_live_mode:
                self._pending_sl_tp[result.order.order_id] = {
                    "signal": trade_signal,
                    "strategy_key": f"{strat_name}_{symbol}",
                }
        else:
            logger.warning(f"OPEN REJECTED {strat_name}: {result.rejection_reason}")

    def _close_position_for_strategy(self, strat_name: str, symbol: str, tracker: SymbolTracker):
        """Close any open position for this strategy/symbol."""
        if self._paper_trader is None:
            return

        positions = self._paper_trader.get_open_positions()
        for pos in positions:
            if pos.symbol == symbol and pos.status.value == "open":
                if self._paper_trader.is_live_mode and self._api_client:
                    # Live mode: use close_position with position_id for hedging account
                    broker_pos_id = self._position_ids.get(f"{strat_name}_{symbol}")
                    close_order = self._api_client._client.close_position(
                        symbol=pos.symbol,
                        direction=pos.direction,
                        volume=pos.volume,
                        position_id=broker_pos_id,
                    )
                    if close_order:
                        logger.info(
                            f"LIVE CLOSE {strat_name} {symbol} sent: {close_order.order_id} pos_id={broker_pos_id}"
                        )
                    else:
                        logger.error(f"LIVE CLOSE {strat_name} {symbol} failed")
                    # Cancel linked SL/TP orders
                    if broker_pos_id:
                        self._cancel_linked_orders(broker_pos_id)
                        self._position_ids.pop(f"{strat_name}_{symbol}", None)
                    # Also update local PaperTrader state
                    if tracker.last_tick:
                        close_price = (
                            tracker.last_tick.bid if pos.direction == CTradeDirection.LONG else tracker.last_tick.ask
                        )
                    else:
                        close_price = pos.current_price
                    self._paper_trader.close_position(pos.position_id, close_price, reason=f"signal_flip_{strat_name}")
                else:
                    # Paper mode: local close only — use bid for long, ask for short
                    if tracker.last_tick:
                        close_price = (
                            tracker.last_tick.bid if pos.direction == CTradeDirection.LONG else tracker.last_tick.ask
                        )
                    else:
                        close_price = pos.current_price
                    result = self._paper_trader.close_position(
                        pos.position_id, close_price, reason=f"signal_flip_{strat_name}"
                    )
                    if result.success:
                        logger.info(f"CLOSE {strat_name} {symbol} PnL=${result.realized_pnl:.2f}")
                        if self._trade_logger:
                            self._trade_logger.log_trade_closed(pos, close_price, result.realized_pnl, "signal_flip")
                break

    def _print_status(self):
        """Periodic status report."""
        if self._paper_trader is None:
            return
        stats = self._paper_trader.get_stats()
        positions = self._paper_trader.get_open_positions()
        open_pos = [f"{p.symbol} {p.direction.value}" for p in positions]
        dd = stats.current_balance - stats.starting_balance
        dd_pct = (dd / stats.starting_balance) * 100
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(
            f"[{now} UTC] Bal=${stats.current_balance:.2f} | "
            f"PnL=${stats.realized_pnl:.2f} | DD={dd_pct:+.2f}% | "
            f"Open: {len(positions)} {', '.join(open_pos) if open_pos else 'none'} | "
            f"Trades: {stats.trades_executed}"
        )

    def _print_summary(self):
        """Final summary on shutdown."""
        if self._paper_trader is None:
            return
        stats = self._paper_trader.get_stats()
        positions = self._paper_trader.get_open_positions()
        print("\n" + "=" * 60)
        print("LIVE PAPER TRADING — SHUTDOWN SUMMARY")
        print("=" * 60)
        print(f"Starting Balance:  ${stats.starting_balance:.2f}")
        print(f"Current Balance:   ${stats.current_balance:.2f}")
        print(f"Realized PnL:      ${stats.realized_pnl:.2f}")
        print(f"Unrealized PnL:    ${stats.unrealized_pnl:.2f}")
        print(f"Total Trades:      {stats.trades_executed}")
        print(f"Rejected Trades:   {stats.trades_rejected}")
        print(f"Risk-Blocked:      {stats.signals_blocked_by_risk}")
        print(f"Open Positions:    {len(positions)}")
        for p in positions:
            print(f"  - {p.symbol} {p.direction.value} @ {p.entry_price} PnL=${p.unrealized_pnl:.2f}")
        wr = (stats.trades_executed - stats.trades_rejected) / max(stats.trades_executed, 1) * 100
        print(f"Win Rate (approx): {wr:.1f}%")
        print("=" * 60)

    def _status_loop(self):
        """Background thread for periodic status + bar close checks."""
        while not self._shutdown:
            # Check bar closes every 5 seconds (catches M15 boundaries reliably)
            time.sleep(5)
            if self._shutdown:
                break
            self._check_bar_closes()

            # Print status every STATUS_INTERVAL_S seconds
            if int(time.time()) % STATUS_INTERVAL_S < 6:
                if not self._shutdown:
                    self._print_status()

    def _status_loop_wrapper(self):
        """Wrapper that catches and logs exceptions in status thread."""
        try:
            self._status_loop()
        except Exception as e:
            logger.error(f"Status loop error: {e}", exc_info=True)

    def _graceful_shutdown(self, signum=None, frame=None):
        print("\nShutdown signal received...")
        self._shutdown = True
        # Close all open positions on cTrader (real orders) before local state
        if self._paper_trader and self._paper_trader.is_live_mode and self._api_client:
            positions = self._paper_trader.get_open_positions()
            for pos in positions:
                if pos.status.value == "open":
                    # Find broker position_id for this position
                    broker_pos_id = None
                    for key, pid in self._position_ids.items():
                        if pos.symbol in key:
                            broker_pos_id = pid
                            break
                    close_order = self._api_client._client.close_position(
                        symbol=pos.symbol,
                        direction=pos.direction,
                        volume=pos.volume,
                        position_id=broker_pos_id,
                    )
                    if close_order:
                        print(f"  Sent close order for {pos.symbol} {pos.direction.value}: {close_order.order_id}")
                    else:
                        print(f"  FAILED to send close order for {pos.symbol} {pos.direction.value}")
                    # Cancel linked SL/TP orders
                    if broker_pos_id:
                        self._cancel_linked_orders(broker_pos_id)
            print("Waiting 3s for close orders to submit...")
            time.sleep(3)
        # Also close local state
        for internal, tracker in self._trackers.items():
            for strat_name, strategy in self._strategies.items():  # noqa: B007
                cfg = STRATEGY_PARAMS[strat_name]
                if cfg["symbol"] == internal and self._last_signal_dir.get(strat_name) is not None:
                    self._close_position_for_strategy(strat_name, internal, tracker)
        self._print_summary()
        # Disconnect trade connection
        if self._api_client:
            self._api_client.disconnect()
        sys.exit(0)

    def run(self):
        """Main entry point."""
        account = os.environ.get("CTRADER_ACCOUNT", "17087404")
        print("=" * 60)
        if self._paper_only:
            print("AYUMI — Live Paper Trading (PAPER-ONLY MODE - no real trades)")
        else:
            print("AYUMI — Live Paper Trading (DEMO EXECUTION)")
        print("=" * 60)
        print(f"Account: {account}")
        print("Market Data: port 5211 (SSL)")
        print("Trade Execution: port 5212 (SSL)")
        print("-" * 60)

        # Initialize
        self._init_strategies()
        self._init_paper_trader()
        self._init_feed()

        # Load historical bars to avoid 25-hour cold start
        self._load_historical_bars(max_bars=200)

        # Wire up tick callback
        self._feed.on_tick(self._on_tick)

        # Register signal handlers
        sig.signal(sig.SIGINT, self._graceful_shutdown)
        sig.signal(sig.SIGTERM, self._graceful_shutdown)

        # Subscribe to symbols
        cTrader_names = [SYMBOLS[k] for k in SYMBOLS]
        print(f"Subscribing to: {', '.join(cTrader_names)}")

        try:
            self._feed.start(auto_subscribe=cTrader_names)
        except Exception as e:
            print(f"Failed to connect to cTrader market data: {e}")
            print("Make sure CTRADER_* environment variables are set.")
            sys.exit(1)

        # Connect to trade port for real execution
        try:
            self._init_trade_connection()
        except Exception as e:
            print(f"Failed to connect to cTrader trade port: {e}")
            sys.exit(1)

        # Wait for connections to stabilize
        print("Waiting for connections to stabilize...")
        time.sleep(3)

        if not self._feed.is_running:
            print("ERROR: Market data feed not connected")
            sys.exit(1)
        if not self._api_client.is_connected:
            print("ERROR: Trade connection not connected")
            sys.exit(1)

        print("Connected — market data + trade execution ready.")

        # Start status thread
        self._status_thread = threading.Thread(target=self._status_loop_wrapper, daemon=True)
        self._status_thread.start()

        print(f"Live paper trading active (DEMO). Balance=${STARTING_BALANCE:,.2f}")
        print("Press Ctrl+C to shut down gracefully.")
        print("-" * 60)

        # Main loop — keep alive
        try:
            while not self._shutdown:
                time.sleep(1)
        except KeyboardInterrupt:
            self._graceful_shutdown()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Ayumi Live Paper Trading")
    parser.add_argument(
        "--paper-only",
        action="store_true",
        help="Local simulation only - no real broker orders",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    system = LivePaperTradingSystem(paper_only=args.paper_only)
    system.run()


if __name__ == "__main__":
    main()
