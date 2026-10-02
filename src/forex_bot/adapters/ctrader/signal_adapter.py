import logging
from collections.abc import Callable
from datetime import datetime

from backtest.engine import MarketState
from backtest.strategies import ISignalStrategy

from .models import CTraderTradeSignal, TradeDirection
from .paper_trader import PaperTrader
from .risk_guard import _DEFAULT_SYMBOL_SPREADS

logger = logging.getLogger(__name__)

# ── Spread gate defaults ──────────────────────────────────────────────────────
#
# Per-symbol max spread in pips.  Signals arriving when the live spread
# exceeds the threshold for the symbol are rejected at the adapter level,
# before strategy evaluation — saving CPU and providing clean audit logs.
# Values mirror risk_guard._DEFAULT_SYMBOL_SPREADS (SRB-AYUMI-011 §5.1).
_DEFAULT_ADAPTER_SPREADS: dict[str, float] = dict(_DEFAULT_SYMBOL_SPREADS)


# ── Price sanity guardrail ─────────────────────────────────────────────────────
#
# Defense against data-feed / decoder bugs that emit physically impossible
# entry prices (e.g. $4.1M for XAUUSD when gold trades at ~$5,000+). Triggered
# multiple times in production by SRMR+ and Session-Range Mean Reversion on
# XAUUSD bars (forward_test-stderr.log: 2026-07-08 to 2026-07-10). Root cause
# is upstream in the spot-feed trendbar/tick decode for non-JPY pairs where
# `digits < 5`; the JPY-pair workaround in open_api_spot_feed._handle_spot_event
# does not cover XAUUSD. This guardrail blocks the corrupted signal BEFORE it
# reaches the orchestrator, sizing gate, paper trader, or live broker.
#
# Values are intentionally generous — gold has traded above $5,000 (Q3 2026),
# so XAUUSD=10000 leaves >85% headroom while still rejecting any 100x+ inflation
# bug or fat-finger entry. TODO (card 271cba95 follow-up): derive the bound
# dynamically from a rolling percentile of recent closes × margin so this does
# not rot again as gold price drifts upward. Defaults catch any unknown symbol
# at $10k.
_MAX_REASONABLE_PRICES: dict[str, float] = {
    "XAUUSD": 10_000.0,  # gold sane max — gold Q3 2026 spot ~$5,000+ (was 5000; bumped 2026-09-08, card 271cba95)
    "EURUSD": 2.0,
    "GBPUSD": 3.0,
    "USDJPY": 300.0,
    "AUDUSD": 2.0,
    "USDCHF": 2.0,
    "USDCAD": 3.0,
}
_DEFAULT_SANE_PRICE_MAX = 10_000.0

# Periodic loud summary: emit a "PRICE-SANITY REJECTION SUMMARY" ERROR every N
# rejections so operators watching harness output see the running total (the
# "silent discard" failure pattern is the disease this guards against — card
# 271cba95).
_SANITY_SUMMARY_EVERY_N = 5


def _max_reasonable_price(symbol: str) -> float:
    """Sane maximum price for the given symbol. Used to reject decoder bugs."""
    return _MAX_REASONABLE_PRICES.get(symbol.upper(), _DEFAULT_SANE_PRICE_MAX)


def _price_exceeds_sanity_bound(symbol: str, value: float | None) -> bool:
    """True if value is non-positive, NaN, or beyond the sane-max for the symbol."""
    if value is None:
        return True
    try:
        v = float(value)
    except (TypeError, ValueError):
        return True
    if v != v:  # NaN check (NaN != NaN)
        return True
    if v <= 0:
        return True
    return v > _max_reasonable_price(symbol)


class cTraderSignalAdapter:
    def __init__(
        self,
        paper_trader: PaperTrader,
        strategy: ISignalStrategy,
        symbol: str = "EURUSD",
        *,
        blend_mode: bool = False,
        max_spread_thresholds: dict[str, float] | None = None,
        default_max_spread: float = 2.0,
    ):
        self._paper_trader = paper_trader
        self._strategy = strategy
        self._symbol = symbol
        self._min_confidence: float = 0.50
        self._last_signal_time: datetime | None = None
        self._current_spread: float = 0.0
        self._callbacks: list[tuple[str, Callable]] = []
        # In blend mode, signal routing and sizing is handled by BlendForwardTestRunner.
        # The adapter should return signals without executing through paper_trader.
        self._blend_mode = blend_mode
        # Spread gate config — reject signals when spread exceeds per-symbol
        # maximum.  Prevents entries during news spikes (e.g. 50-pip XAUUSD).
        # Defaults from risk_guard._DEFAULT_SYMBOL_SPREADS.
        resolved = dict(_DEFAULT_ADAPTER_SPREADS)
        if max_spread_thresholds:
            resolved.update(max_spread_thresholds)
        self._max_spread_thresholds = resolved
        self._default_max_spread = default_max_spread
        # Loud rejection counter (card 271cba95). The original guardrail logged
        # WARNING on each rejection but produced no aggregate signal — silent
        # discards ate ~25% of valid XAUUSD signals during integration re-run
        # #4 because nobody noticed the bound was stale. Track and surface.
        self._sanity_rejection_count: int = 0

    def set_min_confidence(self, confidence: float):
        self._min_confidence = confidence

    def update_spread(self, spread: float):
        self._current_spread = spread

    def evaluate_and_trade(
        self,
        market_state: MarketState,
        spread: float = 0.0,
        bid: float = 0.0,
        ask: float = 0.0,
    ) -> CTraderTradeSignal | None:
        if spread > 0:
            self._current_spread = spread

        # ── Spread gate ────────────────────────────────────────────────────
        # Reject signals when the live spread exceeds the per-symbol max.
        # This blocks entries during news spikes (e.g. 50-pip XAUUSD spread)
        # at the source, before strategy evaluation.
        if self._current_spread > 0:
            threshold = self._max_spread_thresholds.get(self._symbol.upper(), self._default_max_spread)
            if self._current_spread > threshold:
                logger.warning(
                    "spread_too_wide: symbol=%s strategy=%s spread=%.2f threshold=%.2f — signal rejected at adapter",
                    self._symbol,
                    self._strategy.name,
                    self._current_spread,
                    threshold,
                )
                return None

        signal = self._strategy.evaluate(market_state)

        if signal is None:
            return None

        if signal.confidence < self._min_confidence:
            logger.info(
                "Signal rejected: strategy=%s symbol=%s reason=confidence_threshold raw_confidence=%.3f threshold=%.2f",
                self._strategy.name,
                self._symbol,
                signal.confidence,
                self._min_confidence,
            )
            return None

        # Price sanity guardrail — drop signals with impossible entry/SL/TP.
        # Defends against upstream spot-feed decoder bugs that have produced
        # XAUUSD entry prices in the millions (see log evidence 2026-07-08..10).
        sane_max = _max_reasonable_price(self._symbol)
        for field_name, field_value in (
            ("entry_price", signal.entry_price),
            ("stop_loss", signal.stop_loss),
            ("take_profit_1", signal.take_profit_1),
            ("take_profit_2", signal.take_profit_2),
            ("take_profit_3", signal.take_profit_3),
        ):
            if _price_exceeds_sanity_bound(self._symbol, field_value):
                self._sanity_rejection_count += 1
                # ERROR level (was WARNING): makes the line visible in harness
                # stdout instead of being swallowed by default WARN-only
                # filters. Counter is included so operators see accumulation.
                logger.error(
                    "PRICE-SANITY REJECTION [#%d]: strategy=%s symbol=%s %s=%.5f "
                    "exceeds sane_max=%.2f — likely data-feed decoder bug OR "
                    "stale sane_max bound "
                    "(see open_api_spot_feed._handle_spot_event non-JPY path)",
                    self._sanity_rejection_count,
                    self._strategy.name,
                    self._symbol,
                    field_name,
                    field_value if field_value is not None else 0.0,
                    sane_max,
                )
                if self._sanity_rejection_count % _SANITY_SUMMARY_EVERY_N == 0:
                    # Loud periodic summary: visible without scrolling.
                    logger.error(
                        "PRICE-SANITY REJECTION SUMMARY: symbol=%s strategy=%s "
                        "total_rejected=%d since adapter init — investigate "
                        "sane_max bound or upstream decoder",
                        self._symbol,
                        self._strategy.name,
                        self._sanity_rejection_count,
                    )
                return None

        trade_direction = self._convert_direction(signal.direction)

        trade_signal = CTraderTradeSignal(
            symbol=self._symbol,
            direction=trade_direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            volume=0.0,  # Sized by orchestrator/blend runner, not here
            confidence=signal.confidence,
            rationale=signal.rationale,
            strategy_id=self._strategy.name,
        )

        logger.info(
            "Signal adapted: %s %s %s entry=%.5f sl=%.5f sl_dist=%.6f conf=%.2f",
            trade_direction.value,
            self._symbol,
            self._strategy.name,
            signal.entry_price,
            signal.stop_loss,
            abs(signal.entry_price - signal.stop_loss),
            signal.confidence,
        )

        if self._blend_mode:
            # In blend mode, sizing and execution are handled by the blend runner.
            # Return the signal without executing through paper_trader.
            return trade_signal

        result = self._paper_trader.process_signal(
            trade_signal,
            spread=self._current_spread,
            bid=bid,
            ask=ask,
        )

        if result.success:
            self._last_signal_time = datetime.utcnow()
            slippage = result.slippage_applied
            logger.info(
                f"Signal traded: {self._strategy.name} {trade_direction.value} {self._symbol} "
                f"@ signal={signal.entry_price:.5f} slippage={slippage:.5f}"
            )
            self._trigger_callback("on_signal_traded", result)
        else:
            logger.warning(f"Signal rejected: {result.rejection_reason}")
            self._trigger_callback("on_signal_rejected", result)

        return trade_signal

    def _convert_direction(self, direction) -> TradeDirection:
        # Direction can arrive as a BacktestTradeDirection enum, a core TradeDirection
        # StrEnum, or a plain string ("long"/"short") from the signal engine.
        # Compare by value to handle all three correctly.
        val = str(direction).lower() if direction is not None else "neutral"
        if val == "long":
            return TradeDirection.LONG
        elif val == "short":
            return TradeDirection.SHORT
        return TradeDirection.NEUTRAL

    def register_callback(self, event: str, callback: Callable):
        if event not in ["on_signal_traded", "on_signal_rejected"]:
            raise ValueError(f"Unknown event: {event}")
        self._callbacks.append((event, callback))

    def _trigger_callback(self, event: str, *args, **kwargs):
        for evt, callback in self._callbacks:
            if evt == event:
                try:
                    callback(*args, **kwargs)
                except Exception as e:
                    logger.error(f"Callback error for {event}: {e}")

    @property
    def strategy_name(self) -> str:
        return self._strategy.name

    @property
    def last_signal_time(self) -> datetime | None:
        return self._last_signal_time

    def get_sanity_rejection_count(self) -> int:
        """Return the count of price-sanity rejections since adapter init.

        Card 271cba95: surface aggregate rejection counts so operators can
        detect a stale bound (silent discard pattern) without grepping the
        full log. Reset semantics: lifetime of the adapter instance.
        """
        return self._sanity_rejection_count


class cTraderLiveAdapter:
    def __init__(
        self,
        paper_trader: PaperTrader,
        strategies: list[ISignalStrategy],
        symbols: list[str],
        *,
        blend_mode: bool = False,
        max_spread_thresholds: dict[str, float] | None = None,
        default_max_spread: float = 2.0,
    ):
        self._paper_trader = paper_trader
        self._strategies = {s.name: s for s in strategies}
        self._symbols = symbols
        self._adapters: dict = {}
        self._running = False

        for symbol in symbols:
            for strategy in strategies:
                # Strategy-pair matching: only create adapter if strategy is registered for this symbol
                if hasattr(strategy, "symbols") and strategy.symbols:
                    if symbol.upper().replace("/", "") not in {s.upper().replace("/", "") for s in strategy.symbols}:
                        continue
                key = f"{strategy.name}_{symbol}"
                self._adapters[key] = cTraderSignalAdapter(
                    paper_trader=paper_trader,
                    strategy=strategy,
                    symbol=symbol,
                    blend_mode=blend_mode,
                    max_spread_thresholds=max_spread_thresholds,
                    default_max_spread=default_max_spread,
                )

    def evaluate_all_strategies(
        self,
        market_states: dict[str, MarketState],
        spread: float = 0.0,
        bid: float = 0.0,
        ask: float = 0.0,
    ) -> list[CTraderTradeSignal]:
        results = []
        for symbol, state in market_states.items():
            for strategy_name, _strategy in self._strategies.items():
                key = f"{strategy_name}_{symbol}"
                adapter = self._adapters.get(key)
                if adapter is None:
                    logger.debug("No adapter for %s — skipped", key)
                    continue
                result = adapter.evaluate_and_trade(
                    state,
                    spread=spread,
                    bid=bid,
                    ask=ask,
                )
                if result is None:
                    logger.debug("%s %s: no signal (conditions not met)", strategy_name, symbol)
                else:
                    results.append(result)
        return results

    def update_spread(self, spread: float):
        for adapter in self._adapters.values():
            adapter.update_spread(spread)

    def get_adapter(self, strategy_name: str, symbol: str) -> cTraderSignalAdapter | None:
        return self._adapters.get(f"{strategy_name}_{symbol}")

    @property
    def paper_trader(self) -> PaperTrader:
        return self._paper_trader

    @property
    def is_running(self) -> bool:
        return self._running
