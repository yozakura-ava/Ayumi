"""Tests for the HeartbeatRecorder added in card 9f427c3b.

Acceptance-criteria matrix (from card 9f427c3b-478a-49d4-a067-15e83814d480):

  AC2 — heartbeat emits at most once/hour with the required fields
        (utc_ts, signals_generated, regime_filtered, traded,
        window_start).
  AC3 — real-signal write path unchanged when signals DO pass
        (SignalStatsRecorder.record_signal still appends the canonical
        {signal_id, strategy, ...} row and is unaffected by heartbeat
        wiring; BlendForwardTestRunner keeps mixing the real-signal
        record/reject hook in the orchestrator branch).
  AC4 — heartbeat writer uses temp + os.replace (atomic write);
        heartbeat failure is a non-fatal warning and never blocks the
        signal path.

All file-system effects are isolated via ``tmp_path`` (pytest-provided);
the tests never read or write the production data/ tree. The
``_isolate_repo_data_writes`` autouse fixture in tests/conftest.py
provides a snapshot/compare guard against any accidental data/
pollution so unisolated writes would fail at teardown.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from forward_test import blend_runner
from signal_engine import signal_stats as ss_mod
from signal_engine.signal_stats import (
    HeartbeatRecorder,
    SignalRecord,
    SignalStatsRecorder,
)

# ---------------------------------------------------------------------------
# HeartbeatRecorder direct tests (AC2, AC4)
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic clock for heartbeat-throttle tests.

    Returns ``epoch_seconds`` from a manually-advanced timeline so we
    can drive the 3600s default throttle without sleeping.
    """

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


def _read_rows(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def test_heartbeat_emits_required_fields(tmp_path: Path) -> None:
    """AC2: first emit must include utc_ts, signals_generated,
    regime_filtered, traded, window_start with the correct types."""
    log = tmp_path / "signal_stats.heartbeat.jsonl"
    clock = _FakeClock()
    rec = HeartbeatRecorder(log_path=str(log), clock=clock)

    rec.record_signal_generated()
    rec.record_regime_filtered()
    rec.record_regime_filtered()
    rec.record_traded()

    emitted = rec.force_emit()
    assert emitted is True

    rows = _read_rows(log)
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == {
        "utc_ts",
        "signals_generated",
        "regime_filtered",
        "traded",
        "window_start",
    }
    assert isinstance(row["utc_ts"], str) and row["utc_ts"].endswith("Z")
    assert row["signals_generated"] == 1
    assert row["regime_filtered"] == 2
    assert row["traded"] == 1
    # window_start == utc_ts on the very first emit (no prior window)
    assert row["window_start"] == row["utc_ts"]


def test_heartbeat_throttles_to_one_per_window(tmp_path: Path) -> None:
    """AC2: heartbeat emits at most once per throttle window.

    Design: the very first maybe_emit() fires immediately (so the
    heartbeat sidecar reflects "alive" without waiting an hour); every
    subsequent call inside the throttle window is silent; once the
    window elapses the next call fires again.
    """
    log = tmp_path / "signal_stats.heartbeat.jsonl"
    clock = _FakeClock()
    rec = HeartbeatRecorder(
        log_path=str(log),
        throttle_seconds=3600.0,
        clock=clock,
    )

    # First call after construction: first emit fires immediately.
    assert rec.maybe_emit() is True
    rows = _read_rows(log)
    assert len(rows) == 1

    # Inside the same window, additional maybe_emit() calls are silent.
    clock.advance(3599.0)
    assert rec.maybe_emit() is False
    assert len(_read_rows(log)) == 1

    # Cross the threshold — next emit fires.
    clock.advance(1.0)
    assert rec.maybe_emit() is True
    assert len(_read_rows(log)) == 2

    # Inside the new window, additional calls remain silent.
    for _ in range(10):
        assert rec.maybe_emit() is False
    assert len(_read_rows(log)) == 2

    # After another full window elapses, the next maybe_emit() fires.
    clock.advance(3600.0)
    assert rec.maybe_emit() is True
    assert len(_read_rows(log)) == 3


def test_heartbeat_rolling_window_resets_counters(tmp_path: Path) -> None:
    """AC2: counters reset after each emit (rolling window semantics)."""
    log = tmp_path / "signal_stats.heartbeat.jsonl"
    clock = _FakeClock()
    rec = HeartbeatRecorder(
        log_path=str(log),
        throttle_seconds=10.0,
        clock=clock,
    )

    rec.record_signal_generated()
    rec.record_signal_generated()
    rec.record_regime_filtered()
    rec.record_traded()

    clock.advance(11.0)
    assert rec.maybe_emit() is True

    # Counters reset.
    snap = rec.counters_snapshot()
    assert snap == {"signals_generated": 0, "regime_filtered": 0, "traded": 0}

    # Window starts where the previous window ended.
    rows = _read_rows(log)
    assert len(rows) == 1
    first_window_start = rows[0]["window_start"]

    # Inject fresh counters and force a second window.
    rec.record_signal_generated()
    rec.record_regime_filtered()
    rec.record_traded()
    rec.record_traded()
    clock.advance(11.0)
    assert rec.maybe_emit() is True

    rows = _read_rows(log)
    assert len(rows) == 2
    second_row = rows[1]
    assert second_row["signals_generated"] == 1
    assert second_row["regime_filtered"] == 1
    assert second_row["traded"] == 2
    # Second window_start is the previous window's utc_ts (continuity).
    assert second_row["window_start"] == rows[0]["utc_ts"]
    assert second_row["window_start"] != first_window_start or first_window_start == rows[0]["utc_ts"]


def test_heartbeat_atomic_write_uses_replace(tmp_path: Path, monkeypatch) -> None:
    """AC4: heartbeat writer uses atomic temp + os.replace.

    Inject a fake ``os.replace`` to capture the call signature; verify
    the heartbeat writer stages the new content there atomically.
    """
    log = tmp_path / "signal_stats.heartbeat.jsonl"
    clock = _FakeClock()
    rec = HeartbeatRecorder(log_path=str(log), clock=clock)

    # Spy on os.replace to verify it's invoked with (tmp_path, target).
    original_replace = os.replace
    calls: list[tuple[str, str]] = []
    real_tempfile_kids: list[str] = []

    def spy_replace(src: str, dst: str) -> None:  # noqa: ARG001
        calls.append((src, dst))
        original_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    rec.record_signal_generated()
    emitted = rec.force_emit()
    assert emitted is True

    # Exactly one replace per emit; source is a tempfile, destination
    # is the heartbeat sidecar log.
    assert len(calls) == 1
    src, dst = calls[:1][0]
    assert dst == str(log)
    assert src != str(log)  # source is the temp file, not the target
    # The temp file name starts with the prefix the writer uses.
    assert ".signal_stats_heartbeat." in os.path.basename(src)
    assert src.endswith(".tmp")
    # The temp file should not exist after a successful replace
    # (os.replace atomically renames it onto dst on POSIX).
    assert not Path(src).exists() or real_tempfile_kids  # noqa: F841 — placeholder for further temp-file inspection


def test_heartbeat_failure_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    """AC4: when the underlying write raises, heartbeat must NOT
    propagate the exception (non-fatal by design) and the caller must
    be able to continue incrementing counters for the next window."""
    log = tmp_path / "signal_stats.heartbeat.jsonl"
    clock = _FakeClock()
    rec = HeartbeatRecorder(log_path=str(log), clock=clock)

    # Capture the genuine os.replace FIRST so we can restore it later.
    original_replace = os.replace

    # Make os.replace raise to simulate a filesystem failure mid-write.
    def boom_replace(src, dst):  # noqa: ARG001
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "replace", boom_replace)

    rec.record_signal_generated()
    rec.record_regime_filtered()

    # First attempt — failure path; returns False, does NOT propagate.
    started = time.time()
    emitted = rec.maybe_emit()
    assert emitted is False  # noqa: S101 — beta/observability test asserting failure-path return value
    assert (time.time() - started) < 1.0, "maybe_emit must not stall on failure"

    # Counters remain (failed write does NOT reset).
    snap = rec.counters_snapshot()
    assert snap["signals_generated"] == 1
    assert snap["regime_filtered"] == 1

    # Subsequent counter increments and force_emit calls do not raise.
    rec.record_traded()
    snap = rec.counters_snapshot()
    assert snap["traded"] == 1

    # Restore os.replace to the genuine implementation, advance past
    # the throttle, and verify recovery: the next emit succeeds and
    # captures BOTH the pre-failure counters (carried over, because
    # rolling window resets only on success) AND the new increment.
    monkeypatch.setattr(os, "replace", original_replace)
    clock.advance(3601.0)
    rec.record_signal_generated()
    assert rec.maybe_emit() is True

    rows = _read_rows(log)
    assert len(rows) == 1
    assert rows[0]["signals_generated"] == 2  # 1 from before + 1 from after recovery
    assert rows[0]["regime_filtered"] == 1


def test_heartbeat_utc_ts_is_well_formed_iso8601_z(tmp_path: Path) -> None:
    """AC2: utc_ts is ISO-8601 with explicit Z suffix (Python
    datetime.fromtimestamp(..., tz=timezone.utc).isoformat() with
    the +00:00 → Z rewrite)."""
    log = tmp_path / "signal_stats.heartbeat.jsonl"
    clock = _FakeClock()
    rec = HeartbeatRecorder(log_path=str(log), clock=clock)

    rec.record_signal_generated()
    rec.force_emit()

    rows = _read_rows(log)
    assert len(rows) == 1
    ts = rows[0]["utc_ts"]
    assert ts.endswith("Z")
    # Parseable as ISO-8601
    from datetime import datetime

    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# BlendForwardTestRunner integration tests (AC2, AC3)
# ---------------------------------------------------------------------------


def test_blend_runner_on_signal_increments_generated_counter(tmp_path: Path) -> None:
    """AC2: every call to BlendForwardTestRunner.on_signal increments
    the signals_generated counter, even when downstream processing
    raises (counters are checked at the top of on_signal).

    Implementation note: maybe_emit() resets the rolling-window counters
    after a successful write. To observe the increment without the reset,
    we patch _append_line to raise so maybe_emit() returns False and
    leaves the counters intact — this is exactly the non-fatal failure
    path AC4 requires.
    """
    heartbeat_log = tmp_path / "signal_stats.heartbeat.jsonl"

    runner = object.__new__(blend_runner.BlendForwardTestRunner)
    runner._heartbeat_log_path = str(heartbeat_log)
    runner._heartbeat = HeartbeatRecorder(log_path=str(heartbeat_log))

    # Force maybe_emit() to take the failure path so counters are not
    # reset — lets us observe the increment.
    def boom(payload):  # noqa: ARG001
        raise OSError("forced write failure for counter-isolation test")

    runner._heartbeat._append_line = boom  # type: ignore[assignment]

    class _StubAdapter:
        def adapt_signal(self, strategy_id, signal_data):  # noqa: ARG002
            raise RuntimeError("simulated pipeline failure")

    runner._adapter = _StubAdapter()

    with pytest.raises(RuntimeError):
        runner.on_signal("srmr_gbpusd_h1", {"x": 1})

    snap = runner._heartbeat.counters_snapshot()
    assert snap["signals_generated"] == 1


def test_blend_runner_on_signal_emits_heartbeat(tmp_path: Path) -> None:
    """AC2 (positive path): a successful on_signal() emits a heartbeat
    row to the sidecar file with the rolling-window counter captured.
    Counter-reset after a successful emit is by design (rolling window)."""
    heartbeat_log = tmp_path / "signal_stats.heartbeat.jsonl"

    runner = object.__new__(blend_runner.BlendForwardTestRunner)
    runner._heartbeat_log_path = str(heartbeat_log)
    runner._heartbeat = HeartbeatRecorder(log_path=str(heartbeat_log))

    class _StubAdapter:
        def adapt_signal(self, sid, sd):  # noqa: ARG002
            return _FakeSignal()

    class _StubOrchestrator:
        def process_signal(self, sig):  # noqa: ARG002
            return _AcceptedOrder()

    runner._adapter = _StubAdapter()
    runner._orchestrator = _StubOrchestrator()
    runner._check_daily_reset = lambda *args, **kwargs: None  # type: ignore[assignment]

    # on_signal reaches the regime-aware exposure branch after a non-
    # rejected order; stub the regime + edge helpers so the test path
    # does not depend on production wiring.
    class _NoRegime:
        def get_exposure_multiplier(self, regime):  # noqa: ARG002
            return 1.0

    class _NoEdge:
        def get_risk_multiplier(self, strategy_id, symbol):  # noqa: ARG002
            return 1.0

    runner._regime_thresholds = _NoRegime()  # type: ignore[assignment]
    runner._edge_tracker = _NoEdge()  # type: ignore[assignment]
    runner._current_regime = None  # type: ignore[assignment]
    runner._regime_history = []
    runner._open_positions = {}
    runner._sizer = type("S", (), {"register": lambda *a, **kw: None})()
    runner._queue_signal_for_mapping = lambda *a, **kw: None  # type: ignore[assignment]

    order = runner.on_signal("srmr_xauusd_h1", {"y": 1})
    assert order.rejected is False

    # After a successful emit, rolling-window counters reset to zero.
    snap = runner._heartbeat.counters_snapshot()
    assert snap == {"signals_generated": 0, "regime_filtered": 0, "traded": 0}

    # The heartbeat row IS on disk — it has signals_generated=1, traded=1.
    rows = _read_rows(heartbeat_log)
    assert len(rows) == 1
    row = rows[0]
    assert row["signals_generated"] == 1
    assert row["traded"] == 1
    assert row["regime_filtered"] == 0


def test_blend_runner_heartbeat_failure_does_not_block_signal(tmp_path: Path, monkeypatch) -> None:
    """AC4: if the heartbeat raise-es, the signal pipeline keeps going."""
    heartbeat_log = tmp_path / "signal_stats.heartbeat.jsonl"

    # Build a runner with a HeartbeatRecorder whose _append_line raises.
    runner = object.__new__(blend_runner.BlendForwardTestRunner)
    runner._heartbeat_log_path = str(heartbeat_log)
    runner._heartbeat = ss_mod.HeartbeatRecorder(log_path=str(heartbeat_log))

    class _BoomAppend:
        def __call__(self, payload):  # noqa: ARG002
            raise OSError("disk full")

    # Patch the writer to raise — but the wrapping maybe_emit() must
    # catch it and return False (no propagation).
    runner._heartbeat._append_line = _BoomAppend()  # type: ignore[assignment]

    adapter_calls = []

    class _RecordingAdapter:
        def adapt_signal(self, strategy_id, signal_data):  # noqa: ARG002
            adapter_calls.append((strategy_id, signal_data))
            return _FakeSignal()

    runner._adapter = _RecordingAdapter()

    class _FakeSignal:
        timestamp = None
        symbol = "XAUUSD"
        direction = "BUY"
        confidence = 0.5
        metadata = {"rationale": "heartbeat-failure-probe"}
        entry_price = 100.0
        stop_loss = 99.0
        take_profit = 101.0

    # Process the signal — the orchestrator stub returns a rejected
    # order, so no real order is recorded, but the pipeline MUST keep
    # going past the heartbeat call.
    class _StubOrchestrator:
        def process_signal(self, signal):  # noqa: ARG002
            return _RejectedOrder()

    runner._orchestrator = _StubOrchestrator()
    runner._check_daily_reset = lambda *args, **kwargs: None  # type: ignore[assignment]

    order = runner.on_signal("srmr_xauusd_h1", {"y": 1})
    assert order.rejected is True
    # Adapter was called: signal pipeline continued despite heartbeat raise.
    assert adapter_calls == [("srmr_xauusd_h1", {"y": 1})]


# ---------------------------------------------------------------------------
# Shared test helpers (used by multiple tests)
# ---------------------------------------------------------------------------


class _FakeSignal:
    """Minimal OrchestratorTradeSignal-shaped stub."""

    import datetime as _dt

    timestamp = _dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=_dt.timezone.utc)
    strategy_id = "srmr_xauusd_h1"
    symbol = "XAUUSD"
    direction = "BUY"
    confidence = 0.5
    metadata = {"rationale": "heartbeat-test-stub"}
    entry_price = 100.0
    stop_loss = 99.0
    take_profit = 101.0


class _RejectedOrder:
    rejected = True
    rejection_reason = "regime_filtered_for_test"


class _AcceptedOrder:
    rejected = False
    rejection_reason = ""
    risk_amount = 100.0
    lots = 0.1


# ---------------------------------------------------------------------------
# Real-signal write path regression test (AC3)
# ---------------------------------------------------------------------------


def test_signal_stats_recorder_record_signal_still_appends(tmp_path: Path) -> None:
    """AC3: SignalStatsRecorder.record_signal() still appends the
    canonical {signal_id, strategy, ...} row with the same schema as
    before the heartbeat feature landed. Regression guard for the
    real-signal write path."""
    log = tmp_path / "signal_stats.jsonl"
    rec = SignalStatsRecorder(log_path=str(log))

    rec.record_signal(
        SignalRecord(
            signal_id="regression-sig-001",
            timestamp="2026-10-02T12:00:00Z",
            strategy="srmr_gbpusd_h1",
            symbol="GBPUSD",
            direction="BUY",
            confidence=0.72,
        )
    )
    rec.record_outcome("regression-sig-001", "tp_hit", pips=15.5, time_to_close=1800)

    rows = _read_rows(log)
    assert len(rows) == 2  # open line + close line

    open_row = rows[0]
    close_row = rows[1]
    assert open_row["signal_id"] == "regression-sig-001"
    assert open_row["strategy"] == "srmr_gbpusd_h1"
    assert open_row["outcome"] == "open"
    assert close_row["signal_id"] == "regression-sig-001"
    assert close_row["outcome"] == "tp_hit"
    assert close_row["pips_realized"] == 15.5


def test_real_signal_path_unaffected_when_heartbeat_also_fires(tmp_path: Path) -> None:
    """AC3: when a real signal is recorded AND the heartbeat fires in
    the same window, both files receive their separate sink — the
    heartbeat sidecar never pollutes the main 'data/signal_stats.jsonl'
    file, and the main file's schema is unchanged."""
    main_log = tmp_path / "signal_stats.jsonl"
    heartbeat_log = tmp_path / "signal_stats.heartbeat.jsonl"

    stats = SignalStatsRecorder(log_path=str(main_log))
    heartbeat = HeartbeatRecorder(
        log_path=str(heartbeat_log),
        throttle_seconds=0.0,  # emit on every maybe_emit()
        clock=_FakeClock(),
    )

    # Real signal path.
    stats.record_signal(
        SignalRecord(
            signal_id="regression-sig-002",
            timestamp="2026-10-02T12:30:00Z",
            strategy="ttcxauusd",
            symbol="XAUUSD",
            direction="SELL",
            confidence=0.81,
        )
    )

    # Heartbeat path runs in parallel.
    heartbeat.record_signal_generated()
    heartbeat.record_regime_filtered()
    heartbeat.record_traded()
    heartbeat.maybe_emit()

    # Main file: exactly one row, signal schema, no HB fields.
    main_rows = _read_rows(main_log)
    assert len(main_rows) == 1
    assert main_rows[0]["signal_id"] == "regression-sig-002"
    assert "utc_ts" not in main_rows[0]
    assert "signals_generated" not in main_rows[0]

    # Heartbeat file: exactly one row, heartbeat schema, no signal fields.
    hb_rows = _read_rows(heartbeat_log)
    assert len(hb_rows) == 1
    hb_row = hb_rows[0]
    assert "utc_ts" in hb_row
    assert "signals_generated" in hb_row
    assert "signal_id" not in hb_row
    assert hb_row["signals_generated"] == 1
    assert hb_row["regime_filtered"] == 1
    assert hb_row["traded"] == 1
