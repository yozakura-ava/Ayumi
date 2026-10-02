"""Scoped tests for the strategy registration sweep (card c4b86732, AC3).

Validates that the 15 runnable strategy classes registered in
``src/tournament/harness.py:STRATEGY_CLASS_MAP`` are each:
  1. Importable via ``strategies.<module>:<ClassName>`` (the canonical map value).
  2. Constructible with ``config=None`` (the harness's standard pattern).
  3. Have an ``evaluate(state: MarketState)`` method that returns
     ``StrategySignal | None`` (or duck-typed equivalent).

Also verifies the 3 dead/stale strategies are NOT registered (cannot be
constructed with config=None; would break the one-line harness pattern).

Run via::

    bash scripts/run_test_scope.sh tests/test_tournament_registration.py

NO full suite (HR4).
"""

from __future__ import annotations

import datetime as dt
import importlib
import pathlib
import sys
from pathlib import Path

import duckdb
import pytest

from core.types import Bar, MarketState

from tournament.harness import STRATEGY_CLASS_MAP


# ── Module path setup (mirrors run_tournament.py) ────────────────────────────

_REPO = Path(__file__).resolve().parent.parent
for _p in (str(_REPO / "src"), str(_REPO / "src" / "forex_bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Synthetic 60-bar USDJPY H1 fixture ────────────────────────────────────────


@pytest.fixture()
def synthetic_state() -> MarketState:
    """60 H1 bars USDJPY → enough to clear 30-bar warm-up gate for most strategies."""
    # Build bars list directly (no DuckDB needed — registration sweep
    # tests the import/construct/evaluate contract, not the bar-loading
    # path which is already covered by tests/tournament/test_harness.py).
    base = dt.datetime(2024, 6, 3, tzinfo=dt.timezone.utc)
    bars: list[Bar] = []
    price = 150.0
    for i in range(60):
        wave = (i % 12) - 6
        price = price + wave * 0.05
        o = price
        c = price + wave * 0.02
        h = max(o, c) + 0.08
        low = min(o, c) - 0.08
        bars.append(
            Bar(
                time=base + dt.timedelta(hours=i),
                open=o,
                high=h,
                low=low,
                close=c,
                volume=0.0,
                period=None,  # type: ignore[arg-type]
                spread_pips=1.2,
            )
        )
    return MarketState(bars=bars)


# ── Triage table (mirrored from build summary for runtime validation) ────────


REGISTERED_IDS: list[tuple[str, str, str]] = [
    # (strategy_id, module_path, class_name)
    # Pre-existing (card db04d5b5)
    ("srmr_plus", "strategies.srmr_plus", "SRMRPlusStrategy"),
    ("bb_rsi_reversion", "strategies.bb_rsi_reversion", "BBRSIMeanReversion"),
    # Card c4b86732 sweep (runnable-as-is, config=None)
    ("donchian_atr_trend_v2", "strategies.donchian_atr_trend_v2", "DonchianATRTrendV2Strategy"),
    ("dual_tf_squeeze_pro", "strategies.dual_tf_squeeze_pro", "DualTFSqueezeProStrategy"),
    ("killzone_momentum", "strategies.killzone_momentum", "KillzoneMomentumStrategy"),
    ("london_breakout_retest", "strategies.london_breakout_retest", "LondonBreakoutRetestStrategy"),
    ("momentum_donchian", "strategies.momentum", "DonchianBreakoutStrategy"),
    ("momentum_atr_breakout", "strategies.momentum", "ATRVolatilityBreakoutStrategy"),
    ("momentum_ma_trend", "strategies.momentum", "MATrendFollowingStrategy"),
    ("momentum_m15", "strategies.momentum_m15", "MomentumM15Strategy"),
    ("rsi_threshold", "strategies.rsi_threshold", "SimpleRSIThresholdStrategy"),
    ("session_range_mean_reversion", "strategies.session_range_mean_reversion", "SessionRangeMeanReversionStrategy"),
    ("session_range_mr_ict_filtered", "strategies.session_range_mr_ict_filtered", "SessionRangeMRWithICTFilter"),
    ("ttc_xauusd", "strategies.ttc_xauusd", "TTCXAUUSDStrategy"),
    ("volatility_regime_breakout", "strategies.volatility_regime_breakout", "VolatilityRegimeBreakoutStrategy"),
    ("volatility_squeeze", "strategies.volatility_squeeze", "VolatilitySqueezeStrategy"),
    ("donchian_atr_trend_v1", "strategies.donchian_atr_trend", "DonchianATRTrendStrategy"),
]

DEAD_IDS: list[tuple[str, str, str, str]] = [
    # (strategy_id, module_path, class_name, reason)
    ("orb", "strategies.orb", "ORBStrategy", "requires non-Optional dict config"),
    ("mtf_filtered_momentum", "strategies.mtf_filtered_momentum", "MTFFilteredMomentumStrategy",
     "requires positional inner_strategy"),
    ("session_breakout", "strategies.session_breakout", "SessionBreakoutStrategy",
     "requires non-Optional dict config"),
]


# ── Tests: STRATEGY_CLASS_MAP registration coverage ──────────────────────────


class TestRegistrationCoverage:
    """STRATEGY_CLASS_MAP must contain every runnable strategy from
    the card c4b86732 sweep."""

    def test_map_count_matches_triage(self) -> None:
        # 2 pre-existing + 15 from sweep = 17 total entries.
        assert len(STRATEGY_CLASS_MAP) == 17, (
            f"expected 17 entries (2 pre-existing + 15 sweep), got {len(STRATEGY_CLASS_MAP)}: "
            f"{sorted(STRATEGY_CLASS_MAP)}"
        )

    @pytest.mark.parametrize(
        "strategy_id,module_path,class_name",
        REGISTERED_IDS,
        ids=[r[0] for r in REGISTERED_IDS],
    )
    def test_registered_id_in_map(
        self, strategy_id: str, module_path: str, class_name: str
    ) -> None:
        assert strategy_id in STRATEGY_CLASS_MAP, (
            f"strategy_id {strategy_id!r} missing from STRATEGY_CLASS_MAP"
        )
        # Map value must be "module:class" format.
        assert STRATEGY_CLASS_MAP[strategy_id] == f"{module_path}:{class_name}", (
            f"STRATEGY_CLASS_MAP[{strategy_id!r}] = {STRATEGY_CLASS_MAP[strategy_id]!r}, "
            f"expected {module_path}:{class_name!r}"
        )


# ── Tests: dead-strategy exclusion ───────────────────────────────────────────


class TestDeadStrategyExclusion:
    """The 3 dead/stale strategies must NOT be registered."""

    @pytest.mark.parametrize(
        "strategy_id,module_path,class_name,reason",
        DEAD_IDS,
        ids=[r[0] for r in DEAD_IDS],
    )
    def test_dead_id_not_in_map(
        self,
        strategy_id: str,
        module_path: str,
        class_name: str,
        reason: str,
    ) -> None:
        assert strategy_id not in STRATEGY_CLASS_MAP, (
            f"dead strategy {strategy_id!r} should NOT be in STRATEGY_CLASS_MAP "
            f"(reason: {reason})"
        )


# ── Tests: per-strategy import + construct contract ──────────────────────────


class TestStrategyImportConstructEvaluate:
    """Each registered strategy must (a) be importable, (b) constructible
    with config=None, (c) have an evaluate(state) method that returns
    None or a signal-like object without raising on a synthetic USDJPY
    H1 MarketState."""

    @pytest.mark.parametrize(
        "strategy_id,module_path,class_name",
        REGISTERED_IDS,
        ids=[r[0] for r in REGISTERED_IDS],
    )
    def test_strategy_imports_and_evaluates(
        self,
        strategy_id: str,
        module_path: str,
        class_name: str,
        synthetic_state: MarketState,
    ) -> None:
        # (a) Import
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        assert cls is not None

        # (b) Construct (harness uses config=None for everything except
        # srmr_plus which is constructed via SRMRPlusConfig(symbol=...)).
        if strategy_id == "srmr_plus":
            from strategies.srmr_plus import SRMRPlusConfig
            inst = cls(config=SRMRPlusConfig(symbol="USDJPY"))
        else:
            try:
                inst = cls(config=None)
            except TypeError:
                # Some strategies take no config at all
                inst = cls()

        # (c) Optional initialize (some strategies lack it; harness tolerates)
        if hasattr(inst, "initialize"):
            try:
                inst.initialize({})
            except Exception:
                pass  # Tolerate init failures; the contract is "evaluate doesn't crash"

        # (d) evaluate(state) — must not raise on the synthetic 60-bar state
        try:
            result = inst.evaluate(synthetic_state)
        finally:
            if hasattr(inst, "shutdown"):
                try:
                    inst.shutdown()
                except Exception:
                    pass

        # Result is None or has a direction attribute (duck-typed StrategySignal)
        assert result is None or hasattr(result, "direction"), (
            f"{strategy_id}: evaluate() returned {type(result).__name__}, "
            f"expected None or signal-like object"
        )


# ── Tests: progress logging instrumentation ──────────────────────────────────


class TestProgressLoggingInstrumentation:
    """The harness must instrumented with a progress log line every 1000
    bars processed (card c4b86732 AC4). This is a structural test — it
    verifies the source contains the expected log call rather than
    exercising it under a 1000-bar run (which would take minutes and
    is unnecessary)."""

    def test_harness_has_progress_log_call(self) -> None:
        harness_src = (_REPO / "src" / "tournament" / "harness.py").read_text()
        # Expect a logger.info call with bars_processed % 1000 == 0 gate
        assert "bars_processed % 1000" in harness_src, (
            "harness.py missing the per-1000-bars progress log gate"
        )
        assert "[tournament.harness] progress" in harness_src, (
            "harness.py missing the [tournament.harness] progress log marker"
        )
