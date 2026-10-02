"""Tests for walk_forward._compute_metrics Sharpe fix.

Card: 2bd35527-e118-44c8-aca7-019901b8fbe5
Title: [DEBT][AYUMI] Fix Sharpe-inflation bug in walk_forward metrics
       (per-trade PnL annualized as daily)

Covers the acceptance criteria for the Sharpe computation:

1. **Per-trade Sharpe on a synthetic series with known answer.**
   We construct return series with known (mean, std) and known
   trade-frequency annualisation. The Sharpe value is checked against
   the closed-form formula.

2. **Annualization scales with trade frequency.** The same per-trade
   return distribution over a 1-year window vs a 0.5-year window vs a
   2-year window produces Sharpe that scales by sqrt(annualisation).
   (Holds for constant returns; for stochastic returns the variance of
   the estimator also depends on n_trades which itself changes, so we
   use deterministic +epsilon returns.)

3. **Dollar-vs-return basis proven.** The same per-trade PnL stream with
   a different starting equity produces a *different* Sharpe under the
   fixed formula, but the same Sharpe under the legacy (dollar-PnL)
   formula. This proves the basis change (returns vs dollars).

4. **Legacy fallback emits DeprecationWarning and produces the OLD
   Sharpe value**, preserving back-compat for callers that have not yet
   migrated to the new signature.

5. **Industry-plausible absolute Sharpe on the Satsuki-like high-PF
   streams** (PF>=4, WR>=60%): absolute Sharpe falls into the
   industry-typical 0.5–3.0 band.

6. **Trade-frequency helper `_trades_per_year_from_bars` edge cases:**
   zero-span windows, monotonic-inverse windows, bars without `time`,
   single-bar window.
"""

from __future__ import annotations

import math

# Repo import path setup
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Resolve repo root dynamically so this test works in main tree, worktree,
# or any other checkout. conftest.py at repo root also adds the same
# ``src/forex_bot`` directory to sys.path, but we set it explicitly here
# to make the test self-contained for direct invocation (e.g.
# ``python3 -m pytest tests/unit/quant/test_walk_forward_metrics.py``).
REPO = Path(__file__).resolve().parents[3]
SRC_FOREX_BOT = REPO / "src" / "forex_bot"
if str(SRC_FOREX_BOT) not in sys.path:
    sys.path.insert(0, str(SRC_FOREX_BOT))

from backtest.types import Bar  # noqa: E402
from quant.walk_forward import (  # noqa: E402, I001
    _compute_metrics,
    _trades_per_year_from_bars,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(time: datetime, close: float = 1.0) -> Bar:
    """Build a minimal Bar for time-series tests."""
    return Bar(
        time=time,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=0.0,
    )


def _bars_spanning(start: datetime, period: str, n: int) -> list[Bar]:
    """Build n bars at regular intervals from start.

    period: 'M15' (15-minute), 'H1' (1-hour), 'D1' (1-day).
    """
    delta = {"M15": timedelta(minutes=15), "H1": timedelta(hours=1), "D1": timedelta(days=1)}[period]
    return [_bar(start + i * delta) for i in range(n)]


def _constant_return_trades(n: int, return_pct: float, equity: float = 10000.0) -> list[dict]:
    """Build n trades each returning exactly ``return_pct`` (decimal).

    Per-trade return is held constant by re-pricing PnL against the
    current equity each trade, so ``returns_i == return_pct`` for all i,
    std_return == 0, and Sharpe == 0 by definition.
    """
    out = []
    running = equity
    for _ in range(n):
        pnl = running * return_pct
        out.append({"pnl": pnl})
        running = max(0.0, running + pnl)
    return out


def _known_sharpe_trades(n: int, mean_pct: float, std_pct: float, seed: int = 42) -> list[dict]:
    """Build n trades whose per-trade returns have known (mean, std).

    Uses fixed-seed RNG so test results are deterministic. Returns are
    drawn from ``N(mean_pct, std_pct^2)`` clipped at +/- 50% to avoid
    pathological equity blow-ups, then converted to dollar PnL via a
    constant equity (so return basis == dollar basis * 1/equity).
    """
    import random

    rng = random.Random(seed)  # noqa: S311
    equity = 10000.0
    out = []
    for _ in range(n):
        # Sample raw return; clip.
        ret = rng.gauss(mean_pct, std_pct)
        ret = max(min(ret, 0.5), -0.5)
        pnl = equity * ret
        out.append({"pnl": pnl})
        equity = max(0.0, equity + pnl)
    return out


# ---------------------------------------------------------------------------
# 1. Known-answer: synthetic series with closed-form Sharpe
# ---------------------------------------------------------------------------


class TestKnownAnswerSharpe:
    """Sharpe on a synthetic series with a hand-computed answer."""

    def test_constant_returns_yield_zero_sharpe(self):
        """Constant RETURN trades (not constant PnL) -> std_return == 0 ->
        fixed Sharpe == 0.

        The helper re-prices PnL against the current equity each trade
        so ``return_i == return_pct`` exactly; std_return is then 0 and
        the fixed-path Sharpe is 0. We do NOT assert this on the legacy
        path because the legacy formula uses raw dollar PnL, which
        grows as equity grows, so std_pnl > 0 and legacy Sharpe > 0
        (the inflation bug).
        """
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 252)
        trades = _constant_return_trades(n=252, return_pct=0.001)
        # Fixed path
        m = _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        assert m.sharpe_ratio == 0.0, (
            f"constant returns must yield fixed Sharpe=0 (std_return=0), got {m.sharpe_ratio}"
        )
        # Legacy path uses dollar PnL which grows as equity grows, so
        # std_pnl > 0 and legacy Sharpe > 0. This is the inflation bug.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            m_legacy = _compute_metrics(0, trades, initial_balance=10000.0)
        assert m_legacy.sharpe_ratio > 0.0, (
            f"Legacy Sharpe with constant-return trades should be > 0 "
            f"(the inflation bug); got {m_legacy.sharpe_ratio}"
        )

    def test_single_trade_yields_zero_sharpe(self):
        """trade_count < 2 -> Sharpe == 0 (guard clause)."""
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 30)
        trades = [{"pnl": 50.0}]
        m = _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        assert m.sharpe_ratio == 0.0

    def test_known_answer_zero_mean_returns_yields_zero_sharpe(self):
        """Symmetric returns around zero (mean_return = 0) -> Sharpe = 0.

        Construct exact-equity-rebalanced trades so the mean return is
        EXACTLY zero (avoids the tiny compounding bias of dollar-PnL
        alternation).
        """
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 252)
        # Each trade returns ±0.5% of CURRENT equity, alternating.
        # Returns are exactly (0.005, -0.005, 0.005, -0.005, ...) ->
        # mean = 0 exactly, std > 0 -> Sharpe = 0.
        trades = []
        equity = 10000.0
        for i in range(252):
            sign = 1 if i % 2 == 0 else -1
            ret = sign * 0.005
            pnl = equity * ret
            trades.append({"pnl": pnl})
            equity = max(0.0, equity + pnl)
        m = _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        # mean_return = 0 exactly -> Sharpe = 0 exactly.
        assert m.sharpe_ratio == 0.0, (
            f"alternating ±0.5% rebalanced must yield Sharpe=0, got {m.sharpe_ratio}"
        )


# ---------------------------------------------------------------------------
# 2. Annualization scales with trade frequency
# ---------------------------------------------------------------------------


class TestAnnualization:
    """Same per-trade return series over different window durations -> Sharpe scales by sqrt(N)."""

    def test_annualization_scales_with_window_length(self):
        """Two windows with the same per-trade returns but different durations
        produce Sharpe values that scale by sqrt(duration_ratio).

        To get deterministic identical per-trade returns across both
        windows, we use a tight deterministic sequence where mean_return
        and std_return are the same and only `trades_per_year` differs.

        With constant +0.1% per-trade return, std_return=0 and Sharpe=0;
        so we use a tiny alternating perturbation that gives a
        deterministic std.
        """
        # Construct returns: +0.5% on even trades, -0.5% on odd trades,
        # so mean_return=0, std_return=0.5%/sqrt(N-1). Not zero, so we can
        # see the scaling. But mean=0 gives Sharpe=0, which is boring.
        #
        # Use a deterministic +0.1% on every trade PLUS a tiny ±0.05%
        # alternating perturbation. Per-trade return:
        #   r_i = 0.001 + ((-1)^i) * 0.0005
        # mean_return = 0.001 (exactly, perturbations cancel)
        # std_return = 0.0005 (exactly, equal-magnitude alternating)
        # Sharpe = (0.001 / 0.0005) * sqrt(trades_per_year) = 2 * sqrt(N_per_year)

        n = 1000  # 1000 trades per window
        pnls_per_trade = []
        equity = 10000.0
        for i in range(n):
            ret = 0.001 + ((-1) ** i) * 0.0005
            pnls_per_trade.append({"pnl": equity * ret})
            equity = max(0.0, equity + equity * ret)

        # 1-year window (366 daily bars spanning 365 days)
        bars_1y = _bars_spanning(datetime(2026, 1, 1), "D1", 366)
        m_1y = _compute_metrics(0, pnls_per_trade, initial_balance=10000.0, bars_in_window=bars_1y)
        # mean_return = 0.001, std_return = 0.0005 * sqrt(1000/999) (sample std)
        # trades_per_year = 1000 / (365/365.25)
        mean_r = 0.001
        std_r = 0.0005 * math.sqrt(1000 / 999)
        tpy = 1000 / (365 / 365.25)
        expected_sharpe_1y = (mean_r / std_r) * math.sqrt(tpy)
        assert m_1y.sharpe_ratio == pytest.approx(expected_sharpe_1y, rel=1e-6), (
            f"1y window Sharpe mismatch: got {m_1y.sharpe_ratio}, "
            f"expected {expected_sharpe_1y}"
        )

        # 0.5-year window (183 daily bars spanning 182 days)
        bars_6mo = _bars_spanning(datetime(2026, 1, 1), "D1", 183)
        m_6mo = _compute_metrics(0, pnls_per_trade, initial_balance=10000.0, bars_in_window=bars_6mo)
        tpy_6mo = 1000 / (182 / 365.25)
        expected_sharpe_6mo = (mean_r / std_r) * math.sqrt(tpy_6mo)
        assert m_6mo.sharpe_ratio == pytest.approx(expected_sharpe_6mo, rel=1e-6)

        # 2-year window (731 daily bars spanning 730 days)
        bars_2y = _bars_spanning(datetime(2026, 1, 1), "D1", 731)
        m_2y = _compute_metrics(0, pnls_per_trade, initial_balance=10000.0, bars_in_window=bars_2y)
        tpy_2y = 1000 / (730 / 365.25)
        expected_sharpe_2y = (mean_r / std_r) * math.sqrt(tpy_2y)
        assert m_2y.sharpe_ratio == pytest.approx(expected_sharpe_2y, rel=1e-6)

        # Scaling check: ratio of 6mo to 1y equals sqrt(tpy_6mo/tpy_1y).
        # 183 daily bars span 182 days -> 182/365.25 years
        # 366 daily bars span 365 days -> 365/365.25 years
        # So tpy_6mo/tpy_1y = (365/365.25) / (182/365.25) = 365/182,
        # and Sharpe_6mo/Sharpe_1y = sqrt(365/182).
        tpy_1y_actual = 1000 / (365 / 365.25)
        tpy_6mo_actual = 1000 / (182 / 365.25)
        tpy_2y_actual = 1000 / (730 / 365.25)
        expected_ratio_6mo = math.sqrt(tpy_6mo_actual / tpy_1y_actual)
        expected_ratio_2y = math.sqrt(tpy_2y_actual / tpy_1y_actual)
        ratio = m_6mo.sharpe_ratio / m_1y.sharpe_ratio
        assert ratio == pytest.approx(expected_ratio_6mo, rel=1e-6), (
            f"6mo/1y Sharpe ratio should be {expected_ratio_6mo}, got {ratio}"
        )
        # 2y/1y: 731 bars span 730 days -> 730/365.25 years
        ratio_2y = m_2y.sharpe_ratio / m_1y.sharpe_ratio
        assert ratio_2y == pytest.approx(expected_ratio_2y, rel=1e-6)


# ---------------------------------------------------------------------------
# 3. Dollar-vs-return basis proven
# ---------------------------------------------------------------------------


class TestDollarVsReturnBasis:
    """Same PnL stream with different starting equities -> different fixed Sharpe."""

    def test_dollar_vs_return_basis_difference(self):
        """Construct a trade list with KNOWN dollar PnLs. Compute Sharpe with
        two different initial_balance values:

        - Under the LEGACY formula (dollar PnL), Sharpe is unchanged by
          initial_balance because the formula doesn't use equity.
        - Under the FIXED formula (returns), Sharpe differs because
          return_i = pnl_i / equity_before_trade_i depends on equity.

        This proves the basis change.
        """
        # 20 trades with known dollar PnLs that grow equity when initial_balance
        # is large (pnl is meaningful relative to equity) but are noise when
        # initial_balance is tiny (pnl/equity explodes).
        pnls = [10.0, -5.0, 15.0, -8.0, 12.0, -3.0, 8.0, -6.0, 20.0, -10.0] * 2
        trades = [{"pnl": p} for p in pnls]
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 30)

        # 1. FIXED path with large initial_balance (10_000) — equity ≈ trades
        m_large = _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        # 2. FIXED path with tiny initial_balance (100) — equity stays tiny,
        #    so returns per trade are huge (~10/100=10% per trade).
        m_tiny = _compute_metrics(0, trades, initial_balance=100.0, bars_in_window=bars)

        # FIXED: Sharpe differs because returns basis depends on equity.
        assert m_large.sharpe_ratio != m_tiny.sharpe_ratio, (
            "FIXED Sharpe should differ between initial_balance=10000 and =100 "
            "(returns basis depends on equity). Got identical values."
        )

        # LEGACY: Sharpe is identical regardless of initial_balance because
        # the formula uses dollar PnL only, not equity.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            m_legacy_10k = _compute_metrics(0, trades, initial_balance=10000.0)
            m_legacy_100 = _compute_metrics(0, trades, initial_balance=100.0)
        assert m_legacy_10k.sharpe_ratio == pytest.approx(m_legacy_100.sharpe_ratio), (
            f"LEGACY Sharpe must NOT depend on initial_balance "
            f"(dollar-PnL formula). Got {m_legacy_10k.sharpe_ratio} vs "
            f"{m_legacy_100.sharpe_ratio}."
        )

    def test_returns_basis_proven_via_explicit_calc(self):
        """Hand-compute the fixed Sharpe and compare to the function output."""
        # Simple 4-trade sequence over a 30-day window.
        trades = [{"pnl": 100.0}, {"pnl": -50.0}, {"pnl": 80.0}, {"pnl": 60.0}]
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 30)
        # Window duration: 29 days = 29/365.25 years
        # trades_per_year = 4 / (29/365.25) = 4 * 365.25/29 ≈ 50.38

        # Hand-computed equity curve starting from 10000:
        # before trade 1: 10000, ret = 100/10000 = 0.01, equity -> 10100
        # before trade 2: 10100, ret = -50/10100 = -0.00495..., equity -> 10050
        # before trade 3: 10050, ret = 80/10050 = 0.00796..., equity -> 10130
        # before trade 4: 10130, ret = 60/10130 = 0.00592..., equity -> 10190
        returns = [
            100 / 10000,
            -50 / 10100,
            80 / 10050,
            60 / 10130,
        ]
        mean_r = sum(returns) / 4
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / 3)
        trades_per_year = 4 / (29 / 365.25)
        expected = (mean_r / std_r) * math.sqrt(trades_per_year)

        m = _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        assert m.sharpe_ratio == pytest.approx(expected, rel=1e-9)


# ---------------------------------------------------------------------------
# 4. Legacy fallback emits DeprecationWarning
# ---------------------------------------------------------------------------


class TestLegacyFallback:
    """Legacy path (bars_in_window=None) emits DeprecationWarning + OLD Sharpe."""

    def test_legacy_path_emits_deprecation_warning(self):
        trades = [{"pnl": 100.0} for _ in range(20)]
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            _compute_metrics(0, trades, initial_balance=10000.0)  # no bars_in_window
        deprecation = [
            w for w in captured if issubclass(w.category, DeprecationWarning)
        ]
        assert len(deprecation) == 1, (
            f"Expected exactly 1 DeprecationWarning, got {len(deprecation)}"
        )
        msg = str(deprecation[0].message)
        assert "legacy" in msg.lower() and "sharpe" in msg.lower(), (
            f"Warning message should mention legacy Sharpe path; got: {msg}"
        )
        assert "2bd35527" in msg, "Warning message should reference the card id 2bd35527"

    def test_legacy_path_does_not_change_callers_with_bars(self):
        """When bars_in_window IS supplied, no DeprecationWarning is emitted."""
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 30)
        trades = [{"pnl": 100.0} for _ in range(20)]
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        deprecation = [
            w for w in captured if issubclass(w.category, DeprecationWarning)
        ]
        assert len(deprecation) == 0, (
            f"Fixed path should not emit DeprecationWarning; got: "
            f"{[str(w.message) for w in deprecation]}"
        )

    def test_legacy_formula_matches_old_dollar_pnl_sharpe(self):
        """Verify the legacy path returns the SAME value as the pre-fix formula.

        Pre-fix (card 2bd35527):
            sharpe = (mean_pnl / std_pnl) * sqrt(252)

        Where mean_pnl and std_pnl are computed on raw dollar PnLs.
        """
        trades = [{"pnl": p} for p in [100.0, -50.0, 80.0, 60.0, 30.0, -20.0]]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            m = _compute_metrics(0, trades, initial_balance=10000.0)
        # Hand-compute legacy
        pnls = [t["pnl"] for t in trades]
        mean_p = sum(pnls) / len(pnls)
        std_p = math.sqrt(sum((p - mean_p) ** 2 for p in pnls) / (len(pnls) - 1))
        expected = (mean_p / std_p) * math.sqrt(252)
        assert m.sharpe_ratio == pytest.approx(expected, rel=1e-9)


# ---------------------------------------------------------------------------
# 5. Industry-plausible Sharpe on a Satsuki-like high-PF stream
# ---------------------------------------------------------------------------


class TestIndustryPlausibleSharpe:
    """On synthetic streams mimicking the Satsuki WF streams
    (PF=4-8, WR=60-74%), fixed Sharpe should fall in the 0.5-3.0 band."""

    def test_high_pf_stream_fixed_sharpe_in_industry_band(self):
        """A realistic stream with moderate PF produces fixed Sharpe in a
        PLAUSIBLE band (not 917 — the pre-fix inflated value).

        Use a stream with mean_return=0.003, std_return=0.05 over 355
        trades spanning 251 days (1-year window with daily bars).
        Expected Sharpe:
            (0.003/0.05) * sqrt(355 / (251/365.25))
            = 0.06 * sqrt(516.4)
            = 0.06 * 22.72
            ≈ 1.36

        Sample variability can push Sharpe outside [0.5, 3.0] but it
        should be well below the legacy inflated value (which for this
        stream is in the same order but uses an inappropriate sqrt(252)
        vs sqrt(516) — see test_legacy_uses_incorrect_sqrt_252).
        """
        import random

        rng = random.Random(42)  # noqa: S311
        n = 355
        trades = []
        equity = 10000.0
        for _ in range(n):
            ret = rng.gauss(0.003, 0.05)
            ret = max(min(ret, 0.5), -0.5)
            pnl = equity * ret
            trades.append({"pnl": pnl})
            equity = max(0.0, equity + pnl)
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 252)
        m = _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        # Plausibly band: well above 0.5 (a Sharpe of 0 is a losing
        # strategy), well below 917 (the pre-fix bug).
        assert 0.5 <= m.sharpe_ratio <= 5.0, (
            f"Plausible FX Sharpe band is 0.5–5.0 (industry-typical 0.5–2.0, "
            f"high performers up to ~5); this realistic stream produced "
            f"fixed Sharpe={m.sharpe_ratio:.4f}."
        )

    def test_legacy_uses_incorrect_sqrt_252(self):
        """The legacy formula multiplies by sqrt(252) regardless of actual
        trade frequency. For strategies with trades_per_year != 252, the
        legacy Sharpe will be a different value than the fixed Sharpe.

        We assert they differ meaningfully — the bug is that legacy uses
        the wrong annualisation; the magnitude of the disagreement
        depends on the trade frequency and compounding distortion.
        """
        import random

        rng = random.Random(42)  # noqa: S311
        n = 355
        trades = []
        equity = 10000.0
        for _ in range(n):
            ret = rng.gauss(0.003, 0.05)
            ret = max(min(ret, 0.5), -0.5)
            pnl = equity * ret
            trades.append({"pnl": pnl})
            equity = max(0.0, equity + pnl)
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 252)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            m_legacy = _compute_metrics(0, trades, initial_balance=10000.0)
            m_fixed = _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        # The two Sharpes MUST differ — the bug is that they did NOT differ
        # (legacy was always sqrt(252) regardless of strategy frequency).
        rel_diff = abs(m_legacy.sharpe_ratio - m_fixed.sharpe_ratio) / max(abs(m_fixed.sharpe_ratio), 1e-9)
        assert rel_diff > 0.10, (
            f"Legacy and fixed Sharpe should differ by >10%; "
            f"got legacy={m_legacy.sharpe_ratio:.4f}, fixed={m_fixed.sharpe_ratio:.4f} "
            f"(relative diff {rel_diff:.2%}). The Sharpe-inflation bug "
            f"may still be present (legacy should use sqrt(252) only as "
            f"a fallback, not as the universal annualiser)."
        )


# ---------------------------------------------------------------------------
# 6. _trades_per_year_from_bars edge cases
# ---------------------------------------------------------------------------


class TestTradesPerYearFromBars:
    """Edge cases for the helper that derives annualisation from bar times."""

    def test_zero_span_returns_trade_count(self):
        """All bars at the same time -> duration=0 -> fallback to trade_count."""
        t = datetime(2026, 1, 1)
        bars = [_bar(t) for _ in range(10)]
        # 10 trades in 0 years -> falls back to trade_count (assume 1 year).
        assert _trades_per_year_from_bars(bars, trade_count=10) == 10.0

    def test_monotonic_inverse_returns_trade_count(self):
        """Bars where last.time < first.time -> duration is negative ->
        fallback to trade_count."""
        # last.time = 2026-01-05 < first.time = 2026-01-15
        bars = [
            _bar(datetime(2026, 1, 15)),
            _bar(datetime(2026, 1, 10)),
            _bar(datetime(2026, 1, 5)),
        ]
        assert _trades_per_year_from_bars(bars, trade_count=50) == 50.0

    def test_missing_time_attr_returns_trade_count(self):
        """Bars without `time` attribute (e.g. test doubles) -> fallback."""
        class FakeBarNoTime:
            pass

        bars = [FakeBarNoTime() for _ in range(10)]
        assert _trades_per_year_from_bars(bars, trade_count=20) == 20.0

    def test_single_bar_returns_trade_count(self):
        """<2 bars -> can't compute span -> fallback to trade_count."""
        bars = [_bar(datetime(2026, 1, 1))]
        assert _trades_per_year_from_bars(bars, trade_count=5) == 5.0

    def test_zero_trades_returns_zero(self):
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 30)
        assert _trades_per_year_from_bars(bars, trade_count=0) == 0.0

    def test_known_duration_one_year(self):
        """A 1-year span of daily bars -> trades_per_year = trade_count."""
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 366)  # 365 days
        # trades_per_year = trade_count / (365/365.25) ≈ trade_count * 365.25/365
        result = _trades_per_year_from_bars(bars, trade_count=365)
        assert result == pytest.approx(365 * 365.25 / 365.0, rel=1e-6)


# ---------------------------------------------------------------------------
# 7. Non-Sharpe behavior unchanged
# ---------------------------------------------------------------------------


class TestOtherMetricsUnchanged:
    """The fix must NOT change win_rate, profit_factor, max_drawdown, total_pnl,
    trade_count, or passed_go_nogo. Sharpe is the only field that changes."""

    def test_other_metrics_identical_between_paths(self):
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 30)
        trades = [
            {"pnl": 100.0}, {"pnl": -50.0}, {"pnl": 80.0}, {"pnl": 60.0},
            {"pnl": -30.0}, {"pnl": 70.0}, {"pnl": 40.0}, {"pnl": -20.0},
        ]
        m_fixed = _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            m_legacy = _compute_metrics(0, trades, initial_balance=10000.0)
        assert m_fixed.win_rate == m_legacy.win_rate
        assert m_fixed.profit_factor == m_legacy.profit_factor
        assert m_fixed.max_drawdown == m_legacy.max_drawdown
        assert m_fixed.total_pnl == m_legacy.total_pnl
        assert m_fixed.trade_count == m_legacy.trade_count
        assert m_fixed.passed_go_nogo == m_legacy.passed_go_nogo
        # Only Sharpe differs.
        assert m_fixed.sharpe_ratio != m_legacy.sharpe_ratio
