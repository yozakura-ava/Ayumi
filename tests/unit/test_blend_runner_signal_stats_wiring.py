"""Regression tests for BlendForwardTestRunner signal_stats wiring (card dcc7817d).

Background
----------
Forward-test runs in ``blend_mode=True`` (production launcher at
``scripts/launch_blend_forward_test.py:1430``). In blend mode,
``cTraderSignalAdapter.evaluate_and_trade()`` short-circuits at
``signal_adapter.py:206-208`` and returns the trade signal WITHOUT
calling ``PaperTrader.process_signal()``. Because PaperTrader was the
only call site of ``SignalStatsRecorder.record_signal``, the JSONL
file ``data/signal_stats.jsonl`` went silent — 22h+ with no appends
despite 15 strategy signals being produced (B5 health line: signals=15
across eval #57–#88 on Aug 19 12:15 → 20:00).

Fix: wire a ``SignalStatsRecorder`` directly into ``BlendForwardTestRunner``
and call ``record_signal`` / ``record_rejection`` from ``on_signal`` and
``record_outcome`` from ``on_fill``. This mirrors the PaperTrader wiring
in the non-blend path.

These tests lock the contract:
1. ``_get_stats_recorder`` is exposed and lazy-initialises a recorder
   against ``stats_log_path`` (config or env override).
2. ``on_signal`` writes the open row to the JSONL log when the order is
   accepted (sustained flow, monkeypatched clock >3 bars).
3. ``on_signal`` writes the rejection row when the orchestrator rejects.
4. ``on_fill`` writes the close row.
5. The recorder survives a simulated process restart: closing the first
   recorder and instantiating a fresh ``SignalStatsRecorder`` against the
   same ``log_path`` appends cleanly, line count strictly grows, all lines
   parse, no tail loss.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

WORKSPACE = Path("$AYUMI_ROOT")
# Tests run from the worktree directory (autodev/sigstats-dcc7817d).
# The harness sets cwd to the worktree, so use Path.cwd() to find the
# local src/ for the AST check below.
WORKTREE = Path.cwd()
SRC = WORKTREE / "src" / "forex_bot"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from forward_test.blend_runner import BlendForwardTestRunner  # noqa: E402, I001
from signal_engine.signal_stats import SignalStatsRecorder  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_runner(log_path: str) -> BlendForwardTestRunner:
    """Construct a BlendForwardTestRunner with the JSONL pointed at a
    per-test temp directory. Skips ``start()`` so we don't restore sizer
    state from disk.
    """
    return BlendForwardTestRunner(
        config={
            "account_balance": 10_000.0,
            "risk_per_trade_pct": 0.005,
            "daily_risk_cap_pct": 0.03,
            "spread_pips": {},
            "state_path": "/dev/null",  # avoid touching real risk_state.json
            "stats_log_path": log_path,
        }
    )


def _make_signal_data(
    strategy_id: str = "srmr_plus",
    symbol: str = "XAUUSD",
    direction: str = "LONG",
    confidence: float = 0.65,
    timestamp: datetime | None = None,
) -> dict:
    """Build the dict shape BlendForwardTestRunner.on_signal expects.

    The runner routes through ``StrategyAdapter.adapt_signal`` which
    extracts these fields from the dict and constructs an
    ``OrchestratorTradeSignal``.
    """
    return {
        "strategy_id": strategy_id,
        "symbol": symbol,
        "direction": direction,
        "entry_price": 2000.0,
        "stop_loss": 1995.0,
        "take_profit": 2010.0,
        "confidence": confidence,
        "spread": 0.5,
        "timestamp": (timestamp or datetime(2026, 8, 19, 12, 15, 0, tzinfo=timezone.utc)),
    }


# ---------------------------------------------------------------------------
# 1. _get_stats_recorder is exposed and lazy-initialises
# ---------------------------------------------------------------------------


def test_blend_runner_exposes_get_stats_recorder():
    """_get_stats_recorder() must exist and return a SignalStatsRecorder."""
    runner = _build_runner(log_path="/tmp/_unused_signal_stats_wiring.jsonl")  # noqa: S108
    recorder = runner._get_stats_recorder()
    assert isinstance(recorder, SignalStatsRecorder)
    assert recorder.log_path == "/tmp/_unused_signal_stats_wiring.jsonl"  # noqa: S108


def test_blend_runner_stats_recorder_is_lazy(tmp_path):
    """The recorder must NOT be instantiated at __init__ time — only on
    first use — so unit tests that never call on_signal() don't leave a
    file on disk.
    """
    log_path = str(tmp_path / "lazy.jsonl")
    runner = _build_runner(log_path=log_path)
    assert runner._stats_recorder is None, (
        "SignalStatsRecorder must be lazy; constructing it at __init__ "
        "creates a half-initialised file on disk for tests that never "
        "invoke on_signal()."
    )
    assert not Path(log_path).exists()


def test_blend_runner_stats_log_path_overrides_via_config(tmp_path):
    """config['stats_log_path'] wins over the default."""
    log_path = str(tmp_path / "override.jsonl")
    runner = _build_runner(log_path=log_path)
    assert runner._stats_log_path == log_path


def test_blend_runner_stats_log_path_falls_back_to_env(tmp_path, monkeypatch):
    """STATS_LOG_PATH env var wins over the default when config lacks it."""
    log_path = str(tmp_path / "env.jsonl")
    monkeypatch.setenv("STATS_LOG_PATH", log_path)
    runner = BlendForwardTestRunner(
        config={
            "account_balance": 10_000.0,
            "risk_per_trade_pct": 0.005,
            "daily_risk_cap_pct": 0.03,
            "spread_pips": {},
            "state_path": "/dev/null",
        }
    )
    assert runner._stats_log_path == log_path


def test_blend_runner_stats_log_path_default(tmp_path):
    """Default stats_log_path is data/signal_stats.jsonl when no override."""
    runner = BlendForwardTestRunner(
        config={
            "account_balance": 10_000.0,
            "risk_per_trade_pct": 0.005,
            "daily_risk_cap_pct": 0.03,
            "spread_pips": {},
            "state_path": "/dev/null",
        }
    )
    assert runner._stats_log_path == "data/signal_stats.jsonl"


# ---------------------------------------------------------------------------
# 2. on_signal writes the open row (sustained flow)
# ---------------------------------------------------------------------------


def test_on_signal_records_open_line_for_accepted_order(tmp_path):
    """on_signal() with an accepted order appends a JSONL row containing
    the canonical signal_id and metadata. Mirrors the
    PaperTrader.process_signal() write path.
    """
    log_path = str(tmp_path / "open.jsonl")
    runner = _build_runner(log_path=log_path)
    signal_data = _make_signal_data(strategy_id="srmr_plus")
    order = runner.on_signal(signal_data["strategy_id"], signal_data)

    assert order is not None
    assert not order.rejected, (
        "Test setup error: orchestrator should accept a sane XAUUSD LONG "
        f"signal but got rejection_reason={order.rejection_reason!r}"
    )

    # The JSONL file must exist with at least one row.
    assert Path(log_path).exists()
    rows = [json.loads(line) for line in Path(log_path).read_text().splitlines() if line.strip()]
    assert len(rows) == 1, (
        f"Expected exactly 1 open row, got {len(rows)} — the writer silence regression is back if this is 0."
    )
    row = rows[0]
    assert row["strategy"] == "srmr_plus"
    assert row["symbol"] == "XAUUSD"
    assert row["direction"] == "LONG"
    assert row["outcome"] == "open"
    assert row["confidence"] == pytest.approx(0.65, abs=1e-6)
    assert row["signal_id"].startswith("srmr_plus_")


def test_on_signal_records_open_line_for_sustained_flow(tmp_path):
    """Drive >3 signals through on_signal() (sustained flow) and assert
    every signal lands in the JSONL. Simulates a multi-bar evaluation
    cycle with the monkeypatched clock advancing past bar boundaries.
    """
    log_path = str(tmp_path / "sustained.jsonl")
    runner = _build_runner(log_path=log_path)

    # Five signals with monotonically-increasing timestamps to mimic
    # successive bar evaluations.
    _base = datetime(2026, 8, 19, 12, 15, 0, tzinfo=timezone.utc)
    expected_ids = []
    for i in range(5):
        signal_data = _make_signal_data(
            strategy_id="srmr_plus",
            timestamp=datetime(2026, 8, 19, 12, 15 + i, 0, tzinfo=timezone.utc),
        )
        order = runner.on_signal(signal_data["strategy_id"], signal_data)
        # Some evals may reject (regime/edge), but if accepted they must
        # appear in the JSONL.
        if not order.rejected:
            adapted = runner._adapter.adapt_signal(signal_data["strategy_id"], signal_data)
            expected_ids.append(runner.make_signal_id(adapted))

    rows = [json.loads(line) for line in Path(log_path).read_text().splitlines() if line.strip()]
    # At least 3 accepted (sustained flow floor).
    assert len(rows) >= 3, (
        f"Expected ≥3 JSONL rows from 5 on_signal() calls, got {len(rows)}. Writer silence is back if this is 0."
    )
    # Every accepted signal_id must appear as an open row.
    recorded_ids = {r["signal_id"] for r in rows}
    for sid in expected_ids:
        assert sid in recorded_ids, (
            f"Accepted signal_id={sid!r} not found in JSONL rows (recorded: {sorted(recorded_ids)})"
        )


# ---------------------------------------------------------------------------
# 3. on_signal writes the rejection row when the orchestrator rejects
# ---------------------------------------------------------------------------


def test_on_signal_records_rejection_line(tmp_path):
    """When the orchestrator rejects a signal, record_rejection must
    append a row with outcome='rejected' so the aggregator's
    rejection_rate is non-zero.
    """
    log_path = str(tmp_path / "reject.jsonl")
    runner = _build_runner(log_path=log_path)
    # Drive a signal with confidence=0.05 — well below the swarm threshold
    # of 0.40, so the orchestrator must reject.
    signal_data = _make_signal_data(strategy_id="srmr_plus", confidence=0.05)
    order = runner.on_signal(signal_data["strategy_id"], signal_data)
    assert order.rejected, (
        f"Test setup error: expected rejection at confidence=0.05 but got accepted: lots={order.lots}"
    )

    rows = [json.loads(line) for line in Path(log_path).read_text().splitlines() if line.strip()]
    assert len(rows) == 1, f"Expected exactly 1 rejection row, got {len(rows)}"
    row = rows[0]
    assert row["outcome"] == "rejected"
    assert row["rejection_reason"]  # non-empty


# ---------------------------------------------------------------------------
# 4. on_fill writes the close row
# ---------------------------------------------------------------------------


def test_on_fill_records_outcome_line(tmp_path):
    """After an accepted order is opened via on_signal, calling on_fill
    appends a close row with the close-time pnl outcome so the
    aggregator's hit_rate has data to consume.
    """
    log_path = str(tmp_path / "fill.jsonl")
    runner = _build_runner(log_path=log_path)
    signal_data = _make_signal_data(strategy_id="srmr_plus")
    order = runner.on_signal(signal_data["strategy_id"], signal_data)
    assert not order.rejected

    signal_id = runner.make_signal_id(runner._adapter.adapt_signal(signal_data["strategy_id"], signal_data))
    runner.on_fill(signal_id, fill_price=2005.0, pnl=5.0)

    rows = [json.loads(line) for line in Path(log_path).read_text().splitlines() if line.strip()]
    # Expect: open row (from on_signal) + close row (from on_fill) = 2 rows
    assert len(rows) == 2, f"Expected 2 rows (open + close), got {len(rows)}"

    open_row = rows[0]
    close_row = rows[1]
    assert open_row["outcome"] == "open"
    assert close_row["outcome"] == "tp_hit"  # pnl > 0
    assert close_row["signal_id"] == signal_id
    assert close_row["pips_realized"] == pytest.approx(5.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. Recorder survives a simulated process restart
# ---------------------------------------------------------------------------


def test_recorder_survives_simulated_restart_append_cleanly(tmp_path):
    """Close one SignalStatsRecorder, instantiate a fresh one against the
    same log_path, append cleanly. Line count strictly grows, all lines
    parse, no tail loss. Covers the "writer survives restart" AC.
    """
    log_path = str(tmp_path / "restart.jsonl")
    rec_a = SignalStatsRecorder(log_path=log_path)

    from signal_engine.signal_stats import SignalRecord

    # Process A: 3 records
    rec_a.record_signal(
        SignalRecord(
            signal_id="restart-A-1",
            timestamp="2026-08-19T12:15:00+00:00",
            strategy="srmr_plus",
            symbol="XAUUSD",
            direction="LONG",
            confidence=0.6,
        )
    )
    rec_a.record_signal(
        SignalRecord(
            signal_id="restart-A-2",
            timestamp="2026-08-19T12:30:00+00:00",
            strategy="srmr_plus",
            symbol="XAUUSD",
            direction="SHORT",
            confidence=0.55,
        )
    )
    rec_a.record_signal(
        SignalRecord(
            signal_id="restart-A-3",
            timestamp="2026-08-19T12:45:00+00:00",
            strategy="srmr_plus",
            symbol="XAUUSD",
            direction="LONG",
            confidence=0.7,
        )
    )

    # Simulate clean process exit. The recorder has no close() method;
    # each _append_line uses temp + os.replace, so no tail is buffered.
    lines_before = Path(log_path).read_text().splitlines()
    assert len(lines_before) == 3

    # Process B: instantiate a fresh recorder against the same path.
    rec_b = SignalStatsRecorder(log_path=log_path)
    rec_b.record_signal(
        SignalRecord(
            signal_id="restart-B-1",
            timestamp="2026-08-19T13:00:00+00:00",
            strategy="srmr_plus",
            symbol="XAUUSD",
            direction="LONG",
            confidence=0.65,
        )
    )

    lines_after = Path(log_path).read_text().splitlines()
    assert len(lines_after) == 4, (
        f"Expected 4 lines after restart-append, got {len(lines_after)}. The writer lost or truncated the tail."
    )

    # Every line must parse as JSON (no torn lines).
    parsed = [json.loads(line) for line in lines_after]
    ids_in_order = [r["signal_id"] for r in parsed]
    assert ids_in_order == [
        "restart-A-1",
        "restart-A-2",
        "restart-A-3",
        "restart-B-1",
    ], f"Restart-append must preserve all pre-existing rows in their original order; got {ids_in_order}"


# ---------------------------------------------------------------------------
# 6. AST check: on_signal calls record_signal / record_rejection, on_fill
#    calls record_outcome (defence-in-depth against regressions that drop
#    the recorder wiring).
# ---------------------------------------------------------------------------


def test_blend_runner_on_signal_calls_record_signal():
    """AST check: on_signal() must invoke record_signal (for accepted
    orders) and record_rejection (for rejected orders) on the
    SignalStatsRecorder. This is the regression sentinel for the
    writer-silence bug.
    """
    src = (SRC / "forward_test" / "blend_runner.py").read_text()
    tree = __import__("ast").parse(src)
    on_signal_node = None
    for node in __import__("ast").walk(tree):
        if isinstance(node, __import__("ast").FunctionDef) and node.name == "on_signal":
            on_signal_node = node
            break
    assert on_signal_node is not None

    method_calls = {
        n.func.attr
        for n in __import__("ast").walk(on_signal_node)
        if isinstance(n, __import__("ast").Call) and isinstance(n.func, __import__("ast").Attribute)
    }
    assert "record_signal" in method_calls, (
        "on_signal() must call record_signal on the recorder for accepted "
        "orders. Dropping this regresses card dcc7817d."
    )
    assert "record_rejection" in method_calls, (
        "on_signal() must call record_rejection on the recorder for "
        "rejected orders. Dropping this regresses card dcc7817d."
    )


def test_blend_runner_on_fill_calls_record_outcome():
    """AST check: on_fill() must invoke record_outcome on the recorder
    so the aggregator's hit_rate has close-row data.
    """
    src = (SRC / "forward_test" / "blend_runner.py").read_text()
    tree = __import__("ast").parse(src)
    on_fill_node = None
    for node in __import__("ast").walk(tree):
        if isinstance(node, __import__("ast").FunctionDef) and node.name == "on_fill":
            on_fill_node = node
            break
    assert on_fill_node is not None

    method_calls = {
        n.func.attr
        for n in __import__("ast").walk(on_fill_node)
        if isinstance(n, __import__("ast").Call) and isinstance(n.func, __import__("ast").Attribute)
    }
    assert "record_outcome" in method_calls, (
        "on_fill() must call record_outcome on the recorder for the close row. Dropping this regresses card dcc7817d."
    )  # noqa: W292
