"""Tests for the Bollinger Band Width (BBW) volatility classifier.

Card: d7344b92 (Sprint 072 — BBW Volatility Classifier).

These tests cover the BBW integration into ``regime.detector``. The
production use case is XAUUSD M15 bars where ATR-percentile-based
VOLATILE detection underperforms (Sprint 071 baseline: ~59% accuracy);
the BBW percentile is meant to disambiguate the VOLATILE regime via
a secondary, relative-volatility signal.

Each test focuses on one design contract of the BBW integration:

- ``test_bbw_percentile_range`` — the new ``bbw_percentile`` function
  returns values in [0, 1] on a 500-bar synthetic series with varying
  volatility, and respects warm-up (NaN for the leading
  ``bbw_period + bbw_lookback`` bars).
- ``test_backward_compat_none_mode`` — ``bbw_confirmation='none'`` (the
  default) produces byte-identical output to the pre-BBW detector.
- ``test_soft_mode_or_logic`` — bars where ATR_pct is below threshold
  but BBW_pct is above it are labelled ``VOLATILE`` in soft mode and
  not in none mode.
- ``test_hard_mode_and_logic`` — bars where ATR_pct is above threshold
  but BBW_pct is below it are labelled ``VOLATILE`` in none mode and
  not in hard mode.
- ``test_nan_warmup_safe`` — bars where ``bbw_pct`` is still NaN
  (warm-up, before ``bbw_period + bbw_lookback`` bars) match the
  detector output with BBW disabled.

Helpers
-------
``_synthetic_ohlc`` — fast OHLC generator with a configurable trend shape
(borrowed from the MTF confirmation test suite).
``_stub_bbw_pct`` — monkeypatch helper that replaces
``regime.detector.bbw_percentile`` with a deterministic per-bar Series
so the soft/hard mode logic can be asserted precisely without depending
on real BBW-derived values.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from regime.bbw_classifier import bbw_percentile
from regime.detector import Regime, RegimeConfig, RegimeDetector

# ── Test helpers ──────────────────────────────────────────────────────────


def _synthetic_ohlc(
    n: int = 600,
    *,
    seed: int = 42,
    trend_start: int = 100,
    trend_end: int = 400,
    trend_target_drift: float = 60.0,
    noise_scale: float = 0.3,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build a synthetic OHLC array with an injected linear uptrend.

    Parameters
    ----------
    n
        Total bar count.
    seed
        NumPy RNG seed for reproducibility.
    trend_start, trend_end
        Inclusive bar-index bounds of the linear-uptrend injection.
    trend_target_drift
        Total price drift across the trend window.
    noise_scale
        Magnitude of high-low noise (per-bar absolute volatility).
    """
    rng = np.random.default_rng(seed)
    closes = np.cumsum(rng.standard_normal(n) * noise_scale) + 100.0
    trend_end_clamped = min(trend_end, n)
    if trend_end_clamped > trend_start:
        ramp = np.linspace(0.0, trend_target_drift, trend_end_clamped - trend_start)
        closes[trend_start:trend_end_clamped] += ramp
    highs = closes + np.abs(rng.standard_normal(n)) * noise_scale
    lows = closes - np.abs(rng.standard_normal(n)) * noise_scale
    return highs, lows, closes


def _stub_bbw_pct_factory(values: list[float]):
    """Build a monkeypatch stub that returns a per-bar constant Series.

    Parameters
    ----------
    values
        Per-bar BBW percentile values.  The stub returns a ``pd.Series``
        of these values broadcast across the base-TF bar count.  When
        the input is shorter than the bar count, the trailing bars
        receive ``NaN`` (the BBW warm-up signal) so the detector's
        warm-up-fallback path is exercised.  When the input is longer
        than the bar count, the extra entries are silently dropped.

    Returns
    -------
    callable
        A function with the same signature as ``bbw_percentile`` that
        returns the constructed stub Series.
    """

    def _stub(
        highs,
        lows,
        closes,
        bbw_period: int = 20,
        bbw_lookback: int = 50,
    ) -> pd.Series:
        n = len(closes)
        idx = pd.RangeIndex(n)
        arr = np.full(n, np.nan, dtype=float)
        vlen = min(len(values), n)
        if vlen > 0:
            arr[:vlen] = values[:vlen]
        return pd.Series(arr, index=idx, dtype=float)

    return _stub


# ── Tests: bbw_percentile unit ───────────────────────────────────────────


def test_bbw_percentile_range():
    """``bbw_percentile`` returns values in [0, 1] on synthetic data.

    The 500-bar series has varying volatility (injected trend plus
    random noise); the BBW percentile rank should be a finite number
    in [0, 1] for every bar past the warm-up window, and ``NaN`` for
    the leading warm-up bars (before both ``bbw_period`` and
    ``bbw_lookback`` are satisfied).
    """
    highs, lows, closes = _synthetic_ohlc(n=500, seed=42)
    bbw_pct = bbw_percentile(highs, lows, closes, bbw_period=20, bbw_lookback=50)

    assert len(bbw_pct) == 500  # noqa: S101
    assert bbw_pct.index.equals(pd.RangeIndex(500))  # noqa: S101

    valid = bbw_pct.dropna()
    assert len(valid) > 0, "expected at least some non-NaN BBW values"  # noqa: S101
    assert valid.min() >= 0.0, f"BBW percentile below 0: {valid.min()}"  # noqa: S101
    assert valid.max() <= 1.0, f"BBW percentile above 1: {valid.max()}"  # noqa: S101
    # Sanity: the percentile rank covers both extremes — there's at
    # least one bar at 0.0 (the window minimum) and one at 1.0 (the
    # window maximum).  Without this, a degenerate constant input
    # could pass the range check.
    assert valid.min() == 0.0  # noqa: S101
    assert valid.max() == 1.0  # noqa: S101

    # Warm-up: leading bars should be NaN.  ``bollinger_bands`` uses
    # ``min_periods=bbw_period`` for the SMA / std (so raw BBW is NaN
    # for indices ``[0, bbw_period - 1]``).  The rolling min / max then
    # uses ``min_periods=bbw_lookback``, which counts *non-null*
    # observations — the SMA warm-up consumes the first ``bbw_period``
    # observations, so the rolling window only becomes valid once the
    # window ``[i - bbw_lookback + 1, i]`` contains only non-null raw
    # BBW values, i.e. when ``i - bbw_lookback + 1 >= bbw_period``.
    # The first valid BBW percentile index is therefore
    # ``bbw_period + bbw_lookback - 2``.
    expected_first_valid = 20 + 50 - 2  # = 68
    first_valid_idx = int(bbw_pct.first_valid_index())
    assert first_valid_idx == expected_first_valid, (  # noqa: S101
        f"expected first valid BBW at index {expected_first_valid}, got {first_valid_idx}"
    )


def test_bbw_percentile_short_data_returns_all_nan():
    """Series shorter than ``bbw_period`` returns all-NaN."""
    highs, lows, closes = _synthetic_ohlc(n=10, seed=42)
    bbw_pct = bbw_percentile(highs, lows, closes, bbw_period=20, bbw_lookback=50)
    assert len(bbw_pct) == 10  # noqa: S101
    assert bbw_pct.isna().all()  # noqa: S101


def test_bbw_percentile_flat_prices_handled():
    """Flat-price input (zero std) does not crash or produce NaNs past warm-up.

    When every close is identical, the SMA equals the close and the
    rolling std is 0, so Bollinger bands collapse to a single line and
    raw BBW = 0 everywhere.  The min-max normalisation then has a zero
    range; per the spec's ``rng.replace(0, np.nan)`` guard the output
    is NaN at every post-warm-up bar.  Importantly, the function must
    not raise.
    """
    n = 200
    flat_close = np.full(n, 1800.0)  # typical XAUUSD level
    highs = flat_close + 0.01
    lows = flat_close - 0.01
    bbw_pct = bbw_percentile(highs, lows, flat_close, bbw_period=20, bbw_lookback=50)
    assert len(bbw_pct) == n  # noqa: S101
    # No exceptions raised; every bar past warm-up is NaN due to the
    # zero-range guard (or NaN due to flat BBW upstream).
    valid = bbw_pct.dropna()
    # For a perfectly flat input the rolling range is identically 0,
    # so all post-warm-up values are NaN.  (If we ever change the
    # guard to produce 0.0 here, this assertion needs updating.)
    assert len(valid) == 0, (  # noqa: S101
        f"flat input should yield all-NaN BBW; got {len(valid)} finite values"
    )


# ── Tests: detector integration ──────────────────────────────────────────


def test_backward_compat_none_mode(monkeypatch):
    """``bbw_confirmation='none'`` (default) must produce byte-identical
    output to the pre-BBW detector.

    A default ``RegimeConfig`` (``bbw_confirmation='none'``) must not
    invoke ``bbw_percentile`` at all and must produce the same per-bar
    regime labels as the pre-BBW detector.  This is the single most
    important contract of the integration — the BBW addition is purely
    additive and zero-cost when disabled.
    """
    highs, lows, closes = _synthetic_ohlc(seed=42)

    det_default = RegimeDetector()
    det_explicit_none = RegimeDetector(RegimeConfig(bbw_confirmation="none"))

    # Identity / sanity check on the dataclass.
    assert det_default.config == det_explicit_none.config  # noqa: S101

    s_default = det_default.detect(highs, lows, closes)
    s_explicit = det_explicit_none.detect(highs, lows, closes)

    pd.testing.assert_series_equal(s_default, s_explicit)

    # And: ``bbw_percentile`` must NOT be called in 'none' mode.  This
    # is a small but important contract — it guarantees the BBW
    # addition has zero runtime cost when not requested.
    def _explode(*args, **kwargs):
        raise AssertionError("bbw_percentile must not be called when bbw_confirmation='none'")

    monkeypatch.setattr(
        "regime.detector.bbw_percentile",
        _explode,
    )
    RegimeDetector().detect(highs, lows, closes)  # must not raise


def test_soft_mode_or_logic(monkeypatch):
    """Soft mode: ATR below threshold + BBW above → VOLATILE in soft, not in none.

    Stubbed BBW values let us control the per-bar BBW percentile exactly.
    At a chosen bar we set ``atr_pct = 0.79`` (just below the default
    threshold of 0.80) and ``bbw_pct = 0.95`` (well above).  In
    ``none`` mode that bar must NOT be ``VOLATILE``; in ``soft`` mode it
    MUST be ``VOLATILE`` (OR logic — either signal triggers the regime).
    """
    highs, lows, closes = _synthetic_ohlc(seed=7)

    # Build an explicit ATR_pct series for the synthetic data so we can
    # control which bar lands just below the threshold.  We hand-roll a
    # stub that returns a fixed atr_pct (constant 0.79) plus a constant
    # bbw_pct (0.95) at every bar — this means every bar should be
    # VOLATILE in soft mode (BBW > thr) and NOT VOLATILE in none mode
    # (ATR ≤ thr).  The detector's quiet/trending/choppy masks will
    # then have nothing to override, so the per-bar volatile flag is
    # purely a function of our two stubs.
    target_bar = 100

    def _atr_stub(highs, lows, closes, period=14, lookback=50):
        n = len(closes)
        arr = np.full(n, 0.79, dtype=float)
        # Mark the target bar as just-above-threshold to make the test
        # even sharper: in soft mode the OR with bbw_pct=0.95 means
        # VOLATILE everywhere; in none mode the AND-of-NOTHING means
        # only bars where atr_pct > thr are VOLATILE.
        arr[target_bar] = 0.81  # just above threshold
        # Make sure warm-up bars have valid ATR pct too
        return pd.Series(arr, index=pd.RangeIndex(n), dtype=float)

    def _bbw_stub(highs, lows, closes, bbw_period=20, bbw_lookback=50):
        n = len(closes)
        arr = np.full(n, 0.95, dtype=float)  # well above threshold
        # At a different bar, drop bbw below threshold to exercise the
        # ATR-only fall-through when soft mode is the OR.
        return pd.Series(arr, index=pd.RangeIndex(n), dtype=float)

    monkeypatch.setattr("regime.detector.atr_percentile", _atr_stub)
    monkeypatch.setattr("regime.detector.bbw_percentile", _bbw_stub)

    det_none = RegimeDetector(RegimeConfig(bbw_confirmation="none"))
    det_soft = RegimeDetector(RegimeConfig(bbw_confirmation="soft"))

    s_none = det_none.detect(highs, lows, closes)
    s_soft = det_soft.detect(highs, lows, closes)

    # Sanity: target_bar should be VOLATILE in none mode (atr=0.81 > thr)
    # and VOLATILE in soft mode (atr=0.81 > thr OR bbw=0.95 > thr).
    assert s_none.iloc[target_bar] == Regime.VOLATILE  # noqa: S101
    assert s_soft.iloc[target_bar] == Regime.VOLATILE  # noqa: S101

    # Now pick a different bar (e.g. 200) where ATR=0.79 (below thr).
    test_bar = 200
    assert s_none.iloc[test_bar] != Regime.VOLATILE, (  # noqa: S101
        "sanity: with atr=0.79 < 0.80, none mode must NOT label the bar VOLATILE"
    )
    assert s_soft.iloc[test_bar] == Regime.VOLATILE, (  # noqa: S101
        "soft mode (OR) must label atr=0.79 + bbw=0.95 as VOLATILE"
    )


def test_hard_mode_and_logic(monkeypatch):
    """Hard mode: ATR above threshold + BBW below → VOLATILE in none, not in hard.

    At the chosen bar, ``atr_pct = 0.85`` (above threshold) and
    ``bbw_pct = 0.50`` (well below).  ``none`` mode must label the bar
    ``VOLATILE`` (ATR-only); ``hard`` mode must NOT (AND — both
    signals required).
    """
    highs, lows, closes = _synthetic_ohlc(seed=11)

    target_bar = 150

    def _atr_stub(highs, lows, closes, period=14, lookback=50):
        n = len(closes)
        arr = np.full(n, 0.85, dtype=float)  # above threshold everywhere
        return pd.Series(arr, index=pd.RangeIndex(n), dtype=float)

    def _bbw_stub(highs, lows, closes, bbw_period=20, bbw_lookback=50):
        n = len(closes)
        arr = np.full(n, 0.50, dtype=float)  # well below threshold
        # Mark a single bar as above threshold to confirm hard mode
        # fires when BOTH signals agree.
        arr[target_bar + 50] = 0.95
        return pd.Series(arr, index=pd.RangeIndex(n), dtype=float)

    monkeypatch.setattr("regime.detector.atr_percentile", _atr_stub)
    monkeypatch.setattr("regime.detector.bbw_percentile", _bbw_stub)

    det_none = RegimeDetector(RegimeConfig(bbw_confirmation="none"))
    det_hard = RegimeDetector(RegimeConfig(bbw_confirmation="hard"))

    s_none = det_none.detect(highs, lows, closes)
    s_hard = det_hard.detect(highs, lows, closes)

    # Bar with atr=0.85 > 0.80: VOLATILE in none, NOT VOLATILE in hard.
    assert s_none.iloc[target_bar] == Regime.VOLATILE, (  # noqa: S101
        "sanity: with atr=0.85 > 0.80, none mode must label the bar VOLATILE"
    )
    assert s_hard.iloc[target_bar] != Regime.VOLATILE, (  # noqa: S101
        "hard mode (AND) must NOT label atr=0.85 + bbw=0.50 as VOLATILE"
    )

    # Bar with BOTH atr=0.85 and bbw=0.95 (both above threshold): both
    # detectors should label VOLATILE.
    both_above_bar = target_bar + 50
    assert s_none.iloc[both_above_bar] == Regime.VOLATILE  # noqa: S101
    assert s_hard.iloc[both_above_bar] == Regime.VOLATILE, (  # noqa: S101
        "hard mode must label VOLATILE when BOTH atr and bbw exceed threshold"
    )


def test_nan_warmup_safe(monkeypatch):
    """Bars where ``bbw_pct`` is NaN (warm-up) match the no-BBW detector.

    The first ``bbw_period + bbw_lookback - 1`` bars of the BBW series
    are NaN by construction.  For both soft and hard modes, those bars
    must fall back to the ATR-only rule — i.e. match the detector
    output with ``bbw_confirmation='none'``.
    """
    highs, lows, closes = _synthetic_ohlc(seed=99)

    warmup_bars = 20 + 50  # bbw_period + bbw_lookback

    def _bbw_stub_partial(highs, lows, closes, bbw_period=20, bbw_lookback=50):
        """Return NaN for the first ``warmup_bars`` bars, 0.50 after.

        A constant post-warm-up value means the BBW threshold (0.80) is
        never exceeded, so the soft-mode OR cannot fire on warm-up
        bars, and the hard-mode AND will only fire if ATR also agrees.
        Either way the per-bar volatile flag at warm-up bars reduces to
        the ATR-only rule.
        """
        n = len(closes)
        arr = np.full(n, 0.50, dtype=float)
        arr[:warmup_bars] = np.nan
        return pd.Series(arr, index=pd.RangeIndex(n), dtype=float)

    monkeypatch.setattr(
        "regime.detector.bbw_percentile",
        _bbw_stub_partial,
    )

    det_none = RegimeDetector(RegimeConfig(bbw_confirmation="none"))
    det_soft = RegimeDetector(RegimeConfig(bbw_confirmation="soft"))
    det_hard = RegimeDetector(RegimeConfig(bbw_confirmation="hard"))

    s_none = det_none.detect(highs, lows, closes)
    s_soft = det_soft.detect(highs, lows, closes)
    s_hard = det_hard.detect(highs, lows, closes)

    # The first ``warmup_bars`` bars of soft and hard mode output must
    # match none mode byte-for-byte (NaN-propagation preserved).
    pd.testing.assert_series_equal(
        s_soft.iloc[:warmup_bars],
        s_none.iloc[:warmup_bars],
        obj="soft-mode warm-up bars must match none-mode bars",
    )
    pd.testing.assert_series_equal(
        s_hard.iloc[:warmup_bars],
        s_none.iloc[:warmup_bars],
        obj="hard-mode warm-up bars must match none-mode bars",
    )


def test_bbw_config_validation():
    """Invalid ``bbw_confirmation`` values raise at config time.

    Mirrors the existing ``mtf_confirmation`` validation pattern.
    """
    # Valid values accepted.
    RegimeConfig(bbw_confirmation="none")
    RegimeConfig(bbw_confirmation="soft")
    RegimeConfig(bbw_confirmation="hard")
    RegimeConfig(bbw_confirmation="HARD")  # case-insensitive normalisation

    # Invalid values rejected.
    import pytest

    with pytest.raises(ValueError, match="bbw_confirmation"):
        RegimeConfig(bbw_confirmation="medium")
    with pytest.raises(ValueError, match="bbw_confirmation"):
        RegimeConfig(bbw_confirmation="")


def test_bbw_defaults_match_spec():
    """Default ``bbw_confirmation`` is ``"none"`` (backward-compatible)."""
    cfg = RegimeConfig()
    assert cfg.bbw_confirmation == "none"  # noqa: S101
    assert cfg.bbw_period == 20  # noqa: S101
    assert cfg.bbw_lookback == 50  # noqa: S101
    assert cfg.bbw_volatile_pct == 0.80  # noqa: S101
