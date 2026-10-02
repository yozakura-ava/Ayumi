"""Quality gates for the confidence engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GateCheck:
    """Result of a gate check."""

    gate_name: str
    passed: bool
    reason: str = ""
    boost: float = 0.0


@dataclass
class GateConfig:
    """Configuration for quality gates."""

    # Spread gate
    default_max_spread: float = 2.0  # pips
    symbol_max_spreads: dict[str, float] = None

    # Session gate
    london_open: int = 8
    london_close: int = 17
    ny_open: int = 13
    ny_close: int = 22
    asia_open: int = 0
    asia_close: int = 8

    # Volatility gate
    min_atr_multiplier: float = 0.5
    max_atr_multiplier: float = 3.0
    atr_lookback_default: float = 1.0  # default ATR value if none provided
    volatility_min_atr: dict[str, float] = None  # per-symbol minimum ATR
    volatility_max_atr: dict[str, float] = None  # per-symbol maximum ATR

    def __post_init__(self):
        if self.symbol_max_spreads is None:
            self.symbol_max_spreads = {}
        if self.volatility_min_atr is None:
            self.volatility_min_atr = {}
        if self.volatility_max_atr is None:
            self.volatility_max_atr = {}


class SpreadGate:
    """Reject if spread too wide."""

    def __init__(self, config: GateConfig):
        self._config = config

    def check(self, ctx: dict[str, Any]) -> GateCheck:
        symbol = ctx.get("symbol", "")
        spread = ctx.get("spread", 0.0)

        max_spread = self._config.symbol_max_spreads.get(symbol, self._config.default_max_spread)

        if spread > max_spread:
            return GateCheck(
                gate_name="spread",
                passed=False,
                reason=f"Spread {spread:.1f} pips exceeds max {max_spread:.1f} for {symbol}",
            )

        return GateCheck(gate_name="spread", passed=True)


class SessionGate:
    """Reject if outside configured trading sessions."""

    def __init__(self, config: GateConfig):
        self._config = config

    def check(self, ctx: dict[str, Any]) -> GateCheck:
        hour = ctx.get("hour_utc", 0)
        cfg = self._config

        # Check if hour falls within any session
        in_session = False
        if self._in_range(hour, cfg.asia_open, cfg.asia_close):
            in_session = True
        if self._in_range(hour, cfg.london_open, cfg.london_close):
            in_session = True
        if self._in_range(hour, cfg.ny_open, cfg.ny_close):
            in_session = True

        if not in_session:
            return GateCheck(
                gate_name="session",
                passed=False,
                reason=f"Hour {hour} UTC outside all trading sessions",
            )

        return GateCheck(gate_name="session", passed=True)

    @staticmethod
    def _in_range(hour: int, open_h: int, close_h: int) -> bool:
        if open_h <= close_h:
            return open_h <= hour < close_h
        # Wraps midnight (e.g., 22-8)
        return hour >= open_h or hour < close_h


class VolatilityGate:
    """Reject if volatility outside acceptable range.

    Uses per-symbol ATR min/max from GateConfig when configured,
    falls back to multiplier-based check against lookback default.
    """

    def __init__(self, config: GateConfig, atr_provider=None):
        self._config = config
        self._atr_provider = atr_provider  # Callable: atr_provider(symbol) -> float

    def check(self, ctx: dict[str, Any]) -> GateCheck:
        symbol = ctx.get("symbol", "")
        atr = ctx.get("atr", 0.0)

        # Try atr_provider if no atr in context
        if atr <= 0 and self._atr_provider:
            atr = self._atr_provider(symbol)

        if atr is None or atr <= 0:
            return GateCheck(
                gate_name="volatility",
                passed=True,
                reason="No ATR data available, gate skipped",
            )

        # Per-symbol ATR range check (takes priority when configured)
        min_atr = self._config.volatility_min_atr.get(symbol, 0.0)
        max_atr = self._config.volatility_max_atr.get(symbol, float("inf"))

        if min_atr > 0 or max_atr < float("inf"):
            if atr < min_atr:
                return GateCheck(
                    gate_name="volatility",
                    passed=False,
                    reason=f"ATR {atr:.4f} below minimum {min_atr} for {symbol}",
                )
            if atr > max_atr:
                return GateCheck(
                    gate_name="volatility",
                    passed=False,
                    reason=f"ATR {atr:.4f} above maximum {max_atr} for {symbol}",
                )
            return GateCheck(
                gate_name="volatility",
                passed=True,
                reason=f"ATR {atr:.4f} within range for {symbol}",
            )

        # Fallback: multiplier-based check against lookback default
        default = self._config.atr_lookback_default

        # Domain mismatch guard: if ATR is in price domain (very small, e.g.
        # 0.0015 for GBPUSD) but the lookback default is in a different scale
        # (>= 0.01), the ratio is meaningless and would block every signal
        # with realistic price-domain ATR.  Skip the multiplier check.
        if atr < 0.01 and default >= 0.01:
            return GateCheck(
                gate_name="volatility",
                passed=True,
                reason=(
                    f"ATR {atr:.5f} appears to be in price domain while "
                    f"lookback default {default} is in a different scale "
                    f"— multiplier check skipped (domain mismatch)"
                ),
            )

        ratio = atr / default if default > 0 else 0.0

        if ratio < self._config.min_atr_multiplier:
            return GateCheck(
                gate_name="volatility",
                passed=False,
                reason=f"ATR ratio {ratio:.2f} below minimum {self._config.min_atr_multiplier:.1f}",
            )

        if ratio > self._config.max_atr_multiplier:
            return GateCheck(
                gate_name="volatility",
                passed=False,
                reason=f"ATR ratio {ratio:.2f} above maximum {self._config.max_atr_multiplier:.1f}",
            )

        return GateCheck(gate_name="volatility", passed=True)


class NewsBlackoutGate:
    """Rejects signals near high-impact news events.

    Stub — always passes until news feed integrated.
    """

    def __init__(self, blackout_minutes: int = 30):
        self._blackout_minutes = blackout_minutes

    def check(self, ctx: dict[str, Any]) -> GateCheck:
        # TODO: integrate economic calendar feed
        return GateCheck(
            gate_name="news_blackout",
            passed=True,
            reason="News gate stub — no calendar configured",
        )
