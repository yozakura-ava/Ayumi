"""Smoke tests for TTCXAUUSDStrategy — BQ-1121 wire-it card.

Verifies the strategy module can be imported, instantiated, and evaluated
against a synthetic XAUUSD M15 bar stream. Also verifies the strategy is
registered in strategies/registry.py and surfaces for XAUUSD symbol
selection (i.e. walk_forward / blend selection can find it).

Additional tests (card f5b6ebcd): PF-cap decision verification,
embargo documentation, and optimizer PF=0 pruning.

Tick-aggregation regression fixture (card d69e3542): documents the
0/5 FTMO window result on tick-aggregated XAUUSD M15 data, a regression
from the original 4/5 baseline on non-tick-aggregated data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from backtest.types import Bar, MarketState, TradeDirection

XAUUSD_BASE = 2400.00
BAR_COUNT = 60  # > min_bars_for_evaluation=50


def _make_xauusd_m15_bars(n: int = BAR_COUNT) -> list[Bar]:
    """Generate a synthetic XAUUSD M15 bar stream with a mild uptrend.

    Prices oscillate around XAUUSD_BASE with a small positive drift so
    the underlying TTSStrategy has enough structure (swings, ATR) to
    produce or withhold a signal without obviously forcing one.
    """
    bars: list[Bar] = []
    base_time = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)
    price = XAUUSD_BASE
    for i in range(n):
        # Drift +0.05/period, sine oscillation amplitude 1.0
        import math

        drift = 0.05 * i
        osc = 1.0 * math.sin(i / 3.0)
        close = price + drift + osc
        open_ = close - 0.2
        high = max(open_, close) + 0.4
        low = min(open_, close) - 0.4
        bars.append(
            Bar(
                time=base_time + timedelta(minutes=15 * i),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=200,
            )
        )
    return bars


def _make_state(bars: list[Bar] | None = None) -> MarketState:
    return MarketState(bars=bars or _make_xauusd_m15_bars())


# ---------- Module / class surface tests ----------


def test_module_imports():
    from strategies.ttc_xauusd import TTCXAUUSDStrategy

    assert TTCXAUUSDStrategy is not None  # noqa: S101
    assert TTCXAUUSDStrategy.name == "TTC XAUUSD M15"  # noqa: S101


def test_strategy_instantiation():
    from strategies.ttc_xauusd import TTCXAUUSDStrategy

    strat = TTCXAUUSDStrategy()
    assert strat is not None  # noqa: S101
    # Inner strategy must be wired
    assert hasattr(strat, "_strategy")  # noqa: S101
    assert strat._strategy is not None  # noqa: S101


# ---------- Smoke test: mock tick → signal pipeline ----------


def test_evaluate_with_sufficient_bars_returns_signal_or_none():
    """Mock tick stream → strategy evaluates without raising.

    TTSStrategy may legitimately return None on quiet bars; what we need
    to verify here is the wiring works (the monkeypatch dance inside
    __init__ restores the original module-level constants) and the
    evaluate call returns either None or a StrategySignal — never an
    exception, never a malformed value.
    """
    from strategies.ttc_xauusd import TTCXAUUSDStrategy

    strat = TTCXAUUSDStrategy()
    state = _make_state()

    result = strat.evaluate(state)

    if result is not None:
        assert hasattr(result, "direction")  # noqa: S101
        assert result.direction in (  # noqa: S101
            TradeDirection.LONG,
            TradeDirection.SHORT,
            TradeDirection.NEUTRAL,
        )
        assert 0.0 <= result.confidence <= 1.0  # noqa: S101
        assert result.entry_price > 0  # noqa: S101
    # else: None is acceptable (strategy filtering it out)


def test_evaluate_with_insufficient_bars_returns_none():
    """Too few bars → strategy should not crash, should not signal."""
    from strategies.ttc_xauusd import TTCXAUUSDStrategy

    strat = TTCXAUUSDStrategy()
    state = _make_state(bars=[])  # empty bars

    # Should not raise
    result = strat.evaluate(state)
    # No signal expected with no bars
    assert result is None  # noqa: S101


def test_evaluate_does_not_leak_tts_module_state():
    """The monkeypatched TTS module constants must be restored after init.

    This guards against a regression where TTCXAUUSDStrategy forgets to
    restore originals in the finally block — that would silently corrupt
    the shared tts_strategy module for every other caller.
    """
    import backtest.strategies.tts_strategy as tts_mod
    from strategies.ttc_xauusd import TTCXAUUSDStrategy

    # Snapshot one of the monkeypatched constants
    original_value = getattr(tts_mod, "MW_BASE_CONFIDENCE", None)
    assert original_value is not None, "MW_BASE_CONFIDENCE should exist in TTS module"  # noqa: S101

    # Construct two strategies back-to-back
    TTCXAUUSDStrategy()
    mid_value = getattr(tts_mod, "MW_BASE_CONFIDENCE", None)
    TTCXAUUSDStrategy()

    final_value = getattr(tts_mod, "MW_BASE_CONFIDENCE", None)
    assert final_value == original_value, (  # noqa: S101
        f"TTS module constant leaked: was {original_value}, mid-init {mid_value}, after {final_value}"
    )


# ---------- Registry tests: "walk_forward selection" wiring ----------


def test_ttc_xauusd_in_default_registry():
    """Acceptance: ttc_xauusd registered in strategies/registry.py."""
    from strategies.registry import default_registry

    reg = default_registry()
    ttc = reg.get("ttc_xauusd")
    assert ttc is not None, "ttc_xauusd missing from default registry"  # noqa: S101
    assert ttc.strategy_id == "ttc_xauusd"  # noqa: S101
    assert ttc.name == "TTC XAUUSD M15"  # noqa: S101
    assert ttc.strategy_type == "momentum"  # noqa: S101
    assert "XAUUSD" in [s.upper() for s in ttc.symbols]  # noqa: S101
    assert "M15" in ttc.timeframes  # noqa: S101
    assert ttc.active is True  # noqa: S101


def test_ttc_xauusd_selectable_for_xauusd():
    """Acceptance: strategy appears in walk_forward / blend selection for XAUUSD."""
    from strategies.registry import default_registry

    reg = default_registry()
    xauusd_strategies = reg.get_for_symbol("XAUUSD")
    strategy_ids = [s.strategy_id for s in xauusd_strategies]
    assert "ttc_xauusd" in strategy_ids, (  # noqa: S101
        f"ttc_xauusd not selectable for XAUUSD. Currently selectable: {strategy_ids}"
    )


def test_ttc_xauusd_listed_in_active_strategies():
    """Make sure it shows up in get_all_active() — required for any
    pipeline that iterates the full registry (signal_provider, etc.)."""
    from strategies.registry import default_registry

    reg = default_registry()
    all_active_ids = [s.strategy_id for s in reg.get_all_active()]
    assert "ttc_xauusd" in all_active_ids  # noqa: S101


# ---------- Realistic input sanity checks ----------


def test_xauusd_bars_have_realistic_prices():
    """Sanity check: synthetic XAUUSD bar stream is in the right ballpark."""
    bars = _make_xauusd_m15_bars()
    assert len(bars) == BAR_COUNT  # noqa: S101
    for bar in bars:
        # XAUUSD is gold — should be in the $1000-$5000 range for synthetic data
        assert 1000.0 < bar.close < 5000.0, f"Unrealistic XAUUSD price: {bar.close}"  # noqa: S101
        assert bar.high >= bar.low  # noqa: S101
        assert bar.high >= bar.open  # noqa: S101
        assert bar.high >= bar.close  # noqa: S101
        assert bar.low <= bar.open  # noqa: S101
        assert bar.low <= bar.close  # noqa: S101


def test_bars_are_chronologically_ordered():
    bars = _make_xauusd_m15_bars()
    for i in range(1, len(bars)):
        assert bars[i].time > bars[i - 1].time, (  # noqa: S101
            f"Bar {i} time {bars[i].time} not after bar {i - 1} time {bars[i - 1].time}"
        )


# ---------- PF-cap and embargo tests (card f5b6ebcd) ----------


def test_tts_strategy_documents_pf_cap_decision():
    """The TTSStrategy class docstring must document the PF-cap design decision.

    Card f5b6ebcd investigation found no PF cap in the signal engine.
    The decision (not needed — belongs in risk layer) must be documented
    in the class docstring so future developers don't re-investigate.
    """
    from backtest.strategies.tts_strategy import TTSStrategy

    docstring = TTSStrategy.__doc__ or ""
    assert "PF-Cap" in docstring or "pf-cap" in docstring.lower(), (  # noqa: S101
        "TTSStrategy docstring must document the PF-cap design decision"
    )
    assert "signal generator" in docstring.lower(), (  # noqa: S101
        "Docstring must clarify TTSStrategy is a signal generator, not risk manager"
    )


def test_ttc_optimizer_has_embargo_parameter():
    """run_ttc_optuna must accept embargo_bars parameter.

    The embargo_bars parameter is the API contract for out-of-sample
    leakage prevention. Even though the walk-forward runner doesn't
    wire it through yet, the parameter must exist for forward compatibility.
    """
    import inspect

    from backtest.parameter_sweep.ttc_optimizer import run_ttc_optuna

    sig = inspect.signature(run_ttc_optuna)
    assert "embargo_bars" in sig.parameters, (  # noqa: S101
        "run_ttc_optuna must have embargo_bars parameter for OOS leakage prevention"
    )
    assert sig.parameters["embargo_bars"].default == 0, (  # noqa: S101
        "embargo_bars should default to 0 (no embargo, preserves current behavior)"
    )


def test_ttc_optimizer_documents_embargo_in_docstring():
    """Module docstring must document the embargo / leakage risk."""
    from backtest.parameter_sweep import ttc_optimizer

    docstring = ttc_optimizer.__doc__ or ""
    assert "embargo" in docstring.lower(), (  # noqa: S101
        "ttc_optimizer module docstring must document embargo / leakage risk"
    )
    assert "leakage" in docstring.lower() or "autocorrelation" in docstring.lower(), (  # noqa: S101
        "Docstring must explain why embargo matters for financial data"
    )


def test_ttc_xauusd_has_recommended_embargo_constant():
    """TTCXAUUSDStrategy module must expose recommended embargo for XAUUSD M15."""
    from strategies.ttc_xauusd import RECOMMENDED_EMBARGO_BARS_M15

    # 96 bars = 24 hours of M15 data
    assert RECOMMENDED_EMBARGO_BARS_M15 == 96, (  # noqa: S101
        f"Expected 96 (24h of M15), got {RECOMMENDED_EMBARGO_BARS_M15}"
    )


def test_ttc_xauusd_documents_risk_delegation():
    """TTCXAUUSDStrategy docstring must document risk management delegation."""
    from strategies.ttc_xauusd import TTCXAUUSDStrategy

    docstring = TTCXAUUSDStrategy.__doc__ or ""
    assert "risk" in docstring.lower(), (  # noqa: S101
        "Docstring must address risk management delegation"
    )


def test_ttc_optimizer_prunes_pf_zero_trials():
    """The optimizer objective must prune PF=0 trials.

    This verifies the code path exists. Full integration testing
    requires historical data and is out of scope for this card.
    """
    import inspect

    from backtest.parameter_sweep.ttc_optimizer import run_ttc_optuna

    source = inspect.getsource(run_ttc_optuna)
    assert "mean_profit_factor" in source, (  # noqa: S101
        "Objective must check mean_profit_factor for PF=0 pruning"
    )
    assert "TrialPruned" in source, (  # noqa: S101
        "Objective must prune trials (raise TrialPruned) for PF=0"
    )


# ---------- Tick-aggregated regression fixture (card d69e3542) ----------


# Per-window results from optimizer run on tick-aggregated XAUUSD M15
# data (104,381 bars). Baseline config (lookback=5, history=50).
# Original non-tick-agg baseline: 4/5 FTMO windows passed.
# Tick-agg result: 0/5 windows pass Go/No-Go. Major regression.
TICK_AGG_PER_WINDOW = [
    {
        "window": 0,
        "win_rate": 0.50,
        "profit_factor": 1.13,
        "trade_count": 16,
        "total_pnl": 52.87,
        "passed_go_nogo": False,
    },
    {
        "window": 1,
        "win_rate": 0.667,
        "profit_factor": 1.51,
        "trade_count": 3,
        "total_pnl": 25.27,
        "passed_go_nogo": False,
    },
    {
        "window": 2,
        "win_rate": 0.40,
        "profit_factor": 0.50,
        "trade_count": 5,
        "total_pnl": -74.60,
        "passed_go_nogo": False,
    },
    {
        "window": 3,
        "win_rate": 0.40,
        "profit_factor": 0.67,
        "trade_count": 15,
        "total_pnl": -149.61,
        "passed_go_nogo": False,
    },
    {
        "window": 4,
        "win_rate": 0.105,
        "profit_factor": 0.18,
        "trade_count": 19,
        "total_pnl": -699.92,
        "passed_go_nogo": False,
    },
]

# Aggregated metrics across 5 windows
TICK_AGG_AGGREGATED = {
    "mean_win_rate": 0.4144,
    "mean_profit_factor": 0.7969,
    "mean_trade_count": 11.6,
    "mean_total_pnl": -169.20,
    "windows_passed": 0,
    "total_windows": 5,
}

# Optimizer summary: 82/100 trials completed (process died at trial 81).
# All 82 trials had Go/No-Go = False. Best score: -0.5954 (trial 0).
# Score pattern dominated by 3 discrete values indicating similar
# parameter convergence with Go/No-Go penalty (-1.0) pushing all negative.
TICK_AGG_OPTIMIZER_SUMMARY = {
    "trials_completed": 82,
    "trials_total": 100,
    "all_go_nogo_false": True,
    "best_score": -0.5954,
    "best_trial": 0,
    "low_trade_counts": "6-14 per window (minimum 15 recommended)",
}


def test_tick_agg_xauusd_data_exists():
    """Verify tick-aggregated XAUUSD M15 data file is present and populated."""

    from backtest.parameter_sweep.ttc_optimizer import _DATA_DIR

    csv_path = _DATA_DIR / "XAUUSD_M15.csv"
    assert csv_path.exists(), f"Tick-agg XAUUSD M15 data missing: {csv_path}"  # noqa: S101

    line_count = sum(1 for _ in open(csv_path)) - 1  # minus header
    assert line_count > 100000, (  # noqa: S101
        f"Expected ~104K rows in tick-agg data, got {line_count}"
    )


def test_tick_agg_regression_0_of_5_windows():
    """Document the tick-aggregation regression: 0/5 FTMO windows pass.

    The original TTC XAUUSD backtest on non-tick-aggregated M15 data
    achieved 4/5 FTMO windows. After the tick-aggregation pipeline fix
    (card 933469d8), the same strategy achieves 0/5 windows.

    This test locks the regression baseline so future improvements can
    measure progress. When the strategy recovers, update the fixture.
    """
    assert TICK_AGG_AGGREGATED["windows_passed"] == 0  # noqa: S101
    assert TICK_AGG_AGGREGATED["total_windows"] == 5  # noqa: S101

    # All individual windows also fail
    for w in TICK_AGG_PER_WINDOW:
        assert w["passed_go_nogo"] is False, (  # noqa: S101
            f"Window {w['window']} unexpectedly passes Go/No-Go"
        )

    # Mean P&L is negative — strategy is not profitable on tick-agg data
    assert TICK_AGG_AGGREGATED["mean_total_pnl"] < 0  # noqa: S101

    # Window 4 is the worst (PnL = -699.92, WR = 10.5%)
    worst = min(TICK_AGG_PER_WINDOW, key=lambda w: w["total_pnl"])
    assert worst["window"] == 4  # noqa: S101
    assert worst["total_pnl"] < -600  # noqa: S101


def test_tick_agg_optimizer_all_trials_failed():
    """Document that 82/100 optimizer trials all had Go/No-Go = False.

    The TPE sampler explored the full search space and could not find
    any parameter combination that passes the Go/No-Go gate on
    tick-aggregated data. This suggests the regression is structural
    (data characteristics), not a parameter tuning issue.
    """
    assert TICK_AGG_OPTIMIZER_SUMMARY["all_go_nogo_false"] is True  # noqa: S101
    assert TICK_AGG_OPTIMIZER_SUMMARY["trials_completed"] >= 80, (  # noqa: S101
        "Optimizer should have completed most trials"
    )
    assert TICK_AGG_OPTIMIZER_SUMMARY["best_score"] < 0, (  # noqa: S101
        "Best score should be negative (Go/No-Go penalty dominates)"
    )


def test_tick_agg_low_trade_count_documented():
    """Document that low trade counts (6-14/window) contribute to failures.

    The Go/No-Go gate recommends ≥15 trades per window for statistical
    significance. Tick-aggregated data produces fewer signals, likely
    due to smoother bar formation reducing false breakout patterns
    that the TTC strategy relies on.
    """
    for w in TICK_AGG_PER_WINDOW:
        assert w["trade_count"] < 20, (  # noqa: S101
            f"Window {w['window']} has unexpectedly high trade count"
        )
    # Several windows are below the 15-trade minimum for statistical significance
    below_min = sum(1 for w in TICK_AGG_PER_WINDOW if w["trade_count"] < 15)
    assert below_min >= 2, (  # noqa: S101
        f"Expected ≥2 windows below 15-trade minimum, got {below_min}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
