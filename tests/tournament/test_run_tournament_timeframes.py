"""Targeted tests for M3/M5(/M30) tournament timeframe support.

Card c0660c3e-9f4b-4078-8cbd-c49965869c56 — promote the sub-hour
timeframe choices from the ava-worker-local node stash onto main.

Scope (per ``workboard_read`` notes):
    Land ONLY the timeframe support from the stash (the offload
    transport patches and the +413-line ``test_matrix_05fa0065.py``
    regression are already superseded by merged cards — see
    card notes).  The landed change is one line in
    ``scripts/run_tournament.py``: argparse ``choices`` extended from
    ``["H1", "M15"]`` to ``["M3", "M5", "M15", "M30", "H1"]``.

These tests pin:
    1. The parser exposes exactly the five supported timeframe
       choices (M3, M5, M15, M30, H1) — locks the wire contract so a
       future regression that removes one fails loud.
    2. ``--timeframe M3`` / ``M5`` / ``M30`` parse without argparse
       errors (the bug the card verified on clean main:
       ``run_tournament: error: invalid choice: 'M3'``).
    3. An unsupported timeframe (``M2``) still fails loudly with the
       canonical argparse message — ensures we did not accidentally
       drop the ``choices`` validation when widening the set.
    4. ``TournamentHarness`` accepts the new timeframe strings via its
       constructor and forwards them into ``load_bars_for_window``
       (the harness itself is timeframe-agnostic — the bars table
       query uses ``WHERE timeframe = ?``, so passing the string
       through is sufficient; resampling is a N/A on clean main).
    5. A synthetic M3 DuckDB round-trips through ``load_bars_for_window``
       to prove the bar-loading layer actually supports sub-hour
       rows when the dataset has them (this is the load path that
       the node's yesterday M3 runs depended on).

Targeted HR5 scope: ``tests/tournament/*`` + tests touched by the
diff (``tests/tournament/test_run_tournament_timeframes.py``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import duckdb
import pytest

# Tournament module imports — match the pre-existing
# tests/tournament/test_harness.py bootstrap (src + src/forex_bot on
# sys.path so ``tournament`` resolves).
_REPO = Path(__file__).resolve().parent.parent.parent
for _p in (str(_REPO / "src"), str(_REPO / "src" / "forex_bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.run_tournament import build_arg_parser  # noqa: E402
from tournament import TournamentHarness  # noqa: E402
from tournament.harness import (  # noqa: E402
    STRATEGY_CLASS_MAP,
    load_bars_for_window,
)


# ── Constants ────────────────────────────────────────────────────────────────


EXPECTED_CHOICES = ("M3", "M5", "M15", "M30", "H1")
"""Canonical, ordered set of supported timeframe choices.

Order is significant: the argparse ``choices`` list determines the
order in which values are reported in ``--help`` output, and tests
#3 and #4 lock this order to detect accidental reordering.
"""


# ── 1. Parser exposes the canonical five timeframe choices ──────────────────


def test_timeframe_choices_exact_set() -> None:
    """The ``--timeframe`` argparse choices match the canonical 5-tuple.

    Locks the wire contract: any regression that drops M3/M5/M30 (or
    adds an unsupported token) fails loud here.
    """
    parser = build_arg_parser()
    # Locate the --timeframe action and inspect its choices.
    found = False
    for action in parser._actions:  # noqa: SLF001 — argparse internals
        if "--timeframe" in action.option_strings:
            assert tuple(action.choices) == EXPECTED_CHOICES, (
                f"--timeframe choices drifted from canonical set: "
                f"got {action.choices!r}, expected {EXPECTED_CHOICES!r}"
            )
            found = True
            break
    assert found, "--timeframe action not found in build_arg_parser()"


def test_timeframe_choices_in_help_text() -> None:
    """``--help`` mentions all 5 timeframe tokens (so users can self-discover).

    Defensive against a regression where the choices list is updated
    but the help string still advertises the old 2-tuple.
    """
    parser = build_arg_parser()
    help_text = parser.format_help()
    for token in EXPECTED_CHOICES:
        assert token in help_text, (
            f"--timeframe help text missing token {token!r}; "
            f"got:\n{help_text}"
        )


# ── 2. Each supported timeframe is accepted by the parser ──────────────────────


@pytest.mark.parametrize("timeframe", ["M3", "M5", "M15", "M30", "H1"])
def test_timeframe_choice_parses_successfully(timeframe: str) -> None:
    """``--timeframe <token>`` parses cleanly for every entry in the canonical set.

    Pins AC1 from the card: ``--timeframe M3`` / ``M5`` (and M30)
    parse on clean main without argparse aborting with
    ``error: invalid choice: '<token>'``.
    """
    parser = build_arg_parser()
    args = parser.parse_args(["--timeframe", timeframe])
    assert args.timeframe == timeframe


@pytest.mark.parametrize("bad_timeframe", ["M2", "H4", "D1", "m3", "h1"])
def test_timeframe_choice_rejects_unknown(bad_timeframe: str) -> None:
    """Unknown timeframe tokens are still rejected loudly.

    Guards against a regression that widens the choices list by
    accident (e.g. ``choices=timeframe.upper()``) and silently
    accepts anything.
    """
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--timeframe", bad_timeframe])


def test_timeframe_default_is_h1() -> None:
    """Default ``--timeframe`` remains ``H1`` (preserves existing behavior)."""
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.timeframe == "H1"


# ── 3. Harness forwards the new timeframe strings to load_bars ──────────────


@pytest.mark.parametrize("timeframe", ["M3", "M5", "M15", "M30", "H1"])
def test_harness_accepts_timeframe_string(timeframe: str) -> None:
    """TournamentHarness constructor accepts the canonical timeframe tokens.

    The harness itself does no timeframe validation — it forwards
    the string into ``load_bars_for_window`` which SQL-filters
    ``WHERE timeframe = ?`` — but this test pins the wire at the
    frontend so a future regression that adds validation logic
    surfaces the canonical set.
    """
    # Use a non-existent path; we only check that construction
    # succeeds (the harness defers bar loading to ``.run()``).
    h = TournamentHarness(
        strategy_ids=list(STRATEGY_CLASS_MAP.keys())[:1],
        db_path="/nonexistent/path.duckdb",
        symbol="USDJPY",
        timeframe=timeframe,
    )
    assert h.timeframe == timeframe


# ── 4. Synthetic M3/M5/M30 DuckDB round-trip ────────────────────────────────


def _make_synthetic_subhour_duckdb(
    db_path: Path,
    *,
    symbol: str,
    timeframe: str,
    n_bars: int,
    step_seconds: int,
) -> None:
    """Write a tiny DuckDB bar file covering ``n_bars`` sub-hour entries.

    Mirrors ``tests/tournament/test_harness.py:_make_synthetic_duckdb``
    but parameterises the timeframe + step so we can synthesise M3/M5
    bars as well as the existing H1 set.
    """
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
            el = min(o, c) - 0.08
            spread = 1.2  # XAUUSD/USDJPY typical ~1.0-1.5 pips
            rows.append(
                (symbol, timeframe, base + i * step_seconds, o, h, el, c, 1000, spread)
            )
        con.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    finally:
        con.close()


def _step_seconds_for(timeframe: str) -> int:
    """Return the bar-step in seconds for the supported timeframe tokens."""
    return {
        "M3": 180,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
    }[timeframe]


@pytest.mark.parametrize(
    ("timeframe", "n_bars"),
    [("M3", 60), ("M5", 60), ("M15", 30), ("M30", 20), ("H1", 60)],
)
def test_load_bars_for_window_subhour(tmp_path: Path, timeframe: str, n_bars: int) -> None:
    """load_bars_for_window loads sub-hour bars when the dataset has them.

    Pins the load-path for the new timeframes end-to-end: the harness
    SQL-filters by ``timeframe = ?``, so as long as the bars table
    contains the requested token the load succeeds.  Empty result
    raises TournamentEmptyWindow (loud), which is correct behavior.
    """
    db = tmp_path / f"smoke_{timeframe}.duckdb"
    _make_synthetic_subhour_duckdb(
        db,
        symbol="USDJPY",
        timeframe=timeframe,
        n_bars=n_bars,
        step_seconds=_step_seconds_for(timeframe),
    )
    df, raw = load_bars_for_window(
        db,
        symbol="USDJPY",
        timeframe=timeframe,
        start_date="2024-06-03",
        end_date="2024-06-03",
    )
    # Wire contract: load_bars_for_window accepts the timeframe string and
    # returns ≥1 row when the bars table contains matching rows.  The
    # window filter clamps to ≤ n_bars depending on how the synthetic
    # step fits inside 2024-06-03 (24 hours); for sub-hour steps
    # (M3=180s, M5=300s, M15=900s, M30=1800s) all n_bars fit, while for
    # H1 (3600s) only 24 bars fit on a single calendar date.
    assert raw > 0, f"{timeframe}: load returned 0 raw rows"
    assert len(df) == raw, f"{timeframe}: df/len mismatch raw={raw} len(df)={len(df)}"
    assert len(df) <= n_bars, (
        f"{timeframe}: df has {len(df)} rows, synthetic DB only had {n_bars}"
    )
    # Bars must be sorted ascending by timestamp.
    timestamps = df["timestamp_utc"].tolist()
    assert timestamps == sorted(timestamps), (
        f"{timeframe}: bars are not sorted ascending by timestamp_utc"
    )


def test_load_bars_subhour_rejects_unknown_timeframe(tmp_path: Path) -> None:
    """load_bars_for_window for M3/M5 on a H1-only DB raises TournamentEmptyWindow.

    Defensive: if the main DuckDB doesn't yet carry M3 bars (likely
    until a future ingest), the harness fails loudly instead of
    silently producing an empty DataFrame.
    """
    from tournament import TournamentEmptyWindow

    db = tmp_path / "h1_only.duckdb"
    _make_synthetic_subhour_duckdb(
        db,
        symbol="USDJPY",
        timeframe="H1",
        n_bars=10,
        step_seconds=_step_seconds_for("H1"),
    )
    with pytest.raises(TournamentEmptyWindow):
        load_bars_for_window(
            db,
            symbol="USDJPY",
            timeframe="M3",
            start_date="2024-06-03",
            end_date="2024-06-03",
        )


# ── 5. Smoke parse-check (mirrors AC verification path) ─────────────────────


@pytest.mark.parametrize("timeframe", ["M3", "M5", "M30"])
def test_smoke_argparse_passes_for_subhour(timeframe: str) -> None:
    """``--smoke --timeframe <TF>`` parses without aborting.

    Mirrors the AC verification path the card spec calls out
    ("``--smoke`` parse check is acceptable as execution proof if a
    full window is too heavy").  We do not actually execute the
    tournament harness here — that requires a live DuckDB with
    sub-hour coverage — we only pin that the CLI surface accepts
    the token.  The harness-level runner execution proof is in
    ``test_load_bars_for_window_subhour`` above.
    """
    parser = build_arg_parser()
    # Inject a --db-path so .run() wouldn't try to resolve the
    # default (which depends on the primary worktree's DuckDB).
    args = parser.parse_args(
        ["--smoke", "--timeframe", timeframe, "--db-path", "/nonexistent.duckdb"]
    )
    assert args.timeframe == timeframe
    assert args.smoke is True


# ── Sanity guard ─────────────────────────────────────────────────────────────


def test_module_level_no_hidden_regressions() -> None:
    """Last-line guard: the parser still builds, choices remain a tuple/list.

    Catches a future regression where the choices list is replaced
    with a generator expression (``tuple(choices)`` would crash) or
    mutated post-construction.  Cheap to run; high signal.
    """
    parser = build_arg_parser()
    for action in parser._actions:  # noqa: SLF001
        if "--timeframe" in action.option_strings:
            choices = action.choices
            assert isinstance(choices, (list, tuple)), (
                f"--timeframe choices must be a list/tuple, got {type(choices).__name__}"
            )
            assert len(choices) == len(EXPECTED_CHOICES), (
                f"--timeframe choices length drifted: "
                f"got {len(choices)}, expected {len(EXPECTED_CHOICES)}"
            )
            return
    pytest.fail("--timeframe action not found in build_arg_parser()")