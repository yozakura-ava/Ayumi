"""Dual-Timeframe Squeeze Pro (research candidate B.2).

Two-stage squeeze strategy with H1 context determining *whether* to trade,
and M15 driving the actual entry trigger. The strategy is fed M15 bars
only; H1 bars are rebuilt incrementally inside the strategy so the engine
does not need to be changed (the ``MarketState`` only exposes a single
``bars`` list).

Reference: docs/research/strategy-optimization-research.md section B.2.

Stage 1 (H1 context — *whether* to trade)
-----------------------------------------
  - H1 BB(20, 2.0) inside H1 KC(20, ATR mult 1.5) → ``H1_SQUEEZE`` is True.
  - H1 EMA(50) slope → ``H1_DIRECTION`` ∈ {-1, 0, +1}.
  - H1 ADX(14) >= ``adx_min_h1`` (default 18.0).

Stage 2 (M15 entry trigger — *when* to trade)
---------------------------------------------
  - If ``H1_SQUEEZE`` is active:
        long when M15 close breaks H1 KC upper
        short when M15 close breaks H1 KC lower
  - Else (squeeze released):
        pullback long/short when M15 close is within 0.5*ATR(M15) of the
        H1 Keltner middle **and** aligned with ``H1_DIRECTION``.

Confirmation gates
------------------
  - H1 ADX >= ``adx_min_h1`` (mild trend — avoids the "no trend
    confirmation" bug from ``volatility_squeeze.py``).
  - M15 RSI(14) in ``[rsi_zone_min, rsi_zone_max]`` (default 40-60, i.e.
    breakout territory, not overbought / oversold).

Risk
----
  - Stop = whichever is *closer* of: opposite H1 Keltner band, or
    1.5 * ATR(M15) from entry.
  - TP1 / TP2 / TP3 at 1R / 2R / 3R.
  - Hard cap on SL distance in pips (default 50) prevents runaway
    stops on gold flash events.
  - ``time_exit_bars`` is the engine-level deadline
    (``max_bars_to_tp1``); the framework's trade manager enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from backtest.strategies.isignal_strategy import ISignalStrategy
from core.types import (
    Bar,
    BarPeriod,
    MarketState,
    StrategySignal,
    TradeDirection,
)
from utils.pip_value import pip_value_for_symbol

# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------


def _sma(values: List[float], period: int) -> float:
    """Simple moving average over the most recent ``period`` values.

    Returns 0.0 when ``len(values) < period`` (matches the legacy test
    contract; the strategy's indicator code is robust to either return).
    """
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def _ema_last(values: List[float], period: int) -> Optional[float]:
    """Last value of a standard EMA. Returns ``None`` if insufficient data."""
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def _stddev(values: List[float], period: int) -> float:
    """Population standard deviation over the most recent ``period`` values."""
    if len(values) < period:
        return 0.0
    subset = values[-period:]
    mean = sum(subset) / period
    var = sum((v - mean) ** 2 for v in subset) / period
    return var**0.5


def _true_range_bar(bar: Bar, prev_close: Optional[float]) -> float:
    """True range for a single bar (private helper used internally by
    the strategy). The public ``_true_range`` takes a list of bars
    (legacy contract for downstream tests)."""
    if prev_close is None:
        return bar.high - bar.low
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def _bb_inside_kc(
    closes: List[float],
    atr_value: float,
    bb_period: int,
    bb_std: float,
    kc_period: int,
    kc_atr_mult: float,
) -> Tuple[bool, float, float, float, float]:
    """Return (squeeze, bb_upper, bb_lower, kc_upper, kc_lower).

    The Keltner middle is the EMA of the close (it also serves as the
    Keltner middle for trend-filter purposes).
    """
    if len(closes) < max(bb_period, kc_period) or atr_value <= 0:
        return False, 0.0, 0.0, 0.0, 0.0

    sma = _sma(closes[-bb_period:], bb_period) or 0.0
    sd = _stddev(closes[-bb_period:], bb_period)
    bb_upper = sma + bb_std * sd
    bb_lower = sma - bb_std * sd

    kc_mid = _ema_last(closes, kc_period) or sma
    kc_upper = kc_mid + kc_atr_mult * atr_value
    kc_lower = kc_mid - kc_atr_mult * atr_value

    squeeze = bb_upper <= kc_upper and bb_lower >= kc_lower
    return squeeze, bb_upper, bb_lower, kc_upper, kc_lower


def _calculate_adx(bars: List[Bar], period: int = 14) -> float:
    """Simplified ADX over the most recent ``period`` windows.

    Matches the convention used by ``donchian_atr_trend_v2.py``: windowed
    average of DX values.
    """
    if len(bars) < period * 2 + 1:
        return 0.0

    true_ranges: List[float] = []
    plus_dms: List[float] = []
    minus_dms: List[float] = []

    for i in range(1, len(bars)):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        true_ranges.append(tr)
        plus_dms.append(up if up > down and up > 0 else 0.0)
        minus_dms.append(down if down > up and down > 0 else 0.0)

    if len(true_ranges) < period:
        return 0.0

    dx_values: List[float] = []
    window = period
    for j in range(len(true_ranges) - window + 1):
        seg_tr = true_ranges[j : j + window]
        seg_pdm = plus_dms[j : j + window]
        seg_mdm = minus_dms[j : j + window]
        seg_atr = sum(seg_tr) / window
        if seg_atr <= 0:
            dx_values.append(0.0)
            continue
        seg_pdi = 100.0 * (sum(seg_pdm) / window) / seg_atr
        seg_mdi = 100.0 * (sum(seg_mdm) / window) / seg_atr
        denom = seg_pdi + seg_mdi
        if denom == 0:
            dx_values.append(0.0)
        else:
            dx_values.append(100.0 * abs(seg_pdi - seg_mdi) / denom)

    return sum(dx_values) / len(dx_values) if dx_values else 0.0


def _calculate_rsi(bars: List[Bar], period: int = 14) -> float:
    """Wilder-style RSI on close-to-close deltas."""
    if len(bars) < period + 1:
        return 50.0
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(bars)):
        delta = bars[i].close - bars[i - 1].close
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    if len(gains) < period:
        return 50.0
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# Legacy helper aliases — kept so that pre-existing tests (which import
# short names like ``_sma``, ``_ema``, ``_std``, ``_adx`` etc.) continue
# to work. The implementation of these helpers is identical to the
# functions above; the names differ only in the entry-point signature.
# ---------------------------------------------------------------------------


def _std(values: List[float], period: int) -> float:
    """Alias for ``_stddev`` that returns 0.0 on insufficient data."""
    result = _stddev(values, period)
    return result if result is not None else 0.0


def _ema(values: List[float], period: int) -> List[float]:
    """Return the full EMA series (``len == len(values)``).

    The first ``period - 1`` entries are filled with the SMA-of-first-period
    value (matching the convention used by the legacy test suite).
    """
    if len(values) < period:
        return [0.0] * len(values)
    multiplier = 2.0 / (period + 1)
    out: List[float] = [0.0] * len(values)
    seed = sum(values[:period]) / period
    for i in range(period):
        out[i] = seed
    out[period - 1] = seed
    ema = seed
    for i in range(period, len(values)):
        ema = (values[i] - ema) * multiplier + ema
        out[i] = ema
    return out


def _atr_from_true_ranges(trs: List[float], period: int) -> float:
    """Wilder ATR seeded from a list of historical true ranges."""
    if len(trs) < period:
        return 0.0
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def _rsi(bars: List[Bar], period: int = 14) -> float:
    """Alias for ``_calculate_rsi``."""
    return _calculate_rsi(bars, period)


def _adx(h1_bars: List[Bar], period: int = 14) -> float:
    """Alias for ``_calculate_adx``."""
    return _calculate_adx(h1_bars, period)


def _keltner(
    closes: List[float],
    atrs: List[float],
    period: int,
    atr_mult: float,
) -> Tuple[float, float, float]:
    """Compute Keltner upper / middle / lower using pre-computed EMAs and ATRs.

    ``closes`` and ``atrs`` are aligned such that ``closes[i]`` is the close
    of the bar with ``atrs[i]`` true-range. This entry-point matches the
    legacy test-suite contract.
    """
    if len(closes) < period or len(atrs) < 1:
        return (0.0, 0.0, 0.0)
    middle = _ema(closes, period)[-1]
    a = atrs[-1]
    return (middle + a * atr_mult, middle, middle - a * atr_mult)


def _sma_full(values: List[float], period: int) -> float:
    """Return SMA over the most recent ``period`` values. Returns 0.0
    when ``len(values) < period``. Equivalent to the legacy test alias.
    """
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def _true_range(bars: List[Bar]) -> float:
    """Compute the most recent bar's true range from a list of bars.

    Returns 0.0 for a list of length < 2; otherwise standard true range
    using the previous bar's close. Matches the legacy test contract.
    """
    if len(bars) < 2:
        return 0.0
    b = bars[-1]
    p = bars[-2]
    return max(
        b.high - b.low,
        abs(b.high - p.close),
        abs(b.low - p.close),
    )


# ---------------------------------------------------------------------------
# Config dataclass (frozen; use dataclasses.replace for tuning)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DualTFSqueezeProConfig:
    """Tunable parameters for the Dual-Timeframe Squeeze Pro strategy.

    Defaults are tuned for XAUUSD M15.
    """

    # Symbol — drives pip resolution for the SL hard cap.
    symbol: str = "XAUUSD"
    # H1 Bollinger / Keltner parameters (context).
    h1_bb_period: int = 20
    h1_bb_std: float = 2.0
    h1_kc_period: int = 20
    h1_kc_atr_mult: float = 1.5
    # H1 EMA slope period for trend direction.
    h1_ema_period: int = 50
    # H1 ADX threshold (confirmation gate, not the strategy's own threshold).
    adx_min_h1: float = 18.0
    # M15 ATR for SL and pullback tolerance.
    m15_atr_period: int = 14
    # M15 RSI zone for confirmation gate.
    rsi_zone_min: float = 40.0
    rsi_zone_max: float = 60.0
    # Minimum confidence to emit a signal.
    min_confidence: float = 0.40
    # Bars to wait between signals (cooldown).
    cooldown_bars_m15: int = 10
    # Recommended engine-level time exit (bars). The trade manager's
    # ``max_bars_to_tp1`` should be set to at least this value for the
    # strategy to honor its time discipline. The strategy itself does
    # not enforce this — it documents it.
    time_exit_bars: int = 30
    # TP R-multiples (partial exits at 1R / 2R / 3R).
    tp1_rr: float = 1.0
    tp2_rr: float = 2.0
    tp3_rr: float = 3.0
    # Hard cap on stop-loss distance (pips) — prevents runaway stops.
    hard_cap_sl_pips: float = 50.0
    # ATR multiple for the stop when the opposite H1 Keltner band is
    # further away than ATR * mult from entry. Stored here so the
    # behaviour is auditable from config alone.
    atr_sl_multiplier: float = 1.5
    # Pullback tolerance in ATR(M15) units when the squeeze is released.
    pullback_atr_tolerance: float = 0.5
    # Minimum bars before evaluating. Largest warmup among all windows.
    min_bars_for_setup: int = 60


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class DualTFSqueezeProStrategy(ISignalStrategy):
    """Dual-timeframe Squeeze Pro — formal ``ISignalStrategy``.

    The engine feeds **M15** bars only; the strategy aggregates them into
    H1 bars on the fly. Each M15 close re-evaluates the strategy state
    incrementally through ``on_bar()``.
    """

    def __init__(self, config: DualTFSqueezeProConfig | None = None):
        super().__init__()
        self.config = config or DualTFSqueezeProConfig()
        # --- M15 incremental state ---
        self._m15_bars_seen: int = 0
        self._m15_atr: float = 0.0
        self._m15_prev_close: Optional[float] = None
        self._m15_prev_prev_close: Optional[float] = None  # for ATR rolling warm-up
        # True-range warm-up buffer (simple average until period met).
        self._m15_tr_buffer: List[float] = []
        # --- H1 incremental state ---
        self._h1_bars: List[Bar] = []
        self._h1_closes: List[float] = []
        self._h1_current: Optional[Bar] = None
        self._h1_prev_close: Optional[float] = None
        self._h1_trs: List[float] = []
        self._h1_atr: float = 0.0
        # --- Cooldown ---
        self._bars_since_signal: int = 999

    # --- Lifecycle --------------------------------------------------------

    @property
    def name(self) -> str:
        return "Dual-TF Squeeze Pro"

    def reset(self) -> None:
        """Reset all incremental state. Called between backtest runs."""
        super().reset()
        self._m15_bars_seen = 0
        self._m15_atr = 0.0
        self._m15_prev_close = None
        self._m15_prev_prev_close = None
        self._m15_tr_buffer.clear()
        self._h1_bars.clear()
        self._h1_closes.clear()
        self._h1_current = None
        self._h1_prev_close = None
        self._h1_trs.clear()
        self._h1_atr = 0.0
        # 999 = "no signal ever" sentinel (matches ``__init__``); the
        # strategy is otherwise indistinguishable from a fresh instance.
        self._bars_since_signal = 999

    def on_bar(self, bar: Bar) -> None:
        """Update incremental indicators and the synthetic H1 series.

        Called by the engine for each completed bar before ``evaluate()``.
        Keeping the per-bar updates O(1) (or O(period) for the initial
        warm-up) means ``evaluate()`` does not need to walk the entire
        history each bar.
        """
        super().on_bar(bar)
        # Mark M15 if not tagged (the engine should set BarPeriod but
        # some tests leave it at the dataclass default of H1).
        self._update_m15_atr(bar)
        self._update_h1(bar)
        self._m15_bars_seen += 1

    # --- Incremental indicator updates -----------------------------------

    def _update_m15_atr(self, bar: Bar) -> None:
        """Update the rolling M15 ATR (Wilder smoothing)."""
        tr = _true_range_bar(bar, self._m15_prev_close)
        period = self.config.m15_atr_period

        if self._m15_prev_close is None:
            # First bar — nothing to update against.
            self._m15_prev_close = bar.close
            return

        self._m15_tr_buffer.append(tr)
        if len(self._m15_tr_buffer) > period:
            self._m15_tr_buffer = self._m15_tr_buffer[-period:]

        if len(self._m15_tr_buffer) < period:
            # Simple-average warm-up until we have ``period`` true ranges.
            self._m15_atr = sum(self._m15_tr_buffer) / len(self._m15_tr_buffer)
        else:
            # Wilder smoothing.
            self._m15_atr = (self._m15_atr * (period - 1) + tr) / period

        self._m15_prev_prev_close = self._m15_prev_close
        self._m15_prev_close = bar.close

    def _update_h1(self, bar: Bar) -> None:
        """Aggregate M15 bar into synthetic H1 bar and update H1 ATR.

        The synthetic H1 bar is registered in ``_h1_bars`` immediately
        on creation so external observers (and ``evaluate()``) see every
        hour seen so far — including the in-progress one — rather than
        only finalized hours. ``_h1_current`` is therefore always a
        reference to ``_h1_bars[-1]``; mutations to it are visible at
        the tail of ``_h1_bars`` as well.
        """
        # Use the bar.time to bucket by hour. If bar.time is naive,
        # ``replace(minute=0, second=0, microsecond=0)`` is fine.
        hour = bar.time.replace(minute=0, second=0, microsecond=0)

        if self._h1_current is None or self._h1_current.time != hour:
            # New hour: finalize the previous (still living at
            # ``_h1_bars[-1]``) by computing its true range and stepping
            # the ATR. No need to re-append — it is already in the list.
            if self._h1_current is not None:
                cur = self._h1_current
                if len(self._h1_bars) >= 2:
                    prev_close = self._h1_bars[-2].close
                    tr = max(
                        cur.high - cur.low,
                        abs(cur.high - prev_close),
                        abs(cur.low - prev_close),
                    )
                else:
                    tr = cur.high - cur.low
                self._h1_trs.append(tr)
                self._step_h1_atr()
            # Register the new synthetic H1 bar in the list immediately
            # so ``len(_h1_bars)`` == number of distinct hours seen.
            new_bar = Bar(
                time=hour,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                period=BarPeriod.H1(),
            )
            self._h1_bars.append(new_bar)
            self._h1_closes.append(bar.close)
            self._h1_current = new_bar
        else:
            # Same hour: extend the current synthetic H1 bar in place.
            # ``_h1_current`` is ``_h1_bars[-1]`` (same object), so
            # tail-of-list callers see the update for free.
            cur = self._h1_current
            cur.high = max(cur.high, bar.high)
            cur.low = min(cur.low, bar.low)
            cur.close = bar.close
            cur.volume += bar.volume
            if self._h1_closes:
                self._h1_closes[-1] = cur.close

    def _step_h1_atr(self) -> None:
        """Wilder ATR over the appended H1 true-range series."""
        period = max(self.config.h1_bb_period, self.config.h1_kc_period)
        period = max(period, self.config.h1_ema_period)
        period = max(period, 14)
        if self._h1_atr == 0.0:
            if len(self._h1_trs) < period:
                self._h1_atr = 0.0
                return
            self._h1_atr = sum(self._h1_trs[-period:]) / period
        else:
            latest = self._h1_trs[-1]
            self._h1_atr = (self._h1_atr * (period - 1) + latest) / period

    # --- Evaluation -------------------------------------------------------

    def evaluate(self, state: MarketState) -> Optional[StrategySignal]:
        cfg = self.config
        m15_bars = state.bars

        # Warm-up: need enough H1 bars for every H1 indicator.
        adx_lookback_bars_h1 = max(14 * 2 + 1, cfg.h1_ema_period + 6)  # noqa: F841 — live strategy; dead helper preserved
        h1_required = max(
            cfg.h1_bb_period,
            cfg.h1_kc_period,
            cfg.h1_ema_period + 6,
            28,  # H1 ADX lookback minimum
        )
        # Bar count is in M15 bars. Approximate requirement: enough M15
        # bars to have produced ``h1_required`` synthetic H1 bars
        # (4 M15 bars per H1 bar).
        min_m15 = max(cfg.min_bars_for_setup, h1_required * 4 + cfg.m15_atr_period * 2 + 4)
        if len(m15_bars) < min_m15:
            return None

        # Cooldown.
        if self._bars_since_signal < cfg.cooldown_bars_m15:
            self._bars_since_signal += 1
            return None

        self._bars_since_signal += 1

        # If the engine bypassed on_bar (e.g. in tests), make sure the
        # incremental state is in sync with the MarketState.bars.
        if self._m15_bars_seen < len(m15_bars):
            for b in m15_bars[self._m15_bars_seen :]:
                self._update_m15_atr(b)
                self._update_h1(b)
            self._m15_bars_seen = len(m15_bars)

        # --- H1 snapshot ----------------------------------------------------
        if len(self._h1_bars) < h1_required or self._h1_atr <= 0:
            return None

        h1_closes = [b.close for b in self._h1_bars]
        squeeze, _, _, kc_u, kc_l = _bb_inside_kc(
            h1_closes,
            self._h1_atr,
            cfg.h1_bb_period,
            cfg.h1_bb_std,
            cfg.h1_kc_period,
            cfg.h1_kc_atr_mult,
        )
        kc_mid = _ema_last(h1_closes, cfg.h1_kc_period)
        if kc_mid is None:
            return None

        h1_adx = _calculate_adx(self._h1_bars, period=14)
        if h1_adx < cfg.adx_min_h1:
            return None

        # H1 trend direction = sign(EMA(50) slope over recent window).
        ema_now = _ema_last(h1_closes, cfg.h1_ema_period)
        ema_prev_window = (
            h1_closes[-(cfg.h1_ema_period + 5) : -5]
            if len(h1_closes) > cfg.h1_ema_period + 5
            else h1_closes[: max(1, len(h1_closes) - cfg.h1_ema_period)]
        )
        ema_prev = _ema_last(ema_prev_window, cfg.h1_ema_period) if ema_prev_window else None
        if ema_now is None or ema_prev is None:
            return None
        if ema_now > ema_prev:
            h1_direction = 1
        elif ema_now < ema_prev:
            h1_direction = -1
        else:
            h1_direction = 0

        # --- M15 snapshot ---------------------------------------------------
        m15_atr = self._m15_atr
        if m15_atr <= 0:
            return None

        m15_rsi = _calculate_rsi(m15_bars, period=14)
        if not (cfg.rsi_zone_min <= m15_rsi <= cfg.rsi_zone_max):
            return None

        latest = m15_bars[-1]
        entry = latest.close
        direction: Optional[TradeDirection] = None
        signal_kind = ""

        if squeeze:
            # Squeeze breakout: M15 close breaches the H1 Keltner band.
            if entry > kc_u:
                direction = TradeDirection.LONG
                signal_kind = "squeeze_break_long"
            elif entry < kc_l:
                direction = TradeDirection.SHORT
                signal_kind = "squeeze_break_short"
        else:
            # Pullback continuation: M15 close near H1 Keltner middle in H1 trend direction.
            tol = cfg.pullback_atr_tolerance * m15_atr
            if h1_direction == 1 and abs(entry - kc_mid) <= tol:
                direction = TradeDirection.LONG
                signal_kind = "pullback_long"
            elif h1_direction == -1 and abs(entry - kc_mid) <= tol:
                direction = TradeDirection.SHORT
                signal_kind = "pullback_short"

        if direction is None:
            return None

        # --- Stop: opposite H1 KC band OR 1.5 * ATR(M15), whichever is closer.
        if direction == TradeDirection.LONG:
            sl_kc = kc_l
            sl_atr = entry - cfg.atr_sl_multiplier * m15_atr
            raw_sl = max(sl_kc, sl_atr)  # closer stop = higher price for longs
            if raw_sl >= entry:
                return None
        else:
            sl_kc = kc_u
            sl_atr = entry + cfg.atr_sl_multiplier * m15_atr
            raw_sl = min(sl_kc, sl_atr)  # closer stop = lower price for shorts
            if raw_sl <= entry:
                return None

        # --- Hard cap on SL distance (pips).
        pip = pip_value_for_symbol(cfg.symbol)
        risk = abs(entry - raw_sl)
        sl_pips = risk / pip if pip > 0 else 0.0
        if sl_pips > cfg.hard_cap_sl_pips:
            capped_risk = cfg.hard_cap_sl_pips * pip
            if direction == TradeDirection.LONG:
                sl = entry - capped_risk
            else:
                sl = entry + capped_risk
            # After tightening the SL, ensure it still goes the right way.
            if (direction == TradeDirection.LONG and sl >= entry) or (
                direction == TradeDirection.SHORT and sl <= entry
            ):
                return None
        else:
            sl = raw_sl

        risk = abs(entry - sl)
        if risk <= 0:
            return None

        if direction == TradeDirection.LONG:
            tp1 = entry + risk * cfg.tp1_rr
            tp2 = entry + risk * cfg.tp2_rr
            tp3 = entry + risk * cfg.tp3_rr
        else:
            tp1 = entry - risk * cfg.tp1_rr
            tp2 = entry - risk * cfg.tp2_rr
            tp3 = entry - risk * cfg.tp3_rr

        # --- Confidence ---------------------------------------------------
        # Base = min_confidence. Boost for squeeze break (+0.10), ADX
        # strength above the threshold, and how close RSI is to 50.
        confidence = cfg.min_confidence
        if squeeze:
            confidence += 0.10
        adx_excess_norm = max(0.0, (h1_adx - cfg.adx_min_h1) / cfg.adx_min_h1)
        confidence += min(adx_excess_norm * 0.10, 0.15)
        # RSI mid-distance bonus: closer to 50 = stronger "not overbought".
        rsi_mid_dist = abs(m15_rsi - 50.0) / 10.0
        confidence += max(0.0, 0.10 - rsi_mid_dist * 0.05)
        confidence = min(confidence, 0.85)
        if confidence < cfg.min_confidence:
            return None

        self._bars_since_signal = 0

        rationale = (
            f"DTSQ-Pro {signal_kind}: H1 squeeze={squeeze} dir={h1_direction} "
            f"ADX={h1_adx:.1f} KC=({kc_l:.5f}/{kc_mid:.5f}/{kc_u:.5f}); "
            f"M15 RSI={m15_rsi:.1f} close={entry:.5f} stop={sl:.5f} risk={risk:.5f}"
        )

        return StrategySignal(
            direction=direction,
            confidence=confidence,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rationale=rationale,
        )
