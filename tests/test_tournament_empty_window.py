"""Scoped tests for the tournament harness fail-loud guard (card c4b86732, AC1).

These tests cover the TournamentNoSignals exception class added to
``src/tournament/harness.py`` (c4b86732 AC1: registered strategy that
processes 0 bars or emits 0 signals over the window must not exit silently).
Card 82d33f73 (2026-10-04) refined the contract: the harness now
**marks** the strategy as ``signals_skipped=True`` in ``run_meta`` and
**continues** to the next strategy (council binding #3: skip-and-continue
for per-cell no-signals heatmap; the loud-exit guard is now the CLI's
single source of truth — ``if not scorecard.rows: return 1``).

Run via::

    bash scripts/run_test_scope.sh tests/test_tournament_empty_window.py

NO full suite (HR5).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tournament import TournamentHarness, TournamentNoSignals
from tournament.harness import STRATEGY_CLASS_MAP, TournamentEmptyWindow


# ── Synthetic DuckDB fixtures (scoped to this test file) ──────────────────────


def _make_synthetic_duckdb(
    db_path: Path,
    *,
    symbol: str = "USDJPY",
    n_bars: int = 60,
) -> None:
    """Write a tiny synthetic DuckDB bars table covering ``n_bars`` H1 entries."""
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE bars ("
            "symbol VARCHAR, timeframe VARCHAR, timestamp_utc BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
            "volume BIGINT, spread_pips DOUBLE)"
        )
        base = int(dt.datetime(2024, 6, 3, tzinfo=dt.timezone.utc).timestamp())
        rows = []
        price = 150.0
        for i in range(n_bars):
            wave = (i % 12) - 6
            price = price + wave * 0.05
            o = price
            c = price + wave * 0.02
            h = max(o, c) + 0.08
            low = min(o, c) - 0.08
            rows.append((symbol, "H1", base + i * 3600, o, h, low, c, 1000, 1.2))
        con.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    finally:
        con.close()


@pytest.fixture()
def synthetic_duckdb_60(tmp_path: Path) -> Path:
    """60 H1 bars USDJPY — enough to clear 30-bar warm-up, low signal density."""
    db = tmp_path / "smoke60.duckdb"
    _make_synthetic_duckdb(db, n_bars=60)
    return db


@pytest.fixture()
def tiny_duckdb_5(tmp_path: Path) -> Path:
    """5 H1 bars USDJPY — too small to clear 30-bar warm-up; expect 0 bars processed."""
    db = tmp_path / "tiny5.duckdb"
    _make_synthetic_duckdb(db, n_bars=5)
    return db


# ── Tests: TournamentNoSignals contract ─────────────────────────────────────


class TestTournamentNoSignalsContract:
    """The new exception class must (a) extend TournamentEmptyWindow,
    (b) capture strategy/symbol/window metadata, (c) name all of those
    in its message."""

    def test_subclass_of_tournament_empty_window(self) -> None:
        assert issubclass(TournamentNoSignals, TournamentEmptyWindow)

    def test_message_names_strategy_symbol_and_window(self) -> None:
        exc = TournamentNoSignals(
            strategy_id="srmr_plus",
            symbol="USDJPY",
            timeframe="H1",
            start_date="2024-06-03",
            end_date="2024-06-09",
            bars_processed=120,
            signals_emitted=0,
        )
        msg = str(exc)
        assert "srmr_plus" in msg
        assert "USDJPY" in msg
        assert "2024-06-03" in msg
        assert "2024-06-09" in msg
        assert "signals=0" in msg
        assert "bars_processed=120" in msg

    def test_attributes_are_set(self) -> None:
        exc = TournamentNoSignals(
            strategy_id="x",
            symbol="EURUSD",
            timeframe="H1",
            start_date=None,
            end_date=None,
            bars_processed=0,
            signals_emitted=0,
        )
        assert exc.strategy_id == "x"
        assert exc.symbol == "EURUSD"
        assert exc.timeframe == "H1"
        assert exc.bars_processed == 0
        assert exc.signals_emitted == 0


# ── Tests: harness fail-loud integration ─────────────────────────────────────


class TestHarnessFailLoud:
    """The harness must skip-and-continue (NOT raise) when a registered
    strategy emits 0 signals over the window (card 82d33f73 council
    binding #3).  The CLI checks ``if not scorecard.rows: return 1``
    AFTER the scorecard is rendered (council binding #1, Kaito 2026-10-04)."""

    def test_zero_signals_marks_strategy_skipped_not_raise(
        self, synthetic_duckdb_60: Path
    ) -> None:
        """bb_rsi_reversion has a low-volatility filter that suppresses
        signals on the synthetic 60-bar USDJPY H1 walk → harness must
        NOT raise; it marks the strategy as signals_skipped=True in
        run_meta and the scorecard is empty."""
        harness = TournamentHarness(
            strategy_ids=["bb_rsi_reversion"],
            db_path=synthetic_duckdb_60,
            symbol="USDJPY",
            timeframe="H1",
            start_date="2024-06-03",
            end_date="2024-06-05",
        )
        scorecard = harness.run()  # must NOT raise
        assert len(scorecard.rows) == 0
        run_meta = scorecard.meta["run_meta"]
        assert "bb_rsi_reversion" in run_meta
        assert run_meta["bb_rsi_reversion"]["skipped"] is True
        assert run_meta["bb_rsi_reversion"]["reason"] in (
            "no_signals",
            "warmup_window_too_small",
        )
        assert "USDJPY" in run_meta["bb_rsi_reversion"]["error"]

    def test_window_too_small_returns_empty_scorecard(
        self, tiny_duckdb_5: Path
    ) -> None:
        """Window smaller than the 30-bar warm-up gate: 5 bars loaded
        from DuckDB, but no strategy produces signals after warm-up.
        The 8ndb33f73 skip-and-continue change converts this from a
        raise (TournamentNoSignals IS-A TournamentEmptyWindow) into a
        normal scorecard-empty return.  The CLI's
        ``if not scorecard.rows: return 1`` guard then fails loud.
        Document the new contract: harness returns empty scorecard,
        CLI fails loud."""
        harness = TournamentHarness(
            strategy_ids=["srmr_plus"],
            db_path=tiny_duckdb_5,
            symbol="USDJPY",
            timeframe="H1",
            start_date="2024-06-03",
            end_date="2024-06-03",
        )
        scorecard = harness.run()  # must NOT raise
        assert len(scorecard.rows) == 0
        run_meta = scorecard.meta["run_meta"]
        assert "srmr_plus" in run_meta
        assert run_meta["srmr_plus"]["skipped"] is True

    def test_cli_returns_nonzero_on_empty_scorecard(
        self, synthetic_duckdb_60: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end smoke: run_tournament.py on a zero-signal run
        returns non-zero exit (the new loud-exit guard enforces this
        for both host AND node surfaces)."""
        import importlib
        import sys

        # Ensure harness imports work from the CLI module
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        sys.path.insert(
            0, str(Path(__file__).resolve().parent.parent / "src" / "forex_bot")
        )

        # Invoke the CLI in-process so we can capture its return value
        # without spawning a subprocess (the worktree's pytest config
        # sets RLIMIT_AS limits that would SIGSEGV subprocess.duckdb).
        from scripts import run_tournament as cli  # type: ignore[import-not-found]

        argv = [
            "--strategies", "bb_rsi_reversion",
            "--symbol", "USDJPY",
            "--timeframe", "H1",
            "--window", "2024-06-03:2024-06-05",
            "--db-path", str(synthetic_duckdb_60),
            "--output", str(synthetic_duckdb_60.parent / "out.json"),
        ]
        exit_code = cli.main(argv)
        assert exit_code == 1, (
            f"CLI must exit 1 on silent-empty scorecard (Kaito parity guard); "
            f"got {exit_code}"
        )


# ── Tests: STRATEGY_CLASS_MAP coverage ───────────────────────────────────────


class TestStrategyClassMap:
    """The registration sweep must keep the previously-registered strategies
    reachable AND add the 15 runnable ones from card c4b86732."""

    def test_pre_existing_ids_present(self) -> None:
        assert "srmr_plus" in STRATEGY_CLASS_MAP
        assert "bb_rsi_reversion" in STRATEGY_CLASS_MAP

    def test_card_c4b86732_registered_ids_present(self) -> None:
        expected_registered = [
            "donchian_atr_trend_v2",
            "dual_tf_squeeze_pro",
            "killzone_momentum",
            "london_breakout_retest",
            "momentum_donchian",
            "momentum_atr_breakout",
            "momentum_ma_trend",
            "momentum_m15",
            "rsi_threshold",
            "session_range_mean_reversion",
            "session_range_mr_ict_filtered",
            "ttc_xauusd",
            "volatility_regime_breakout",
            "volatility_squeeze",
            "donchian_atr_trend_v1",
        ]
        for sid in expected_registered:
            assert sid in STRATEGY_CLASS_MAP, f"missing registration: {sid}"

    def test_dead_strategies_not_registered(self) -> None:
        """The 3 dead/stale strategies must NOT appear in STRATEGY_CLASS_MAP
        (they cannot be constructed with config=None)."""
        dead_ids = ["orb", "mtf_filtered_momentum", "session_breakout"]
        for sid in dead_ids:
            assert sid not in STRATEGY_CLASS_MAP, (
                f"dead strategy {sid} should NOT be registered (requires non-trivial config)"
            )
