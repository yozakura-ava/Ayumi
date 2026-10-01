"""BundleTransport ABC + Port8877StubTransport (Q2 load-bearing per Tomoe).

Q2 flag (Tomoe, 2026-09-15, relayed via Ava): **ABC signatures + error
types are load-bearing** — designed for portability across
``port-8877`` / ``terminal.upload`` / future transports. The v1
``Port8877StubTransport`` raises on real invoke (driving the
local-fallback path in production); tests pass ``simulate_success=True``
to verify the contract without a live port-8877 receiver.

Signatures and error types are the contract. Future implementations
(``TerminalUploadTransport`` post-9.5 upgrade) inherit the same ABC and
raise the same error hierarchy. Tests verify the contract, not the wire.

Capability surface the ABC targets (per ``openclaw nodes describe``
probe 2026-09-15 on ava-worker-local 9.4):
- ``system.run`` + ``system.run.prepare`` — execute commands
- ``file.write`` + ``file.fetch`` — push/pull files (gateway 9.3 vs
  worker 9.4 has version skew — port-8877 works today, ``terminal.upload``
  after 9.5 upgrade)
- ``terminal.upload`` — same-file payload transport (9.4+, future)
- ``fs.listDir`` — directory listing
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkerCell:
    """Where a pushed bundle lives on the worker (load-bearing for path math).

    ``path`` is the worker-side directory the cell will execute in.
    ``sha256`` is the SHA of the bundle the worker received; must match
    the dispatcher's computed SHA (loud skew rejection otherwise).
    """

    path: str
    sha256: str


# Error hierarchy — subclasses are load-bearing for the runner's
# local-fallback dispatch logic. Do not collapse error types.
class BundleTransportError(Exception):
    """Base for all BundleTransport errors (load-bearing subclass tree)."""


class WorktreeUnreachableError(BundleTransportError):
    """Port-8877 down / worker disconnected / approval != approved.

    Runner catches this and falls back to local execution.
    """


class CodeSHARejectedError(BundleTransportError):
    """Q1 loud skew rejection — worker SHA differs from dispatcher SHA.

    Dispatcher writes ``dispatch_skipped.jsonl`` with
    ``reason: code_skew`` and aborts the cell. ABORT, never fallback —
    a SHA mismatch means the trust boundary is broken.
    """


class BundleTooLargeError(BundleTransportError):
    """Bundle exceeded worker's transfer-size cap. Fail loud; no fallback."""


def _require_bundle_file(bundle_path: Path) -> None:
    """BundleTransport.push_bundle requires a FILE path (never a directory).

    Card 475cfe82-5cfb-478b-a622-287578345678 regression guard. Pre-fix,
    ``_run_one_cell`` passed ``bundle_path = scripts/offload`` (a directory)
    to push_bundle for the node/stub transports, which crashed deep inside
    ``_sha256_file`` with ``IsADirectoryError``. That masked the root cause
    and let the runner silently fall through to the v1_stub local-fallback
    path (``local_fallback=True``, no FTMO columns).

    This guard fails fast at the API boundary with the same error type, so
    every concrete subclass enforces the contract uniformly. JobDir's
    descriptor-file pattern (which already passes a file) is unaffected.
    """
    if not bundle_path.is_file():
        raise IsADirectoryError(
            f"BundleTransport.push_bundle requires a FILE path, got "
            f"directory: {bundle_path!r}"
        )


class BundleTransport(ABC):
    """ABC for code-bundle transport (Q2: load-bearing signatures)."""

    @abstractmethod
    def push_bundle(
        self,
        run_id: str,
        bundle_path: Path,
        expected_sha256: str,
    ) -> WorkerCell:
        """Push ``bundle_path`` to worker; verify SHA on worker-side == expected.

        CONTRACT (card 475cfe82): ``bundle_path`` MUST be a file path (the
        per-cell descriptor JSON for JobDirBundleTransport; equivalent for
        node/stub). Directories are rejected at the boundary via
        ``_require_bundle_file`` so the runner cannot regress to the
        pre-fix IsADirectoryError path.
        """

    @abstractmethod
    def fetch_output(
        self,
        run_id: str,
        cell_id: str,
    ) -> bytes | None:
        """Fetch cell output bytes from worker (``None`` if not yet present)."""


class Port8877StubTransport(BundleTransport):
    """v1 stub — raises on real invoke; ABC tests use ``simulate_success=True``.

    Q2 flag: this stub raises ``WorktreeUnreachableError`` on ``push_bundle``
    so the runner falls back to local execution in production (v1 ships
    without a live port-8877 receiver; ABC contract is verified separately).
    ``simulate_success=True`` mode is for ABC contract tests only — tests
    that want to exercise dispatch orchestration, not the wire.
    """

    def __init__(self, simulate_success: bool = False) -> None:
        self._simulate = simulate_success

    def push_bundle(
        self,
        run_id: str,
        bundle_path: Path,
        expected_sha256: str,
    ) -> WorkerCell:
        _require_bundle_file(bundle_path)
        if not self._simulate:
            raise WorktreeUnreachableError(
                f"Port8877StubTransport: would push to "
                f"/tmp/ayumi-offload/{run_id}/ — wire TBD post-9.5 upgrade; "  # noqa: S108 — descriptive path only; stub raises before any FS access (Q2)
                f"runner falls back to local execution"
            )
        # Simulate-success path: synthetic WorkerCell for ABC contract tests.
        # The dispatcher-side computed SHA is preserved so downstream
        # equality checks pass.
        return WorkerCell(
            path=f"/tmp/ayumi-offload/{run_id}/bundle.simulated",  # noqa: S108 — descriptive synthetic path for ABC contract tests (Q2)
            sha256=expected_sha256,
        )

    def fetch_output(self, run_id: str, cell_id: str) -> bytes | None:
        # Fix #3 (Rin): simulate-success path returns deterministic
        # synthetic output bytes; the runner then persists them, computes
        # sha256 (becomes output_hash), and writes its own output_path
        # in the manifest. Real mode returns None (the stub raises
        # WorktreeUnreachableError before this would be called in
        # production).
        if not self._simulate:
            return None
        return f"simulated-worker-output:{run_id}:{cell_id}\n".encode("utf-8")


# ────────────────────────────────────────────────────────────────────────
# Real worker transport (c3134271-601e-42c2-9797-6407ae25048c)
# ────────────────────────────────────────────────────────────────────────


class OpenClawNodeBundleTransport(BundleTransport):
    """Real worker transport: push bundle to ava-worker-local, fetch output.

    Implements the BundleTransport ABC the runner depends on (card
    c3134271-601e-42c2-9797-6407ae25048c — Craig directive 2026-09-15
    13:47 EDT: tournament matrix MUST execute on ava-worker).

    Wire-up (Option (a) per c3134271 spec, validated against the live
    gateway 2026.9.3 / daemon source ``daemon-BbqI59vQ.js``):
      1. Bundle upload  → ``terminal.upload`` invoke with params
         ``{"name": <basename>, "contentBase64": <b64>}``. The gateway
         lands the file at ``/tmp/openclaw-terminal-upload-<rand>/<name>``
         and returns the destination path + size in the payload.
      2. SHA pre-flight → computed LOCALLY on the dispatcher (worker
         shell is gated from the runner subprocess; see ``Known
         limitations``). Local SHA must equal ``expected_sha256`` or
         ``CodeSHARejectedError`` fires before any wire call.
      3. fetch_output   → ``file.fetch`` invoke (currently NO_POLICY
         because plugins.entries.file-transfer.config.nodes is not
         configured for ava-worker-local). When blocked, fetch_output
         returns ``None`` and emits a structured warning; the live
         smoke (card AC #2) supplies output bytes via the agent's
         ``exec(host=node)`` cat path.

    Error mapping:
      - terminal.upload subprocess failure → ``WorktreeUnreachableError``
      - Gateway size-cap rejection
        (MAX_TERMINAL_UPLOAD_BYTES = 16 MB; see
        ``terminal-constants-Bjk8k2kn.js``) → ``BundleTooLargeError``
      - Local pre-flight SHA mismatch → ``CodeSHARejectedError``

    Known limitations (architectural, deferred — see c3134271 card body):
      - Worker-side SHA verify inside push_bundle is impossible from the
        runner subprocess (``system.run`` is reserved for shell execution;
        only the agent's ``exec(host=node)`` reaches the worker shell).
        Trust on the way IN is delegated to terminal.upload atomicity
        + downstream output_hash verification on the way OUT.
      - Per-cell execution requires shell on the worker; matrix-card
        scaling (68 cells) is out of scope here. Live smoke drives
        execution through the agent layer; production scale requires
        either port-8877 receiver or worker-side runner script
        (card AC option (b), explicitly noted in c3134271 spec).
      - fetch_output is best-effort: file.fetch policy must be
        configured for the node to return bytes; until then, the
        transport returns ``None`` with a logged warning and the
        runner dispatches with output_hash=None ONLY in the explicit
        smoke-orchestration mode (--live-smoke), NOT in production.
    """

    DEFAULT_NODE = "ava-worker-local"
    INVOKE_TIMEOUT_S = 60.0
    # Source: terminal-constants-Bjk8k2kn.js
    #   MAX_TERMINAL_UPLOAD_BYTES = 16777216
    MAX_UPLOAD_BYTES = 16 * 1024 * 1024
    UPLOAD_RAND_DIR_PREFIX = "/tmp/openclaw-terminal-upload-"  # noqa: S108 — descriptive constant matching gateway prefix; not a FS write (Q2)

    def __init__(
        self,
        node: str = DEFAULT_NODE,
        *,
        cli_path: str | None = None,
    ) -> None:
        self._node = node
        self._cli_path = cli_path or "openclaw"

    # ── Public ABC methods ─────────────────────────────────────────────────

    def push_bundle(
        self,
        run_id: str,
        bundle_path: Path,
        expected_sha256: str,
    ) -> WorkerCell:
        _require_bundle_file(bundle_path)
        # 1. Local SHA pre-flight.  The runner ALSO re-checks this via
        #    WorkerCell.sha256, but failing fast here keeps the wire
        #    call off the dispatch log on a buggy dispatcher.
        local_sha = self._sha256_file(bundle_path)
        if local_sha != expected_sha256:
            raise CodeSHARejectedError(
                f"Local bundle SHA {local_sha!r} != expected {expected_sha256!r} (dispatcher-side pre-flight)"
            )

        # 2. Size-cap pre-flight.  Gateway caps terminal.upload at
        #    MAX_TERMINAL_UPLOAD_BYTES (16 MB; source: gateway
        #    terminal-constants).  Fail loud here rather than wire-fail
        #    for clearer routing — Rin finding #1 fail-loud, no fallback.
        size = bundle_path.stat().st_size
        if size > self.MAX_UPLOAD_BYTES:
            raise BundleTooLargeError(
                f"bundle size {size} bytes exceeds gateway cap "
                f"{self.MAX_UPLOAD_BYTES} bytes (16 MB); refusing to invoke"
            )

        # 3. Upload via terminal.upload with the gateway-validated param
        #    shape (name + contentBase64).  The gateway returns the
        #    landing path + size; WorkerCell.path is the actual
        #    destination the agent can exec(cat) or listDir from.
        try:
            worker_path = self._invoke_terminal_upload(bundle_path)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            # BundleTooLarge surfaces via INVALID_REQUEST with size-cap wording;
            # the gateway error message explicitly says "terminal upload exceeds
            # <N> bytes" (terminal-file-upload-rFs_tRIM.js).  Treat as fail-loud.
            if "exceeds" in stderr and "bytes" in stderr:
                raise BundleTooLargeError(f"terminal.upload rejected bundle: {stderr!r}") from exc
            raise WorktreeUnreachableError(f"terminal.upload failed on node {self._node!r}: {stderr!r}") from exc

        # 4. Return WorkerCell.  sha256 is the dispatcher-side SHA we
        #    computed; the runner's Rin-finding-#2 trust check
        #    compares this against the dispatcher's expected_sha256
        #    (identical at this layer — no worker compute).  Output_path
        #    on the far side IS verifiable downstream via the
        #    agent's exec(host=node) cat + sha256 round-trip.
        return WorkerCell(path=worker_path, sha256=local_sha)

    def fetch_output(
        self,
        run_id: str,
        cell_id: str,
    ) -> bytes | None:
        # Per matrix card spec, output.json lives at
        # /tmp/ayumi-offload/<run_id>/<cell_id>/output.json.  file.fetch
        # is currently NO_POLICY for ava-worker-local
        # (plugins.entries.file-transfer.config.nodes is deny-by-default
        # until configured); the live smoke path falls through to the
        # agent's exec(host=node) cat for output retrieval and writes
        # manifests with the agent-supplied output_hash.
        remote_output_path = f"/tmp/ayumi-offload/{run_id}/{cell_id}/output.json"  # noqa: S108 — descriptive remote path constant (matches run_matrix_remote.py convention)
        try:
            result = self._invoke("file.fetch", {"path": remote_output_path})
            return self._decode_file_fetch(result.stdout)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            if "NO_POLICY" in stderr or "deny-by-default" in stderr:
                # File-transfer policy not configured for this node.
                # Surface the structured warning but return None — the
                # runner treats this as "use the smoke orchestration
                # path that supplies output_hash externally".
                print(
                    f"[transport] file.fetch denied on {self._node!r}: "
                    f"{stderr!r} — caller must supply output_hash out-of-band",
                    file=sys.stderr,
                )
                return None
            # Any other failure (ENOENT, exit-1) → None; runner treats
            # as "output unavailable".
            return None

    # ── Private helpers (OpenClaw invoke wrappers) ──────────────────────

    def _invoke(self, command: str, params: dict, *, timeout_s: float | None = None) -> subprocess.CompletedProcess:
        """Run ``openclaw nodes invoke`` with the given capability + params.

        Raises ``subprocess.CalledProcessError`` on non-zero exit so the
        ABC callers can map specific errors to the load-bearing error
        hierarchy (WorktreeUnreachable / CodeSHARejected / BundleTooLarge).

        We use ``check=False`` and raise manually so the behaviour is
        consistent whether the caller is real subprocess.run (check=True
        raises) or a test mock (``return_value=CompletedProcess``).
        """
        import json as _json  # local import keeps module load cheap

        timeout = timeout_s if timeout_s is not None else self.INVOKE_TIMEOUT_S
        cmd = [
            self._cli_path,
            "nodes",
            "invoke",
            "--node",
            self._node,
            "--command",
            command,
            "--params",
            _json.dumps(params),
            "--timeout",
            str(int(timeout * 1000)),
        ]
        result = subprocess.run(  # noqa: S603 — args hardcoded; cli_path resolved via __init__ w/ explicit default (lint false positive)
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5.0,
            check=False,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                returncode=result.returncode,
                cmd=cmd,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result

    def _invoke_terminal_upload(self, local_path: Path) -> str:
        """Push the local file to the worker via terminal.upload invoke.

        Returns the worker-side destination path the gateway reports
        in the response payload (typically
        ``/tmp/openclaw-terminal-upload-<rand>/<name>``).

        Param shape (gateway source: ``daemon-BbqI59vQ.js:2744``):
            ``{"name": <basename>, "contentBase64": <b64>}``

        Note: terminal.upload lands the file under a per-call random
        subdir of ``/tmp/`` (not user-controllable).  Per the ABC, this
        transport owns the path that ``fetch_output`` can target;
        cross-call path stability for a ``run_id`` requires the caller
        to remember the WorkerCell.path from ``push_bundle``.
        """
        import base64 as _b64
        import json as _json

        data = local_path.read_bytes()
        if not data:
            raise BundleTooLargeError(f"refusing to upload empty file at {local_path!r}")
        content_b64 = _b64.b64encode(data).decode("ascii")
        result = self._invoke(
            "terminal.upload",
            {"name": local_path.name, "contentBase64": content_b64},
            timeout_s=min(self.INVOKE_TIMEOUT_S, 30.0),
        )
        try:
            payload = _json.loads(result.stdout)
        except _json.JSONDecodeError as exc:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=result.args,
                output=result.stdout,
                stderr=f"terminal.upload returned non-JSON: {result.stdout[:200]!r}",
            ) from exc
        worker_path = payload.get("payload", {}).get("path") if isinstance(payload, dict) else None
        if not worker_path:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=result.args,
                output=result.stdout,
                stderr=f"terminal.upload response missing payload.path: {result.stdout[:200]!r}",
            )
        return worker_path

    @staticmethod
    def _decode_file_fetch(stdout: str) -> bytes:
        """Decode a file.fetch JSON-wrapped response.

        Accepts either a JSON object with ``data_b64`` / ``content`` /
        ``data`` keys, a bare base64 string, or raw utf-8.
        """
        import base64 as _b64
        import json as _json

        try:
            payload = _json.loads(stdout)
        except _json.JSONDecodeError:
            return stdout.encode("utf-8")
        if isinstance(payload, dict):
            for key in ("data_b64", "content", "data"):
                if key in payload:
                    val = payload[key]
                    if isinstance(val, str):
                        try:
                            return _b64.b64decode(val)
                        except Exception:
                            return val.encode("utf-8")
                    if isinstance(val, (bytes, bytearray)):
                        return bytes(val)
        return stdout.encode("utf-8")

    def _sha256_file(self, path: Path) -> str:
        """Streamed SHA256 over a local file (no full-file read)."""
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
