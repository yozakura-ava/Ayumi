"""Incremental indicator cache for the Ayumi tournament harness.

Card b1bb93e8 [BUILD][AYUMI][URGENT] Shared-indicator precompute —

The strategies in ``src/forex-bot/strategies/*.py`` recompute their
indicators (ATR / RSI / ADX / EMA / SMA / STD / Bollinger) on every
``evaluate()`` call by re-walking the full ``state.bars`` list.  When
the harness walks ``N`` bars sequentially and the strategy maintains
``state.bars`` of length 1..N (a sliding window), each strategy
incurs ``O(N)`` work per bar ⇒ ``O(N²)`` total — 5–10h for 17
strategies over ~25k EURUSD H1 bars.

This module adds a **harness-side** incremental cache so strategy
sources stay byte-identical (the card's preferred path: harness-side
incremental cache that strategies already call through).  How it
works:

1. ``IncrementalIndicatorCache`` maintains per-(family, period)
   state across calls.  Each cache keeps the bookkeeping the
   indicator needs — running TR sums for ATR, Wilder-smoothed
   avg_gain/avg_loss for one RSI variant, a rolling deque of last
   ``period`` changes for the other RSI variant (the SRMR+ style:
   sum of gains/losses over the last ``period`` changes), smoothed
   TR/+DM/-DM plus the DX seed sum for ADX, etc.
2. ``install_indicator_cache(strategy, ...)`` is called by the
   harness immediately after the strategy is constructed and BEFORE
   the bar loop runs.  It enumerates candidate indicator functions
   on the strategy's module (``_calculate_atr``, ``_calculate_rsi``,
   ``_calculate_adx``, ``_calculate_ema``, ``_sma``, ``_std``,
   ``_bollinger_bands`` — a curated allow-list, not a name pattern
   blast radius) and replaces each with a cached wrapper backed by
   the strategy's own ``IncrementalIndicatorCache`` instance.
3. ``advance_to(bars)`` is called once per bar to feed the new bar's
   high/low/close (for ``bars``-input functions) or close (for
   ``values``-input functions) into every cached stream.  After
   that, the cached wrapper answers subsequent calls with the
   pre-computed scalar — O(1) per call instead of O(N).

Equivalence: every cached function is implemented to produce values
IDENTICAL to the inline ``_calculate_*`` implementations in
``src/forex-bot/strategies/*`` within ``1e-9`` floating-point
tolerance.  The validating test suite under
``tests/tournament/test_indicator_cache.py`` asserts bar-by-bar
equality against the inline (un-patched) reference so any future
divergence trips the test.

Constraint (card b1bb93e8 §2): strategy source files stay
byte-identical.  This module is purely additive — the harness is the
ONLY modification; the monkey-patch replaces function attributes on
already-loaded modules at runtime and is reverted when the harness
exits the strategy loop (via ``__exit__`` in
``install_indicator_cache``).
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from core.types import Bar


# ── Per-strategy indicator state containers ──────────────────────────────────


@dataclass
class _AtrState:
    """Incremental ATR (period).  Maintains a rolling deque of TRs.

    O(1) per new bar; O(period) memory.  Matches the inline
    ``_calculate_atr`` arithmetic: ``sum(TR[-period:]) / period``.
    """

    period: int
    tr_ring: list[float] = field(default_factory=list)
    tr_sum: float = 0.0
    prev_close: float | None = None


@dataclass
class _RsiWilderState:
    """Incremental RSI via Wilder's smoothing (bb_rsi_reversion style).

    Seed: first ``period`` gains/losses averaged.  After the seed
    window completes, each new bar advances the smoothed averages by
    ``avg = (avg*(p-1) + x)/p`` — the same arithmetic the inline
    functions apply on a per-call re-walk.
    """

    period: int
    seed_count: int = 0
    seed_gain_sum: float = 0.0
    seed_loss_sum: float = 0.0
    avg_gain: float = 0.0
    avg_loss: float = 0.0
    prev_close: float | None = None
    smoothed: bool = False


@dataclass
class _RsiSmaState:
    """Incremental RSI via SMA of last ``period`` changes (SRMR+ style).

    SRMR+'s ``_calculate_rsi(bars, period)`` iterates over the LAST
    ``period`` bar changes (``range(len(bars)-period, len(bars))``),
    bucketing each into a separate gain or loss, then takes the
    simple mean.  This is fundamentally NOT Wilder smoothing — it is
    a rolling sum / period mean, so the cache state is a ring buffer
    of changes plus two running sums (gains_sum, losses_sum).
    """

    period: int
    ring: list[float] = field(default_factory=list)
    gain_sum: float = 0.0
    loss_sum: float = 0.0
    prev_close: float | None = None


@dataclass
class _AdxState:
    """Incremental ADX (period) via Wilder smoothing.

    Tracks seed-stage TR/+DM/-DM sums until ``period`` bars have been
    observed; after the seed, sm_tr/sm_pdm/sm_mdm are updated via
    Wilder's formula each bar.  The DX list is tracked implicitly via
    a sum-first-period accumulator (not a full list) plus the running
    smoothed ADX value.
    """

    period: int
    seed_count: int = 0
    seed_tr: float = 0.0
    seed_pdm: float = 0.0
    seed_mdm: float = 0.0
    sm_tr: float = 0.0
    sm_pdm: float = 0.0
    sm_mdm: float = 0.0
    smoothed: bool = False
    # After smoothing=True:
    # - dx_seed_count = number of DX values seen so far (each is one dx_* value)
    # - dx_seed_sum   = sum of the first ``period`` DX values, fixed once full
    # When dx_seed_count > period (i.e. the (period+1)-th DX arrived),
    # the ADX seed-mean + one Wilder-step is computed from those values.
    dx_seed_count: int = 0
    dx_seed_sum: float = 0.0
    adx_val: float = 0.0
    adx_smoothed: bool = False
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None


@dataclass
class _EmaState:
    """Incremental EMA (period) — seed with SMA of first ``period`` values."""

    period: int
    seed_count: int = 0
    seed_sum: float = 0.0
    ema_val: float = 0.0
    smoothed: bool = False


@dataclass
class _SmaState:
    """Rolling SMA via deque + running sum (period)."""

    period: int
    ring: list[float] = field(default_factory=list)
    ring_sum: float = 0.0


@dataclass
class _StdState:
    """Rolling std (population).  variance = sum_sq/period - mean^2."""

    period: int
    ring: list[float] = field(default_factory=list)
    ring_sum: float = 0.0
    ring_sum_sq: float = 0.0


# ── Helpers ─────────────────────────────────────────────────────────────────


def _dx_from_sums(sm_tr: float, sm_pdm: float, sm_mdm: float) -> float:
    """Compute DX from smoothed (or seed) TR/+DM/-DM sums.

    Returns 0.0 when sm_tr is zero (matches the inline ``if tr_sum == 0``
    branches that yield ``dx = 0.0`` in both srmr_plus and the inline
    builder).
    """
    if sm_tr <= 0:
        return 0.0
    plus_di = 100.0 * sm_pdm / sm_tr
    minus_di = 100.0 * sm_mdm / sm_tr
    di_sum = plus_di + minus_di
    if di_sum <= 0:
        return 0.0
    return 100.0 * abs(plus_di - minus_di) / di_sum


# ── Cache façade ─────────────────────────────────────────────────────────────


class IncrementalIndicatorCache:
    """Per-strategy state holder for incremental indicator computation."""

    def __init__(self) -> None:
        self.atr_states: dict[int, _AtrState] = {}
        self.rsi_wilder_states: dict[int, _RsiWilderState] = {}
        self.rsi_sma_states: dict[int, _RsiSmaState] = {}
        self.adx_states: dict[int, _AdxState] = {}
        self.ema_states: dict[int, _EmaState] = {}
        self.sma_states: dict[int, _SmaState] = {}
        self.std_states: dict[int, _StdState] = {}

    # ── Per-bar advance ────────────────────────────────────────────────
    def advance_bar(self, bar: Bar) -> None:
        """Append ``bar`` to every active ATR / RSI (both variants) / ADX stream.

        EMA / SMA / STD are value-stream caches and use ``advance_value``
        from the harness instead (they take ``closes`` arrays, not raw bars).
        """
        for state in self.atr_states.values():
            self._advance_atr(state, bar)
        for state in self.rsi_wilder_states.values():
            self._advance_rsi_wilder(state, bar)
        for state in self.rsi_sma_states.values():
            self._advance_rsi_sma(state, bar)
        for state in self.adx_states.values():
            self._advance_adx(state, bar)

    def advance_value(self, value: float) -> None:
        """Append a numeric value to EMA / SMA / STD streams."""
        for state in self.ema_states.values():
            self._advance_ema(state, value)
        for state in self.sma_states.values():
            self._advance_sma(state, value)
        for state in self.std_states.values():
            self._advance_std(state, value)

    # ── Incremental primitives ─────────────────────────────────────────
    @staticmethod
    def _advance_atr(state: _AtrState, bar: Bar) -> None:
        if state.prev_close is not None:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - state.prev_close),
                abs(bar.low - state.prev_close),
            )
            if len(state.tr_ring) >= state.period:
                state.tr_sum -= state.tr_ring[0]
                state.tr_ring.pop(0)
            state.tr_ring.append(tr)
            state.tr_sum += tr
        state.prev_close = bar.close

    @staticmethod
    def _advance_rsi_wilder(state: _RsiWilderState, bar: Bar) -> None:
        if state.prev_close is None:
            state.prev_close = bar.close
            return
        change = bar.close - state.prev_close
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        if not state.smoothed:
            state.seed_gain_sum += gain
            state.seed_loss_sum += loss
            state.seed_count += 1
            if state.seed_count >= state.period:
                state.avg_gain = state.seed_gain_sum / state.period
                state.avg_loss = state.seed_loss_sum / state.period
                state.smoothed = True
        else:
            state.avg_gain = (state.avg_gain * (state.period - 1) + gain) / state.period
            state.avg_loss = (state.avg_loss * (state.period - 1) + loss) / state.period
        state.prev_close = bar.close

    @staticmethod
    def _advance_rsi_sma(state: _RsiSmaState, bar: Bar) -> None:
        if state.prev_close is None:
            state.prev_close = bar.close
            return
        change = bar.close - state.prev_close
        new_gain = change if change > 0 else 0.0
        new_loss = -change if change < 0 else 0.0
        # Slide the change ring: pop oldest gain/loss contribution off the
        # running sums before adding the new one so the cached sum always
        # equals sum(gains over last `period` changes).
        if len(state.ring) >= state.period:
            old = state.ring[0]
            state.gain_sum -= old if old > 0 else 0.0
            state.loss_sum -= -old if old < 0 else 0.0
            state.ring.pop(0)
        state.ring.append(change)
        state.gain_sum += new_gain
        state.loss_sum += new_loss
        state.prev_close = bar.close

    @staticmethod
    def _advance_adx(state: _AdxState, bar: Bar) -> None:
        # First bar: no prior reference; just record OHLC for next iteration.
        if state.prev_high is None:
            state.prev_high = bar.high
            state.prev_low = bar.low
            state.prev_close = bar.close
            return

        ph, pl, pc = state.prev_high, state.prev_low, state.prev_close
        tr = max(
            bar.high - bar.low,
            abs(bar.high - pc),
            abs(bar.low - pc),
        )
        up = bar.high - ph
        down = pl - bar.low
        pdm = up if (up > down and up > 0) else 0.0
        mdm = down if (down > up and down > 0) else 0.0

        # Two-stage update: first update sm_* via the seed OR Wilder rule
        # (matching how the inline algorithm processes TR/DM before
        # computing DI), then feed the resulting DX value into the ADX
        # accumulator.  Crucially, dx_seed_count increments ONLY when we
        # have a real DX value (i.e. once ``smoothed=True``).
        current_dx: float | None = None
        if not state.smoothed:
            state.seed_tr += tr
            state.seed_pdm += pdm
            state.seed_mdm += mdm
            state.seed_count += 1
            if state.seed_count >= state.period:
                state.sm_tr = state.seed_tr
                state.sm_pdm = state.seed_pdm
                state.sm_mdm = state.seed_mdm
                state.smoothed = True
                # First DX: computed with seed sums (= dx_list[0] in inline).
                current_dx = _dx_from_sums(state.sm_tr, state.sm_pdm, state.sm_mdm)
        else:
            state.sm_tr = state.sm_tr - state.sm_tr / state.period + tr
            state.sm_pdm = state.sm_pdm - state.sm_pdm / state.period + pdm
            state.sm_mdm = state.sm_mdm - state.sm_mdm / state.period + mdm
            current_dx = _dx_from_sums(state.sm_tr, state.sm_pdm, state.sm_mdm)

        # Feed ADX accumulator only when we actually have a DX value.
        if current_dx is not None:
            state.dx_seed_count += 1
            state.dx_seed_sum += current_dx
            if not state.adx_smoothed:
                if state.dx_seed_count > state.period:
                    # We just crossed into the (period+1)-th DX; seed ADX
                    # from the first `period` DX values mean and apply one
                    # Wilder step with the current (period+1)-th DX value.
                    first_period_sum = state.dx_seed_sum - current_dx
                    seed_mean = first_period_sum / state.period
                    state.adx_val = (
                        seed_mean * (state.period - 1) + current_dx
                    ) / state.period
                    state.adx_smoothed = True
            else:
                state.adx_val = (
                    state.adx_val * (state.period - 1) + current_dx
                ) / state.period

        state.prev_high = bar.high
        state.prev_low = bar.low
        state.prev_close = bar.close

    @staticmethod
    def _advance_ema(state: _EmaState, value: float) -> None:
        if not state.smoothed:
            state.seed_sum += value
            state.seed_count += 1
            if state.seed_count >= state.period:
                state.ema_val = state.seed_sum / state.period
                state.smoothed = True
            return
        k = 2.0 / (state.period + 1)
        state.ema_val = value * k + state.ema_val * (1 - k)

    @staticmethod
    def _advance_sma(state: _SmaState, value: float) -> None:
        if len(state.ring) >= state.period:
            state.ring_sum -= state.ring[0]
            state.ring.pop(0)
        state.ring.append(value)
        state.ring_sum += value

    @staticmethod
    def _advance_std(state: _StdState, value: float) -> None:
        if len(state.ring) >= state.period:
            old = state.ring[0]
            state.ring_sum -= old
            state.ring_sum_sq -= old * old
            state.ring.pop(0)
        state.ring.append(value)
        state.ring_sum += value
        state.ring_sum_sq += value * value

    # ── Public getters ─────────────────────────────────────────────────
    def atr_value(self, period: int) -> float | None:
        state = self.atr_states.get(period)
        if state is None or len(state.tr_ring) < state.period:
            return None
        return state.tr_sum / state.period

    def rsi_value(self, period: int) -> float | None:
        # Returns the Wilder variant (default for most strategies).
        state = self.rsi_wilder_states.get(period)
        if state is None or not state.smoothed:
            return None
        if state.avg_loss == 0:
            return 100.0
        rs = state.avg_gain / state.avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def rsi_sma_value(self, period: int) -> float | None:
        # Returns the SMA variant (SRMR+ style).
        state = self.rsi_sma_states.get(period)
        if state is None or len(state.ring) < state.period:
            return None
        avg_gain = state.gain_sum / state.period
        avg_loss = state.loss_sum / state.period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def adx_value(self, period: int) -> float | None:
        state = self.adx_states.get(period)
        if state is None or not state.adx_smoothed:
            return None
        return state.adx_val

    def ema_value(self, period: int) -> float | None:
        state = self.ema_states.get(period)
        if state is None or not state.smoothed:
            return None
        return state.ema_val

    def sma_value(self, period: int) -> float | None:
        state = self.sma_states.get(period)
        if state is None or len(state.ring) < state.period:
            return None
        return state.ring_sum / state.period

    def std_value(self, period: int) -> float | None:
        state = self.std_states.get(period)
        if state is None or len(state.ring) < state.period:
            return None
        n = state.period
        mean = state.ring_sum / n
        variance = state.ring_sum_sq / n - mean * mean
        if variance < 0:
            variance = 0.0
        return math.sqrt(variance)

    def bollinger_value(
        self, period: int, std_dev: float
    ) -> tuple[float | None, float | None, float | None]:
        sma = self.sma_value(period)
        std = self.std_value(period)
        if sma is None or std is None:
            return (None, None, None)
        upper = sma + std * std_dev
        lower = sma - std * std_dev
        return (upper, sma, lower)

    # ── State book-keeping ──────────────────────────────────────────────
    def register_periods(
        self,
        *,
        atr_periods: set[int] | None = None,
        rsi_periods: set[int] | None = None,
        rsi_sma_periods: set[int] | None = None,
        adx_periods: set[int] | None = None,
        ema_periods: set[int] | None = None,
        sma_periods: set[int] | None = None,
        std_periods: set[int] | None = None,
    ) -> None:
        """Pre-allocate state objects for the periods the strategy uses.

        ``rsi_periods`` allocates Wilder-variant states;
        ``rsi_sma_periods`` allocates SMA-variant states.  Most
        strategies use Wilder; SRMR+ uses SMA on its
        ``_calculate_rsi``.  Idempotent — re-registering the same
        period is a no-op so the harness can call this on every
        iteration.
        """
        for p in atr_periods or set():
            self.atr_states.setdefault(p, _AtrState(period=p))
        for p in rsi_periods or set():
            self.rsi_wilder_states.setdefault(p, _RsiWilderState(period=p))
        for p in rsi_sma_periods or set():
            self.rsi_sma_states.setdefault(p, _RsiSmaState(period=p))
        for p in adx_periods or set():
            self.adx_states.setdefault(p, _AdxState(period=p))
        for p in ema_periods or set():
            self.ema_states.setdefault(p, _EmaState(period=p))
        for p in sma_periods or set():
            self.sma_states.setdefault(p, _SmaState(period=p))
        for p in std_periods or set():
            self.std_states.setdefault(p, _StdState(period=p))


# ── Wrapper factories (monkey-patch points) ──────────────────────────────────


def make_atr_wrapper(orig_fn: Callable, cache: IncrementalIndicatorCache, period: int):
    def wrapper(bars, period: int = period) -> float:
        cached = cache.atr_value(period)
        if cached is not None:
            return cached
        return orig_fn(bars, period)

    wrapper.__name__ = orig_fn.__name__
    wrapper.__wrapped__ = orig_fn  # type: ignore[attr-defined]
    return wrapper


def make_rsi_wrapper(orig_fn: Callable, cache: IncrementalIndicatorCache, period: int):
    def wrapper(bars, period: int = period):
        cached = cache.rsi_value(period)
        if cached is not None:
            return cached
        return orig_fn(bars, period)

    wrapper.__name__ = orig_fn.__name__
    wrapper.__wrapped__ = orig_fn  # type: ignore[attr-defined]
    return wrapper


def make_rsi_sma_wrapper(orig_fn: Callable, cache: IncrementalIndicatorCache, period: int):
    def wrapper(bars, period: int = period):
        cached = cache.rsi_sma_value(period)
        if cached is not None:
            return cached
        return orig_fn(bars, period)

    wrapper.__name__ = orig_fn.__name__
    wrapper.__wrapped__ = orig_fn  # type: ignore[attr-defined]
    return wrapper


def make_adx_wrapper(orig_fn: Callable, cache: IncrementalIndicatorCache, period: int):
    def wrapper(bars, period: int = period):
        cached = cache.adx_value(period)
        if cached is not None:
            return cached
        return orig_fn(bars, period)

    wrapper.__name__ = orig_fn.__name__
    wrapper.__wrapped__ = orig_fn  # type: ignore[attr-defined]
    return wrapper


def make_ema_wrapper(orig_fn: Callable, cache: IncrementalIndicatorCache, period: int):
    def wrapper(values, period: int = period):
        cached = cache.ema_value(period)
        if cached is not None:
            return cached
        return orig_fn(values, period)

    wrapper.__name__ = orig_fn.__name__
    wrapper.__wrapped__ = orig_fn  # type: ignore[attr-defined]
    return wrapper


def make_sma_wrapper(orig_fn: Callable, cache: IncrementalIndicatorCache, period: int):
    def wrapper(values, period: int = period):
        cached = cache.sma_value(period)
        if cached is not None:
            return cached
        return orig_fn(values, period)

    wrapper.__name__ = orig_fn.__name__
    wrapper.__wrapped__ = orig_fn  # type: ignore[attr-defined]
    return wrapper


def make_std_wrapper(orig_fn: Callable, cache: IncrementalIndicatorCache, period: int):
    def wrapper(values, period: int = period):
        cached = cache.std_value(period)
        if cached is not None:
            return cached
        return orig_fn(values, period)

    wrapper.__name__ = orig_fn.__name__
    wrapper.__wrapped__ = orig_fn  # type: ignore[attr-defined]
    return wrapper


def make_bollinger_wrapper(
    orig_fn: Callable, cache: IncrementalIndicatorCache, period: int, std_dev: float
):
    def wrapper(values, period: int = period, std_dev: float = std_dev):
        cached = cache.bollinger_value(period, std_dev)
        if cached[0] is not None:
            return cached
        return orig_fn(values, period, std_dev)

    wrapper.__name__ = orig_fn.__name__
    wrapper.__wrapped__ = orig_fn  # type: ignore[attr-defined]
    return wrapper


# ── Strategy introspection + monkey-patch ───────────────────────────────────


# Curated allow-list of indicator names.  Adding to this map is the
# reviewed path for new indicators.  Unknown names are NEVER modified.
KNOWN_INDICATOR_FUNCTIONS: dict[str, str] = {
    # name -> family
    "_calculate_atr": "atr",
    "_atr": "atr",
    "_calculate_rsi": "rsi",  # default = Wilder; SMA variant comes via override
    "_rsi": "rsi",
    "_calculate_adx": "adx",
    "_adx": "adx",
    "_calculate_ema": "ema",
    "_ema": "ema",
    "_sma": "sma",
    "_std": "std",
    "_bollinger_bands": "bb",
}

# Per-(module, attr) override for the algorithm variant of the
# ``rsi`` family.  Most strategies use Wilder smoothing (bb_rsi,
# momentum, etc.); some — SRMR+, session_range_mean_reversion,
# momentum, killzone_momentum — use an SMA-of-last-period-changes
# variant which is structurally different so the cache needs to
# know which.  Keep this map in sync with the per-strategy inline
# implementations; if you add a new strategy and its inline RSI is
# SMA-style rather than Wilder, add an entry here.
RSI_VARIANT_OVERRIDES: dict[tuple[str, str], str] = {
    ("strategies.srmr_plus", "_calculate_rsi"): "sma",
    ("strategies.session_range_mean_reversion", "_calculate_rsi"): "sma",
    ("strategies.momentum", "_calculate_rsi"): "sma",
    ("strategies.killzone_momentum", "_calculate_rsi"): "sma",
}


@dataclass
class CacheSummary:
    """What ``install_indicator_cache`` actually replaced.

    Surfaced to the harness for diagnostics + the workboard proof
    packet so we can show reviewers exactly which functions got
    cached for a given strategy.
    """

    strategy_id: str
    module_name: str
    patched: list[tuple[str, int]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class _CacheRestorer:
    """Context manager that reverts any module-level attribute swap."""

    def __init__(self, restore_map: dict[tuple[str, str], tuple[Any, Any]]):
        self._restore_map = restore_map

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        for (module_name, attr_name), (orig, _) in self._restore_map.items():
            module = importlib.import_module(module_name)
            setattr(module, attr_name, orig)
        return False


def install_indicator_cache(
    strategy: Any,
    strategy_id: str,
    *,
    atr_periods: set[int] | None = None,
    rsi_periods: set[int] | None = None,
    rsi_sma_periods: set[int] | None = None,
    adx_periods: set[int] | None = None,
    ema_periods: set[int] | None = None,
    sma_periods: set[int] | None = None,
    std_periods: set[int] | None = None,
    bb_periods: set[tuple[int, float]] | None = None,
) -> tuple[IncrementalIndicatorCache, _CacheRestorer, CacheSummary]:
    """Monkey-patch the strategy module's indicator functions.

    Returns ``(cache, restorer, summary)``.  ``cache`` is the
    per-strategy cache the harness should advance each bar.
    ``restorer`` is a context manager the harness SHOULD enter so
    strategy modules are restored to their original (byte-identical)
    function attributes when the strategy loop ends — strategy
    modules stay byte-identical ONLY across the harness.run() call,
    not across concurrent runs in the same process.

    Card b1bb93e8 constraint: strategy SOURCES stay byte-identical.
    The monkey-patch touches module attribute bindings in memory
    only; the on-disk source files are NEVER modified.
    """
    cls = type(strategy)
    module_name = cls.__module__
    module = importlib.import_module(module_name)

    cache = IncrementalIndicatorCache()
    cache.register_periods(
        atr_periods=atr_periods,
        rsi_periods=rsi_periods,
        rsi_sma_periods=rsi_sma_periods,
        adx_periods=adx_periods,
        ema_periods=ema_periods,
        sma_periods=sma_periods,
        std_periods=std_periods,
    )

    restore_map: dict[tuple[str, str], tuple[Any, Any]] = {}
    summary = CacheSummary(strategy_id=strategy_id, module_name=module_name)

    for attr_name, family in KNOWN_INDICATOR_FUNCTIONS.items():
        if not hasattr(module, attr_name):
            continue
        orig = getattr(module, attr_name)
        if not callable(orig):
            continue

        if family == "atr":
            periods = atr_periods or set()
        elif family == "rsi":
            # Default = Wilder; SMA only if explicit override says so.
            override = RSI_VARIANT_OVERRIDES.get((module_name, attr_name), "wilder")
            if override == "sma":
                periods = rsi_sma_periods or set()
                wrapper_factory = make_rsi_sma_wrapper
            else:
                periods = rsi_periods or set()
                wrapper_factory = make_rsi_wrapper
        elif family == "adx":
            periods = adx_periods or set()
        elif family == "ema":
            periods = ema_periods or set()
        elif family == "sma":
            periods = sma_periods or set()
        elif family == "std":
            periods = std_periods or set()
        else:
            periods = set()

        if not periods:
            summary.skipped.append(f"{attr_name}:no_periods")
            continue

        wrapped = orig
        for period in periods:
            if family == "atr":
                new_fn = make_atr_wrapper(orig, cache, period)
            elif family == "rsi":
                new_fn = wrapper_factory(orig, cache, period)
            elif family == "adx":
                new_fn = make_adx_wrapper(orig, cache, period)
            elif family == "ema":
                new_fn = make_ema_wrapper(orig, cache, period)
            elif family == "sma":
                new_fn = make_sma_wrapper(orig, cache, period)
            elif family == "std":
                new_fn = make_std_wrapper(orig, cache, period)
            else:
                continue
            wrapped = new_fn
            summary.patched.append((attr_name, period))

        if (module_name, attr_name) not in restore_map:
            restore_map[(module_name, attr_name)] = (orig, wrapped)
            setattr(module, attr_name, wrapped)

    # Bollinger bands: separate handling because the signature carries
    # both ``period`` and ``std_dev`` — install once per (period, std_dev).
    for attr_name in ("_bollinger_bands",):
        if not hasattr(module, attr_name):
            continue
        if attr_name not in KNOWN_INDICATOR_FUNCTIONS:
            continue
        orig = getattr(module, attr_name)
        if not callable(orig):
            continue
        for (period, std_dev) in bb_periods or []:
            new_fn = make_bollinger_wrapper(orig, cache, period, std_dev)
            if (module_name, attr_name) not in restore_map:
                restore_map[(module_name, attr_name)] = (orig, new_fn)
                setattr(module, attr_name, new_fn)
            summary.patched.append((attr_name, period))

    restorer = _CacheRestorer(restore_map)
    return cache, restorer, summary
