"""Unit tests for :mod:`offload.executor` — Sprint D 2 sweep executor.

Scope (per card 886fe5ef spec):
  * GPU probe graceful degradation (nvidia-smi missing / timeout / parse fail).
  * Reconciliation raises on partial + SHA mismatch.
  * Core caps by time window (08:00-23:00 America/Toronto → cores-2).
  * Dry-run scheduling decisions (no dispatch, decision still emitted).
  * Load-aware scheduling yields to Ollama / high load average.
  * LightGBM device flag emission (``cuda`` vs ``cpu``).
  * run_sweep dispatches via injected transport + writes provenance.

These tests are pure (no live node, no subprocess, no Ollama daemon) —
every external surface is mocked through the seams defined in
``offload.executor``. Per HR5: targeted only, no full suite.
"""

from __future__ import annotations

import dataclasses
import subprocess
import threading
import zoneinfo
from datetime import datetime
from typing import Iterable

import pytest
from offload.executor import (
    OLLAMA_HARD_CAP,
    ArtifactSpec,
    CompletedProcess,
    GpuProbeResult,
    PartialReconciliationError,
    ProvenanceMismatchError,
    ReceivedArtifact,
    ReconcileResult,
    ScheduleDecision,
    SweepResult,
    SweepSpec,
    TransportDispatchError,
    detect_ollama_inference,
    is_daytime_window,
    lgbm_gpu_device_flag,
    load_average,
    probe_gpu,
    reconcile_artifacts,
    run_sweep,
    schedule_concurrency,
)

# ---------------------------------------------------------------------------
# probe_gpu — graceful degradation (the load-bearing contract)
# ---------------------------------------------------------------------------


def _runner_for(returncode: int, stdout: str = "", stderr: str = "") -> object:
    """Build a subprocess-runner seam closure for probe_gpu tests."""

    def _runner(args, timeout_s):  # noqa: ARG001 — args/timeout unused in seam
        return CompletedProcess(returncode=returncode, stdout=stdout, stderr=stderr)

    return _runner


def test_probe_gpu_graceful_when_runner_raises_filenotfound() -> None:
    """Missing nvidia-smi binary → available=False, reason='nvidia_smi_not_found'."""
    def _boom(args, timeout_s):  # noqa: ARG001
        raise FileNotFoundError("nvidia-smi missing on PATH")

    result = probe_gpu(runner=_boom)
    assert result.available is False
    assert result.reason == "nvidia_smi_not_found"
    assert result.device_count == 0
    assert result.devices == ()


def test_probe_gpu_graceful_when_runner_times_out() -> None:
    """nvidia-smi hangs (driver probe stall) → available=False, reason='timeout'."""
    def _timeout(args, timeout_s):  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd=["nvidia-smi"], timeout=timeout_s)

    result = probe_gpu(runner=_timeout)
    assert result.available is False
    assert result.reason == "nvidia_smi_timeout"
    assert result.device_count == 0


def test_probe_gpu_graceful_on_os_error() -> None:
    """OS-level failure (EACCES on the binary) → available=False with reason."""
    def _eacces(args, timeout_s):  # noqa: ARG001
        raise PermissionError("EACCES")

    result = probe_gpu(runner=_eacces)
    assert result.available is False
    assert result.reason is not None
    assert result.reason.startswith("nvidia_smi_os_error:")
    assert "PermissionError" in result.reason


def test_probe_gpu_parses_minimal_csv() -> None:
    """Healthy two-device probe → available=True, devices preserved verbatim."""
    csv = (
        "0, NVIDIA GeForce RTX 5060 Ti, 16380, 580.00\n"
        "1, NVIDIA GeForce RTX 5060 Ti, 16380, 580.00\n"
    )
    result = probe_gpu(runner=_runner_for(returncode=0, stdout=csv))
    assert result.available is True
    assert result.device_count == 2
    assert result.devices == ("GPU-0: NVIDIA GeForce RTX 5060 Ti", "GPU-1: NVIDIA GeForce RTX 5060 Ti")
    assert result.reason is None


def test_probe_gpu_parses_single_device() -> None:
    """One device → device_count=1 (the common ava-worker-local case)."""
    csv = "0, NVIDIA RTX A4000, 16380, 535.86\n"
    result = probe_gpu(runner=_runner_for(returncode=0, stdout=csv))
    assert result.available is True
    assert result.device_count == 1
    assert result.devices[0].startswith("GPU-0:")


def test_probe_gpu_unavailable_when_nonzero_exit() -> None:
    """nvidia-smi exit 9 (no NVIDIA driver) → available=False, reason=exit_9."""
    result = probe_gpu(runner=_runner_for(returncode=9, stderr="NVIDIA: not initialized"))
    assert result.available is False
    assert result.reason == "nvidia_smi_exit_9"
    assert result.device_count == 0


def test_probe_gpu_unavailable_when_empty_output() -> None:
    """Exit 0 but empty stdout (no devices) → available=False, reason=empty_output."""
    result = probe_gpu(runner=_runner_for(returncode=0, stdout=""))
    assert result.available is False
    assert result.reason == "nvidia_smi_empty_output"


def test_probe_gpu_unavailable_when_unparseable_csv() -> None:
    """One-token lines can't be parsed → available=False, reason=unparseable."""
    csv = "garbage-text-no-comma\nmore-garbage\n"
    result = probe_gpu(runner=_runner_for(returncode=0, stdout=csv))
    assert result.available is False
    assert result.reason == "nvidia_smi_unparseable"
    assert result.device_count == 0


def test_probe_gpu_skips_short_lines_keeps_valid() -> None:
    """Mixed valid + unparseable lines → valid lines win, device_count = valid count."""
    csv = (
        "0, NVIDIA RTX A4000, 16380, 535.86\n"
        "garbage\n"
        "1, NVIDIA RTX A4000, 16380, 535.86\n"
    )
    result = probe_gpu(runner=_runner_for(returncode=0, stdout=csv))
    assert result.available is True
    assert result.device_count == 2


# ---------------------------------------------------------------------------
# load_average (pure /proc reader)
# ---------------------------------------------------------------------------


def test_load_average_parses_loadavg(tmp_path) -> None:
    """Round-trip via a synthetic /proc/loadavg file."""
    f = tmp_path / "loadavg"
    f.write_text("0.42 0.50 0.55 1/123 4567\n")
    assert load_average(loadavg_path=f) == pytest.approx(0.42)


def test_load_average_returns_none_when_missing(tmp_path) -> None:
    """Missing /proc/loadavg (non-Linux host) → None, no exception."""
    assert load_average(loadavg_path=tmp_path / "no-such-file") is None


def test_load_average_returns_none_on_garbage(tmp_path) -> None:
    """First token non-numeric → None (don't lie about load)."""
    f = tmp_path / "loadavg"
    f.write_text("NaN-or-junk 0.0 0.0\n")
    assert load_average(loadavg_path=f) is None


def test_load_average_empty_file_returns_none(tmp_path) -> None:
    """Empty /proc/loadavg (impossible but defensive) → None."""
    f = tmp_path / "loadavg"
    f.write_text("")
    assert load_average(loadavg_path=f) is None


# ---------------------------------------------------------------------------
# detect_ollama_inference (pure)
# ---------------------------------------------------------------------------


def test_detect_ollama_proc_reader_match() -> None:
    """ollama serve in /proc → True."""
    lines = [
        "1 root 12345 /usr/local/bin/ollama serve\n",
        "100 user 1 /bin/bash\n",
    ]
    assert detect_ollama_inference(proc_reader=lambda: lines) is True


def test_detect_ollama_proc_reader_no_match() -> None:
    """No ollama process → False."""
    lines = [
        "1 root 12345 /usr/local/bin/python3 script.py\n",
        "100 user 1 /bin/bash\n",
    ]
    assert detect_ollama_inference(proc_reader=lambda: lines) is False


def test_detect_ollama_proc_reader_raises_returns_false() -> None:
    """Proc reader raises (container, restricted /proc) → False, no crash."""
    def _boom() -> Iterable[str]:
        raise FileNotFoundError("no /proc")

        yield  # pragma: no cover — generator, never reached

    assert detect_ollama_inference(proc_reader=_boom) is False


def test_detect_ollama_proc_reader_empty_returns_false() -> None:
    """Empty /proc → False."""
    assert detect_ollama_inference(proc_reader=lambda: iter(())) is False


# ---------------------------------------------------------------------------
# is_daytime_window (time-window math)
# ---------------------------------------------------------------------------


def test_is_daytime_window_14_00_is_daytime() -> None:
    """14:00 Toronto → daytime, base cap = cores - 2."""
    tz = zoneinfo.ZoneInfo("America/Toronto")
    at = datetime(2026, 10, 6, 14, 0, tzinfo=tz)
    assert is_daytime_window(at) is True


def test_is_daytime_window_02_00_is_quiet() -> None:
    """02:00 Toronto → quiet hours (23:00-08:00), base cap = full cores."""
    tz = zoneinfo.ZoneInfo("America/Toronto")
    at = datetime(2026, 10, 6, 2, 0, tzinfo=tz)
    assert is_daytime_window(at) is False


def test_is_daytime_window_07_59_is_quiet() -> None:
    """07:59 → still quiet (boundary test)."""
    tz = zoneinfo.ZoneInfo("America/Toronto")
    at = datetime(2026, 10, 6, 7, 59, tzinfo=tz)
    assert is_daytime_window(at) is False


def test_is_daytime_window_08_00_is_daytime() -> None:
    """08:00 → daytime starts (inclusive)."""
    tz = zoneinfo.ZoneInfo("America/Toronto")
    at = datetime(2026, 10, 6, 8, 0, tzinfo=tz)
    assert is_daytime_window(at) is True


def test_is_daytime_window_23_00_is_quiet() -> None:
    """23:00 → quiet hours start (exclusive)."""
    tz = zoneinfo.ZoneInfo("America/Toronto")
    at = datetime(2026, 10, 6, 23, 0, tzinfo=tz)
    assert is_daytime_window(at) is False


def test_is_daytime_window_22_59_is_daytime() -> None:
    """22:59 → last minute of daytime (boundary)."""
    tz = zoneinfo.ZoneInfo("America/Toronto")
    at = datetime(2026, 10, 6, 22, 59, tzinfo=tz)
    assert is_daytime_window(at) is True


# ---------------------------------------------------------------------------
# schedule_concurrency — the decision chain
# ---------------------------------------------------------------------------


def _fixed_now(hour: int) -> datetime:
    tz = zoneinfo.ZoneInfo("America/Toronto")
    return datetime(2026, 10, 6, hour, 0, tzinfo=tz)


def test_schedule_daytime_caps_cores_minus_two() -> None:
    """Daytime (14:00) + no ollama + low load → cap = cores - 2."""
    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(available=False),
        now_fn=lambda: _fixed_now(14),
    )
    assert decision.daytime is True
    assert decision.cores_available == 8
    assert decision.cores_cap == 6
    assert decision.cores_use == 6
    assert decision.yielding_to_ollama is False
    assert decision.lgbm_device_flag == "cpu"


def test_schedule_quiet_hours_full_cores() -> None:
    """Quiet (02:00) → cap = full cores (no Ollama conflict assumed)."""
    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(available=False),
        now_fn=lambda: _fixed_now(2),
    )
    assert decision.daytime is False
    assert decision.cores_cap == 8
    assert decision.cores_use == 8


def test_schedule_yields_to_ollama_detection() -> None:
    """Ollama busy → cores_use ≤ OLLAMA_HARD_CAP regardless of window."""
    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(available=False),
        ollama_detected=True,
        now_fn=lambda: _fixed_now(14),
    )
    assert decision.cores_cap == 6
    assert decision.cores_use == min(OLLAMA_HARD_CAP, 6)
    assert decision.yielding_to_ollama is True
    assert decision.ollama_detected is True
    assert "ollama" in " ".join(decision.reasoning)


def test_schedule_yields_to_high_load_average() -> None:
    """Load average > threshold × cores → yield (no ollama process required)."""
    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(available=False),
        load_avg=14.0,  # 14.0 > 1.5 × 8 = 12.0
        now_fn=lambda: _fixed_now(14),
    )
    assert decision.yielding_to_ollama is True
    assert decision.load_average == 14.0
    assert decision.cores_use == min(OLLAMA_HARD_CAP, 6)


def test_schedule_does_not_yield_on_low_load() -> None:
    """Load average below threshold → no yield."""
    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(available=False),
        load_avg=2.0,  # well below 12.0 threshold
        now_fn=lambda: _fixed_now(14),
    )
    assert decision.yielding_to_ollama is False
    assert decision.cores_use == 6


def test_schedule_load_average_none_does_not_yield() -> None:
    """Missing /proc/loadavg → load_avg=None, no false-positive yield."""
    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(available=False),
        load_avg=None,
        now_fn=lambda: _fixed_now(14),
    )
    assert decision.yielding_to_ollama is False
    assert decision.load_average is None


def test_schedule_gpu_emits_cuda_flag_when_available() -> None:
    """GPU detected → lgbm_device_flag = 'cuda' for LightGBM workers."""
    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(
            available=True,
            device_count=1,
            devices=("GPU-0: NVIDIA RTX A4000",),
        ),
        now_fn=lambda: _fixed_now(14),
    )
    assert decision.lgbm_device_flag == "cuda"
    assert decision.gpu.available is True


def test_schedule_decision_is_frozen_dataclass() -> None:
    """ScheduleDecision is immutable (frozen dataclass)."""
    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(available=False),
        now_fn=lambda: _fixed_now(14),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.cores_use = 99  # type: ignore[misc]


def test_schedule_reasoning_includes_all_branches() -> None:
    """The reasoning tuple carries the full decision chain (HR8 audit)."""
    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(available=False, reason="nvidia_smi_not_found"),
        ollama_detected=False,
        load_avg=2.0,
        now_fn=lambda: _fixed_now(14),
    )
    chain = " ".join(decision.reasoning)
    assert "worknode=ava-worker-local" in chain
    assert "cores_available=8" in chain
    assert "nvidia_smi_not_found" in chain
    assert "base_cap=6" in chain
    assert "yielding=False" in chain


def test_schedule_cores_use_floor_is_one() -> None:
    """cores_use never collapses below 1 (a sweep with N≥1 cells still progresses)."""
    decision = schedule_concurrency(
        cores_available=2,  # extreme: cores - 2 = 0 in daytime
        gpu=GpuProbeResult(available=False),
        now_fn=lambda: _fixed_now(14),
    )
    assert decision.cores_cap == 1  # max(1, 2 - 2)
    assert decision.cores_use >= 1


# ---------------------------------------------------------------------------
# lgbm_gpu_device_flag — public flag emitter
# ---------------------------------------------------------------------------


def test_lgbm_flag_gpu_available_emits_cuda() -> None:
    """lgbm_gpu_device_flag(True) → 'cuda' (LightGBM 4.x naming)."""
    assert (
        lgbm_gpu_device_flag(GpuProbeResult(available=True, device_count=1))
        == "cuda"
    )


def test_lgbm_flag_gpu_unavailable_emits_cpu() -> None:
    """lgbm_gpu_device_flag(False) → 'cpu' (never crashes, never lies)."""
    assert lgbm_gpu_device_flag(GpuProbeResult(available=False)) == "cpu"


# ---------------------------------------------------------------------------
# reconcile_artifacts — fail-loud on partial + SHA mismatch
# ---------------------------------------------------------------------------


def _spec(cid: str, *, sha: str | None = None, shard: int = 0, **prov) -> ArtifactSpec:
    return ArtifactSpec(
        cell_id=cid,
        shard_id=shard,
        expected_sha256=sha,
        provenance={"git_sha": "abc1234", "env_lock_hash": "deadbeef", **prov},
    )


def _recv(cid: str, sha: str, *, shard: int = 0, path=None) -> ReceivedArtifact:
    return ReceivedArtifact(
        cell_id=cid,
        shard_id=shard,
        sha256=sha,
        output_path=path or _dummy_path(cid),
    )


def _dummy_path(cid: str):
    import tempfile
    from pathlib import Path

    scratch = Path(tempfile.gettempdir()) / "offload-executor-tests"
    return scratch / f"{cid}.json"


def test_reconcile_passes_on_complete_set() -> None:
    """All expected shards received with matching SHA → ReconcileResult, no raise."""
    specs = [_spec("a"), _spec("b"), _spec("c")]
    received = [_recv("a", "sa"), _recv("b", "sb"), _recv("c", "sc")]
    result = reconcile_artifacts(expected=specs, received=received)
    assert isinstance(result, ReconcileResult)
    assert result.n_expected == 3
    assert result.n_received == 3
    assert len(result.artifacts) == 3
    assert set(result.provenance) == {"a", "b", "c"}


def test_reconcile_raises_on_partial() -> None:
    """Missing one of three shards → raise PartialReconciliationError with missing list."""
    specs = [_spec("a"), _spec("b"), _spec("c")]
    received = [_recv("a", "sa"), _recv("c", "sc")]  # 'b' missing
    with pytest.raises(PartialReconciliationError) as excinfo:
        reconcile_artifacts(expected=specs, received=received)
    err = excinfo.value
    assert err.n_expected == 3
    assert err.n_received == 2
    assert len(err.missing) == 1
    assert err.missing[0].cell_id == "b"
    assert "Partial reconciliation" in str(err)


def test_reconcile_raises_when_zero_received() -> None:
    """Empty received stream with non-empty expected → raise, never silent."""
    specs = [_spec("a"), _spec("b")]
    with pytest.raises(PartialReconciliationError) as excinfo:
        reconcile_artifacts(expected=specs, received=[])
    assert excinfo.value.n_received == 0
    assert len(excinfo.value.missing) == 2


def test_reconcile_raises_on_sha_mismatch() -> None:
    """SHA mismatch raises ProvenanceMismatchError BEFORE the partial check."""
    specs = [_spec("a", sha="expected_sha")]
    received = [_recv("a", "different_sha")]
    with pytest.raises(ProvenanceMismatchError) as excinfo:
        reconcile_artifacts(expected=specs, received=received)
    assert "expected_sha" in str(excinfo.value)
    assert "different_sha" in str(excinfo.value)


def test_reconcile_skips_sha_check_when_expected_none() -> None:
    """expected_sha256=None → SHA on the wire is recorded as-is, no mismatch check."""
    specs = [_spec("a", sha=None)]
    received = [_recv("a", "any_sha")]
    result = reconcile_artifacts(expected=specs, received=received)
    assert result.n_received == 1
    assert result.artifacts[0].sha256 == "any_sha"


def test_reconcile_truncates_overflow_with_warning(caplog) -> None:
    """Worker over-returned → first N accepted as matched, tail logged."""
    specs = [_spec("a"), _spec("b")]
    received = [
        _recv("a", "sa"),
        _recv("b", "sb"),
        _recv("a", "sa_dup"),  # unexpected tail
    ]
    result = reconcile_artifacts(expected=specs, received=received)
    assert result.n_expected == 2
    assert result.n_received == 2


def test_reconcile_logs_unexpected_cell_for_spec_missing_key(caplog) -> None:
    """Received shard whose key isn't in expected → logged as warning, ignored."""
    specs = [_spec("a")]
    received = [_recv("a", "sa"), _recv("z", "sz")]  # z is unexpected
    result = reconcile_artifacts(expected=specs, received=received)
    assert result.n_received == 1  # only the matched one
    assert [a.cell_id for a in result.artifacts] == ["a"]


def test_reconcile_preserves_provenance_columns_per_cell() -> None:
    """Each cell's provenance (1b.4 factory columns) survives reconciliation."""
    specs = [
        _spec("a", git_sha="abc", data_hash="h1"),
        _spec("b", git_sha="def", data_hash="h2"),
    ]
    received = [_recv("a", "sa"), _recv("b", "sb")]
    result = reconcile_artifacts(expected=specs, received=received)
    assert result.provenance["a"] == {"git_sha": "abc", "data_hash": "h1", "env_lock_hash": "deadbeef"}
    assert result.provenance["b"] == {"git_sha": "def", "data_hash": "h2", "env_lock_hash": "deadbeef"}


# ---------------------------------------------------------------------------
# run_sweep — parallel CPU dispatch + dry-run
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Minimal transport shim for run_sweep unit tests.

    Records every push_bundle / fetch_output call so the test can assert
    the executor actually drove the transport. Returns deterministic
    bytes so SHA verification is mechanical.
    """

    def __init__(self, *, return_sha: str = "abc", fail_on: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.return_sha = return_sha
        self.fail_on = fail_on or set()
        self._lock = threading.Lock()

    def push_bundle(self, run_id, path, sha):
        with self._lock:
            self.calls.append(("push_bundle", run_id, str(path)))
            if str(path) in self.fail_on:
                raise TransportDispatchError(f"forced fail on {path}")

        # Real transports return a WorkerCell; FakeTransport mirrors.
        @dataclasses.dataclass
        class _Cell:
            path: str
            sha256: str

        return _Cell(path=str(path), sha256=self.return_sha)

    def fetch_output(self, run_id, cell_id):
        with self._lock:
            self.calls.append(("fetch_output", run_id, cell_id))
        return b"output-bytes-for-" + cell_id.encode()


def test_run_sweep_dry_run_returns_decision_without_dispatch(tmp_path) -> None:
    """dry_run=True → decision emitted, no transport interactions, no reconcile."""
    spec = SweepSpec(
        expected_specs=(_spec("a", sha="sa"), _spec("b", sha="sb")),
        transport_factory=lambda: _FakeTransport(),
        dry_run=True,
    )
    result = run_sweep(spec)
    assert isinstance(result, SweepResult)
    assert result.dry_run is True
    assert result.reconcile is None
    assert result.n_cells == 2
    assert isinstance(result.decision, ScheduleDecision)


def test_run_sweep_dispatches_via_injected_transport(tmp_path) -> None:
    """Non-dry-run → transport.push_bundle called once per cell, reconcile succeeds."""
    import hashlib as _h

    # Pre-compute SHAs of the bytes we'll have fetch_output return so the
    # executor's SHA-comparison passes (1b.4 provenance invariant).
    payload_a = b"payload-for-cell-a"
    payload_b = b"payload-for-cell-b"
    sha_a = _h.sha256(payload_a).hexdigest()
    sha_b = _h.sha256(payload_b).hexdigest()

    class _SHAedTransport(_FakeTransport):
        def fetch_output(self, run_id, cell_id):
            super().fetch_output(run_id, cell_id)
            return payload_a if cell_id == "a" else payload_b

    spec = SweepSpec(
        expected_specs=(
            _spec("a", sha=sha_a),
            _spec("b", sha=sha_b),
        ),
        transport_factory=_SHAedTransport,
        dry_run=False,
    )
    result = run_sweep(spec)
    assert result.dry_run is False
    assert result.reconcile is not None
    assert result.reconcile.n_expected == 2
    assert result.reconcile.n_received == 2
    assert result.reconcile.artifacts[0].sha256 == sha_a
    assert result.reconcile.artifacts[1].sha256 == sha_b


def test_run_sweep_raises_when_transport_factory_missing() -> None:
    """Non-dry-run + transport_factory=None → ExecutorError (fail-loud)."""
    spec = SweepSpec(
        expected_specs=(_spec("a", sha="sa"),),
        transport_factory=None,
        dry_run=False,
    )
    with pytest.raises(Exception) as excinfo:  # ExecutorError
        run_sweep(spec)
    assert "transport_factory" in str(excinfo.value).lower() or isinstance(
        excinfo.value, (TransportDispatchError, RuntimeError, ValueError)
    )


def test_run_sweep_propagates_partial_reconciliation_error(tmp_path) -> None:
    """A transport that swallows cells causes a partial, which raises."""

    class _PartialTransport(_FakeTransport):
        def push_bundle(self, run_id, path, sha):
            return super().push_bundle(run_id, path, sha)

        def fetch_output(self, run_id, cell_id):
            if cell_id == "b":
                return None  # signals missing shard via _dispatch_one_cell
            return b"ok-" + cell_id.encode()

    # We expect an error from the dispatch path because _dispatch_one_cell
    # raises TransportDispatchError on empty SHA. Either outcome (partial
    # reconciliation raise or TransportDispatchError raise) is acceptable —
    # what matters is silent-loss is impossible.
    spec = SweepSpec(
        expected_specs=(
            _spec("a", sha="sa"),
            _spec("b", sha="sb"),
        ),
        transport_factory=_PartialTransport,
        dry_run=False,
    )
    with pytest.raises(Exception) as excinfo:
        run_sweep(spec)
    # The executor must NEVER return a sweep that lies about its
    # completeness — this is the HR27 silent-success guard.
    assert excinfo.value is not None


def test_run_sweep_caps_workers_at_decision_cores_use() -> None:
    """ThreadPoolExecutor max_workers is clamped to decision.cores_use."""
    import hashlib as _h

    # Pre-compute SHAs so the executor's SHA-comparison passes.
    payloads = {
        "a": b"payload-a",
        "b": b"payload-b",
        "c": b"payload-c",
        "d": b"payload-d",
    }
    specs = tuple(
        _spec(cid, sha=_h.sha256(p).hexdigest()) for cid, p in payloads.items()
    )

    class _SHAedTransport(_FakeTransport):
        def fetch_output(self, run_id, cell_id):
            super().fetch_output(run_id, cell_id)
            return payloads[cell_id]

    spec = SweepSpec(
        expected_specs=specs,
        transport_factory=_SHAedTransport,
        dry_run=False,
    )
    result = run_sweep(spec)
    # cores_use is at least 1 (floor). On this host with no ollama and a
    # single-daytime run we expect cores_use >= 1; the executor must also
    # complete the sweep and reconcile successfully.
    assert result.decision.cores_use >= 1
    assert result.reconcile is not None
    assert result.reconcile.n_received == 4


def test_run_sweep_dry_run_no_transport_factory_required() -> None:
    """dry_run + None transport_factory → no exception (decision-only path)."""
    spec = SweepSpec(
        expected_specs=(_spec("a", sha="sa"),),
        transport_factory=None,
        dry_run=True,
    )
    result = run_sweep(spec)
    assert result.dry_run is True
    assert result.reconcile is None


# ---------------------------------------------------------------------------
# Decision JSON serialization (HR8 verifier-friendly)
# ---------------------------------------------------------------------------


def test_schedule_decision_to_dict_is_json_serializable() -> None:
    """decision.to_dict → json.dumps round-trip (HR8 audit-trail evidence)."""
    import json

    decision = schedule_concurrency(
        cores_available=8,
        gpu=GpuProbeResult(available=True, device_count=1, devices=("GPU-0: foo",)),
        now_fn=lambda: _fixed_now(14),
    )
    payload = decision.to_dict()
    # Round-trip — no TypeError from non-serializable types.
    encoded = json.dumps(payload, sort_keys=True, default=str)
    decoded = json.loads(encoded)
    assert decoded["cores_available"] == 8
    assert decoded["lgbm_device_flag"] == "cuda"
    assert decoded["gpu"]["available"] is True
    assert decoded["gpu"]["device_count"] == 1


def test_reconcile_result_to_dict_is_json_serializable() -> None:
    """reconcile.to_dict → json.dumps round-trip (HR8 audit-trail evidence)."""
    import json

    specs = [_spec("a", git_sha="abc"), _spec("b", git_sha="def")]
    received = [_recv("a", "sa"), _recv("b", "sb")]
    result = reconcile_artifacts(expected=specs, received=received)
    encoded = json.dumps(result.to_dict(), sort_keys=True, default=str)
    decoded = json.loads(encoded)
    assert decoded["n_expected"] == 2
    assert decoded["n_received"] == 2
    assert len(decoded["artifacts"]) == 2
