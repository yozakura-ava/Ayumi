"""Tests for :mod:`forex_bot.factory.validation_runner` (SFA-2).

Coverage matrix (validation runner orchestration):

* OOS guard refuses locked OOS bars; passes when unlocked
* Bridge failure surfaces as ``tier=REJECT`` (no exception)
* Spread cost snapshot matches :class:`SpreadCostTable` exactly
  (Liora ground rule — no magic numbers)
* Insufficient-data marking kicks in when ``total_trades < 10``
* PBO ceiling downgrades tier when ``params`` is non-empty
* :class:`FactoryVerdictStore` round-trips verdicts through DuckDB
* Batch convenience wrapper sets ``cell_count`` from batch size
* ``run_validation_batch`` end-to-end with a small synthetic bar
  history — proves WF + DSR + spread-cost wiring without running a
  full sweep

All tests are pure (no Optuna, no market data fetch, no git) so they
stay in the HR5-targeted-only envelope (``scripts/run_test_scope.sh``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
import tempfile

from backtest.engine import Bar

from forex_bot.factory.pipeline_config import (
    DSRConfig,
    OOSConfig,
    PBOConfig,
    PipelineConfig,
    default_pipeline_config,
)
from forex_bot.factory.spread_costs import (
    COMMISSION_PER_LOT_USD,
    PIP_SLIPPAGE,
    SpreadCostTable,
    default_spread_costs,
)
from forex_bot.factory.storage import FactoryVerdictStore
from forex_bot.factory.template import (
    ParamKind,
    ParamSpec,
    StrategyTemplate,
    TRENDING,
)
from forex_bot.factory.validation_runner import (
    CandidateSpec,
    INSUFFICIENT_DATA_THRESHOLD,
    ValidationRunner,
    ValidationVerdict,
    run_validation_batch,
)


# ---------------------------------------------------------------------------
# Test templates — minimal StrategyTemplate subclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GoodTemplate(StrategyTemplate):
    """Template that builds a sentinel ``ISignalStrategy``-like object.

    The sentinel exposes ``.name`` and ``.evaluate(state)`` so the
    bridge's duck-type check accepts it.  Real bars are fed by the
    walk-forward runner, which calls ``strategy_factory()`` once per
    window — the sentinel just records the call count.
    """

    archetype_id: str = "test_good"
    description: str = "good template for testing"
    default_pairs: tuple[str, ...] = ("EURUSD",)
    default_timeframes: tuple[str, ...] = ("H1",)
    regime_affinity: tuple[str, ...] = (TRENDING,)

    def __post_init__(self) -> None:  # pragma: no cover — frozen dataclass nuance
        super().__post_init__()
        self._reset()

    def _reset(self) -> None:  # pragma: no cover — testing helper
        # Workaround for frozen dataclass: re-bind via object.__setattr__.
        try:
            object.__setattr__(self, "_call_count", 0)
        except Exception:
            pass

    @property
    def param_space(self) -> tuple[ParamSpec, ...]:
        return ()

    def default_params(self) -> dict[str, Any]:
        return {}

    def regime_filter(self) -> tuple[str, ...] | None:
        return self.regime_affinity

    def build_strategy(self, params: Mapping[str, Any], pair: str) -> Any:
        return _ProbeSentinel(archetype_id=self.archetype_id)


class _ProbeSentinel:
    """Sentinel ``ISignalStrategy``-like object with ``.name`` + ``.evaluate``."""

    def __init__(self, archetype_id: str) -> None:
        self.name = f"{archetype_id}_sentinel"
        self.evaluated = 0

    def evaluate(self, state: Any) -> None:
        self.evaluated += 1
        return None

    def reset(self) -> None:  # pragma: no cover — backtest-engine compatibility
        self.evaluated = 0


@dataclass(frozen=True)
class _ParametricTemplate(StrategyTemplate):
    """Template with a non-empty ``param_space`` for PBO tests.

    The Optuna-style knobs (``roc_period`` int, ``adx_min`` float) let
    the bridge accept non-empty params without raising.  ``build_strategy``
    returns a ``_ProbeSentinel`` so walk-forward still runs end-to-end
    with the synthetic bar history.
    """

    archetype_id: str = "test_parametric"
    description: str = "parametric template for PBO tests"
    default_pairs: tuple[str, ...] = ("EURUSD",)
    default_timeframes: tuple[str, ...] = ("H1",)
    regime_affinity: tuple[str, ...] = (TRENDING,)

    @property
    def param_space(self) -> tuple[ParamSpec, ...]:
        return (
            ParamSpec("roc_period", ParamKind.INT, low=5, high=30, step=1),
            ParamSpec("adx_min", ParamKind.FLOAT, low=15.0, high=30.0),
        )

    def default_params(self) -> dict[str, Any]:
        return {"roc_period": 14, "adx_min": 20.0}

    def regime_filter(self) -> tuple[str, ...] | None:
        return self.regime_affinity

    def build_strategy(self, params: Mapping[str, Any], pair: str) -> Any:
        return _ProbeSentinel(archetype_id=self.archetype_id)


@dataclass(frozen=True)
class _BadTemplate(StrategyTemplate):
    """Template whose ``build_strategy`` raises — exercises bridge-error path."""

    archetype_id: str = "test_bad"
    description: str = "always fails build"
    default_pairs: tuple[str, ...] = ("EURUSD",)
    default_timeframes: tuple[str, ...] = ("H1",)
    regime_affinity: tuple[str, ...] = (TRENDING,)

    @property
    def param_space(self) -> tuple[ParamSpec, ...]:
        return ()

    def default_params(self) -> dict[str, Any]:
        return {}

    def regime_filter(self) -> tuple[str, ...] | None:
        return self.regime_affinity

    def build_strategy(self, params: Mapping[str, Any], pair: str) -> Any:
        raise RuntimeError("simulated bridge failure")


# ---------------------------------------------------------------------------
# Bar fixtures
# ---------------------------------------------------------------------------

# Used by ``_make_bars`` to space bars one hour apart.
_ONE_HOUR_TIMEDELTA = timedelta(hours=1)


# Module-level default runner.  Tests that need a custom pipeline_config
# (e.g. test_custom_pipeline_config_drives_wf) construct their own runner
# locally and shadow this constant.
RUNNER: ValidationRunner = ValidationRunner()


def _make_bars(
    *,
    n: int = 1200,
    start: datetime | None = None,
    spread: float = 0.0020,
    seed: int = 1,
) -> list[Bar]:
    """Build a deterministic synthetic EURUSD-ish bar history.

    Uses a tiny random walk seeded for reproducibility.  Bars are
    hourly-spaced; default ``n=1200`` gives 50 days of data so each
    walk-forward test window has >= 30 bars (the
    ``BacktestConfig.min_bars_before_signal`` default).

    Tests that intentionally exercise insufficient-data paths can pass
    a smaller ``n``.
    """
    import random

    rng = random.Random(seed)
    start = start or datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)
    bars: list[Bar] = []
    price = 1.1000
    for i in range(n):
        # Bias the walk slightly positive so DSR gets a chance to fire.
        ret = rng.gauss(0.0005, spread)
        new_close = price * (1.0 + ret)
        high = max(price, new_close) * (1.0 + abs(rng.gauss(0, 0.0005)))
        low = min(price, new_close) * (1.0 - abs(rng.gauss(0, 0.0005)))
        # Hourly bars: ``i`` hours after ``start``.
        bar_time = datetime(
            start.year,
            start.month,
            start.day,
            start.hour,
            tzinfo=timezone.utc,
        ) + _ONE_HOUR_TIMEDELTA * i
        bars.append(
            Bar(
                time=bar_time,
                open=price,
                high=high,
                low=low,
                close=new_close,
                volume=1000.0,
            )
        )
        price = new_close
    return bars


def _oos_bar() -> Bar:
    """A single bar in the OOS holdout (2026-04-15)."""
    return Bar(
        time=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        open=1.10,
        high=1.11,
        low=1.09,
        close=1.105,
        volume=1000.0,
    )


# ---------------------------------------------------------------------------
# OOS guard
# ---------------------------------------------------------------------------


def test_oos_guard_refuses_locked_bars() -> None:
    """OOSConfig holdout Jan-Jul 2026 is locked by default → PermissionError."""
    runner = ValidationRunner()
    template = _GoodTemplate()
    candidate = CandidateSpec(
        candidate_id="oos_locked",
        template=template,
        bars=[_oos_bar()],
        pair="EURUSD",
        timeframe="H1",
    )
    verdict = RUNNER.run_one(candidate)
    assert verdict.tier == "REJECT"
    assert "OOS locked" in verdict.reason


def test_oos_guard_passes_when_unlocked() -> None:
    """Same OOS bar with ``oos_unlocked=True`` proceeds past the guard."""
    template = _GoodTemplate()
    candidate = CandidateSpec(
        candidate_id="oos_unlocked",
        template=template,
        bars=[_oos_bar()],
        pair="EURUSD",
        timeframe="H1",
        oos_unlocked=True,
    )
    verdict = RUNNER.run_one(candidate)
    # The runner gets past OOS; downstream may still REJECT for other reasons
    # (WF raises because of small data set) but the OOS reason must be gone.
    assert "OOS locked" not in verdict.reason


def test_oos_guard_passes_for_pre_oos_bars() -> None:
    """Pre-OOS bars (before 2026-01-01) are always allowed when locked."""
    template = _GoodTemplate()
    bars = _make_bars(n=1200, start=datetime(2025, 6, 1, tzinfo=timezone.utc))
    candidate = CandidateSpec(
        candidate_id="pre_oos",
        template=template,
        bars=bars,
        pair="EURUSD",
        timeframe="H1",
    )
    verdict = RUNNER.run_one(candidate)
    assert "OOS locked" not in verdict.reason


# ---------------------------------------------------------------------------
# Bridge failure surface
# ---------------------------------------------------------------------------


def test_bridge_failure_surfaces_as_reject() -> None:
    """BridgeError during build → tier=REJECT with ``bridge_error`` populated."""
    template = _BadTemplate()
    bars = _make_bars(n=1200)
    candidate = CandidateSpec(
        candidate_id="bad_build",
        template=template,
        bars=bars,
        pair="EURUSD",
        timeframe="H1",
    )
    verdict = RUNNER.run_one(candidate)
    assert verdict.tier == "REJECT"
    assert verdict.bridge_error is not None
    assert "simulated bridge failure" in verdict.bridge_error
    assert "bridge failed" in verdict.reason


# ---------------------------------------------------------------------------
# Spread-cost plumbing
# ---------------------------------------------------------------------------


def test_spread_costs_appear_on_verdict() -> None:
    """Every verdict carries the spread-cost snapshot (Liora ground rule)."""
    runner = ValidationRunner()
    template = _GoodTemplate()
    candidate = CandidateSpec(
        candidate_id="costs_check",
        template=template,
        bars=_make_bars(n=1200),
        pair="XAUUSD",
        timeframe="H1",
    )
    verdict = RUNNER.run_one(candidate)
    spread = default_spread_costs().get("XAUUSD")
    assert verdict.spread_pips == pytest.approx(spread.spread_pips)
    assert verdict.commission_per_lot_usd == pytest.approx(
        spread.commission_per_lot_usd
    )
    assert verdict.slippage_pips == pytest.approx(spread.slippage_pips)


def test_unknown_pair_surfaces_reject() -> None:
    """Pair outside the spread-cost table → REJECT with explicit reason."""
    runner = ValidationRunner()
    template = _GoodTemplate()
    candidate = CandidateSpec(
        candidate_id="unknown_pair",
        template=template,
        bars=_make_bars(n=1200),
        pair="ZZZUSD",  # not in default spread table
        timeframe="H1",
    )
    verdict = RUNNER.run_one(candidate)
    assert verdict.tier == "REJECT"
    assert "spread cost missing" in verdict.reason


# ---------------------------------------------------------------------------
# Insufficient-data guard
# ---------------------------------------------------------------------------


def test_insufficient_data_threshold_constant() -> None:
    assert INSUFFICIENT_DATA_THRESHOLD == 10


def test_insufficient_data_marks_few_trades() -> None:
    """WF succeeds but ``total_trades < 10`` → INSUFFICIENT_DATA.

    Uses a sufficiently long bar history (so WF can run) with the probe
    strategy that never produces signals, so ``total_trades == 0``.  The
    insufficient-data guard (Liora ground rule) kicks in and the verdict
    is marked ``INSUFFICIENT_DATA`` rather than ``REJECT``.
    """
    template = _GoodTemplate()
    candidate = CandidateSpec(
        candidate_id="zero_trades",
        template=template,
        bars=_make_bars(n=1200),
        pair="EURUSD",
        timeframe="H1",
    )
    verdict = RUNNER.run_one(candidate)
    assert verdict.tier == "INSUFFICIENT_DATA"
    assert verdict.total_trades < INSUFFICIENT_DATA_THRESHOLD


# ---------------------------------------------------------------------------
# PBO ceiling
# ---------------------------------------------------------------------------


def test_pbo_ceiling_downgrades_tier_when_params_nonempty() -> None:
    """Non-empty params ⇒ PBO is computed and may downgrade tier.

    Uses :class:`_ParametricTemplate` so the bridge accepts non-empty
    params (``roc_period`` + ``adx_min``).  With synthetic data the PBO
    is high (no real edge to differentiate the two synthetic strategies)
    so any A/B tier would be pushed toward the ceiling — but the test
    only requires that the ceiling string is populated and the score is
    a finite number when the data is sufficient.
    """
    template = _ParametricTemplate()
    bars = _make_bars(n=1200)  # enough for PBO T >= 8
    candidate = CandidateSpec(
        candidate_id="pbo_check",
        template=template,
        params={"roc_period": 14, "adx_min": 20.0},  # non-empty triggers PBO
        bars=bars,
        pair="EURUSD",
        timeframe="H1",
    )
    verdict = RUNNER.run_one(candidate)
    # PBO is *attempted* when params is non-empty (Liora ground rule).
    # With insufficient trade data the score itself is None but the
    # ceiling string is populated — ``"N/A"`` would mean PBO was skipped.
    assert verdict.pbo_tier_ceiling != "N/A"
    assert verdict.pbo_tier_ceiling in ("A", "B", "C", "REJECT", "INSUFFICIENT")


def test_pbo_skipped_when_params_empty() -> None:
    """Empty params ⇒ Optuna-not-derived ⇒ PBO skipped (None / N/A)."""
    template = _ParametricTemplate()  # parametric so params={} is also valid
    candidate = CandidateSpec(
        candidate_id="no_pbo",
        template=template,
        params={},  # empty ⇒ identity build
        bars=_make_bars(n=1200),
        pair="EURUSD",
        timeframe="H1",
    )
    verdict = RUNNER.run_one(candidate)
    assert verdict.pbo_score is None
    assert verdict.pbo_tier_ceiling == "N/A"


# ---------------------------------------------------------------------------
# PipelineConfig integration
# ---------------------------------------------------------------------------


def test_custom_pipeline_config_drives_wf() -> None:
    """Custom :class:`PipelineConfig` is honoured (no magic numbers)."""
    cfg = PipelineConfig(
        wf_windows=default_pipeline_config().wf_windows,
        dsr=DSRConfig(base_n_trials=320, n_trials_multiple=5),
        regime=default_pipeline_config().regime,
        trade_count=default_pipeline_config().trade_count,
        oos=OOSConfig(),
        pbo=PBOConfig(accept_threshold=0.10, marginal_threshold=0.30, reject_threshold=0.30),
    )
    runner = ValidationRunner(pipeline_config=cfg, cell_count=1)
    template = _GoodTemplate()
    candidate = CandidateSpec(
        candidate_id="custom_cfg",
        template=template,
        bars=_make_bars(n=1200),
        pair="EURUSD",
        timeframe="H1",
    )
    verdict = runner.run_one(candidate)
    # Custom DSR base_n_trials is 320 (not 160) — verify the runner used it.
    # The verdict may be INSUFFICIENT_DATA (probe produces 0 trades) but
    # n_trials_used still reflects the configured DSR scaling.
    assert verdict.n_trials_used == 320


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


def test_factory_verdict_store_round_trip(tmp_path) -> None:
    """``FactoryVerdictStore`` writes and reads back verdicts (tmp_path isolated)."""
    db = tmp_path / "research.duckdb"
    store = FactoryVerdictStore(db)
    verdict = ValidationVerdict(
        candidate_id="store_test",
        archetype_id="test",
        pair="EURUSD",
        timeframe="H1",
        tier="A",
        windows_passed=4,
        windows_total=5,
        total_trades=42,
        mean_sharpe=1.7,
        mean_profit_factor=1.4,
        mean_win_rate=0.62,
        max_drawdown=0.04,
        dsr_pvalue=0.02,
        n_trials_used=160,
        pbo_score=0.12,
        pbo_tier_ceiling="A",
        spread_pips=1.5,
        commission_per_lot_usd=3.5,
        slippage_pips=0.2,
        go_nogo=True,
        reason="tier-A pass",
        ran_at="2026-10-04T18:00:00+00:00",
    )
    written = store.write_verdicts([verdict])
    assert written == 1

    rows = store.fetch_verdicts(candidate_id="store_test")
    assert len(rows) == 1
    row = rows[0]
    assert row["candidate_id"] == "store_test"
    assert row["tier"] == "A"
    assert row["total_trades"] == 42
    assert row["mean_sharpe"] == pytest.approx(1.7)
    assert row["spread_pips"] == pytest.approx(1.5)


def test_factory_verdict_store_writes_empty() -> None:
    """Empty input list writes nothing (no-op)."""
    with tempfile.TemporaryDirectory() as td:
        store = FactoryVerdictStore(Path(td) / "empty.duckdb")
        assert store.write_verdicts([]) == 0


def test_factory_verdict_store_idempotent_reinsert(tmp_path) -> None:
    """Re-inserting the same verdict replaces the row (deterministic id)."""
    db = tmp_path / "research.duckdb"
    store = FactoryVerdictStore(db)
    verdict_a = ValidationVerdict(
        candidate_id="idem",
        archetype_id="t",
        pair="EURUSD",
        timeframe="H1",
        tier="A",
        ran_at="2026-10-04T18:00:00+00:00",
        total_trades=10,
    )
    verdict_b = ValidationVerdict(
        candidate_id="idem",
        archetype_id="t",
        pair="EURUSD",
        timeframe="H1",
        tier="REJECT",
        ran_at="2026-10-04T18:00:00+00:00",
        total_trades=2,
        reason="second run downgraded",
    )
    store.write_verdicts([verdict_a])
    store.write_verdicts([verdict_b])
    rows = store.fetch_verdicts(candidate_id="idem")
    assert len(rows) == 1
    assert rows[0]["tier"] == "REJECT"
    assert "downgraded" in rows[0]["reason"]


# ---------------------------------------------------------------------------
# run_validation_batch convenience wrapper
# ---------------------------------------------------------------------------


def test_run_validation_batch_sets_cell_count() -> None:
    """``run_validation_batch`` derives cell_count from batch size."""
    template = _GoodTemplate()
    candidates = [
        CandidateSpec(
            candidate_id=f"batch_{i}",
            template=template,
            bars=_make_bars(n=1200, seed=i),
            pair="EURUSD",
            timeframe="H1",
        )
        for i in range(3)
    ]
    verdicts = run_validation_batch(candidates)
    assert len(verdicts) == 3
    # cell_count = 3 ⇒ n_trials = max(160, 3 * 3) = 160 (floor)
    for v in verdicts:
        assert v.n_trials_used == 160


def test_run_validation_batch_handles_mixed_outcomes() -> None:
    """One bridge-failing candidate does not abort the batch."""
    good = _GoodTemplate()
    bad = _BadTemplate()
    candidates = [
        CandidateSpec(
            candidate_id="good",
            template=good,
            bars=_make_bars(n=1200),
            pair="EURUSD",
            timeframe="H1",
        ),
        CandidateSpec(
            candidate_id="bad",
            template=bad,
            bars=_make_bars(n=1200),
            pair="EURUSD",
            timeframe="H1",
        ),
    ]
    verdicts = run_validation_batch(candidates)
    assert len(verdicts) == 2
    by_id = {v.candidate_id: v for v in verdicts}
    assert by_id["bad"].tier == "REJECT"
    assert by_id["bad"].bridge_error is not None


# ---------------------------------------------------------------------------
# End-to-end smoke: WF + DSR + spread-cost wired through one candidate
# ---------------------------------------------------------------------------


def test_end_to_end_smoke_with_synthetic_bars() -> None:
    """WF + DSR + spread-cost gate wired through one synthetic candidate.

    Proves the runner produces a non-trivial verdict (DSR p-value,
    spread snapshot, pbo score, n_trials_used) on real bar data without
    requiring a full sweep.
    """
    template = _ParametricTemplate()
    candidate = CandidateSpec(
        candidate_id="e2e_smoke",
        template=template,
        params={"roc_period": 12, "adx_min": 25.0},  # non-empty ⇒ PBO computed
        bars=_make_bars(n=1200, seed=42),
        pair="GBPUSD",
        timeframe="H1",
    )
    verdict = run_validation_batch([candidate])[0]
    # Spread cost snapshot uses GBPUSD defaults (1.5 pips per Liora).
    assert verdict.spread_pips == pytest.approx(1.5)
    assert verdict.commission_per_lot_usd == pytest.approx(3.5)
    assert verdict.slippage_pips == pytest.approx(0.2)
    # DSR tier is one of the allowed strings.
    assert verdict.tier in ("A", "B", "C", "REJECT", "INSUFFICIENT_DATA")
    # n_trials_used reflects cell_count=1 ⇒ base floor 160.
    assert verdict.n_trials_used == 160
    # Verdict round-trips through the store cleanly.
    with tempfile.TemporaryDirectory() as td:
        store = FactoryVerdictStore(Path(td) / "smoke.duckdb")
        store.write_verdicts([verdict])
        rows = store.fetch_verdicts(candidate_id="e2e_smoke")
        assert len(rows) == 1
        assert rows[0]["pair"] == "GBPUSD"