"""Regression tests for the tournament root fix (card 7752bf19).

Two root causes produced 17/17 strategy failures since 2026-09-21:

  1. **pytz missing dep.**  Strategies that transitively import
     ``signal_engine.session_logic`` / ``.pattern_detector`` /
     ``.tp_manager`` (which all do ``import pytz``) fail at module
     import time when pytz is absent from the runner's venv.  This
     cascades into 17/17 silent-skips because the tournament harness
     resolves every ``STRATEGY_CLASS_MAP`` entry through
     ``importlib.import_module``.

  2. **strategy-init incompat.**  Pre-9e9aaf30, the harness called
     ``cls(config=None)`` for every strategy.  5 of 17 strategies
     (momentum trio, session_range_mr_ict_filtered, ttc_xauusd)
     reject ``config=`` and raised ``TypeError``; 7 of 17 lacked
     ``initialize()`` and would raise ``AttributeError`` downstream.
     Adapter now inspects ``__init__`` signature and guards
     ``initialize``/``shutdown`` with ``hasattr``.

These three tests assert:

  1. **test_pytz_importable_in_runner_context** — pytz is importable
     from the same sys.path the runner prepends.  Catches "pytz
     missing" regression.
  2. **test_strategies_with_pytz_transitive_imports_resolve** —
     every strategy module that transitively imports pytz is
     importable and the strategy class is constructable.  Catches a
     pytz absence that only manifests at import time.
  3. **test_all_17_strategies_run_no_exception_scorecard_nonempty** —
     full-roster tournament run on a synthetic H1 window completes
     without exceptions and produces ≥1 scorecard row.  Catches a
     regression where the runner exits 1 with empty scorecard (the
     17/17 fail mode).

Run::

    python3 -m pytest tests/tournament/test_tournament_root_fix.py -q

Card 7752bf19 (sprint-c, wave 0.1); ACs: pytz in runtime venv;
adapter resolves all 17 STRATEGY_CLASS_MAP entries; full-roster run
exits 0 with non-empty scorecard.
"""

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import sys
from pathlib import Path

import duckdb
import pytest

# Mimic run_tournament.py's sys.path prepend so we exercise the same
# import context the runner uses (both src/ and src/forex_bot on path).
_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO / "src"), str(_REPO / "src" / "forex_bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tournament import STRATEGY_CLASS_MAP, TournamentHarness  # noqa: E402
from tournament.harness import _build_strategy_instance  # noqa: E402


# ── Synthetic H1 DuckDB fixture (mirrors test_harness.py) ────────────────────


def _make_synthetic_duckdb(db_path: Path, *, n_bars: int = 120) -> None:
    """120-bar H1 window — past the 30-bar warm-up; enough to exercise
    most strategies' entry conditions.  Mirrors test_harness.py's
    pattern so the two test modules stay consistent.
    """
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE bars ("
            "symbol VARCHAR, timeframe VARCHAR, timestamp_utc BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
            "volume BIGINT, spread_pips DOUBLE)"
        )
        base = int(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp())
        rows = []
        price = 2000.0  # XAUUSD-ish level so ATR/RSI gates have room
        for i in range(n_bars):
            wave = (i % 14) - 7
            price = price + wave * 0.30
            o = price
            c = price + wave * 0.15
            h = max(o, c) + 0.50
            el = min(o, c) - 0.50
            rows.append(("XAUUSD", "H1", base + i * 3600, o, h, el, c, 1000, 2.5))
        con.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    finally:
        con.close()


@pytest.fixture()
def synthetic_duckdb(tmp_path: Path) -> Path:
    db = tmp_path / "tournament_root_fix.duckdb"
    _make_synthetic_duckdb(db)
    return db


# ── 1. pytz is importable from the runner context ──────────────────────────


def test_pytz_importable_in_runner_context() -> None:
    """pytz must be importable from the same sys.path the runner uses.

    Card 7752bf19 root cause #1: pytz was missing from the tournament
    runner's venv, so every strategy module transitively importing
    pytz via ``signal_engine`` raised ``ModuleNotFoundError`` at
    import time — the harness's ``except Exception`` swallowed the
    error and produced ``skipped: True`` for all 17 strategies.

    This test pins pytz as a required runtime dep at the same path
    the runner prepends.  If a future refactor drops pytz from
    requirements.txt or removes the prepend, this fails fast.
    """
    # Sanity: this test must run with src + src/forex_bot on path,
    # just like scripts/run_tournament.py.
    src_on_path = any(str(_REPO / "src") in p for p in sys.path)
    forexbot_on_path = any(str(_REPO / "src" / "forex_bot") in p for p in sys.path)
    assert src_on_path, f"src/ not on sys.path (run from repo root): {sys.path[:5]}..."
    assert forexbot_on_path, f"src/forex_bot not on sys.path: {sys.path[:5]}..."

    # pytz is the package-level import; transitive strategies import
    # ``import pytz`` directly.  Pinning the package-level import
    # covers both the explicit and transitive failure modes.
    pytz = importlib.import_module("pytz")
    assert hasattr(pytz, "timezone"), "pytz module missing timezone factory"
    assert hasattr(pytz, "utc"), "pytz module missing utc singleton"
    # Build the canonical NY/ET tz used by signal_engine.* code paths.
    ny = pytz.timezone("America/New_York")
    assert ny is not None


# ── 2. pytz transitive imports succeed for every strategy module ────────────


def test_strategies_with_pytz_transitive_imports_resolve() -> None:
    """Every strategy module that transitively imports pytz is importable.

    Some strategies directly import pytz (e.g. via signal_engine
    submodules used in entry filters); others import via
    ``strategies.registry``.  The harness adapter resolves every
    STRATEGY_CLASS_MAP entry through ``importlib.import_module`` —
    so if any one module fails, the whole tournament exits with
    zero rows.  This test exercises every id and asserts the
    strategy class is constructable (regression for root cause #2).
    """
    # First the import-target signal: strategies package must load,
    # which transitively exercises pytz imports across the
    # strategies/ tree.
    strategies_pkg = importlib.import_module("strategies")
    assert hasattr(strategies_pkg, "default_registry"), (
        "strategies package failed to import — likely a transitive "
        "pytz ImportError or a circular-import regression."
    )

    # Then per-strategy class constructability via the existing
    # adapter (root cause #2 guard; mirrors
    # test_harness_adapter_resolution.py::test_all_17_strategies_construct_via_adapter).
    for sid in STRATEGY_CLASS_MAP:
        target = STRATEGY_CLASS_MAP[sid]
        mod_path, cls_name = target.split(":", 1)
        try:
            module = importlib.import_module(mod_path)
        except ImportError as exc:
            pytest.fail(
                f"strategy {sid}: importlib.import_module({mod_path!r}) "
                f"raised ImportError — pytz missing dep regression? {exc}"
            )
        cls = getattr(module, cls_name, None)
        assert cls is not None, (
            f"strategy {sid}: {target} does not resolve to a class"
        )

        # Adapter must produce a non-None instance.
        instance = _build_strategy_instance(sid)
        assert instance is not None, (
            f"strategy {sid}: harness adapter returned None — "
            f"strategy-init incompat regression?"
        )
        assert hasattr(instance, "evaluate"), (
            f"strategy {sid}: instance missing evaluate() — not a strategy"
        )


# ── 3. full-roster run exits 0 with a non-empty scorecard ──────────────────


def test_all_17_strategies_run_no_exception_scorecard_nonempty(
    synthetic_duckdb: Path,
) -> None:
    """End-to-end regression: full-roster tournament on a synthetic
    window completes without exceptions and produces ≥1 scorecard row.

    Card 7752bf19 root cause mode: pre-fix, all 17 strategies errored
    at init or import; the harness's ``except Exception`` swallowed
    each error into ``skipped: True``; the CLI's empty-rows guard
    then exited 1 with FATAL — the canonical 17/17 failure mode.

    Post-fix, the harness adapter constructs every strategy, signal
    extraction walks bars, and at least the per-strategy session
    filters + RSI/ADX gates should let ≥1 strategy fire ≥1 signal
    on the 120-bar synthetic XAUUSD H1 window.

    This test asserts:
      - harness.run() returns a Scorecard (no raise)
      - scorecard.rows is non-empty (regression for empty-scorecard mode)
      - run_meta covers every STRATEGY_CLASS_MAP entry (no silent
        failures introduced since 9e9aaf30)

    Skipped strategies (zero signals on this window) are tolerated —
    those reflect per-strategy entry-condition strictness, not the
    pytz/init failure modes this card targets.  Separate cards cover
    the zero-signal strategies (out of scope per card title).
    """
    strategy_ids = sorted(STRATEGY_CLASS_MAP.keys())
    assert len(strategy_ids) == 17, (
        f"STRATEGY_CLASS_MAP drift: expected 17 ids, got {len(strategy_ids)} "
        f"({strategy_ids})"
    )

    harness = TournamentHarness(
        strategy_ids=strategy_ids,
        db_path=synthetic_duckdb,
        symbol="XAUUSD",
        timeframe="H1",
        start_date="2024-01-01",
        end_date="2024-01-05",  # 120 bars fit inside this 5-day window
    )

    scorecard = harness.run()  # must NOT raise

    # Functional regression: empty scorecard is the 17/17 fail mode.
    assert scorecard.rows, (
        f"Tournament produced 0 scorecard rows — 17/17 fail mode regression. "
        f"run_meta={scorecard.meta.get('run_meta', {})}"
    )
    # Per-strategy run_meta: every id accounted for (no silent swallow).
    run_meta = scorecard.meta.get("run_meta", {})
    missing = [sid for sid in strategy_ids if sid not in run_meta]
    assert not missing, (
        f"run_meta missing entries for {missing} — harness swallowed "
        f"exceptions without recording skip reason"
    )