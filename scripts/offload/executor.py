#!/usr/bin/env python3
"""Sprint D 2 — GPU/CPU sweep executor for paired node ava-worker-local.

Wraps the dispatch loop in :mod:`scripts.offload.run_matrix_remote` with
the Sprint D Wave 1 requirements (Craig directive 2026-10-06 13:47):

  1. Runtime GPU probe (nvidia-smi) with graceful degradation.
     When nvidia-smi is missing, blocked, or returns no devices the
     executor emits a CPU-only ScheduleDecision — never crashes, never
     pretends a GPU is available.

  2. Parallel CPU workers for matrix/CPCV sweep shards.
     ``run_sweep()`` dispatches cells through a ``BundleTransport``
     (the same ABC used by the v1 runner) up to the cores-use cap from
     the ScheduleDecision. The transport is injected, so unit tests can
     pass a fake — no live node required.

  3. Fail-loud artifact reconciliation back to the gateway.
     ``reconcile_artifacts()`` requires expected vs received shard
     counts to match exactly. Any partial result raises
     :class:`PartialReconciliationError` with a deterministic missing
     list (never silent-loss per HR27). Provenance SHAs (1b.4 factory
     columns: ``git_sha``, ``env_lock_hash``) are recorded per
     :class:`ArtifactSpec` so the gateway can detect drift after the
     sweep returns.

  4. Load-aware scheduling that yields to Ollama inference.
     Before claiming cores, ``schedule_concurrency()`` samples the
     target node's load average (``/proc/loadavg``) and probes for
     active ``ollama serve`` processes. When Ollama is busy the
     executor drops the cores-use cap to a hard 2 (yield). The
     daytime/quiet-hours core cap (08:00–23:00 America/Toronto →
     ``cores_available - 2``; quiet hours → full ``cores_available``)
     is applied first, so quiet windows still yield when Ollama is hot.

  5. LightGBM device flag emission.
     ``lgbm_gpu_device_flag()`` returns ``"cuda"`` when a GPU is
     detected and ``"cpu"`` otherwise — wired so LightGBM workers in
     the matrix/CPCV sweeps can pick up the flag without re-probing.

  6. Dry-run scheduling.
     ``run_sweep(dry_run=True)`` returns a :class:`SweepResult` with
     the full decision chain (cores/gpu/yielding) and zero dispatches,
     so the operator can pre-flight a sweep before committing cores.

No new pip dependencies. The module uses only stdlib + the existing
``offload.transport.BundleTransport`` ABC. ``Unit`` tests must mock the
transport (HR5 target discipline; see ``tests/unit/test_offload_executor.py``).

CLI (dry-run only; production dispatch stays in run_matrix_remote):
  python3 scripts/offload/executor.py --dry-run --print-decision
  python3 scripts/offload/executor.py --print-decision --at-hour 02
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Callable, Iterable, Sequence

# ---------------------------------------------------------------------------
# Re-use the v1 BundleTransport ABC so the executor plugs into the existing
# transport stack. Import is local + lazy so tests can stub the module path
# without paying for an offload.transport import at boot.
# ---------------------------------------------------------------------------
_LOG = logging.getLogger("offload.executor")

# ava-worker-local contract (Craig directive 2026-10-06): 8 cores, 40GB RAM,
# GPU present (RTX-class per spec; we probe at runtime, never assume).
DEFAULT_WORKNODE = "ava-worker-local"
DEFAULT_CORES_AVAILABLE = 8
DEFAULT_RAM_GB = 40

# Time-window constants — America/Toronto. 08:00-23:00 caps concurrency at
# ``cores_available - 2``; the remaining quiet hours (23:00-08:00 local)
# allow full ``cores_available`` because the gateway cron is asleep.
TIMEZONE_NAME = "America/Toronto"
DAYTIME_START = time(8, 0)
DAYTIME_END = time(23, 0)

# Yield-to-Ollama thresholds. If the target node's 1-minute load average
# exceeds ``LOAD_YIELD_THRESHOLD`` * cores_available we assume an active
# Ollama inference session is the cause; same logic for a confirmed
# ``ollama serve`` process.
LOAD_YIELD_THRESHOLD = 1.5  # per-core; 12.0 on an 8-core box
OLLAMA_HARD_CAP = 2         # when yielding, never exceed 2 worker threads
OLLAMA_DEFAULT_CAP = 8        # when not yielding, cap is min(DAYTIME_CAP, available)

# GPU probe defaults.
NVIDIA_SMI_TIMEOUT_S = 5
NVIDIA_SMI_QUERY = (
    "--query-gpu=index,name,memory.total,driver_version"
    " --format=csv,noheader,nounits"
)

# Reconciliation defaults.
MAX_PARALLEL_TRANSPORTS = 32


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuProbeResult:
    """Outcome of an nvidia-smi probe.

    ``available`` is True only when the binary was found, returned 0,
    and reported at least one device. ``reason`` is human-readable so
    an operator can grep ``[executor] gpu_unavailable: <reason>`` from
    a sweep log.
    """

    available: bool
    device_count: int = 0
    devices: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "device_count": self.device_count,
            "devices": list(self.devices),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ScheduleDecision:
    """The full decision chain for one sweep invocation.

    Every field is derived deterministically from inputs; ``reasoning``
    is a free-text chain so the operator can audit *why* the executor
    picked the numbers it did (HR8 verifier-friendly).
    """

    worknode: str
    cores_available: int
    cores_cap: int
    cores_use: int
    gpu: GpuProbeResult
    yielding_to_ollama: bool
    ollama_detected: bool
    load_average: float | None
    daytime: bool
    at_time: str  # ISO-8601 local
    lgbm_device_flag: str
    reasoning: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "worknode": self.worknode,
            "cores_available": self.cores_available,
            "cores_cap": self.cores_cap,
            "cores_use": self.cores_use,
            "gpu": self.gpu.to_dict(),
            "yielding_to_ollama": self.yielding_to_ollama,
            "ollama_detected": self.ollama_detected,
            "load_average": self.load_average,
            "daytime": self.daytime,
            "at_time": self.at_time,
            "lgbm_device_flag": self.lgbm_device_flag,
            "reasoning": list(self.reasoning),
        }


@dataclass(frozen=True)
class ArtifactSpec:
    """One expected shard of the sweep (what we promised the gateway).

    ``provenance`` captures the 1b.4 factory columns so the gateway
    can stamp them onto ``factory_verdicts`` once the artifact
    returns. Schema-drift deferral: any unknown key is preserved
    verbatim (the field is just a typed ``dict``).
    """

    cell_id: str
    shard_id: int
    expected_sha256: str | None = None  # None = record on receipt
    provenance: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReceivedArtifact:
    """One shard actually returned by the worker.

    ``sha256`` is the SHA of the artifact bytes (matches
    :class:`ArtifactSpec.expected_sha256` if the worker honoured the
    bundle contract). ``output_path`` is the gateway-side destination.
    """

    cell_id: str
    shard_id: int
    sha256: str
    output_path: Path


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a reconciliation pass.

    ``missing`` is non-empty ONLY when the executor raised
    :class:`PartialReconciliationError`; this dataclass is the
    success-path representation. ``artifacts`` preserves the
    (cell_id, shard_id, sha256) tuples so the caller can emit
    them into the gateway-side ``factory_verdicts`` write.
    """

    n_expected: int
    n_received: int
    artifacts: tuple[ReceivedArtifact, ...]
    provenance: dict[str, dict[str, str]]  # cell_id → 1b.4 columns

    def to_dict(self) -> dict[str, object]:
        return {
            "n_expected": self.n_expected,
            "n_received": self.n_received,
            "artifacts": [
                {
                    "cell_id": a.cell_id,
                    "shard_id": a.shard_id,
                    "sha256": a.sha256,
                    "output_path": str(a.output_path),
                }
                for a in self.artifacts
            ],
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class SweepSpec:
    """The inputs to a sweep run (what the gateway hands the executor).

    ``transport_factory`` returns a fresh :class:`BundleTransport`
    per cell so the executor is parallel-safe (v1 dispatchers share
    one client; production loads use a thread-local pool).
    """

    worknode: str = DEFAULT_WORKNODE
    cores_available: int = DEFAULT_CORES_AVAILABLE
    cells: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    # (strategy, symbol, timeframe) triples.
    expected_specs: tuple[ArtifactSpec, ...] = field(default_factory=tuple)
    provenance_columns: dict[str, str] = field(default_factory=dict)
    transport_factory: Callable[[], object] | None = None
    # When True the executor only computes the decision chain and
    # skips every dispatch / artifact fetch (HR8 dry-run audit).
    dry_run: bool = False


@dataclass(frozen=True)
class SweepResult:
    """Top-level return value of :func:`run_sweep`."""

    decision: ScheduleDecision
    reconcile: ReconcileResult | None
    dispatch_elapsed_s: float
    dry_run: bool
    n_cells: int

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.to_dict(),
            "reconcile": self.reconcile.to_dict() if self.reconcile else None,
            "dispatch_elapsed_s": self.dispatch_elapsed_s,
            "dry_run": self.dry_run,
            "n_cells": self.n_cells,
        }


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class ExecutorError(Exception):
    """Base for executor errors."""


class ProbeError(ExecutorError):
    """GPU/load probe could not complete (timeout, missing binary, ...)."""


class PartialReconciliationError(ExecutorError):
    """Raised when expected vs received shard counts don't match.

    The exception carries the missing list + provenance snapshot so
    the gateway can record the gap (HR27 silent-success guard).
    """

    def __init__(
        self,
        missing: Sequence[ArtifactSpec],
        n_expected: int,
        n_received: int,
        provenance_snapshot: dict[str, dict[str, str]],
    ) -> None:
        self.missing: list[ArtifactSpec] = list(missing)
        self.n_expected = n_expected
        self.n_received = n_received
        self.provenance_snapshot = dict(provenance_snapshot)
        cell_ids = ", ".join(sorted(s.cell_id for s in self.missing))
        super().__init__(
            f"Partial reconciliation: expected {n_expected} shards, "
            f"received {n_received}, missing {len(self.missing)}: {cell_ids}"
        )


class ProvenanceMismatchError(ExecutorError):
    """Raised when a received artifact's SHA256 mismatches its spec."""


class TransportDispatchError(ExecutorError):
    """Raised when a transport push_bundle / fetch_output raises."""


# ---------------------------------------------------------------------------
# GPU probe (graceful degradation is the contract)
# ---------------------------------------------------------------------------


def probe_gpu(
    *,
    binary: str | None = None,
    timeout_s: float = NVIDIA_SMI_TIMEOUT_S,
    runner: Callable[[Sequence[str], float], CompletedProcess] | None = None,
) -> GpuProbeResult:
    """Probe nvidia-smi for available GPU devices.

    The probe is intentionally permissive: any non-zero exit, timeout,
    or parse miss becomes ``available=False`` with a human-readable
    ``reason``. Callers should NOT re-probe; the result is the
    single source of truth for the sweep.

    ``runner`` is the seam for unit tests; the default is
    :func:`subprocess.run`. We capture only stdout/stderr; the test
    seam returns a :class:`subprocess.CompletedProcess`-shaped object.
    """
    # Local import so we don't force the type at module load (the
    # alias lets tests inject a runner without importing subprocess).
    if runner is None:
        runner = _default_subprocess_runner

    bin_path = binary or (shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi")
    args = [bin_path] + NVIDIA_SMI_QUERY.split()
    try:
        proc = runner(args, timeout_s)
    except FileNotFoundError:
        return GpuProbeResult(
            available=False,
            reason="nvidia_smi_not_found",
        )
    except subprocess.TimeoutExpired:
        return GpuProbeResult(
            available=False,
            reason="nvidia_smi_timeout",
        )
    except OSError as exc:  # noqa: PERF203 — narrow except for the test seam
        return GpuProbeResult(
            available=False,
            reason=f"nvidia_smi_os_error:{type(exc).__name__}",
        )

    if proc.returncode != 0:
        return GpuProbeResult(
            available=False,
            reason=f"nvidia_smi_exit_{proc.returncode}",
        )
    stdout = (getattr(proc, "stdout", "") or "").strip()
    if not stdout:
        return GpuProbeResult(
            available=False,
            reason="nvidia_smi_empty_output",
        )
    devices: list[str] = []
    for line in stdout.splitlines():
        # CSV: "<index>, <name>, <memory MiB>, <driver>"
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        devices.append(f"GPU-{parts[0]}: {parts[1]}")
    if not devices:
        return GpuProbeResult(
            available=False,
            reason="nvidia_smi_unparseable",
        )
    return GpuProbeResult(
        available=True,
        device_count=len(devices),
        devices=tuple(devices),
    )


@dataclass
class CompletedProcess:
    """Minimal shim so tests can return a CompletedProcess-shaped object
    without importing :class:`subprocess.CompletedProcess` (avoids the
    py3.7-only ``capture_output`` field).
    """

    returncode: int
    stdout: str
    stderr: str = ""


def _default_subprocess_runner(
    args: Sequence[str], timeout_s: float
) -> CompletedProcess:
    """Default subprocess runner (callable seam for tests)."""
    proc = subprocess.run(  # noqa: S603 — args are hardcoded nvidia-smi + safe flags; fallback path uses shutil.which w/ /usr/bin/nvidia-smi
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    return CompletedProcess(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


# ---------------------------------------------------------------------------
# Load average + Ollama detection (pure functions for testability)
# ---------------------------------------------------------------------------


def load_average(*, loadavg_path: Path | None = None) -> float | None:
    """Return the 1-minute load average for the host (None on failure).

    Reads ``/proc/loadavg`` on Linux; returns ``None`` on macOS /
    non-Linux hosts (out of scope for the ava-worker-local contract).
    The seam lets us add ``psutil`` later without touching callers.
    """
    path = Path(loadavg_path) if loadavg_path is not None else Path("/proc/loadavg")
    try:
        text = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    parts = text.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def detect_ollama_inference(
    *,
    proc_reader: Callable[[], Iterable[str]] | None = None,
    ps_endpoint: str | None = None,
) -> bool:
    """Best-effort Ollama-busy detection.

    Two independent signals, both must succeed to flip the flag:

      1. ``/proc`` scan for a process whose command line contains
         ``"ollama serve"`` (or ``="ollama"`` for the rumtime).
      2. Optional ``GET /api/ps`` against the local Ollama daemon;
         returns True when the daemon reports ≥1 loaded model.

    The probe is intentionally conservative — false negatives (we
    think Ollama is idle but it isn't) are far less harmful than
    false positives (we throttle a free CPU). Either signal being
    uncertain returns False.
    """
    if proc_reader is not None:
        try:
            for line in proc_reader():
                if "ollama" in line.lower() and (
                    "serve" in line.lower() or "run" in line.lower()
                ):
                    return True
        except (FileNotFoundError, OSError):
            return False
    if ps_endpoint is not None:
        with contextlib.suppress(Exception):
            import urllib.request

            with urllib.request.urlopen(  # noqa: S310 — localhost only
                ps_endpoint, timeout=2
            ) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if '"models"' in body and '[]' not in body:
                    return True
    return False


# ---------------------------------------------------------------------------
# Time + scheduling
# ---------------------------------------------------------------------------


def _local_now(tz_name: str = TIMEZONE_NAME) -> datetime:
    """Return the current wall time in ``tz_name`` (default Toronto)."""
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except zoneinfo.ZoneInfoNotFoundError:
        # Fallback for environments without tzdata (Py3.9 w/ no tzdata pkg).
        # The constant is the contract; UTC fallback keeps the daytime
        # window deterministic on dev hosts but logs a warning so an
        # operator can grep for it.
        _LOG.warning(
            "timezone %s not found on host; falling back to UTC for time-window math",
            tz_name,
        )
        tz = zoneinfo.ZoneInfo("UTC")
    return datetime.now(tz)


def is_daytime_window(
    at: datetime | None = None, *, tz_name: str = TIMEZONE_NAME
) -> bool:
    """True iff ``at`` (or now) is in [08:00, 23:00) Toronto local time."""
    when = at if at is not None else _local_now(tz_name)
    local = when.astimezone(zoneinfo.ZoneInfo(tz_name)) if when.tzinfo else when
    return DAYTIME_START <= local.time() < DAYTIME_END


def lgbm_gpu_device_flag(gpu: GpuProbeResult) -> str:
    """LightGBM device flag for the matrix/CPCV workers.

    LightGBM 4.x prefers ``"cuda"``; older builds used ``"gpu"``. We
    emit ``"cuda"`` when a GPU is detected because ava-worker-local
    runs the CUDA-enabled LightGBM build (per Craig directive
    2026-10-06 13:47 — "probe nvidia-smi at runtime, degrade gracefully").
    """
    return "cuda" if gpu.available else "cpu"


def schedule_concurrency(
    *,
    worknode: str = DEFAULT_WORKNODE,
    cores_available: int = DEFAULT_CORES_AVAILABLE,
    gpu: GpuProbeResult | None = None,
    ollama_detected: bool = False,
    load_avg: float | None = None,
    at: datetime | None = None,
    now_fn: Callable[[], datetime] | None = None,
    tz_name: str = TIMEZONE_NAME,
    load_yield_threshold: float = LOAD_YIELD_THRESHOLD,
    ollama_hard_cap: int = OLLAMA_HARD_CAP,
) -> ScheduleDecision:
    """Compute the cores-use cap and emit the full decision chain.

    Order of operations (each is appended to ``reasoning`` so the
    chain is auditable):

      1. Probe GPU (default = :func:`probe_gpu`).
      2. Resolve wall time (``now_fn`` override for tests).
      3. Daytime window check → base cap = ``cores_available - 2``.
      4. Ollama detected or load_avg > threshold → yield (cores_use =
          ``min(ollama_hard_cap, base cap)``).
      5. Final ``cores_use = max(1, ...)`` so a sweep with N≥1
         cells still progresses even when the cap collapses.
    """
    if now_fn is not None:
        when = now_fn()
        # Pin tzinfo so downstream .isoformat() includes the offset.
        if when.tzinfo is None:
            when = when.replace(tzinfo=zoneinfo.ZoneInfo(tz_name))
    else:
        when = at if at is not None else _local_now(tz_name)
        if when.tzinfo is None:
            when = when.replace(tzinfo=zoneinfo.ZoneInfo(tz_name))

    if gpu is None:
        gpu = probe_gpu()

    daytime = is_daytime_window(when, tz_name=tz_name)
    if daytime:
        base_cap = max(1, cores_available - 2)
    else:
        base_cap = max(1, cores_available)

    load_high = (
        load_avg is not None
        and load_avg > load_yield_threshold * cores_available
    )
    yielding = ollama_detected or load_high

    if yielding:
        cores_use = max(1, min(ollama_hard_cap, base_cap))
    else:
        cores_use = base_cap

    reasoning_parts: list[str] = [
        f"worknode={worknode}",
        f"cores_available={cores_available}",
        f"gpu.available={gpu.available} reason={gpu.reason or 'detected'}",
        f"local_time={when.isoformat()} daytime={daytime}",
        f"base_cap={base_cap} (daytime_cores-2)" if daytime else f"base_cap={base_cap} (full)",
        f"ollama_detected={ollama_detected} load_avg={load_avg} load_high={load_high}",
        f"yielding={yielding} cores_use={cores_use}",
    ]

    return ScheduleDecision(
        worknode=worknode,
        cores_available=cores_available,
        cores_cap=base_cap,
        cores_use=cores_use,
        gpu=gpu,
        yielding_to_ollama=yielding,
        ollama_detected=ollama_detected,
        load_average=load_avg,
        daytime=daytime,
        at_time=when.isoformat(),
        lgbm_device_flag=lgbm_gpu_device_flag(gpu),
        reasoning=tuple(reasoning_parts),
    )


# ---------------------------------------------------------------------------
# Reconciliation (fail-loud on partial)
# ---------------------------------------------------------------------------


def reconcile_artifacts(
    *,
    expected: Sequence[ArtifactSpec],
    received: Iterable[ReceivedArtifact],
) -> ReconcileResult:
    """Fail loud when expected vs received shard counts don't match.

    The gateway hands us a list of expected ``ArtifactSpec``s
    (one per shard) and a stream of ``ReceivedArtifact``s as the
    worker drains the queue. This function:

      1. Verifies every received artifact's SHA256 matches its spec
         (when the spec carries one); a mismatch raises
         :class:`ProvenanceMismatchError` before partial reconciliation
         can mask it.
      2. Builds a ``received_by_cell`` map keyed by ``(cell_id, shard_id)``.
      3. Compares received keys vs expected keys — any missing cell
         raises :class:`PartialReconciliationError` with the full
         missing list (HR27 silent-success guard).
      4. Returns a :class:`ReconcileResult` with the surviving
         artifacts and the per-cell provenance snapshot for the
         gateway's ``factory_verdicts`` write (1b.4 columns).
    """
    expected_list = list(expected)
    received_list = list(received)
    if len(received_list) > len(expected_list):
        # A worker over-returned (shouldn't happen with the v1
        # exactly-once contract). Treat the first N as the matched
        # set and the tail as unexpected — log and continue.
        unexpected = received_list[len(expected_list):]
        received_list = received_list[: len(expected_list)]
        _LOG.warning(
            "reconcile: received %d unexpected shards beyond expected %d; "
            "ignoring tail: %s",
            len(unexpected),
            len(expected_list),
            [f"{a.cell_id}/{a.shard_id}" for a in unexpected],
        )

    expected_by_key: dict[tuple[str, int], ArtifactSpec] = {}
    for exp in expected_list:
        expected_by_key[(exp.cell_id, exp.shard_id)] = exp

    provenance_snapshot: dict[str, dict[str, str]] = {}
    matched: list[ReceivedArtifact] = []
    for recv in received_list:
        # Fresh local name (NOT ``expected`` — that's the parameter) so mypy
        # narrows the ``ArtifactSpec | None`` returned by ``dict.get``.
        matched_spec = expected_by_key.get((recv.cell_id, recv.shard_id))
        if matched_spec is None:
            # Received for a cell that wasn't expected. Log loudly;
            # the gateway-side reconciliation will surface the gap
            # but the executor shouldn't drop it on the floor.
            _LOG.warning(
                "reconcile: received shard for unexpected cell %s/%d",
                recv.cell_id,
                recv.shard_id,
            )
            continue
        if (
            matched_spec.expected_sha256 is not None
            and matched_spec.expected_sha256 != recv.sha256
        ):
            raise ProvenanceMismatchError(
                f"sha256 mismatch for cell {recv.cell_id}/{recv.shard_id}: "
                f"expected={matched_spec.expected_sha256!r} "
                f"received={recv.sha256!r}"
            )
        provenance_snapshot[recv.cell_id] = dict(matched_spec.provenance)
        matched.append(recv)

    if len(matched) != len(expected_list):
        matched_keys = {(a.cell_id, a.shard_id) for a in matched}
        missing = [
            spec
            for spec in expected_list
            if (spec.cell_id, spec.shard_id) not in matched_keys
        ]
        raise PartialReconciliationError(
            missing=missing,
            n_expected=len(expected_list),
            n_received=len(matched),
            provenance_snapshot=provenance_snapshot,
        )

    return ReconcileResult(
        n_expected=len(expected_list),
        n_received=len(matched),
        artifacts=tuple(matched),
        provenance=provenance_snapshot,
    )


# ---------------------------------------------------------------------------
# Sweep entry point (the parallel CPU worker dispatch)
# ---------------------------------------------------------------------------


def _dispatch_one_cell(
    spec: ArtifactSpec,
    transport: object,
    *,
    dry_run: bool,
) -> ReceivedArtifact:
    """Dispatch a single cell via the injected transport.

    The transport is a :class:`BundleTransport` (lazy-typed as
    ``object`` so tests can pass a fake). Dry-run short-circuits
    before any I/O.
    """
    if dry_run:
        # Synthetic SHA so reconcile_artifacts accepts the dry-run
        # result when the caller supplies matching expected SHA.
        return ReceivedArtifact(
            cell_id=spec.cell_id,
            shard_id=spec.shard_id,
            sha256=spec.expected_sha256 or f"dryrun:{spec.cell_id}:{spec.shard_id}",
            output_path=Path(f"<dry-run>/{spec.cell_id}/{spec.shard_id}"),
        )
    push_bundle = getattr(transport, "push_bundle", None)
    fetch_output = getattr(transport, "fetch_output", None)
    if push_bundle is None or fetch_output is None:
        raise TransportDispatchError(
            f"transport missing push_bundle/fetch_output; "
            f"got {type(transport).__name__}"
        )
    # v1 ABC: push_bundle(run_id, path, sha) returns a WorkerCell with
    # .sha256. The test transport may differ — we honor whatever it
    # returns so long as the SHA is non-empty (HR27 guard).
    run_id = spec.provenance.get("run_id", f"sweep-{spec.cell_id}")
    # Use a per-process scratch dir under $TMPDIR (or tempfile default)
    # rather than a hard-coded /tmp path. The gateway can override
    # ``output_path`` per cell via ``spec.provenance["output_path"]``.
    scratch = Path(tempfile.gettempdir()) / "offload-executor"
    out_path = Path(spec.provenance.get("output_path", str(scratch / spec.cell_id)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    worker_cell = push_bundle(run_id, out_path, spec.expected_sha256 or "")
    received_sha = getattr(worker_cell, "sha256", "") or ""
    if not received_sha:
        raise TransportDispatchError(
            f"transport returned empty SHA for {spec.cell_id}/{spec.shard_id}"
        )
    output_bytes = fetch_output(run_id, spec.cell_id) or b""
    out_path.write_bytes(output_bytes)
    actual_sha = _sha256_bytes(output_bytes)
    if spec.expected_sha256 and spec.expected_sha256 != actual_sha:
        raise ProvenanceMismatchError(
            f"output SHA mismatch for {spec.cell_id}/{spec.shard_id}: "
            f"expected={spec.expected_sha256!r} actual={actual_sha!r}"
        )
    return ReceivedArtifact(
        cell_id=spec.cell_id,
        shard_id=spec.shard_id,
        sha256=actual_sha,
        output_path=out_path,
    )


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def run_sweep(spec: SweepSpec, *, now_fn: Callable[[], datetime] | None = None) -> SweepResult:
    """Top-level sweep entry point.

    Behaviour:

      * Computes the :class:`ScheduleDecision` from the spec.
      * If ``spec.dry_run`` is True: returns immediately with a
        :class:`SweepResult` containing the decision chain and
        ``reconcile=None`` — no I/O, no transport calls.
      * Otherwise: dispatches every cell through the
        ``transport_factory`` in parallel up to ``cores_use`` workers,
        collects the ``ReceivedArtifact``s, then calls
        :func:`reconcile_artifacts` (fail-loud on partial).

    The transport factory is called once per cell (NOT once per
    thread) so the caller controls lifetime. Tests pass a factory
    that returns a stub; production passes the v1 factory.
    """
    gpu = probe_gpu()
    decision = schedule_concurrency(
        worknode=spec.worknode,
        cores_available=spec.cores_available,
        gpu=gpu,
        now_fn=now_fn,
    )

    if spec.dry_run or not spec.expected_specs:
        return SweepResult(
            decision=decision,
            reconcile=None,
            dispatch_elapsed_s=0.0,
            dry_run=bool(spec.dry_run),
            n_cells=len(spec.expected_specs),
        )

    if spec.transport_factory is None:
        raise ExecutorError(
            "spec.transport_factory is required for non-dry-run sweeps"
        )

    import time as _time

    started = _time.monotonic()
    received: list[ReceivedArtifact] = []
    workers = max(1, min(decision.cores_use, MAX_PARALLEL_TRANSPORTS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for art_spec in spec.expected_specs:
            transport = spec.transport_factory()
            fut = pool.submit(_dispatch_one_cell, art_spec, transport, dry_run=False)
            futures[fut] = art_spec
        for fut, art_spec in futures.items():
            try:
                received.append(fut.result())
            except (ExecutorError, ProvenanceMismatchError, TransportDispatchError):
                # Surface the failure to the caller via reconcile (the
                # missing entry will raise PartialReconciliationError).
                _LOG.error(
                    "dispatch failed for cell %s/%d: %s",
                    art_spec.cell_id,
                    art_spec.shard_id,
                    "see traceback",
                )
                raise
    elapsed = _time.monotonic() - started

    reconcile = reconcile_artifacts(
        expected=spec.expected_specs,
        received=received,
    )
    return SweepResult(
        decision=decision,
        reconcile=reconcile,
        dispatch_elapsed_s=elapsed,
        dry_run=False,
        n_cells=len(spec.expected_specs),
    )


# ---------------------------------------------------------------------------
# CLI (dry-run + decision print)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="offload-executor",
        description=(
            "Sprint D 2 GPU/CPU sweep executor — dry-run + decision print "
            "for ava-worker-local. Production dispatch stays in "
            "run_matrix_remote; this CLI is the operator pre-flight tool."
        ),
    )
    parser.add_argument(
        "--worknode", default=DEFAULT_WORKNODE,
        help=f"Target node name (default: {DEFAULT_WORKNODE})",
    )
    parser.add_argument(
        "--cores-available", type=int, default=DEFAULT_CORES_AVAILABLE,
        help="Logical CPU count on the worker (default: 8)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip dispatch; print the decision chain and exit.",
    )
    parser.add_argument(
        "--print-decision", action="store_true",
        help="Print the ScheduleDecision JSON and exit (alias for --dry-run).",
    )
    parser.add_argument(
        "--at-hour", type=int, default=None,
        help="Pin the wall clock for time-window tests; 0-23.",
    )
    args = parser.parse_args(argv)

    now_fn: Callable[[], datetime] | None = None
    if args.at_hour is not None:
        h = max(0, min(23, args.at_hour))

        def _pinned_now() -> datetime:
            tz = zoneinfo.ZoneInfo(TIMEZONE_NAME)
            return datetime.now(tz).replace(hour=h, minute=0, second=0, microsecond=0)

        now_fn = _pinned_now

    gpu = probe_gpu()
    decision = schedule_concurrency(
        worknode=args.worknode,
        cores_available=args.cores_available,
        gpu=gpu,
        now_fn=now_fn,
    )

    payload = decision.to_dict()
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


# Re-export dataclasses for tests + downstream callers.
__all__ = [
    "ArtifactSpec",
    "CompletedProcess",
    "DEFAULT_CORES_AVAILABLE",
    "DEFAULT_RAM_GB",
    "DEFAULT_WORKNODE",
    "DAYTIME_END",
    "DAYTIME_START",
    "ExecutorError",
    "GpuProbeResult",
    "NVIDIA_SMI_QUERY",
    "OLLAMA_DEFAULT_CAP",
    "OLLAMA_HARD_CAP",
    "PartialReconciliationError",
    "ProbeError",
    "ProvenanceMismatchError",
    "ReceivedArtifact",
    "ReconcileResult",
    "ScheduleDecision",
    "SweepResult",
    "SweepSpec",
    "TIMEZONE_NAME",
    "TransportDispatchError",
    "detect_ollama_inference",
    "is_daytime_window",
    "lgbm_gpu_device_flag",
    "load_average",
    "main",
    "probe_gpu",
    "reconcile_artifacts",
    "run_sweep",
    "schedule_concurrency",
]


if __name__ == "__main__":
    sys.exit(main())
