"""Crypto-native strategy templates (card fe773687).

Three first-principles crypto-native strategies for BTCUSDT / ETHUSDT /
SOLUSDT on H1 bars:

* :class:`CryptoEMACrossTrend` — trend EMA-cross with ATR-scaled stops
* :class:`CryptoDonchianBreakout` — Donchian channel breakout with ATR buffer
* :class:`CryptoZScoreMeanReversion` — rolling z-score mean reversion

Design constraints (from the 2026-10-06 real-data sweep report —
``docs/reports/2026-10-06-crypto-real-data-sweep.md``):

1. **No pip units, ever.** The FX-paired registry strategies emitted ~0
   signals on crypto bars because their thresholds are FX-pip-calibrated
   (the ``srmr_plus._resolve_pip_size`` anti-pattern). Every threshold
   here is expressed in ATR or rolling-σ units — pure price-relative, so
   it binds identically on BTC at 78k and SOL at 150.
2. **No session filters.** Crypto trades 24/7; London/NY session gates
   (the other FX-paired failure mode) are absent.
3. **Thresholds sized for crypto vol + Sprint C cost realities.**
   Entry confirmation and SL distances are wide (2.5-3.0× ATR) because
   H1 crypto ATR is ~1% of price and the sweep's funding/venue overlays
   (applied downstream) are material per trade — churning signals would
   bleed to costs. Templates only emit signals; the overlays price them.
4. **Parameter grids declared UP FRONT.** :data:`CRYPTO_PARAM_GRIDS` is
   the frozen, auditable candidate set. The BH-FDR gate on real bars is
   the judge; this module does not cherry-pick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.types import Bar, MarketState, StrategySignal, TradeDirection

CRYPTO_NATIVE_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# Minimum bars before any signal — the engine gates at its own
# min_bars_before_signal, but the strategies also guard internally so a
# short window can never produce a degenerate indicator read.
MIN_BARS_REQUIRED = 120


# ---------------------------------------------------------------------------
# Declared parameter grids (UP FRONT — no post-hoc tuning; card fe773687)
# ---------------------------------------------------------------------------

CRYPTO_PARAM_GRIDS: dict[str, dict[str, tuple[Any, ...]]] = {
    # EMA-cross trend: 3 declared variants (fast/slow pairs spanning
    # ~2d-4d / ~1d-2d trend horizons on H1, slope gate in ATR units).
    "crypto_ema_cross_trend": {
        "fast_period": (16, 24, 32),
        "slow_period": (72, 96, 120),
        "slope_gate_atr": (0.10, 0.15),
        "atr_sl_mult": (2.5,),
    },
    # Donchian breakout: 3 declared variants (channel lookbacks spanning
    # ~3d-5d on H1, ATR buffer to avoid stop-run fakeouts).
    "crypto_donchian_breakout": {
        "entry_lookback": (72, 96, 120),
        "exit_lookback": (36, 48),
        "atr_buffer_mult": (0.5,),
        "atr_sl_mult": (3.0,),
    },
    # Z-score mean reversion: 3 declared variants (entry |z| 2.0-3.0 —
    # crypto vol means shallow FX-style 1.5σ triggers churn into costs).
    "crypto_zscore_mean_reversion": {
        "lookback": (72, 96, 120),
        "z_entry": (2.0, 2.5, 3.0),
        "atr_sl_mult": (2.5,),
    },
}

DEFAULT_GRID_CENTERS: dict[str, dict[str, Any]] = {
    "crypto_ema_cross_trend": {"fast_period": 24, "slow_period": 96, "slope_gate_atr": 0.15, "atr_sl_mult": 2.5},
    "crypto_donchian_breakout": {"entry_lookback": 96, "exit_lookback": 48, "atr_buffer_mult": 0.5, "atr_sl_mult": 3.0},
    "crypto_zscore_mean_reversion": {"lookback": 96, "z_entry": 2.5, "atr_sl_mult": 2.5},
}


# ---------------------------------------------------------------------------
# Shared indicator helpers (pure price-relative — no pips)
# ---------------------------------------------------------------------------


def _ema_series(values: list[float], period: int) -> list[float]:
    """Classic EMA series seeded with the first value."""
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1.0 - alpha) * out[-1])
    return out


def _atr(bars: list[Bar], period: int = 14) -> float:
    """Average true range over the last ``period`` bars."""
    if len(bars) < period + 1:
        return 0.0
    tr_sum = 0.0
    for i in range(len(bars) - period, len(bars)):
        prev_close = bars[i - 1].close
        tr = max(
            bars[i].high - bars[i].low,
            max(abs(bars[i].high - prev_close), abs(bars[i].low - prev_close)),
        )
        tr_sum += tr
    return tr_sum / period


def _rolling_stats(bars: list[Bar], period: int) -> tuple[float, float]:
    """(mean, population-std) of closes over the last ``period`` bars."""
    closes = [b.close for b in bars[-period:]]
    mean = sum(closes) / len(closes)
    var = sum((c - mean) ** 2 for c in closes) / len(closes)
    return mean, var**0.5


def _make_signal(
    direction: TradeDirection,
    entry: float,
    stop_loss: float,
    atr: float,
    rationale: str,
    confidence: float,
) -> StrategySignal:
    """Build a StrategySignal with ATR-scaled RR ladder (1.5R / 2.5R / 4R)."""
    risk = abs(entry - stop_loss)
    sign = 1.0 if direction is TradeDirection.LONG else -1.0
    return StrategySignal(
        direction=direction,
        confidence=confidence,
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit_1=entry + sign * 1.5 * risk,
        take_profit_2=entry + sign * 2.5 * risk,
        take_profit_3=entry + sign * 4.0 * risk,
        rationale=rationale,
        is_volatile=atr > 0,  # crypto H1 ATR is always "volatile" by FX eyes
    )


# ---------------------------------------------------------------------------
# 1. Trend EMA-cross
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CryptoEMACrossConfig:
    fast_period: int = 24
    slow_period: int = 96
    slope_gate_atr: float = 0.15
    atr_period: int = 14
    atr_sl_mult: float = 2.5
    min_confidence: float = 0.52
    symbols: tuple[str, ...] = CRYPTO_NATIVE_SYMBOLS


class CryptoEMACrossTrend:
    """Trend-following EMA crossover calibrated for H1 crypto bars.

    Emits on fast/slow EMA cross ONLY when the slow EMA slope over the
    last ``fast_period`` bars exceeds ``slope_gate_atr`` × ATR — a
    price-relative trend gate that suppresses chop-zone crosses (the
    main cost-bleed source once Sprint C funding/venue overlays price
    each turn).
    """

    def __init__(self, config: CryptoEMACrossConfig | None = None) -> None:
        self.config = config or CryptoEMACrossConfig()
        self._cooldown_bars_remaining = 0

    @property
    def name(self) -> str:
        return "Crypto EMA-Cross Trend"

    def evaluate(self, state: MarketState) -> StrategySignal | None:  # noqa: D102
        cfg = self.config
        bars = state.bars
        if len(bars) < max(cfg.slow_period + cfg.fast_period, MIN_BARS_REQUIRED):
            return None
        closes = [b.close for b in bars]
        fast = _ema_series(closes, cfg.fast_period)
        slow = _ema_series(closes, cfg.slow_period)
        atr = _atr(bars, cfg.atr_period)
        if atr <= 0.0:
            return None

        i = len(bars) - 1
        crossed_up = fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
        crossed_down = fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]
        if not (crossed_up or crossed_down):
            return None
        if self._cooldown_bars_remaining > 0:
            self._cooldown_bars_remaining -= 1
            return None
        # Trend gate: slow-EMA slope must exceed slope_gate_atr × ATR.
        slope = abs(slow[i] - slow[i - cfg.fast_period])
        if slope < cfg.slope_gate_atr * atr:
            return None

        entry = bars[i].close
        self._cooldown_bars_remaining = cfg.fast_period
        if crossed_up:
            return _make_signal(
                TradeDirection.LONG, entry, entry - cfg.atr_sl_mult * atr, atr,
                f"EMA{cfg.fast_period} crossed above EMA{cfg.slow_period}; "
                f"slope {slope / atr:.2f}ATR > {cfg.slope_gate_atr}ATR gate",
                cfg.min_confidence,
            )
        return _make_signal(
            TradeDirection.SHORT, entry, entry + cfg.atr_sl_mult * atr, atr,
            f"EMA{cfg.fast_period} crossed below EMA{cfg.slow_period}; "
            f"slope {slope / atr:.2f}ATR > {cfg.slope_gate_atr}ATR gate",
            cfg.min_confidence,
        )


# ---------------------------------------------------------------------------
# 2. Donchian breakout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CryptoDonchianConfig:
    entry_lookback: int = 96
    exit_lookback: int = 48
    atr_buffer_mult: float = 0.5
    atr_period: int = 14
    atr_sl_mult: float = 3.0
    min_confidence: float = 0.55
    symbols: tuple[str, ...] = CRYPTO_NATIVE_SYMBOLS


class CryptoDonchianBreakout:
    """Donchian channel breakout with an ATR buffer against stop-runs.

    Long when close breaks the prior ``entry_lookback``-bar high plus
    ``atr_buffer_mult`` × ATR; short on the mirrored low. The buffer is
    the crypto-native piece: H1 crypto wicks routinely exceed 1× ATR
    and an unbuffered channel break churns exactly where the funding
    overlay punishes churn.
    """

    def __init__(self, config: CryptoDonchianConfig | None = None) -> None:
        self.config = config or CryptoDonchianConfig()
        self._cooldown_bars_remaining = 0

    @property
    def name(self) -> str:
        return "Crypto Donchian Breakout"

    def evaluate(self, state: MarketState) -> StrategySignal | None:  # noqa: D102
        cfg = self.config
        bars = state.bars
        if len(bars) < max(cfg.entry_lookback + 2, MIN_BARS_REQUIRED):
            return None
        atr = _atr(bars, cfg.atr_period)
        if atr <= 0.0:
            return None

        i = len(bars) - 1
        window = bars[i - cfg.entry_lookback : i]
        prior_high = max(b.high for b in window)
        prior_low = min(b.low for b in window)
        buffer = cfg.atr_buffer_mult * atr
        close = bars[i].close

        broke_up = close > prior_high + buffer
        broke_down = close < prior_low - buffer
        if not (broke_up or broke_down):
            return None
        if self._cooldown_bars_remaining > 0:
            self._cooldown_bars_remaining -= 1
            return None

        entry = close
        self._cooldown_bars_remaining = cfg.exit_lookback // 2
        if broke_up:
            return _make_signal(
                TradeDirection.LONG, entry, entry - cfg.atr_sl_mult * atr, atr,
                f"close {close:.2f} broke {cfg.entry_lookback}-bar high "
                f"{prior_high:.2f} + {buffer:.2f} ATR buffer",
                cfg.min_confidence,
            )
        return _make_signal(
            TradeDirection.SHORT, entry, entry + cfg.atr_sl_mult * atr, atr,
            f"close {close:.2f} broke {cfg.entry_lookback}-bar low "
            f"{prior_low:.2f} - {buffer:.2f} ATR buffer",
            cfg.min_confidence,
        )


# ---------------------------------------------------------------------------
# 3. Z-score mean reversion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CryptoZScoreConfig:
    lookback: int = 96
    z_entry: float = 2.5
    atr_period: int = 14
    atr_sl_mult: float = 2.5
    min_confidence: float = 0.50
    symbols: tuple[str, ...] = CRYPTO_NATIVE_SYMBOLS


class CryptoZScoreMeanReversion:
    """Rolling z-score mean reversion with a crypto-wide entry band.

    Fades closes whose z-score against the ``lookback``-bar rolling
    mean/σ exceeds ``z_entry``. The band is deliberately wide relative
    to FX defaults (crypto H1 σ is ~0.5% of price) so the strategy only
    fades genuine statistical extension, not ordinary crypto noise —
    each round trip must clear the downstream funding/venue cost stack.
    """

    def __init__(self, config: CryptoZScoreConfig | None = None) -> None:
        self.config = config or CryptoZScoreConfig()
        self._cooldown_bars_remaining = 0

    @property
    def name(self) -> str:
        return "Crypto Z-Score Mean Reversion"

    def evaluate(self, state: MarketState) -> StrategySignal | None:  # noqa: D102
        cfg = self.config
        bars = state.bars
        if len(bars) < max(cfg.lookback + 1, MIN_BARS_REQUIRED):
            return None
        atr = _atr(bars, cfg.atr_period)
        if atr <= 0.0:
            return None

        mean, std = _rolling_stats(bars, cfg.lookback)
        if std <= 0.0:
            return None
        close = bars[-1].close
        z = (close - mean) / std

        if abs(z) < cfg.z_entry:
            return None
        if self._cooldown_bars_remaining > 0:
            self._cooldown_bars_remaining -= 1
            return None

        entry = close
        self._cooldown_bars_remaining = cfg.lookback // 4
        # Fade the extension: high z → SHORT, low z → LONG.
        if z > 0:
            return _make_signal(
                TradeDirection.SHORT, entry, entry + cfg.atr_sl_mult * atr, atr,
                f"z={z:.2f} > +{cfg.z_entry}: fade extension above "
                f"{cfg.lookback}-bar mean",
                cfg.min_confidence,
            )
        return _make_signal(
            TradeDirection.LONG, entry, entry - cfg.atr_sl_mult * atr, atr,
            f"z={z:.2f} < -{cfg.z_entry}: fade extension below "
            f"{cfg.lookback}-bar mean",
            cfg.min_confidence,
        )


# ---------------------------------------------------------------------------
# Registry-facing factory (used by the bridge's lazy class resolution)
# ---------------------------------------------------------------------------


CRYPTO_NATIVE_STRATEGY_IDS: tuple[str, ...] = (
    "crypto_ema_cross_trend",
    "crypto_donchian_breakout",
    "crypto_zscore_mean_reversion",
)

_STRATEGY_CLASSES: dict[str, type] = {
    "crypto_ema_cross_trend": CryptoEMACrossTrend,
    "crypto_donchian_breakout": CryptoDonchianBreakout,
    "crypto_zscore_mean_reversion": CryptoZScoreMeanReversion,
}


def build_crypto_native_strategy(
    strategy_id: str,
    params: Mapping[str, Any] | None = None,
    pair: str | None = None,
) -> Any:
    """Instantiate a crypto-native strategy by registry id.

    ``params`` may override declared-grid keys only — any key outside
    ``CRYPTO_PARAM_GRIDS[strategy_id]`` raises ``ValueError`` so the
    no-post-hoc-tuning contract is enforced at construction time, not
    by reviewer vigilance.
    """
    if strategy_id not in _STRATEGY_CLASSES:
        raise ValueError(f"unknown crypto-native strategy_id {strategy_id!r}")
    merged: dict[str, Any] = dict(DEFAULT_GRID_CENTERS[strategy_id])
    if params:
        allowed = set(CRYPTO_PARAM_GRIDS[strategy_id])
        unknown = set(params) - allowed
        if unknown:
            raise ValueError(
                f"params {sorted(unknown)} outside declared grid for "
                f"{strategy_id!r} (allowed: {sorted(allowed)})"
            )
        off_grid = [
            key for key, val in params.items()
            if val not in CRYPTO_PARAM_GRIDS[strategy_id][key]
        ]
        if off_grid:
            raise ValueError(
                f"values for {sorted(off_grid)} outside declared grid for "
                f"{strategy_id!r} — no post-hoc tuning (card fe773687)"
            )
        merged.update(params)
    cls = _STRATEGY_CLASSES[strategy_id]
    cfg_cls = {
        "crypto_ema_cross_trend": CryptoEMACrossConfig,
        "crypto_donchian_breakout": CryptoDonchianConfig,
        "crypto_zscore_mean_reversion": CryptoZScoreConfig,
    }[strategy_id]
    _ = pair  # strategies are pair-agnostic (ATR/σ-relative); accepted for bridge parity
    return cls(cfg_cls(**merged))


__all__ = [
    "CRYPTO_NATIVE_SYMBOLS",
    "CRYPTO_NATIVE_STRATEGY_IDS",
    "CRYPTO_PARAM_GRIDS",
    "DEFAULT_GRID_CENTERS",
    "CryptoDonchianBreakout",
    "CryptoDonchianConfig",
    "CryptoEMACrossConfig",
    "CryptoEMACrossTrend",
    "CryptoZScoreConfig",
    "CryptoZScoreMeanReversion",
    "build_crypto_native_strategy",
]
