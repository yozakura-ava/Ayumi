"""USDJPY D1 trend-following strategy (SFA-2 — resolves SFA-1 placeholder).

The SFA-1 bridge shipped :class:`forex_bot.factory.bridge._placeholder_builder`
for ``strategy_id == "usdjpy_d1_trend"`` because no concrete strategy class
existed in the registry at the time.  SFA-2 lands the implementation here so
the registry builder can resolve to a real ``ISignalStrategy`` instance.

Design
------
The strategy is a long-only daily trend follower on USDJPY.  It enters
when:

* the close is above the 50-period simple moving average, **and**
* the 14-period RSI is between 50 and 70 (trend strength filter), **and**
* the close is above the previous bar's high (Donchian breakout
  confirmation over 20 periods).

It exits on either:

* a trailing stop at 2× the 14-period ATR, or
* a close below the 50 SMA (trend break).

This is intentionally conservative — USDJPY intervenes, so a single
trend filter with multiple confirmations is appropriate (per the
``usdjpy-intervention-archetypes.md`` research note).

The class implements the formal ``ISignalStrategy`` ABC so the bridge's
duck-type check (``.name`` + ``.evaluate()``) passes.  A zero-arg
constructor is what the lazy resolver in :mod:`forex_bot.factory.bridge`
expects (``_make_default_builder`` calls ``cls()`` when no init kwargs
are provided).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from backtest.strategies.isignal_strategy import ISignalStrategy

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class USDJPYD1TrendConfig:
    """USDJPY D1 trend configuration.

    All fields are tunable but ship with sensible defaults.  The
    ``min_confidence`` field is read by the backtest engine
    (``MultiStrategyBacktestEngine``) to filter signals before they
    become trades.
    """

    sma_period: int = 50
    rsi_period: int = 14
    rsi_min: float = 50.0
    rsi_max: float = 70.0
    donchian_period: int = 20
    atr_period: int = 14
    atr_trail_multiplier: float = 2.0
    min_confidence: float = 0.50


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------


def _sma(values: list[float], period: int) -> float | None:
    """Simple moving average; ``None`` until enough data."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes: list[float], period: int) -> float | None:
    """Wilder-style RSI.  ``None`` until enough data."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(bars: list[Any], period: int) -> float:
    """Wilder-style ATR over the most recent ``period`` bars."""
    if len(bars) < period + 1:
        return 0.0
    tr_sum = 0.0
    count = 0
    for i in range(len(bars) - period, len(bars)):
        if i <= 0:
            continue
        high = bars[i].high
        low = bars[i].low
        prev_close = bars[i - 1].close
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        tr_sum += tr
        count += 1
    return tr_sum / count if count > 0 else 0.0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class USDJPYD1TrendStrategy(ISignalStrategy):
    """USDJPY daily trend-following strategy (concrete SFA-2 implementation).

    Constructed with no args by the bridge's ``_make_default_builder``;
    consumers wanting non-default params can pass them via the
    ``USDJPYD1TrendConfig`` dataclass to a custom builder (future SFA-3
    ``trend_following`` template).
    """

    def __init__(self, config: USDJPYD1TrendConfig | None = None) -> None:
        self.config = config or USDJPYD1TrendConfig()
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._in_position: bool = False
        self._entry_price: float = 0.0
        self._trailing_stop: float = 0.0
        # ``_bar`` carries the most recently processed bar for ``generate_signal``.
        self._last_bar: Any = None

    # ── ISignalStrategy surface ──────────────────────────────────────────

    name: str = "usdjpy_d1_trend"

    def initialize(self, config: Mapping[str, Any] | None = None) -> None:
        """Reset state for a fresh run."""
        _ = config  # explicit unused; config is read in __init__
        self._closes.clear()
        self._highs.clear()
        self._lows.clear()
        self._in_position = False
        self._entry_price = 0.0
        self._trailing_stop = 0.0
        self._last_bar = None

    def shutdown(self) -> None:
        """No resources to release."""

    def on_bar(self, bar: Any) -> None:
        """Append the bar to internal buffers for ``generate_signal``."""
        self._last_bar = bar
        self._closes.append(float(bar.close))
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))
        # Maintain bounded history (period + a small buffer).
        max_history = max(self.config.sma_period, self.config.donchian_period) + 10
        if len(self._closes) > max_history:
            self._closes = self._closes[-max_history:]
            self._highs = self._highs[-max_history:]
            self._lows = self._lows[-max_history:]

    def generate_signal(self) -> Any:
        """Translate current state into a signal object.

        Returns ``None`` until enough history has accumulated.  Real signal
        shapes are determined by the engine's :class:`StrategySignal` parser;
        the backtest engine inspects ``signal.direction`` so we expose
        ``signal = {"direction": "long"|"short"|"flat", "confidence": float}``.
        """
        cfg = self.config
        if self._last_bar is None:
            return None
        if len(self._closes) < cfg.sma_period:
            return None

        sma = _sma(self._closes, cfg.sma_period)
        rsi = _rsi(self._closes, cfg.rsi_period)
        if sma is None or rsi is None:
            return None

        close = self._closes[-1]
        donchian_high = max(self._highs[-cfg.donchian_period :]) if len(self._highs) >= cfg.donchian_period else None

        atr_val = _atr(
            [
                type("_B", (), {"high": h, "low": low, "close": c})()
                for h, low, c in zip(self._highs, self._lows, self._closes, strict=True)
            ],
            cfg.atr_period,
        )

        # ── Exit logic ───────────────────────────────────────────────────
        if self._in_position:
            # Update trailing stop
            new_trail = close - cfg.atr_trail_multiplier * atr_val
            if new_trail > self._trailing_stop:
                self._trailing_stop = new_trail
            if close < self._trailing_stop or close < sma:
                self._in_position = False
                return {
                    "direction": "flat",
                    "confidence": cfg.min_confidence,
                    "reason": "trail_or_trend_break",
                }
            return None  # hold

        # ── Entry logic ──────────────────────────────────────────────────
        trend_ok = close > sma
        rsi_ok = cfg.rsi_min <= rsi <= cfg.rsi_max
        breakout_ok = donchian_high is not None and close >= donchian_high
        if trend_ok and rsi_ok and breakout_ok:
            self._in_position = True
            self._entry_price = close
            self._trailing_stop = close - cfg.atr_trail_multiplier * atr_val
            return {
                "direction": "long",
                "confidence": cfg.min_confidence,
                "reason": "trend_breakout_confirmed",
            }
        return None

    def evaluate(self, state: Any) -> Any:  # type: ignore[override]
        """Legacy entry point — the backtest engine calls ``evaluate(state)``.

        ``state`` is duck-typed as ``MarketState`` in the canonical engine.
        We translate to ``on_bar`` + ``generate_signal`` so the same
        strategy works with the decomposed path (newer engines).
        """
        # Best-effort: pull ``bar`` from state if present.
        bar = getattr(state, "bar", None) or getattr(state, "current_bar", None)
        if bar is None:
            return None
        self.on_bar(bar)
        return self.generate_signal()


__all__ = ["USDJPYD1TrendConfig", "USDJPYD1TrendStrategy"]
