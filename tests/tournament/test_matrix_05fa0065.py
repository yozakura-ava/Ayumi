"""Tests for the card 05fa0065 matrix infrastructure.

Covers the four foundational pieces added for the 68-cell tournament:

  1. CostModel + FTMO_COST_DEFAULTS + _cost_fraction_for_trade (math)
  2. TOURNAMENT_MATRIX enumeration (17 × 2 × 2 = 68)
  3. _verdict_filter + _rank_rows (max_dd<10% AND trades>=20)
  4. generate_matrix_report end-to-end (matrix.json + matrix.md + verdict_memo.md)

Run from the worktree root::

    python3 -m pytest tests/tournament/test_matrix_05fa0065.py -q

All tests are self-contained — no live worker, no live DuckDB, no
network.  They cover the parts that MUST be correct before the matrix
can be trusted end-to-end (cost math, cell enumeration, verdict
filter, and report structure).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make ``src/`` and ``scripts/`` importable so the tests work whether
# they're invoked from the worktree root or from tests/tournament/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
for sp in (str(_REPO_ROOT / "src"), str(_REPO_ROOT / "scripts")):
    if sp not in sys.path:
        sys.path.insert(0, sp)

from offload.run_matrix_remote import (  # noqa: E402
    TOURNAMENT_MATRIX,
    TOURNAMENT_STRATEGIES,
    TOURNAMENT_SYMBOLS,
    TOURNAMENT_TIMEFRAMES,
    _filter_real_scorecards,
    _rank_rows,
    _run_one_cell,
    _verdict_filter,
    generate_matrix_report,
)

from tournament.harness import (  # noqa: E402
    STRATEGY_CLASS_MAP,
    CostModel,
    _cost_fraction_for_trade,
    cost_model_for,
)

# ── 1. CostModel math ────────────────────────────────────────────────────────


class TestCostModelMath:
    """Cost math tests (card 05fa0065 AC #2: FTMO-realistic costs as precondition)."""

    def test_xauusd_defaults_match_lbo_study(self) -> None:
        """XAUUSD: spread 2.5 pips, slippage 0.2 pips, commission $3.5/lot."""
        cm = cost_model_for("XAUUSD")
        assert cm is not None
        assert cm.spread_pips == 2.5
        assert cm.slippage_pips == 0.2
        assert cm.commission_per_lot == 3.5
        assert cm.pip_value_per_lot == 10.0  # standard lot
        assert cm.pip_size == 0.10  # XAUUSD: 1 pip = $0.10 price movement

    def test_gbpusd_defaults_match_lbo_study(self) -> None:
        """GBPUSD: spread 2.0 pips, slippage 0.2 pips, commission $3.5/lot."""
        cm = cost_model_for("GBPUSD")
        assert cm is not None
        assert cm.spread_pips == 2.0
        assert cm.slippage_pips == 0.2
        assert cm.commission_per_lot == 3.5
        assert cm.pip_value_per_lot == 10.0
        assert cm.pip_size == 0.0001  # GBPUSD: 1 pip = $0.0001

    def test_unknown_symbol_returns_none(self) -> None:
        """Symbols without a registered cost model default to None."""
        assert cost_model_for("USDJPY") is None
        assert cost_model_for("UNKNOWN") is None
        assert cost_model_for("") is None

    def test_cost_fraction_xauusd_20pip_sl(self) -> None:
        """XAUUSD, 20 pip SL, 1.0 equity, 0.5% risk → cost ≈ 0.000813.

        Math:
          qty_lots = (0.005 * 1.0) / (20 * 10) = 0.000025
          spread = 2.5 * 0.000025 * 10 = 0.000625
          slippage = 2 * 0.2 * 0.000025 * 10 = 0.0001
          commission = 3.5 * 0.000025 = 0.0000875
          total = 0.0008125
        """
        cm = cost_model_for("XAUUSD")
        assert cm is not None
        cf = _cost_fraction_for_trade(
            entry_price=2000.0,
            sl_price=1998.0,  # 20 pips SL
            cost_model=cm,
            r_fraction=0.005,
            equity=1.0,
        )
        assert cf == pytest.approx(0.0008125, abs=1e-7)

    def test_cost_fraction_gbpusd_30pip_sl(self) -> None:
        """GBPUSD, 30 pip SL, 1.0 equity, 0.5% risk → cost ≈ 0.000458."""
        cm = cost_model_for("GBPUSD")
        assert cm is not None
        cf = _cost_fraction_for_trade(
            entry_price=1.2500,
            sl_price=1.2470,  # 30 pips SL (pip_size=0.0001)
            cost_model=cm,
            r_fraction=0.005,
            equity=1.0,
        )
        # qty_lots = (0.005 * 1.0) / (30 * 10) = 0.00001667
        # spread = 2.0 * 0.00001667 * 10 = 0.000333
        # slippage = 2 * 0.2 * 0.00001667 * 10 = 0.0000667
        # commission = 3.5 * 0.00001667 = 0.0000583
        # total ≈ 0.000458
        assert cf == pytest.approx(0.000458, abs=1e-5)

    def test_cost_fraction_degenerate_sl_falls_back(self) -> None:
        """Zero SL distance falls back to 10-pip SL (defensive)."""
        cm = cost_model_for("XAUUSD")
        assert cm is not None
        cf = _cost_fraction_for_trade(
            entry_price=2000.0,
            sl_price=2000.0,  # zero SL distance
            cost_model=cm,
            r_fraction=0.005,
            equity=1.0,
        )
        # Should use the 10-pip fallback → same magnitude as a normal trade
        assert 0 < cf < 0.01, f"expected non-zero cost, got {cf}"

    def test_cost_fraction_invalid_pip_value_returns_zero(self) -> None:
        """Zero/negative pip_value → defensive zero (no cost applied)."""
        cm = CostModel(
            spread_pips=2.5,
            commission_per_lot=3.5,
            slippage_pips=0.2,
            pip_value_per_lot=0.0,  # degenerate
            pip_size=0.10,
        )
        cf = _cost_fraction_for_trade(
            entry_price=2000.0,
            sl_price=1998.0,
            cost_model=cm,
            r_fraction=0.005,
            equity=1.0,
        )
        assert cf == 0.0

    def test_cost_fraction_scales_with_equity(self) -> None:
        """Doubling equity doubles cost_per_trade but keeps cost_fraction constant.

        cost_fraction is dimensionless: cost_per_trade / equity. The
        helper normalizes correctly via the FTMO sizing formula.
        """
        cm = cost_model_for("XAUUSD")
        assert cm is not None
        cf_100k = _cost_fraction_for_trade(
            entry_price=2000.0,
            sl_price=1998.0,
            cost_model=cm,
            r_fraction=0.005,
            equity=100_000.0,
        )
        cf_50k = _cost_fraction_for_trade(
            entry_price=2000.0,
            sl_price=1998.0,
            cost_model=cm,
            r_fraction=0.005,
            equity=50_000.0,
        )
        # cost_fraction is invariant to equity (FTMO sizing scales qty).
        assert cf_100k == pytest.approx(cf_50k, abs=1e-9)


# ── 2. Tournament matrix enumeration ────────────────────────────────────────


class TestMatrixEnumeration:
    """Verify the 68-cell enumeration (17 × 2 × 2)."""

    def test_tournament_strategies_count(self) -> None:
        """Exactly 17 strategies registered."""
        assert len(TOURNAMENT_STRATEGIES) == 17
        assert len(STRATEGY_CLASS_MAP) == 17

    def test_tournament_strategies_match_registry(self) -> None:
        """TOURNAMENT_STRATEGIES must mirror STRATEGY_CLASS_MAP keys."""
        assert set(TOURNAMENT_STRATEGIES) == set(STRATEGY_CLASS_MAP.keys())

    def test_tournament_symbols(self) -> None:
        """Exactly XAUUSD and GBPUSD (card 05fa0065 spec)."""
        assert TOURNAMENT_SYMBOLS == ["XAUUSD", "GBPUSD"]

    def test_tournament_timeframes(self) -> None:
        """Exactly M15 and H1 (card 05fa0065 spec)."""
        assert TOURNAMENT_TIMEFRAMES == ["M15", "H1"]

    def test_tournament_matrix_has_68_cells(self) -> None:
        """68 = 17 strategies × 2 symbols × 2 timeframes."""
        assert len(TOURNAMENT_MATRIX) == 68

    def test_tournament_matrix_unique_cells(self) -> None:
        """Every (strategy, symbol, timeframe) triple appears exactly once."""
        assert len(TOURNAMENT_MATRIX) == len(set(TOURNAMENT_MATRIX))

    def test_tournament_matrix_covers_all_combinations(self) -> None:
        """Cartesian product: every (strat, sym, tf) is present."""
        expected = {
            (s, sym, tf)
            for s in TOURNAMENT_STRATEGIES
            for sym in TOURNAMENT_SYMBOLS
            for tf in TOURNAMENT_TIMEFRAMES
        }
        assert set(TOURNAMENT_MATRIX) == expected

    def test_tournament_matrix_includes_damning_gbpusd_h1_cell(self) -> None:
        """Council finding 2026-09-15: GBPUSD H1 was the "damning cost-free"
        result. The matrix MUST include this cell so the FTMO-cost verdict
        can confirm or refute the cost-free finding."""
        assert ("london_breakout_retest", "GBPUSD", "H1") in TOURNAMENT_MATRIX
        # Spot-check at least 5 random cells that should be in the matrix.
        for cell in [
            ("srmr_plus", "XAUUSD", "M15"),
            ("bb_rsi_reversion", "GBPUSD", "H1"),
            ("ttc_xauusd", "XAUUSD", "M15"),
            ("killzone_momentum", "GBPUSD", "H1"),
            ("donchian_atr_trend_v1", "XAUUSD", "H1"),
        ]:
            assert cell in TOURNAMENT_MATRIX, f"missing cell: {cell}"


# ── 3. Verdict filter + ranking ──────────────────────────────────────────────


class TestVerdictFilter:
    """The card 05fa0065 AC #4 verdict: max_dd<10% AND trade_count>=20."""

    def test_filter_passes_max_dd_below_10_and_trades_above_20(self) -> None:
        rows = [
            {
                "cell_id": "ok",
                "strategy_id": "alpha",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "return_pct": 5.0,
                "max_dd_pct": 8.0,
                "trade_count": 25,
            },
        ]
        survivors = _verdict_filter(rows)
        assert len(survivors) == 1
        assert survivors[0]["strategy_id"] == "alpha"

    def test_filter_rejects_max_dd_at_or_above_10(self) -> None:
        rows = [
            {"cell_id": "high_dd", "strategy_id": "a", "symbol": "X", "timeframe": "M15",
             "return_pct": 12.0, "max_dd_pct": 10.0, "trade_count": 30},
            {"cell_id": "very_high_dd", "strategy_id": "b", "symbol": "X", "timeframe": "M15",
             "return_pct": 8.0, "max_dd_pct": 15.0, "trade_count": 30},
        ]
        survivors = _verdict_filter(rows)
        assert survivors == []

    def test_filter_rejects_few_trades(self) -> None:
        rows = [
            {"cell_id": "few", "strategy_id": "a", "symbol": "X", "timeframe": "M15",
             "return_pct": 5.0, "max_dd_pct": 5.0, "trade_count": 19},
            {"cell_id": "borderline", "strategy_id": "b", "symbol": "X", "timeframe": "M15",
             "return_pct": 5.0, "max_dd_pct": 5.0, "trade_count": 20},
        ]
        survivors = _verdict_filter(rows)
        assert [r["cell_id"] for r in survivors] == ["borderline"]

    def test_filter_allows_negative_return_if_dd_ok(self) -> None:
        """FTMO survivor rule is about drawdown, not return sign.

        A negative-return cell that never breaches 10% DD is still a
        survivor — the rule is the prop-firm's "did you survive?" gate,
        not "did you make money?".
        """
        rows = [
            {"cell_id": "neg", "strategy_id": "a", "symbol": "X", "timeframe": "M15",
             "return_pct": -2.0, "max_dd_pct": 5.0, "trade_count": 35},
        ]
        survivors = _verdict_filter(rows)
        assert len(survivors) == 1
        assert survivors[0]["cell_id"] == "neg"

    def test_ranking_deterministic(self) -> None:
        """Rank by return_pct DESC, trade_count DESC, strategy_id ASC."""
        rows = [
            {"strategy_id": "z", "return_pct": 5.0, "trade_count": 30, "cell_id": "z"},
            {"strategy_id": "a", "return_pct": 5.0, "trade_count": 30, "cell_id": "a"},
            {"strategy_id": "y", "return_pct": 10.0, "trade_count": 20, "cell_id": "y"},
            {"strategy_id": "b", "return_pct": 5.0, "trade_count": 50, "cell_id": "b"},
        ]
        ranked = _rank_rows(rows)
        # y (10%) > a/b/z (5%); a/b/z tiebreak by trade_count DESC then strategy_id ASC.
        assert [r["cell_id"] for r in ranked] == ["y", "b", "a", "z"]

    def test_ranking_handles_nan_returns(self) -> None:
        """NaN returns sort to the bottom (mirror of scorecard.rank_scorecard_rows)."""
        rows = [
            {"strategy_id": "good", "return_pct": 5.0, "trade_count": 20, "cell_id": "g"},
            {"strategy_id": "nan", "return_pct": float("nan"), "trade_count": 20, "cell_id": "n"},
            {"strategy_id": "mid", "return_pct": 2.0, "trade_count": 20, "cell_id": "m"},
        ]
        ranked = _rank_rows(rows)
        assert [r["cell_id"] for r in ranked] == ["g", "m", "n"]


class TestFilterRealScorecards:
    """v1_stub scorecards are NEVER ingested into the report."""

    def test_local_fallback_excluded(self) -> None:
        manifests = [
            {"cell_id": "a", "local_fallback": False, "strategy_id": "s1",
             "return_pct": 5.0, "max_dd_pct": 8.0, "trade_count": 25},
            {"cell_id": "b", "local_fallback": True, "strategy_id": "s1",
             "return_pct": 99.9, "max_dd_pct": 0.0, "trade_count": 99},
        ]
        real, stub = _filter_real_scorecards(manifests)
        assert [m["cell_id"] for m in real] == ["a"]
        assert [m["cell_id"] for m in stub] == ["b"]

    def test_v1_stub_marker_excluded(self) -> None:
        """Belt-and-suspenders: explicit v1_stub marker also excluded."""
        manifests = [
            {"cell_id": "a", "v1_stub": True, "strategy_id": "s1",
             "return_pct": 5.0, "max_dd_pct": 8.0, "trade_count": 25},
        ]
        real, stub = _filter_real_scorecards(manifests)
        assert real == []
        assert len(stub) == 1


# ── 4. End-to-end report generation ─────────────────────────────────────────


class TestMatrixReportEndToEnd:
    """generate_matrix_report writes 3 artifacts under report_root/."""

    @pytest.fixture()
    def fake_output_root(self, tmp_path: Path) -> Path:
        """Build 5 fake manifests under {tmp}/output_root/{cell_id}/manifest.json.

        Mix of survivors (2), fails-DD (1), fails-trades (1), and
        v1_stub (1) to exercise the report end-to-end.
        """
        output_root = tmp_path / "output_root"
        cells = [
            # 2 survivors
            {"cell_id": "alpha", "strategy_id": "alpha", "symbol": "XAUUSD",
             "timeframe": "M15", "return_pct": 8.0, "max_dd_pct": 6.0,
             "daily_dd_breaches": 0, "total_dd_breaches": 0, "trade_count": 30,
             "local_fallback": False, "git_sha": "abc123",
             "env_lock_hash": "envlock1", "compute_runtime": "cpu",
             "db_sha": "dbsha1"},
            {"cell_id": "epsilon", "strategy_id": "epsilon", "symbol": "GBPUSD",
             "timeframe": "H1", "return_pct": 6.5, "max_dd_pct": 7.5,
             "daily_dd_breaches": 0, "total_dd_breaches": 0, "trade_count": 40,
             "local_fallback": False, "git_sha": "abc123",
             "env_lock_hash": "envlock1", "compute_runtime": "cpu",
             "db_sha": "dbsha1"},
            # fails DD
            {"cell_id": "beta", "strategy_id": "beta", "symbol": "XAUUSD",
             "timeframe": "M15", "return_pct": 12.0, "max_dd_pct": 12.0,
             "daily_dd_breaches": 0, "total_dd_breaches": 1, "trade_count": 30,
             "local_fallback": False, "git_sha": "abc123",
             "env_lock_hash": "envlock1", "compute_runtime": "cpu",
             "db_sha": "dbsha1"},
            # fails trades
            {"cell_id": "delta", "strategy_id": "delta", "symbol": "XAUUSD",
             "timeframe": "M15", "return_pct": 5.0, "max_dd_pct": 5.0,
             "daily_dd_breaches": 0, "total_dd_breaches": 0, "trade_count": 15,
             "local_fallback": False, "git_sha": "abc123",
             "env_lock_hash": "envlock1", "compute_runtime": "cpu",
             "db_sha": "dbsha1"},
            # v1_stub
            {"cell_id": "stub", "strategy_id": "stub", "symbol": "XAUUSD",
             "timeframe": "M15", "return_pct": 99.9, "max_dd_pct": 0.0,
             "daily_dd_breaches": 0, "total_dd_breaches": 0, "trade_count": 99,
             "local_fallback": True, "git_sha": "abc123",
             "env_lock_hash": "envlock1", "compute_runtime": "cpu",
             "db_sha": "dbsha1"},
        ]
        for c in cells:
            d = output_root / str(c["cell_id"])
            d.mkdir(parents=True)
            (d / "manifest.json").write_text(json.dumps(c, indent=2, sort_keys=True))
        return output_root

    def test_report_writes_three_artifacts(
        self, fake_output_root: Path, tmp_path: Path,
    ) -> None:
        report_root = tmp_path / "report"
        generate_matrix_report(
            output_root=fake_output_root,
            report_root=report_root,
            run_id="matrix-2026-09-15",
            n_total=5,
            statuses=["dispatched"] * 5,
            git_sha="abc123",
            env_lock_hash_val="envlock1",
            data_db_sha="dbsha1",
        )
        assert (report_root / "matrix.json").is_file()
        assert (report_root / "matrix.md").is_file()
        assert (report_root / "verdict_memo.md").is_file()

    def test_report_filters_v1_stub(self, fake_output_root: Path, tmp_path: Path) -> None:
        report_root = tmp_path / "report"
        generate_matrix_report(
            output_root=fake_output_root,
            report_root=report_root,
            run_id="matrix-2026-09-15",
            n_total=5,
            statuses=["dispatched"] * 5,
            git_sha="abc123",
            env_lock_hash_val="envlock1",
            data_db_sha="dbsha1",
        )
        payload = json.loads((report_root / "matrix.json").read_text())
        # 4 real + 1 stub; v1_stub MUST be excluded
        assert payload["n_real_scorecards"] == 4
        assert payload["n_v1_stub_excluded"] == 1
        row_cell_ids = {r["cell_id"] for r in payload["rows"]}
        assert "stub" not in row_cell_ids, "v1_stub cell leaked into report"

    def test_report_verdict_lists_survivors(
        self, fake_output_root: Path, tmp_path: Path,
    ) -> None:
        report_root = tmp_path / "report"
        generate_matrix_report(
            output_root=fake_output_root,
            report_root=report_root,
            run_id="matrix-2026-09-15",
            n_total=5,
            statuses=["dispatched"] * 5,
            git_sha="abc123",
            env_lock_hash_val="envlock1",
            data_db_sha="dbsha1",
        )
        payload = json.loads((report_root / "matrix.json").read_text())
        survivor_ids = {s["strategy_id"] for s in payload["survivors"]}
        # alpha and epsilon pass; beta (dd>=10), delta (trades<20) fail.
        assert survivor_ids == {"alpha", "epsilon"}
        # And the memo file lists them plainly.
        memo = (report_root / "verdict_memo.md").read_text()
        assert "alpha" in memo
        assert "epsilon" in memo
        assert "No survivors" not in memo

    def test_report_no_survivors_is_plain(
        self, fake_output_root: Path, tmp_path: Path,
    ) -> None:
        """When no cells survive, the memo MUST say so plainly (card AC #4)."""
        # Build an output root where every cell fails the filter.
        output_root = tmp_path / "all_fail"
        for c in ["high_dd", "few_trades", "both_fail"]:
            d = output_root / c
            d.mkdir(parents=True)
            (d / "manifest.json").write_text(json.dumps({
                "cell_id": c, "strategy_id": c, "symbol": "XAUUSD", "timeframe": "M15",
                "return_pct": -5.0, "max_dd_pct": 15.0, "trade_count": 5,
                "local_fallback": False,
            }, indent=2, sort_keys=True))
        report_root = tmp_path / "report"
        generate_matrix_report(
            output_root=output_root,
            report_root=report_root,
            run_id="matrix-2026-09-15",
            n_total=3,
            statuses=["dispatched"] * 3,
            git_sha="abc123",
            env_lock_hash_val="envlock1",
            data_db_sha="dbsha1",
        )
        memo = (report_root / "verdict_memo.md").read_text()
        assert "**No survivors.**" in memo, "memo must say 'No survivors' plainly"
        payload = json.loads((report_root / "matrix.json").read_text())
        assert payload["survivors"] == []

    def test_report_gap_detection(
        self, fake_output_root: Path, tmp_path: Path,
    ) -> None:
        """When n_total > manifests found, the gap is recorded."""
        report_root = tmp_path / "report"
        generate_matrix_report(
            output_root=fake_output_root,
            report_root=report_root,
            run_id="matrix-2026-09-15",
            n_total=68,  # pretending 68-cell matrix
            statuses=["dispatched"] * 5,
            git_sha="abc123",
            env_lock_hash_val="envlock1",
            data_db_sha="dbsha1",
        )
        payload = json.loads((report_root / "matrix.json").read_text())
        # 68 requested but only 5 manifests → 63 missing
        assert payload["n_gap"] == 63
        assert payload["n_cells_requested"] == 68
        assert payload["n_manifests"] == 5


# ── 5. Card 475cfe82 — Offload runner push_bundle IsADirectoryError regression ──
#
# These tests pin the contract for the offload runner that the matrix
# report must trust end-to-end:
#
#   AC2 (regression): BundleTransport.push_bundle must NEVER receive a
#       directory path. The pre-fix code passed ``bundle_path`` (a
#       directory: scripts/offload/) to push_bundle for the node/stub
#       transports, which called _sha256_file() on a directory and
#       raised IsADirectoryError. The fix asserts at the ABC level and
#       always writes a per-cell descriptor file regardless of transport.
#
#   AC3 (loud abort): Tournament matrix runs with --transport=jobdir do
#       NOT silently degrade to v1_stub local-fallback. Explicit
#       transport + push failure = loud abort, never "complete".


class TestPushBundleRejectsDirectory:
    """AC2: push_bundle must never receive a directory path."""

    def test_stub_simulate_success_rejects_directory(self, tmp_path: Path) -> None:
        """Port8877StubTransport(simulate_success=True) raises on directory bundle_path.

        Pre-fix: the if/else branch in _run_one_cell selected
        ``push_path = bundle_path`` (the scripts/offload directory) for
        non-jobdir transports. push_bundle then called _sha256_file()
        on the directory and crashed with IsADirectoryError — masking
        the actual bug because the error was IsADirectoryError, not a
        clean BundleTransportError subclass.

        Post-fix: BundleTransport.push_bundle asserts that
        ``bundle_path.is_file()`` is True at the ABC entrypoint.
        """
        from offload.transport import BundleTransportError, Port8877StubTransport

        transport = Port8877StubTransport(simulate_success=True)
        with pytest.raises((AssertionError, BundleTransportError, TypeError, IsADirectoryError)):
            transport.push_bundle(
                run_id="test-run",
                bundle_path=tmp_path,  # ← a directory
                expected_sha256="0" * 64,
            )

    def test_stub_simulate_success_accepts_file(self, tmp_path: Path) -> None:
        """Belt-and-suspenders: when given a real file, push_bundle succeeds.

        Pins the post-fix happy path so the new assertion doesn't
        reject legitimate descriptor-file pushes.
        """
        from offload.transport import Port8877StubTransport, WorkerCell

        transport = Port8877StubTransport(simulate_success=True)
        descriptor = tmp_path / "cell.json"
        descriptor.write_text('{"cell_id": "test"}')
        cell = transport.push_bundle(
            run_id="test-run",
            bundle_path=descriptor,
            expected_sha256="abc",
        )
        assert isinstance(cell, WorkerCell)
        assert cell.sha256 == "abc"

    def test_node_transport_rejects_directory(self, tmp_path: Path) -> None:
        """OpenClawNodeBundleTransport raises on directory bundle_path.

        This was the actual bug path: --transport=node is the default,
        so a directory bundle_path would have crashed at
        _sha256_file() deep inside push_bundle, surfacing the wrong
        error type (IsADirectoryError) instead of a clean rejection.
        """
        from offload.transport import OpenClawNodeBundleTransport

        # Bypass __init__ (would try to use the openclaw CLI binary).
        transport = OpenClawNodeBundleTransport.__new__(OpenClawNodeBundleTransport)
        transport._node = "test-worker"
        transport._cli_path = "openclaw"
        with pytest.raises((AssertionError, IsADirectoryError, TypeError)):
            transport.push_bundle(
                run_id="test-run",
                bundle_path=tmp_path,
                expected_sha256="0" * 64,
            )

    def test_jobdir_transport_rejects_directory(self, tmp_path: Path) -> None:
        """JobDirBundleTransport raises on directory bundle_path."""
        from offload.jobdir_transport import JobDirBundleTransport

        transport = JobDirBundleTransport(node="test-worker")
        with pytest.raises((AssertionError, IsADirectoryError, TypeError)):
            transport.push_bundle(
                run_id="test-run",
                bundle_path=tmp_path,
                expected_sha256="0" * 64,
            )

    def test_require_bundle_file_helper_directly(self, tmp_path: Path) -> None:
        """Direct test of the module-level _require_bundle_file guard.

        Belt-and-suspenders: pin the guard helper's behavior in
        isolation so the contract is independent of the subclass
        push_bundle impls.
        """
        from offload.transport import _require_bundle_file

        # A directory raises IsADirectoryError
        with pytest.raises(IsADirectoryError):
            _require_bundle_file(tmp_path)
        # A file does NOT raise
        descriptor = tmp_path / "cell.json"
        descriptor.write_text('{"cell_id": "test"}')
        _require_bundle_file(descriptor)  # should not raise


class TestJobdirPushFailureDoesNotFallBack:
    """AC3: --transport=jobdir push failure must abort, never silently fall back."""

    def test_jobdir_push_error_raises_not_fall_back(self, tmp_path: Path) -> None:
        """When JobDirBundleTransport.push_bundle raises, _run_one_cell propagates.

        Pre-fix: ``except BundleTransportError: m.local_fallback = True``
        triggered for ANY transport including JobDirBundleTransport.
        Post-fix: the JobDirBundleTransport branch fails loud — appends
        a dispatch_skipped audit row and re-raises (no manifest
        written with local_fallback=True, no v1_stub scorecard).
        """
        from offload.jobdir_transport import JobDirBundleTransport
        from offload.transport import BundleTransportError, WorktreeUnreachableError

        class _FailingJobdirTransport(JobDirBundleTransport):
            """Test double: push_bundle always raises (worker unreachable)."""

            def push_bundle(self, run_id, bundle_path, expected_sha256):
                raise WorktreeUnreachableError(
                    f"simulated worker unreachable on {run_id}"
                )

        transport = _FailingJobdirTransport(node="test-worker")
        import argparse
        args = argparse.Namespace(
            run_id="test-run",
            data_db_path="/tmp/test.duckdb",  # noqa: S108 — test fixture, never written
            data_db_sha=None,
            cell_timeout_s=1.0,
            poll_interval_s=0.01,
            resume=False,
        )
        # Post-fix loud-abort: the exception propagates out of _run_one_cell
        with pytest.raises(BundleTransportError):
            _run_one_cell(
                strategy="srmr_plus",
                symbol="USDJPY",
                timeframe="H1",
                args=args,
                transport=transport,
                output_root=tmp_path,
                git_sha="abc123",
                env_lock_hash_val="envlock1",
                env_lock_files_names=[],
                bundle_path=tmp_path / "fake_bundle",
                bundle_sha="0" * 64,
                bundle_files=[],
            )
        # No manifest written (loud-abort path), no v1_stub scorecard.
        cell_dir = tmp_path / "srmr_plus__usdjpy__h1"
        assert not (cell_dir / "manifest.json").exists() or json.loads(
            (cell_dir / "manifest.json").read_text()
        ).get("local_fallback") is False, (
            "JobDirBundleTransport push failure must NOT produce a "
            "manifest with local_fallback=True (would silently pass "
            "v1_stub as 'complete')"
        )

    def test_jobdir_push_error_audits_to_dispatch_skipped(self, tmp_path: Path) -> None:
        """JobDirBundleTransport push failure must write an audit row to dispatch_skipped.jsonl.

        Post-fix: the loud-abort path writes a ``jobdir_push_failed``
        audit row to dispatch_skipped.jsonl so the operator can grep
        for the cause.
        """
        from offload.jobdir_transport import JobDirBundleTransport
        from offload.transport import BundleTransportError, WorktreeUnreachableError

        class _FailingJobdirTransport(JobDirBundleTransport):
            def push_bundle(self, run_id, bundle_path, expected_sha256):
                raise WorktreeUnreachableError(
                    "simulated: worker exec failed"
                )

        transport = _FailingJobdirTransport(node="test-worker")
        import argparse
        args = argparse.Namespace(
            run_id="test-run",
            data_db_path="/tmp/test.duckdb",  # noqa: S108 — test fixture, never written
            data_db_sha=None,
            cell_timeout_s=1.0,
            poll_interval_s=0.01,
            resume=False,
        )
        with pytest.raises(BundleTransportError):
            _run_one_cell(
                strategy="srmr_plus",
                symbol="USDJPY",
                timeframe="H1",
                args=args,
                transport=transport,
                output_root=tmp_path,
                git_sha="abc123",
                env_lock_hash_val="envlock1",
                env_lock_files_names=[],
                bundle_path=tmp_path / "fake_bundle",
                bundle_sha="0" * 64,
                bundle_files=[],
            )
        # dispatch_skipped.jsonl MUST have a jobdir_push_failed audit row
        skipped_log = tmp_path / "dispatch_skipped.jsonl"
        assert skipped_log.is_file(), (
            f"expected dispatch_skipped.jsonl audit log at {skipped_log}"
        )
        lines = [json.loads(ln) for ln in skipped_log.read_text().splitlines() if ln.strip()]
        reasons = {ln.get("reason") for ln in lines}
        assert "jobdir_push_failed" in reasons, (
            f"expected audit row with reason 'jobdir_push_failed', got {reasons!r}"
        )


class TestRunOneCellPassesFilePath:
    """AC1 (unit equivalent): _run_one_cell passes a FILE path to push_bundle."""

    def test_run_one_cell_passes_file_to_jobdir_transport(self, tmp_path: Path) -> None:
        """When using JobDirBundleTransport, push_bundle receives a file path.

        Pre-fix bug: push_path = bundle_path (the directory scripts/offload)
        when not using JobDirBundleTransport. Even after the if/else was
        correctly routing JobDirBundleTransport to the descriptor file, we
        also want to verify that the descriptor IS a file and not the
        bundle directory.
        """
        from offload.jobdir_transport import JobDirBundleTransport
        from offload.transport import WorkerCell

        captured: dict[str, Path] = {}

        class _CapturingJobdirTransport(JobDirBundleTransport):
            def push_bundle(self, run_id, bundle_path, expected_sha256):
                captured["bundle_path"] = bundle_path
                # Don't actually invoke the real transport — return a
                # synthetic cell so _run_one_cell proceeds to fetch_output
                # (which will return None and trigger the audit row).
                return WorkerCell(
                    path=f"/tmp/fake-incoming/{bundle_path.name}",  # noqa: S108 — test synthetic path
                    sha256=expected_sha256,
                )

            def fetch_output(self, run_id, cell_id):
                return None

        transport = _CapturingJobdirTransport(node="test-worker")
        import argparse
        args = argparse.Namespace(
            run_id="test-run",
            data_db_path="/tmp/test.duckdb",  # noqa: S108 — test fixture, never written
            data_db_sha=None,
            cell_timeout_s=0.5,
            poll_interval_s=0.01,
            resume=False,
        )
        try:
            _run_one_cell(
                strategy="srmr_plus",
                symbol="USDJPY",
                timeframe="H1",
                args=args,
                transport=transport,
                output_root=tmp_path,
                git_sha="abc123",
                env_lock_hash_val="envlock1",
                env_lock_files_names=[],
                bundle_path=tmp_path / "fake_bundle",
                bundle_sha="0" * 64,
                bundle_files=[],
            )
        except Exception as exc:  # noqa: S110 — test asserts push_bundle was called; downstream fetch_output raises are expected artifacts
            captured["_exception"] = exc  # fetch_output returns None → output_missing audit row → raise
        assert "bundle_path" in captured, "push_bundle was not called"
        captured_path = captured["bundle_path"]
        assert captured_path.is_file(), (
            f"push_bundle received a non-file path {captured_path!r} "
            f"(is_dir={captured_path.is_dir()}) — pre-fix bug returns"
        )
        assert captured_path.suffix == ".json", (
            f"expected JSON descriptor, got {captured_path!r}"
        )

    def test_run_one_cell_passes_file_to_node_transport(self, tmp_path: Path) -> None:
        """When using OpenClawNodeBundleTransport, push_bundle receives a file path.

        This is the regression test for the original IsADirectoryError
        bug. The runner must always write a descriptor file (not pass
        the bundle directory) for ANY transport.
        """
        from offload.transport import OpenClawNodeBundleTransport, WorkerCell

        captured: dict[str, Path] = {}

        class _CapturingNodeTransport(OpenClawNodeBundleTransport):
            def push_bundle(self, run_id, bundle_path, expected_sha256):
                captured["bundle_path"] = bundle_path
                # Bypass the real _sha256_file + _invoke call by returning
                # a synthetic WorkerCell. The captured path is the test
                # assertion target.
                return WorkerCell(
                    path=f"/tmp/fake/{bundle_path.name}",  # noqa: S108 — test synthetic path
                    sha256=expected_sha256,
                )

            def fetch_output(self, run_id, cell_id):
                return None

        # Don't init the real __init__ (would try to use openclaw cli).
        transport = _CapturingNodeTransport.__new__(_CapturingNodeTransport)
        transport._node = "test-worker"
        transport._cli_path = "openclaw"

        import argparse
        args = argparse.Namespace(
            run_id="test-run",
            data_db_path=None,
            data_db_sha=None,
            cell_timeout_s=0.5,
            poll_interval_s=0.01,
            resume=False,
        )
        try:
            _run_one_cell(
                strategy="srmr_plus",
                symbol="USDJPY",
                timeframe="H1",
                args=args,
                transport=transport,
                output_root=tmp_path,
                git_sha="abc123",
                env_lock_hash_val="envlock1",
                env_lock_files_names=[],
                bundle_path=tmp_path / "fake_bundle_dir",  # directory!
                bundle_sha="0" * 64,
                bundle_files=[],
            )
        except Exception as exc:  # noqa: S110 — test asserts push_bundle was called; downstream fetch_output raises are expected artifacts
            captured["_exception"] = exc
        assert "bundle_path" in captured, "push_bundle was not called"
        captured_path = captured["bundle_path"]
        assert not captured_path.is_dir(), (
            f"push_bundle received a directory {captured_path!r} — "
            f"pre-fix IsADirectoryError bug returns"
        )
        assert captured_path.is_file(), (
            f"push_bundle should receive a file descriptor, got {captured_path!r}"
        )

    def test_run_one_cell_passes_file_to_stub_transport(self, tmp_path: Path) -> None:
        """When using Port8877StubTransport(simulate_success=True), push_bundle receives a file."""
        from offload.transport import Port8877StubTransport, WorkerCell

        captured: dict[str, Path] = {}

        class _CapturingStubTransport(Port8877StubTransport):
            def push_bundle(self, run_id, bundle_path, expected_sha256):
                captured["bundle_path"] = bundle_path
                return WorkerCell(
                    path=f"/tmp/fake/{bundle_path.name}",  # noqa: S108 — test synthetic path
                    sha256=expected_sha256,
                )

        transport = _CapturingStubTransport(simulate_success=True)

        import argparse
        args = argparse.Namespace(
            run_id="test-run",
            data_db_path=None,
            data_db_sha=None,
            cell_timeout_s=0.5,
            poll_interval_s=0.01,
            resume=False,
        )
        try:
            _run_one_cell(
                strategy="srmr_plus",
                symbol="USDJPY",
                timeframe="H1",
                args=args,
                transport=transport,
                output_root=tmp_path,
                git_sha="abc123",
                env_lock_hash_val="envlock1",
                env_lock_files_names=[],
                bundle_path=tmp_path / "fake_bundle_dir",  # directory!
                bundle_sha="0" * 64,
                bundle_files=[],
            )
        except Exception as exc:  # noqa: S110 — test asserts push_bundle was called; downstream fetch_output raises are expected artifacts
            captured["_exception"] = exc
        assert "bundle_path" in captured, "push_bundle was not called"
        captured_path = captured["bundle_path"]
        assert not captured_path.is_dir(), (
            f"push_bundle received a directory {captured_path!r} — "
            f"pre-fix IsADirectoryError bug returns"
        )
        assert captured_path.is_file(), (
            f"push_bundle should receive a file descriptor, got {captured_path!r}"
        )
