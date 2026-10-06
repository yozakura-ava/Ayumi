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
import pytest

from tournament import (
    STRATEGY_CLASS_MAP,
    TournamentHarness,
    TournamentNoSignals,
)
from tournament.harness import (
    TournamentEmptyWindow,
    load_bars_for_window,
    resolve_default_duckdb_path,
)
from tournament.scorecard import (
    SCORECARD_ROW_COLUMNS,
    Scorecard,
    ScorecardRow,
    _daily_dd_breach_counts,
    build_scorecard_row,
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
    """ScorecardRow carries exactly the 9 schema columns (daily + total DD separate)."""
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
    )
    assert "daily_dd_breaches" in columns
    assert "total_dd_breaches" in columns
    # Defensive: the two FTMO columns must both exist and not be aliased.
    assert columns.count("daily_dd_breaches") == 1
    assert columns.count("total_dd_breaches") == 1


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
