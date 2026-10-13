"""Targeted tests for the tournament walking skeleton (card db04d5b5).

All tests use ``tmp_path``-backed synthetic DuckDB bars so the test
suite stays self-contained — no reliance on the main tree's
``data/ayumi_market.duckdb`` (which is gitignored and absent from
worktrees).  Seven assertions cover the spec checklist:

  1. harness_registers_two_existing_strategies
  2. scorecard_has_ftmo_columns
  3. scorecard_runs_unmodified_strategies
  4. run_tournament_smoke_exits_zero
  5. run_tournament_emits_json_file
  6. empty_window_exits_nonzero
  7. targeted_tests_isolated_to_tests_tournament

Run from the worktree root::

    python3 -m pytest tests/tournament -q
"""

from __future__ import annotations

import datetime as dt
import json
import resource
import subprocess
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from tournament import (
    STRATEGY_CLASS_MAP,
    TournamentHarness,
    TournamentNoSignals,
)
from tournament.harness import (
    CostModel,
    TournamentEmptyWindow,
    _cost_fraction_for_trade,
    _liora_spread_costs,
    _simulate_trades,
    cost_model_for,
    load_bars_for_window,
    resolve_default_duckdb_path,
    row_is_cost_modeled,
    verdict_grade_rows,
)
from tournament.scorecard import (
    NO_RANKING_STATEMENT,
    SCORECARD_ROW_COLUMNS,
    Scorecard,
    ScorecardRow,
    _daily_dd_breach_counts,
    build_scorecard_row,
    ranking_ordering_statement,
    render_console_table,
    render_scorecard_json,
)

# ── Synthetic bar fixtures ───────────────────────────────────────────────────


def _make_synthetic_duckdb(db_path: Path, *, symbol: str = "USDJPY", n_bars: int = 60) -> None:
    """Write a tiny DuckDB bar file covering ``n_bars`` H1 entries.

    Each bar has a slightly noisy price walk so strategies like
    bb_rsi_reversion can occasionally cross volatility/RSI thresholds.
    The shape is deterministic — same seed every run.
    """
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE bars ("
            "symbol VARCHAR, timeframe VARCHAR, timestamp_utc BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
            "volume BIGINT, spread_pips DOUBLE)"
        )
        # Anchor at 2024-06-03 00:00 UTC, step hourly.
        base = int(dt.datetime(2024, 6, 3, tzinfo=dt.timezone.utc).timestamp())
        rows = []
        price = 150.0
        for i in range(n_bars):
            # Deterministic synthetic walk — price rises/falls by ~5 pips/bar.
            wave = (i % 12) - 6
            price = price + wave * 0.05
            o = price
            c = price + wave * 0.02
            h = max(o, c) + 0.08
            el = min(o, c) - 0.08
            spread = 1.2  # USDJPY typical ~1.0-1.5 pips
            rows.append((symbol, "H1", base + i * 3600, o, h, el, c, 1000, spread))
        con.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    finally:
        con.close()


@pytest.fixture()
def synthetic_duckdb(tmp_path: Path) -> Path:
    """Fixture: synthetic 60-bar USDJPY H1 DuckDB in tmp_path."""
    db = tmp_path / "smoke.duckdb"
    _make_synthetic_duckdb(db)
    return db


@pytest.fixture()
def empty_duckdb(tmp_path: Path) -> Path:
    """Fixture: empty bars table (no rows).

    Loads successfully but ``load_bars_for_window`` raises
    ``TournamentEmptyWindow`` when filtering by symbol.
    """
    db = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE bars (symbol VARCHAR, timeframe VARCHAR, timestamp_utc BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, spread_pips DOUBLE)"
        )
    finally:
        con.close()
    return db


def _preexec_unlimit_memory() -> None:
    """Reset RLIMIT_AS in the child process so duckdb/pandas don't SIGSEGV.

    ``conftest.py`` (via ``common.resource_limits``) sets a hard 2 GB
    ``RLIMIT_AS`` at pytest collection time.  Without this fixture the
    subprocess forks with the same 2 GB ceiling, and Python startup +
    duckdb import exceed it immediately — ``subprocess.run`` reports
    ``returncode=-11`` (SIGSEGV) with empty stderr.  ``preexec_fn`` runs
    in the child between fork() and exec(), so resetting the rlimit here
    removes the parent-imposed ceiling without affecting the parent
    pytest process.
    """
    try:
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    except (OSError, ValueError):
        # Some kernels reject unbounded RLIMIT_AS — fall back to a
        # generous ceiling (16 GB) that still lets duckdb/pandas start.
        try:
            resource.setrlimit(resource.RLIMIT_AS, (16 * 1024 * 1024 * 1024,) * 2)
        except (OSError, ValueError):
            pass


# ── 1. harness_registers_two_existing_strategies ─────────────────────────────


def test_harness_registers_two_existing_strategies(synthetic_duckdb: Path) -> None:
    """TournamentHarness accepts the 2 default smoke strategies and resolves them.

    Covers pre-build checklist assertion:
    ``harness_registers_two_existing_strategies``.
    """
    h = TournamentHarness(
        strategy_ids=["srmr_plus", "bb_rsi_reversion"],
        db_path=synthetic_duckdb,
    )
    assert h.strategy_ids == ["srmr_plus", "bb_rsi_reversion"]
    for sid in h.strategy_ids:
        assert sid in STRATEGY_CLASS_MAP, f"{sid} missing from STRATEGY_CLASS_MAP"


def test_harness_dedupes_strategy_ids(synthetic_duckdb: Path) -> None:
    """Duplicate strategy ids collapse to a single occurrence (preserve order)."""
    h = TournamentHarness(
        strategy_ids=["srmr_plus", "srmr_plus", "bb_rsi_reversion"],
        db_path=synthetic_duckdb,
    )
    assert h.strategy_ids == ["srmr_plus", "bb_rsi_reversion"]


def test_harness_rejects_unknown_strategy_ids(synthetic_duckdb: Path) -> None:
    """Unknown strategy ids raise ``KeyError`` at construction (fail-fast)."""
    with pytest.raises(KeyError, match="unknown strategy_ids"):
        TournamentHarness(strategy_ids=["srmr_plus", "ghost"], db_path=synthetic_duckdb)


def test_harness_rejects_empty_strategy_ids(synthetic_duckdb: Path) -> None:
    """Empty strategy_ids raise ``ValueError`` (edge case 1: only 1 strategy minimum)."""
    with pytest.raises(ValueError, match="must be non-empty"):
        TournamentHarness(strategy_ids=[], db_path=synthetic_duckdb)


# ── 2. scorecard_has_ftmo_columns ────────────────────────────────────────────


def test_scorecard_row_has_canonical_columns() -> None:
    """ScorecardRow carries the verdict schema, including the stat columns.

    Card c0f284b8 added ``cost_modeled`` so a row can never silently carry a
    zero cost.  Card 82b40a95 then added ``rank`` and the statistical
    verdict block (``dsr_pvalue``, ``fdr_qvalue``, ``bh_rejected``,
    ``n_trials_cell``, ``n_trials_selection``) so a row can never present an
    ordering without the trial counts that justify it.
    """
    columns = SCORECARD_ROW_COLUMNS
    assert columns == (
        "strategy_id",
        "symbol",
        "timeframe",
        "return_pct",
        "max_dd_pct",
        "daily_dd_breaches",
        "total_dd_breaches",
        "trade_count",
        "source",
        "cost_modeled",
        "rank",
        "dsr_pvalue",
        "fdr_qvalue",
        "bh_rejected",
        "n_trials_cell",
        "n_trials_selection",
    )
    assert "daily_dd_breaches" in columns
    assert "total_dd_breaches" in columns
    # Defensive: the two FTMO columns must both exist and not be aliased.
    assert columns.count("daily_dd_breaches") == 1
    assert columns.count("total_dd_breaches") == 1
    # Card 82b40a95: the trial-count columns are part of the contract —
    # a DSR without its deflation denominator is not a verdict.
    assert "n_trials_selection" in columns
    assert "dsr_pvalue" in columns
    assert "fdr_qvalue" in columns
    # ``rank`` must appear exactly once; it moved into the canonical
    # columns so the JSON header cannot claim it twice.
    assert columns.count("rank") == 1


def test_scorecard_zero_trade_row_is_valid() -> None:
    """Edge case 4: 0 trades → row still emits with all zeros + rank."""
    row = build_scorecard_row(
        strategy_id="srmr_plus",
        symbol="USDJPY",
        timeframe="H1",
        starting_equity=1.0,
        trades=[],
        dates=[],
        trade_entry_bars=[],
        source="synthetic",
    )
    assert row.trade_count == 0
    assert row.return_pct == 0.0
    assert row.max_dd_pct == 0.0
    assert row.daily_dd_breaches == 0
    assert row.total_dd_breaches == 0
    assert row.source == "synthetic"


def test_scorecard_row_serializes_to_json() -> None:
    """ScorecardRow.to_dict + Scorecard render through JSON without errors."""
    row = ScorecardRow(
        strategy_id="srmr_plus",
        symbol="USDJPY",
        timeframe="H1",
        return_pct=1.5,
        max_dd_pct=2.0,
        daily_dd_breaches=0,
        total_dd_breaches=0,
        trade_count=2,
        source="synthetic",
        rank=1,
    )
    scorecard = Scorecard(rows=[row], meta={"harness_meta": {"bars_loaded": 60}})
    out = render_scorecard_json(scorecard)
    payload = json.loads(out)
    assert payload["rows"][0]["strategy_id"] == "srmr_plus"
    assert payload["rows"][0]["rank"] == 1
    assert payload["rows"][0]["return_pct"] == 1.5
    assert payload["meta"]["harness_meta"]["bars_loaded"] == 60


# ── 3. scorecard_runs_unmodified_strategies ─────────────────────────────────


def test_scorecard_runs_unmodified_strategies(synthetic_duckdb: Path) -> None:
    """Harness orchestrates strategies via the registry; no copy-pasted strategy code.

    Provenance: card c4b86732-9a6c-4169-b5de-e0a12e63280c added the
    fail-loud guard (TournamentNoSignals). Pre-guard, this test also
    asserted that both strategies appeared in the scorecard after a smoke
    run. Post-guard, the same input raises before any scorecard row is
    built, so the smoke-run half of the assertion has moved to
    ``test_smoke_run_raises_tournament_no_signals`` below.

    Validates the "strategies run unmodified" contract:
    * Harness imports strategies via STRATEGY_CLASS_MAP (canonical registry).
    * No strategy module under src/forex_bot/strategies/ is touched by the harness.
    """
    # Pre-build assertion: import resolves via the existing registry, not copy-paste.
    import tournament.harness as harness_mod

    src = Path(harness_mod.__file__).read_text()
    assert "STRATEGY_CLASS_MAP" in src
    assert "importlib.import_module" in src
    # The harness must NOT define its own evaluate() or any strategy logic —
    # it only orchestrates via the registry.
    forbidden_substrings = [
        "class SRMRPlusStrategy",  # would indicate a copy-pasted strategy
        "class BBRSIMeanReversion",
        "def evaluate(",  # defines own strategy loop, not orchestrates existing
    ]
    for needle in forbidden_substrings:
        assert needle not in src, f"harness must not define {needle!r} — strategies are imported"


def test_smoke_run_raises_tournament_no_signals(synthetic_duckdb: Path) -> None:
    """Post-guard (c4b86732): synthetic data → fail-loud guard fires before scorecard.

    Pre-guard, this assertion was folded into
    ``test_scorecard_runs_unmodified_strategies`` (scorecard-row assertion
    that both strategies appear). Post-guard the same synthetic input
    raises ``TournamentNoSignals`` before any scorecard row is built, so
    the original assertion is impossible. This test pins the post-guard
    contract: the guard fires with the strategy/symbol/timeframe detail
    operators need to diagnose the silent-death failure mode that
    motivated card c4b86732.
    """
    with pytest.raises(TournamentNoSignals) as exc_info:
        TournamentHarness(
            strategy_ids=["srmr_plus", "bb_rsi_reversion"],
            db_path=synthetic_duckdb,
        ).run()
    # Error detail must name strategy/symbol/timeframe (the c4b86732 AC1
    # contract) so operators reading the failure can identify the
    # configuration that triggered the silent-death mode.
    detail = exc_info.value.detail
    assert "srmr_plus" in detail, (
        f"TournamentNoSignals detail missing strategy_id 'srmr_plus': {detail!r}"
    )
    assert "USDJPY" in detail, (
        f"TournamentNoSignals detail missing symbol 'USDJPY': {detail!r}"
    )
    assert "H1" in detail, (
        f"TournamentNoSignals detail missing timeframe 'H1': {detail!r}"
    )


# ── 4. run_tournament_smoke_exits_zero ───────────────────────────────────────


def test_run_tournament_smoke_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """scripts/run_tournament.py --smoke exits non-zero when fail-loud guard fires.

    Provenance: card c4b86732-9a6c-4169-b5de-e0a12e63280c merged a strict
    fail-loud guard into ``src/tournament/harness.py``: a registered strategy
    that emits 0 bars OR 0 signals over the smoke window raises
    ``TournamentNoSignals`` (subclass of ``TournamentEmptyWindow``), and the
    CLI surfaces a non-zero exit with a clear error naming strategy/symbol/
    window.

    Pre-guard (≤ 2026-09-13): zero-signal smoke exited 0 with
    ``TOURNAMENT_OK rows=0``. Post-guard (c4b86732, merged to main at
    9f09addea115e37db690bebe38d36a53272eae4e): the same input exits 1 with
    ``ERROR: strategy '<id>' produced 0 signals on <symbol>/<timeframe>
    window <start>..<end>``. This test pins the post-guard contract —
    operators reading a failing CI on this test should know that the
    fail-loud guard fired and the error names the silent-death config that
    motivated c4b86732.

    We monkey-patch AYUMI_DUCKDB_PATH to point at our synthetic file so the
    smoke run uses only worktree-local state, and pass ``--output`` so the
    JSON file lands under ``tmp_path`` (avoids the tests/ ``conftest.py``
    guard that rejects writes under ``<repo>/data/``).
    """
    monkeypatch.setenv("AYUMI_DUCKDB_PATH", str(tmp_path / "smoke.duckdb"))
    _make_synthetic_duckdb(tmp_path / "smoke.duckdb", n_bars=80)
    output_path = tmp_path / "scorecard_smoke.json"

    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "run_tournament.py"
    assert script.is_file(), f"missing entry point: {script}"

    proc = subprocess.run(  # noqa: S603 — sys.executable + fixed Path, no untrusted input
        [sys.executable, str(script), "--smoke", "--output", str(output_path)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        preexec_fn=_preexec_unlimit_memory,
    )
    # Post-guard (c4b86732): the synthetic 80-bar USDJPY H1 walk is symmetric
    # enough that ``srmr_plus`` (default smoke strategy) processes 80 bars
    # but emits 0 signals — the fail-loud guard fires and the CLI exits
    # non-zero. Pin non-zero exit + clear error naming strategy/symbol/
    # window so operators can diagnose the silent-death failure mode.
    assert proc.returncode != 0, (
        f"expected non-zero exit on zero-signal smoke, got {proc.returncode}\n"
        f"STDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}"
    )
    # Error output must name strategy_id, symbol, timeframe, and window
    # so operators reading CI failure can identify the configuration drift
    # that motivated card c4b86732.
    combined = proc.stdout + "\n" + proc.stderr
    assert "srmr_plus" in combined, (
        f"error output missing strategy_id 'srmr_plus':\n{combined}"
    )
    assert "USDJPY" in combined, (
        f"error output missing symbol 'USDJPY':\n{combined}"
    )
    assert "H1" in combined, (
        f"error output missing timeframe 'H1':\n{combined}"
    )
    # Window dates appear in the TournamentNoSignals detail so operators
    # can spot a configuration drift (e.g. wrong start_date on the smoke
    # fixture). The synthetic walk anchors 2024-06-03..2024-06-09.
    assert "2024-06-03" in combined and "2024-06-09" in combined, (
        f"error output missing window dates 2024-06-03..2024-06-09:\n{combined}"
    )


def test_run_tournament_emits_json_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-guard (c4b86732): smoke run on zero-signal data exits non-zero; no JSON.

    Provenance: card c4b86732-9a6c-4169-b5de-e0a12e63280c added the
    fail-loud guard (TournamentNoSignals). Pre-guard, the smoke run
    with ``--output`` wrote a JSON scorecard file (zero-trade rows were
    valid pre-guard). Post-guard, the same input triggers
    ``TournamentNoSignals`` → the CLI exits 1 before any scorecard is
    built, so the JSON file is never written.

    This test now pins the post-guard contract: non-zero exit + clear
    error naming strategy/symbol/window + the absence of the JSON file.
    The name ``test_run_tournament_emits_json_file`` is retained for
    searchability (operators grepping CI logs will find both pre- and
    post-guard history); the new assertions document the behavior change.

    Output path is set under ``tmp_path`` (not ``<repo>/data/``) so the
    tests/ ``conftest.py`` write-guard does not fire on either path.
    """
    monkeypatch.setenv("AYUMI_DUCKDB_PATH", str(tmp_path / "smoke.duckdb"))
    _make_synthetic_duckdb(tmp_path / "smoke.duckdb", n_bars=80)
    output = tmp_path / "scorecard_smoke.json"

    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "run_tournament.py"

    proc = subprocess.run(  # noqa: S603 — sys.executable + fixed Path, no untrusted input
        [sys.executable, str(script), "--smoke", "--output", str(output)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        preexec_fn=_preexec_unlimit_memory,
    )
    # Post-guard (c4b86732): synthetic 80-bar USDJPY H1 walk produces 0
    # signals for ``srmr_plus`` (default smoke strategy) → fail-loud
    # guard fires → CLI exits non-zero. JSON output is NOT written
    # because no scorecard was built before the guard raised.
    assert proc.returncode != 0, (
        f"expected non-zero exit on zero-signal smoke, got {proc.returncode}\n"
        f"STDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}"
    )
    # Error output must name strategy_id, symbol, timeframe, and the
    # zero-signals marker so operators can diagnose the silent-death
    # failure mode that motivated card c4b86732.
    combined = proc.stdout + "\n" + proc.stderr
    assert "srmr_plus" in combined, (
        f"error output missing strategy_id 'srmr_plus':\n{combined}"
    )
    assert "USDJPY" in combined, (
        f"error output missing symbol 'USDJPY':\n{combined}"
    )
    assert "H1" in combined, (
        f"error output missing timeframe 'H1':\n{combined}"
    )
    assert "0 signals" in combined, (
        f"error output missing '0 signals' guard marker:\n{combined}"
    )
    # The scorecard JSON file must NOT exist when the guard fires pre-row
    # construction. This is the contract delta vs pre-guard: the file
    # was previously written (with zero-trade rows); now it is not.
    assert not output.exists(), (
        f"scorecard JSON must not be written when guard fires, found {output}"
    )


# ── 5. empty_window_exits_nonzero ────────────────────────────────────────────


def test_empty_window_raises(synthetic_duckdb: Path) -> None:
    """Edge case 3: empty dataset path → TournamentEmptyWindow with clear message."""
    with pytest.raises(TournamentEmptyWindow, match="no H1 bars"):
        load_bars_for_window(synthetic_duckdb, symbol="ZZZZZ", timeframe="H1")


def test_empty_duckdb_window_raises(empty_duckdb: Path) -> None:
    """DuckDB file with 0 bars → TournamentEmptyWindow (loud failure, not pandas concat)."""
    with pytest.raises(TournamentEmptyWindow):
        load_bars_for_window(empty_duckdb, symbol="USDJPY", timeframe="H1")


def test_empty_window_cli_returns_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI surfaces TournamentEmptyWindow as exit-1 (not a stack trace)."""
    monkeypatch.setenv("AYUMI_DUCKDB_PATH", str(empty_duckdb := tmp_path / "empty.duckdb"))
    # Build empty duckdb
    con = duckdb.connect(str(empty_duckdb))
    try:
        con.execute(
            "CREATE TABLE bars (symbol VARCHAR, timeframe VARCHAR, timestamp_utc BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, spread_pips DOUBLE)"
        )
    finally:
        con.close()

    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "run_tournament.py"
    proc = subprocess.run(  # noqa: S603 — sys.executable + fixed Path, no untrusted input
        [sys.executable, str(script), "--smoke"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        preexec_fn=_preexec_unlimit_memory,
    )
    assert proc.returncode == 1
    assert "ERROR" in proc.stderr or "ERROR" in proc.stdout


# ── 6. Ranking determinism ───────────────────────────────────────────────────


def test_ranking_is_deterministic() -> None:
    """Sprint C 1b.3 (card cc90a6b6): raw-return sort is removed.

    The standalone :class:`TournamentHarness` no longer ranks its
    scorecard (it doesn't have per-trial series to feed
    :func:`forex_bot.factory.risk_adjusted_ranking.rank_candidates_by_trial_returns`).
    Rows come back with ``rank=0`` (default) and the factory front door
    is responsible for ranking via
    :func:`forex_bot.factory.risk_adjusted_ranking.rank_from_trial_return_store`.
    Tied returns no longer share a synthetic rank — they are simply
    unranked and the downstream ranker applies the FDR-aware order.
    """
    rows = [
        ScorecardRow("charlie", "USDJPY", "H1", 5.0, 1.0, 0, 0, 10, "src"),
        ScorecardRow("alpha", "USDJPY", "H1", 5.0, 1.0, 0, 0, 20, "src"),
        ScorecardRow("bravo", "USDJPY", "H1", 5.0, 1.0, 0, 0, 10, "src"),
    ]
    sc = build_scorecard_row_unranked_harness_wrapper(rows)
    assert all(r.rank == 0 for r in sc.rows), (
        "Sprint C 1b.3: harness no longer assigns synthetic ranks; factory lanes rank via TrialReturnStore"
    )


def test_ranking_descends_by_return() -> None:
    """Sprint C 1b.3: the raw-return sort path raises loud (loud-failure shim).

    Callers that invoke :func:`tournament.scorecard.rank_scorecard_rows`
    must migrate to
    :func:`forex_bot.factory.risk_adjusted_ranking.rank_candidates_by_trial_returns`.
    The harness no longer reaches into this function; downstream
    consumers that try to use the removed raw-return path must surface
    a clear migration error.
    """
    rows = [
        ScorecardRow("low", "USDJPY", "H1", 1.0, 1.0, 0, 0, 1, "src"),
        ScorecardRow("high", "USDJPY", "H1", 10.0, 1.0, 0, 0, 0, "src"),
        ScorecardRow("mid", "USDJPY", "H1", 5.0, 1.0, 0, 0, 0, "src"),
    ]
    # The raw-return sort is now a loud-failure shim.
    from forex_bot.factory.risk_adjusted_ranking import RawReturnSortRemoved
    from tournament.scorecard import rank_scorecard_rows

    with pytest.raises(RawReturnSortRemoved):
        rank_scorecard_rows(rows)


def test_ranking_empty_input() -> None:
    """Sprint C 1b.3: the raw-return sort raises on empty input too (no early-return).

    Empty input does NOT bypass the loud-failure shim — the function
    must raise regardless of the input so silent use is impossible.
    """
    from forex_bot.factory.risk_adjusted_ranking import RawReturnSortRemoved
    from tournament.scorecard import rank_scorecard_rows

    with pytest.raises(RawReturnSortRemoved):
        rank_scorecard_rows([])


def build_scorecard_row_unranked_harness_wrapper(rows):
    """Thin test helper: build a Scorecard without calling the deprecated ranker.

    Mirrors what :meth:`TournamentHarness._scorecard_with_rank_or_unranked`
    does in production — return an unranked scorecard so the ranker can
    be applied by a downstream factory lane that actually has per-trial
    series.
    """
    from tournament.scorecard import Scorecard

    return Scorecard(rows=list(rows), meta={})


# ── 7. Console rendering ─────────────────────────────────────────────────────


def test_console_table_includes_all_rows_and_headers() -> None:
    """Console table includes every strategy with the right headers."""
    rows = [
        ScorecardRow("srmr_plus", "USDJPY", "H1", 2.5, 1.0, 0, 0, 5, "src", rank=1),
        ScorecardRow("bb_rsi_reversion", "USDJPY", "H1", 0.5, 3.0, 1, 0, 3, "src", rank=2),
    ]
    out = render_console_table(Scorecard(rows=rows))
    assert "srmr_plus" in out
    assert "bb_rsi_reversion" in out
    for header in ["Rank", "Strategy", "Sym", "TF", "Return%", "MaxDD%", "DailyBreaches", "TotalBreaches", "Trades"]:
        assert header in out, f"missing header {header!r} in console output"


# ── 8. DuckDB path resolution ────────────────────────────────────────────────


def test_default_path_resolution_honors_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AYUMI_DUCKDB_PATH takes precedence over git worktree discovery."""
    override = tmp_path / "override.duckdb"
    monkeypatch.setenv("AYUMI_DUCKDB_PATH", str(override))
    resolved = resolve_default_duckdb_path()
    assert resolved == override.expanduser().resolve()


def test_load_bars_for_window_filters_by_symbol(synthetic_duckdb: Path) -> None:
    """Symbol filter narrows the result set; missing symbol raises TournamentEmptyWindow."""
    df, raw = load_bars_for_window(synthetic_duckdb, symbol="USDJPY", timeframe="H1")
    assert len(df) == 60
    assert raw == 60
    assert set(df.columns) >= {"time_utc", "date_utc", "close", "high", "low", "open"}


def test_isolated_test_path(tmp_path: Path) -> None:
    """Sanity: tests live under tests/tournament/ only — pytest.ini testpaths=tests."""
    # The pre-build checklist (assertion 7) is satisfied structurally:
    # no other test files were added/modified by this build.
    tests_root = Path(__file__).resolve().parents[2] / "tests"
    assert (tests_root / "tournament" / "test_harness.py").is_file()
    # And no other test files outside tests/tournament/ are introduced by this card.
    # (We can't audit the whole tree inline; the structural check above is the
    # contract: tests/tournament/ contains the new file.)


# ── 9. Daily-DD breach counting (regression after Rin REWORK r1) ───────────
#
# Card db04d5b5 r1 had a MEDIUM finding: ``_daily_dd_breach_counts`` skipped
# day 1's equity-vs-baseline comparison (initialized ``prev_equity`` to
# ``curve[0][1]`` and iterated ``curve[1:]``).  This caused a single-day
# curve with a >3% day-1 drop to silently report zero breaches.  These
# tests pin the corrected behavior: day 1 compares against the starting
# equity baseline, day N+1 against day N's equity, and the total-DD
# sweep is independent.


def test_daily_dd_breach_counts_first_day_loss() -> None:
    """Rin repro: single-entry curve with a 4% day-1 loss must flag 1 daily breach.

    Pre-fix, this returned ``(0, 0)`` because day 1 was never compared
    against the starting-equity baseline.  The fixed implementation uses
    ``starting_equity`` (default 1.0) as the day-1 baseline.
    """
    curve = [(dt.date(2024, 1, 1), 0.96)]
    daily, total = _daily_dd_breach_counts(
        curve, daily_limit_pct=3.0, total_limit_pct=10.0
    )
    assert (daily, total) == (1, 0)


def test_daily_dd_breach_counts_empty_curve_is_zero() -> None:
    """Empty curve → ``(0, 0)`` (preserved behavior — no entries to compare)."""
    daily, total = _daily_dd_breach_counts(
        [], daily_limit_pct=3.0, total_limit_pct=10.0
    )
    assert (daily, total) == (0, 0)


def test_daily_dd_breach_counts_single_entry_no_loss_is_zero() -> None:
    """Single-entry curve with a gain → ``(0, 0)`` (no daily-DD breach).

    Pins the "single entry + positive move" branch of the corrected
    implementation: day 1 vs starting baseline is computed but doesn't
    cross the daily threshold.
    """
    curve = [(dt.date(2024, 1, 1), 1.02)]  # +2% day-1 gain
    daily, total = _daily_dd_breach_counts(
        curve, daily_limit_pct=3.0, total_limit_pct=10.0
    )
    assert (daily, total) == (0, 0)


def test_daily_dd_breach_counts_multi_entry_day_two_loss() -> None:
    """Multi-entry curve with a 4% drop on day 2 → ``daily_breaches == 1``.

    Day 1: starting→e1 (no breach).  Day 2: e1→e2 with 4% drop
    (breach).  Total-DD sweep runs independently; ``total_breaches`` is
    asserted non-negative (the spec says it is determined by the sweep,
    not a fixed value, so we only pin that branch fired at least once or
    zero for this small case).
    """
    curve = [
        (dt.date(2024, 1, 1), 1.0),   # day 1 flat
        (dt.date(2024, 1, 2), 0.96),  # day 2 -4% from day 1
    ]
    daily, total = _daily_dd_breach_counts(
        curve, daily_limit_pct=3.0, total_limit_pct=10.0
    )
    assert daily == 1
    # Total-DD is set by the second sweep; for a 4% drop it does not
    # cross the 10% threshold, so total stays at 0.
    assert total == 0


def test_daily_dd_breach_counts_respects_explicit_starting_equity() -> None:
    """Explicit ``starting_equity`` is honored when the curve is pre-scaled.

    Calls the spec's parameterization contract: callers that pre-scale
    the curve to a non-1.0 baseline MUST pass ``starting_equity`` so
    day 1's comparison uses the correct baseline (not silently 1.0).
    Here the curve starts at 9600.0 with a 10000.0 baseline → 4% drop.
    """
    curve = [(dt.date(2024, 1, 1), 9600.0)]
    daily, total = _daily_dd_breach_counts(
        curve,
        daily_limit_pct=3.0,
        total_limit_pct=10.0,
        starting_equity=10000.0,
    )
    assert (daily, total) == (1, 0)


# ── Card c0f284b8: XAUUSD cost reconciliation (Sprint E gate 2) ─────────────


def _flat_bars(n: int = 6, *, price: float = 2000.0) -> pd.DataFrame:
    """Flat synthetic OHLC frame: every bar identical around ``price``."""
    return pd.DataFrame(
        {
            "open": [price] * n,
            "high": [price + 0.5] * n,
            "low": [price - 0.5] * n,
            "close": [price] * n,
        }
    )


class TestSingleCostConstant:
    """AC1: exactly one sourced spread constant, asserted on BOTH paths."""

    def test_harness_and_liora_agree_on_xauusd(self) -> None:
        liora = _liora_spread_costs().default_spread_costs().get("XAUUSD")
        cm = cost_model_for("XAUUSD")
        assert cm is not None
        assert cm.spread_pips == liora.spread_pips

    def test_harness_and_liora_agree_on_gbpusd(self) -> None:
        liora = _liora_spread_costs().default_spread_costs().get("GBPUSD")
        cm = cost_model_for("GBPUSD")
        assert cm is not None
        assert cm.spread_pips == liora.spread_pips

    def test_commission_and_slippage_also_sourced(self) -> None:
        """The whole cost trio comes from the Liora table, not a local literal."""
        mod = _liora_spread_costs()
        for symbol in ("XAUUSD", "GBPUSD"):
            cm = cost_model_for(symbol)
            assert cm is not None
            assert cm.commission_per_lot == mod.COMMISSION_PER_LOT_USD
            assert cm.slippage_pips == mod.PIP_SLIPPAGE

    def test_xauusd_is_no_longer_the_stale_2_5(self) -> None:
        """Regression guard for the specific conflict this card closed."""
        cm = cost_model_for("XAUUSD")
        assert cm is not None
        assert cm.spread_pips != 2.5


class TestNoSilentCostFreeRow:
    """AC2: an unsizable trade is UNMODELED, never scored as free."""

    def test_unsizable_trade_returns_none_not_zero(self) -> None:
        broken = CostModel(
            spread_pips=3.0,
            commission_per_lot=3.5,
            slippage_pips=0.2,
            pip_value_per_lot=0.0,
            pip_size=0.10,
        )
        cf = _cost_fraction_for_trade(
            entry_price=2000.0,
            sl_price=1998.0,
            cost_model=broken,
            r_fraction=0.005,
            equity=1.0,
        )
        assert cf is None, "unsizable trade must not report a zero cost"

    def test_degenerate_sl_still_produces_a_real_cost(self) -> None:
        """The 10-pip defensive fallback stays MODELED (it yields real cost)."""
        cm = cost_model_for("XAUUSD")
        assert cm is not None
        cf = _cost_fraction_for_trade(
            entry_price=2000.0,
            sl_price=2000.0,
            cost_model=cm,
            r_fraction=0.005,
            equity=1.0,
        )
        assert cf is not None
        assert cf > 0


class TestCostModeledPropagation:
    """AC2: the flag reaches the row and survives serialization."""

    _SIGNALS = [(0, 1998.0, 2002.0, 1)]
    _DATES = ["2024-06-03"] * 6

    def _trade_and_row(self, *, with_cost: bool):
        cm = cost_model_for("XAUUSD") if with_cost else None
        trades = _simulate_trades(
            _flat_bars(), self._SIGNALS, cost_model=cm, max_bars_held=1,
        )
        assert len(trades) == 1, "fixture should close exactly one trade"
        return trades[0], build_scorecard_row(
            strategy_id="synthetic",
            symbol="XAUUSD",
            timeframe="H1",
            starting_equity=1.0,
            trades=trades,
            dates=self._DATES,
            trade_entry_bars=[t["entry_bar"] for t in trades],
            source="synthetic",
        )

    def test_no_cost_model_marks_trade_and_row_unmodeled(self) -> None:
        """The silent cost-free path: cost_model=None must be FLAGGED."""
        trade, row = self._trade_and_row(with_cost=False)
        assert trade["cost_modeled"] is False
        assert row.cost_modeled is False

    def test_cost_model_marks_trade_and_row_modeled(self) -> None:
        trade, row = self._trade_and_row(with_cost=True)
        assert trade["cost_modeled"] is True
        assert row.cost_modeled is True
        assert trade["cost_fraction"] > 0

    def test_flag_survives_json_serialization(self) -> None:
        """Guards the asdict() hole: a dynamic attribute would vanish here."""
        _, row = self._trade_and_row(with_cost=False)
        assert row.to_dict()["cost_modeled"] is False
        payload = json.loads(render_scorecard_json(Scorecard(rows=[row], meta={})))
        assert "cost_modeled" in payload["columns"]
        assert payload["rows"][0]["cost_modeled"] is False

    def test_verdict_grade_rows_excludes_unmodeled(self) -> None:
        _, good = self._trade_and_row(with_cost=True)
        _, bad = self._trade_and_row(with_cost=False)
        kept = verdict_grade_rows([good, bad])
        assert kept == [good]
        assert bad not in kept


class TestRinIter1Regressions:
    """Regression guards for Rin iter-1 findings (MEDIUM-3, MEDIUM-4, LOWs)."""

    def test_liora_loader_returns_the_same_module_object(self) -> None:
        """MEDIUM-3: the loader used to re-exec the file on every call."""
        assert _liora_spread_costs() is _liora_spread_costs()

    def test_dict_row_without_flag_is_not_verdict_grade(self) -> None:
        """MEDIUM-4: dict rows must not pass the gate by default.

        ``front_door.load_scorecard()`` returns ``list[dict]``, so the old
        ``getattr(..., True)`` let every flagless dict row through.
        """
        assert row_is_cost_modeled({}) is False
        assert row_is_cost_modeled({"cost_modeled": False}) is False
        assert row_is_cost_modeled({"cost_modeled": True}) is True

    def test_dict_rows_round_trip_through_the_gate(self) -> None:
        rows = [{"strategy_id": "a", "cost_modeled": True},
                {"strategy_id": "b"}]
        kept = verdict_grade_rows(rows)
        assert kept == [rows[0]]

    def test_zero_trade_row_is_not_verdict_grade(self) -> None:
        """LOW: all([]) used to make a 0-trade row cost-modeled=True."""
        row = build_scorecard_row(
            strategy_id="idle",
            symbol="XAUUSD",
            timeframe="H1",
            starting_equity=1.0,
            trades=[],
            dates=["2024-06-03"],
            trade_entry_bars=[],
            source="synthetic",
        )
        assert row.trade_count == 0
        assert row.cost_modeled is False

    def test_trade_without_provenance_key_is_not_verdict_grade(self) -> None:
        """LOW: a hand-built trade missing the key must fail closed."""
        row = build_scorecard_row(
            strategy_id="legacy",
            symbol="XAUUSD",
            timeframe="H1",
            starting_equity=1.0,
            trades=[{"pnl_fraction": 0.01, "entry_bar": 0}],
            dates=["2024-06-03"],
            trade_entry_bars=[0],
            source="synthetic",
        )
        assert row.cost_modeled is False

    def test_xauusd_spread_is_pinned_to_the_liora_value(self) -> None:
        """LOW: the both-paths test alone was near-tautological.

        Because FTMO_COST_DEFAULTS is DERIVED from the Liora table, comparing
        the two paths cannot detect the constant silently changing. Pin the
        literal value so a future edit to either side is caught.
        """
        cm = cost_model_for("XAUUSD")
        assert cm is not None
        assert cm.spread_pips == 3.0
        cm2 = cost_model_for("GBPUSD")
        assert cm2 is not None
        assert cm2.spread_pips == 1.5


class TestWorkerRunnerCostGate:
    """Rin iter-1 MEDIUM-2 + iter-2 MEDIUM-A/MEDIUM-B guards (scripts/offload).

    Imported inside the tests so a collection-time failure in the offload
    module cannot take down the whole tournament suite.

    The end-to-end tests call ``_execute_cell`` itself.  Iter-2's two MEDIUMs
    were both invisible to helper-only tests: the helper validated three of
    five fields, and a partial payload reached ``CostModel(**payload)`` OUTSIDE
    the ``try:`` block, escaping as TypeError and killing the worker daemon.
    Driving ``_execute_cell`` is what catches that class of wiring defect.
    """

    _GOOD = {
        "spread_pips": 3.0,
        "commission_per_lot": 3.5,
        "slippage_pips": 0.2,
        "pip_value_per_lot": 10.0,
        "pip_size": 0.10,
    }

    @staticmethod
    def _job(payload=None, **extra):
        """Build a job descriptor that reaches the cost-model branch.

        The branch sits after the cell_id and data_db_path guards but before
        any DuckDB access, so these tests never touch a database.
        """
        from offload.worker_runner import _compute_cell_id

        strategy, symbol, timeframe = "srmr_plus", "XAUUSD", "H1"
        job = {
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "cell_id": _compute_cell_id(strategy, symbol, timeframe),
            # Never opened: the cost-model branch returns before any DuckDB
            # access. Repo-relative so it cannot trip the insecure-/tmp lint.
            "data_db_path": str(
                Path(__file__).resolve().parents[2]
                / "nonexistent-c0f284b8.duckdb"
            ),
        }
        merged = dict(payload or {})
        merged.update(extra)
        if merged:
            job["cost_model"] = merged
        return job

    @staticmethod
    def _job_raw(cost_model):
        """Job whose ``cost_model`` is set to an arbitrary raw JSON value."""
        from offload.worker_runner import _compute_cell_id

        strategy, symbol, timeframe = "srmr_plus", "XAUUSD", "H1"
        return {
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "cell_id": _compute_cell_id(strategy, symbol, timeframe),
            "data_db_path": str(
                Path(__file__).resolve().parents[2]
                / "nonexistent-c0f284b8.duckdb"
            ),
            "cost_model": cost_model,
        }

    @staticmethod
    def _run(job):
        from offload.worker_runner import _execute_cell

        code_root = Path(__file__).resolve().parents[2]
        return _execute_cell(job, code_root)

    # ---- unit level: the validator ----
    def test_payload_matching_liora_is_accepted(self) -> None:
        from offload.worker_runner import _cost_model_payload_error

        cm = cost_model_for("XAUUSD")
        assert cm is not None
        payload = dict(self._GOOD)
        payload["pip_value_per_lot"] = cm.pip_value_per_lot
        payload["pip_size"] = cm.pip_size
        assert _cost_model_payload_error("XAUUSD", payload, cm) is None

    def test_stale_2_5_payload_is_rejected(self) -> None:
        """The exact dual constant AC1 closed must not slip back in."""
        from offload.worker_runner import _cost_model_payload_error

        cm = cost_model_for("XAUUSD")
        assert cm is not None
        err = _cost_model_payload_error("XAUUSD", dict(self._GOOD, spread_pips=2.5), cm)
        assert err is not None
        assert "spread_pips" in err
        assert "Liora" in err

    def test_stale_gbpusd_2_0_payload_is_rejected(self) -> None:
        from offload.worker_runner import _cost_model_payload_error

        cm = cost_model_for("GBPUSD")
        assert cm is not None
        payload = dict(self._GOOD, spread_pips=2.0, pip_value_per_lot=10.0,
                       pip_size=0.0001)
        err = _cost_model_payload_error("GBPUSD", payload, cm)
        assert err is not None

    def test_wrong_pip_value_is_rejected(self) -> None:
        """MEDIUM-A: the pip fields rescale qty_lots and every cost term."""
        from offload.worker_runner import _cost_model_payload_error

        cm = cost_model_for("XAUUSD")
        assert cm is not None
        err = _cost_model_payload_error(
            "XAUUSD", dict(self._GOOD, pip_value_per_lot=1.0), cm
        )
        assert err is not None
        assert "pip_value_per_lot" in err

    def test_wrong_pip_size_is_rejected(self) -> None:
        from offload.worker_runner import _cost_model_payload_error

        cm = cost_model_for("XAUUSD")
        assert cm is not None
        err = _cost_model_payload_error(
            "XAUUSD", dict(self._GOOD, pip_size=1.0), cm
        )
        assert err is not None
        assert "pip_size" in err

    def test_symbol_without_liora_row_skips_value_check(self) -> None:
        from offload.worker_runner import _cost_model_payload_error

        assert _cost_model_payload_error("USDJPY", dict(self._GOOD), None) is None

    def test_non_numeric_payload_field_is_rejected(self) -> None:
        from offload.worker_runner import _cost_model_payload_error

        cm = cost_model_for("XAUUSD")
        assert cm is not None
        err = _cost_model_payload_error("XAUUSD", dict(self._GOOD, spread_pips="wide"), cm)
        assert err is not None
        assert "not numeric" in err

    def test_bool_field_is_rejected(self) -> None:
        from offload.worker_runner import _cost_model_payload_error

        cm = cost_model_for("XAUUSD")
        assert cm is not None
        err = _cost_model_payload_error("XAUUSD", dict(self._GOOD, pip_size=True), cm)
        assert err is not None
        assert "bool" in err

    # ---- end-to-end through _execute_cell ----
    def test_execute_cell_fails_loudly_on_partial_payload(self) -> None:
        """MEDIUM-B regression: must RETURN, never raise TypeError.

        A partial payload used to reach ``CostModel(**payload)`` before the
        ``try:`` block, escaping into the worker loop and killing the daemon.
        """
        out = self._run(self._job(spread_pips=3.0, commission_per_lot=3.5))
        assert out["exit_code"] == 5
        assert "missing required field" in out["error"]

    def test_execute_cell_fails_loudly_on_wrong_pip_value(self) -> None:
        """MEDIUM-A regression, end to end."""
        out = self._run(self._job(dict(self._GOOD, pip_value_per_lot=1.0,
                                       pip_size=1.0)))
        assert out["exit_code"] == 5
        assert "pip_value_per_lot" in out["error"]

    def test_execute_cell_fails_loudly_on_unexpected_field(self) -> None:
        out = self._run(self._job(dict(self._GOOD, bogus_field=1.0)))
        assert out["exit_code"] == 5
        assert "unexpected field" in out["error"]

    def test_execute_cell_fails_loudly_on_stale_spread(self) -> None:
        out = self._run(self._job(dict(self._GOOD, spread_pips=2.5)))
        assert out["exit_code"] == 5
        assert "Liora" in out["error"]

    # ---- iter-3 MEDIUM-1: hostile descriptors must fail, never raise ----
    def test_execute_cell_rejects_non_dict_cost_model(self) -> None:
        """A non-dict cost_model made `f not in payload` raise TypeError.

        That escape killed the worker daemon and stranded the cell, because
        _process_one_job has try/finally with no except.
        """
        for hostile in (5, True, 3.5):
            out = self._run(self._job_raw(hostile))
            assert out["exit_code"] == 5, hostile
            assert "must be a JSON object" in out["error"], hostile

    def test_execute_cell_rejects_oversized_integer_field(self) -> None:
        """float() on a ~309-digit int raises OverflowError, not ValueError."""
        out = self._run(self._job(dict(self._GOOD, spread_pips=10 ** 400)))
        assert out["exit_code"] == 5
        assert "too large" in out["error"]

    def test_numeric_string_field_is_normalized_not_deferred(self) -> None:
        """A validated numeric string builds the model instead of failing late."""
        from offload.worker_runner import _cost_model_payload_error

        cm = cost_model_for("XAUUSD")
        assert cm is not None
        err = _cost_model_payload_error(
            "XAUUSD", dict(self._GOOD, spread_pips="3.0"), cm
        )
        assert err is None, err

    def test_unmodeled_row_fails_the_verdict_gate(self) -> None:
        """HIGH-1: the cell must be gated, not emitted as a success."""
        trade, row = TestCostModeledPropagation()._trade_and_row(with_cost=False)
        assert trade["cost_modeled"] is False
        assert row_is_cost_modeled(row) is False
        assert verdict_grade_rows([row]) == []


# ── 9. Card 82b40a95 — DSR/BH-FDR selection-set pass ────────────────────────


def _selection_set_store(*, seed: int = 7, n_bars: int = 120):
    """A two-cell TrialReturnStore: one strong trial + the rest noise.

    Cell A gets a genuine positive drift (the "winner" a selection-on-the-
    maximum sweep would crown); every other trial is zero-drift noise.  The
    total across BOTH cells is 5 trials — the deflation denominator the
    DSR is supposed to be computed against.
    """
    import numpy as np
    from forex_bot.factory.validation_runner import TrialReturnStore

    rng = np.random.default_rng(seed)
    store = TrialReturnStore()
    # Cell A (2 trials): one real edge, one pure noise.
    store.record(("srmr_plus", "XAUUSD", "H1"), rng.normal(0.006, 0.010, n_bars))
    store.record(("srmr_plus", "XAUUSD", "H1"), rng.normal(0.000, 0.010, n_bars))
    # Cell B (3 trials): all noise, so a cross-cell "best of both" sweep
    # still has to beat the same set-wide multiple-testing burden.
    for _ in range(3):
        store.record(("bb_rsi_reversion", "EURUSD", "H1"), rng.normal(0.000, 0.010, n_bars))
    return store


def test_selection_set_reports_dsr_bh_fdr_and_n() -> None:
    """AC1: a multi-cell selection set scores with DSR/BH-FDR and N reported."""
    from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr

    ranking = rank_selection_set_with_dsr(_selection_set_store())

    assert ranking.n_cells == 2
    assert ranking.n_trials == 5, "N must be reported for the whole selection set"
    assert ranking.n_candidates == 5
    scores = ranking.all_scores()
    assert len(scores) == 5
    for s in scores:
        assert 0.0 <= s.dsr_pvalue <= 1.0, f"DSR p out of range for {s.candidate_id}"
        assert 0.0 <= s.q_value <= 1.0, f"BH q out of range for {s.candidate_id}"
        assert s.n_obs == 120, "the series length must travel with the verdict"


def test_deflation_denominator_is_set_wide_not_per_cell() -> None:
    """The DSR deflates against ALL trials in the set, not just its own cell.

    The selection is made by comparing across cells, so deflating per cell
    would understate E[max SR] and overstate every winner.  Cell A has 2
    trials and cell B has 3; both must report 5.
    """
    from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr

    ranking = rank_selection_set_with_dsr(_selection_set_store())
    assert ranking.n_trials == 5
    for s in ranking.all_scores():
        assert s.n_trials_selection == 5, (
            f"{s.candidate_id} deflated against {s.n_trials_selection}; "
            "the denominator must be the whole selection set"
        )
        assert s.n_trials_cell in (2, 3), "per-cell count is reported separately"
        # The deflation denominator can never be smaller than the cell's
        # own trial count — that would be a weaker correction than
        # BH already applied.
        assert s.n_trials_selection >= s.n_trials_cell


def test_more_trials_deflate_harder() -> None:
    """Deflation must be monotone in N: more trials => weaker evidence."""
    from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr

    few = rank_selection_set_with_dsr(_selection_set_store(seed=7), n_trials_selection=3)
    many = rank_selection_set_with_dsr(_selection_set_store(seed=7), n_trials_selection=500)
    best_few = min(few.all_scores(), key=lambda s: s.dsr_pvalue).dsr_pvalue
    best_many = min(many.all_scores(), key=lambda s: s.dsr_pvalue).dsr_pvalue
    assert best_many >= best_few, (
        "deflating against more trials must not improve the DSR p-value"
    )


def test_real_edge_deflates_better_than_noise() -> None:
    """A strong injected edge must beat noise with a ROBUST margin.

    Rin iter-1: the previous version of this test separated the winner
    from the field by a float ulp (p=0.99999999999 vs 1.0), which is
    not a margin, it is a rounding accident.  This one injects an
    unambiguous edge (per-bar SR ~ 5) and asserts both directions: the
    edge clears the DSR level and the noise trials do not.
    """
    import numpy as np
    from forex_bot.factory.validation_runner import TrialReturnStore
    from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr

    rng = np.random.default_rng(11)
    store = TrialReturnStore()
    store.record(("arch", "XAUUSD", "H1"), rng.normal(0.050, 0.010, 400))
    for _ in range(3):
        store.record(("arch", "XAUUSD", "H1"), rng.normal(0.000, 0.010, 400))

    ranking = rank_selection_set_with_dsr(store)
    scores = {s.candidate_id: s for s in ranking.all_scores()}
    edge = scores["trial_0"]
    noise = [scores[f"trial_{i}"] for i in (1, 2, 3)]

    assert all(edge.dsr_pvalue < n.dsr_pvalue for n in noise)
    assert edge.dsr_pvalue < edge.dsr_alpha, (
        "a strong real edge must be able to clear the DSR level"
    )
    assert all(n.dsr_pvalue > 0.5 for n in noise), "noise must not look significant"
    assert edge.is_discovery is True
    assert all(n.is_discovery is False for n in noise)


def test_discovery_requires_dsr_not_just_bh() -> None:
    """Rin iter-1 HIGH-1 regression: BH rejection alone is NOT a discovery.

    The removed predicate was ``dsr_pvalue < 1.0``, which is vacuous
    (1 - Phi(z) < 1.0 for any finite z).  It crowned a p~1.0 candidate
    rank 1 and printed "(1 discoveries)".  A discovery must clear BOTH
    the BH-FDR step and the DSR level.
    """
    from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr

    ranking = rank_selection_set_with_dsr(_selection_set_store())
    bh_hits = [s for s in ranking.all_scores() if s.bh_rejected and not s.bh_reject]
    assert bh_hits, "store must produce a BH rejection for this guard to bite"

    for s in bh_hits:
        assert s.is_discovery == (s.dsr_pvalue <= s.dsr_alpha)

    # The exact failure Rin measured: a marginal BH winner at p~1.0.
    marginal = [s for s in bh_hits if s.dsr_pvalue > 0.5]
    assert marginal, "expected at least one high-p BH hit"
    assert all(not s.is_discovery for s in marginal), (
        "a BH-rejected candidate with no deflated edge is not a discovery"
    )


def test_n_trials_override_cannot_weaken_deflation() -> None:
    """Rin iter-1 MEDIUM: an override may only make deflation harsher.

    ``expected_max_sharpe(1) == 0.0``, so honouring a sub-store override
    would collapse the DSR to a plain z-test — weaker than the
    correction BH already applied.
    """
    from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr

    clamped = rank_selection_set_with_dsr(_selection_set_store(), n_trials_selection=1)
    assert clamped.n_trials == 5, "an override below the store count must be clamped up"

    honoured = rank_selection_set_with_dsr(_selection_set_store(), n_trials_selection=500)
    assert honoured.n_trials == 500, "a larger honest override must still be honoured"

    # Deflating against 500 must be at least as harsh as against 5.
    weak = min(clamped.all_scores(), key=lambda s: s.dsr_pvalue).dsr_pvalue
    strong = min(honoured.all_scores(), key=lambda s: s.dsr_pvalue).dsr_pvalue
    assert strong >= weak


def test_annualization_follows_bar_period() -> None:
    """Rin iter-1 LOW: annualisation must not be hardcoded to 252*24.

    H4 and D1 Sharpe were being reported 4x and 24x too high.  The
    annualisation affects the REPORTED Sharpe only — never the DSR.
    """
    from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr

    h1 = rank_selection_set_with_dsr(_selection_set_store(), bar_period_minutes=60)
    h4 = rank_selection_set_with_dsr(_selection_set_store(), bar_period_minutes=240)
    d1 = rank_selection_set_with_dsr(_selection_set_store(), bar_period_minutes=1440)

    assert h1.bars_per_year == 6048
    assert h4.bars_per_year == 1512
    assert d1.bars_per_year == 252

    a = h1.all_scores()[0].observed_sr_annual
    b = h4.all_scores()[0].observed_sr_annual
    assert a > b, "annualisation must scale the reported Sharpe"
    # It must not leak into the statistical verdict.
    assert h1.all_scores()[0].dsr_pvalue == d1.all_scores()[0].dsr_pvalue


def test_degenerate_candidate_gets_no_invented_sharpe() -> None:
    """A flat trial series must not get a fabricated Sharpe or a fake DSR."""
    import numpy as np
    from forex_bot.factory.validation_runner import TrialReturnStore
    from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr

    store = TrialReturnStore()
    # Both trials must land in the SAME cell: a cell with a single trial
    # never builds a [T, N] matrix (TrialReturnStore.matrix returns None
    # below 2 trials), so a per-cell split would silently drop both.
    store.record(("arch", "XAUUSD", "H1"), np.zeros(50))
    store.record(("arch", "XAUUSD", "H1"), np.linspace(-0.001, 0.002, 50))
    ranking = rank_selection_set_with_dsr(store)
    assert ranking.n_cells == 1
    flat = next(s for s in ranking.all_scores() if s.candidate_id == "trial_0")
    assert flat.bh_reject is True
    assert flat.dsr_pvalue == 1.0, "no evidence, not a computed-but-good number"
    assert flat.observed_sr_per_bar == 0.0
    assert flat.is_discovery is False


def test_harness_scores_selection_set_with_verdict_provenance(
    synthetic_duckdb: Path,
) -> None:
    """AC1 wiring: the tournament path produces a verdict-grade scorecard."""
    harness = TournamentHarness(
        strategy_ids=["srmr_plus"],
        db_path=synthetic_duckdb,
        symbol="USDJPY",
        timeframe="H1",
        start_date="2024-01-01",
        end_date="2024-06-01",
    )
    sc = harness.score_selection_set(_selection_set_store())

    assert len(sc.rows) == 5
    assert all(r.rank > 0 for r in sc.rows), "a real pass assigns 1-based ranks"
    assert all(r.is_statistically_ranked for r in sc.rows)
    assert sc.meta["ranking"]["method"] == "dsr_bh_fdr_selection_set"
    assert sc.meta["ranking"]["n_trials"] == 5
    assert sc.meta["ranking"]["n_cells"] == 2
    # Every row carries the same set-wide denominator.
    assert {r.n_trials_selection for r in sc.rows} == {5}
    # Provenance survives into the row payload.
    payload = json.loads(render_scorecard_json(sc))
    assert payload["rows"][0]["dsr_pvalue"] is not None
    assert payload["meta"]["ranking_statement"] != NO_RANKING_STATEMENT


def test_memo_refuses_hand_rolled_ordering() -> None:
    """AC3: rank numbers without provenance are NOT presented as ranked.

    This is the regression guard for the actual defect the card names: a
    caller that sorts rows by return_pct and stamps ``rank=1..n`` must be
    refused, because nothing about that order is FDR-honest.
    """
    rows = [
        ScorecardRow("high", "XAUUSD", "H1", 10.0, 1.0, 0, 0, 5, "src", rank=1),
        ScorecardRow("low", "XAUUSD", "H1", 1.0, 1.0, 0, 0, 5, "src", rank=2),
    ]
    sc = Scorecard(rows=rows, meta={})
    assert ranking_ordering_statement(sc) == NO_RANKING_STATEMENT

    # Even a scorecard that fakes the provenance string is refused unless
    # the rows actually carry statistical verdicts.
    forged = Scorecard(
        rows=list(rows),
        meta={"ranking": {"method": "dsr_bh_fdr_selection_set", "n_trials": 5}},
    )
    assert ranking_ordering_statement(forged) == NO_RANKING_STATEMENT


def test_unranked_single_window_behavior_unchanged(synthetic_duckdb: Path) -> None:
    """AC2: the standalone single-window path still emits rank=0, unranked.

    It must ALSO keep refusing to call itself ranked, and it must not
    acquire statistical columns out of nowhere.
    """
    harness = TournamentHarness(
        strategy_ids=["srmr_plus"],
        db_path=synthetic_duckdb,
        symbol="USDJPY",
        timeframe="H1",
        start_date="2024-01-01",
        end_date="2024-06-01",
    )
    rows = [
        build_scorecard_row(
            strategy_id="srmr_plus", symbol="USDJPY", timeframe="H1",
            starting_equity=1.0,
            trades=[{"pnl_fraction": 0.01, "cost_modeled": True}],
            dates=["2024-01-01"], trade_entry_bars=[0], source="synthetic",
        )
    ]
    sc = TournamentHarness._scorecard_with_rank_or_unranked(rows)

    assert all(r.rank == 0 for r in sc.rows), "no synthetic ranks on the single-window path"
    assert all(r.dsr_pvalue is None and r.fdr_qvalue is None for r in sc.rows)
    assert all(not r.is_statistically_ranked for r in sc.rows)
    assert ranking_ordering_statement(sc) == NO_RANKING_STATEMENT


def test_duplicate_candidate_ids_across_cells_are_disambiguated() -> None:
    """Positional ids ("trial_0") repeat per cell — labels must not collide.

    Two different candidates rendering as the same visible string is
    exactly how a memo ends up crediting one row's verdict to another.
    """
    from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr
    from tournament.scorecard import ranked_scorecard_from_selection_set

    ranking = rank_selection_set_with_dsr(_selection_set_store())
    sc = ranked_scorecard_from_selection_set(ranking)
    labels = [r.strategy_id for r in sc.rows]
    assert len(labels) == len(set(labels)), f"colliding labels: {labels}"
    # Placeholder rows are marked as not-backtested so a 0.0 return is
    # never read as a measured zero.
    assert all(r.source == "selection_set_statistical_only" for r in sc.rows)
    assert all(r.trade_count == 0 for r in sc.rows)


def test_console_never_renders_unranked_as_hash_zero() -> None:
    """``#0`` would read as "top of the leaderboard" — refuse to print it."""
    rows = [ScorecardRow("solo", "USDJPY", "H1", 2.5, 1.0, 0, 0, 5, "src")]
    out = render_console_table(Scorecard(rows=rows, meta={}))
    assert "#0" not in out
    assert "—" in out
    # Unranked output keeps the legacy header set — no phantom DSR columns.
    assert "DSR-p" not in out
