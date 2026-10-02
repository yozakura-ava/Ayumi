"""Unit tests for the node-local staging transport (card 79793579).

Card 79793579-37f1-4636-8979-99b5c9ac35c7 — Node-local staging path for
run_matrix_remote transport (bypass gateway round-trip).

Coverage:
  - ``LocalJobDirBundleTransport.push_bundle`` writes directly to
    {job_dir}/{run_id}/incoming/ on the local FS.
  - ``LocalJobDirBundleTransport`` NEVER invokes
    ``openclaw nodes invoke`` (no subprocess.run call).
  - ``LocalJobDirBundleTransport.fetch_output`` reads the same
    done/ tree the wire transport would have read (byte-equality).
  - ``fetch_descriptor_done_sentinel_local`` reads the .done sentinel
    locally, no exec.
  - ``LocalJobDirBundleTransport`` preserves the SHA contract
    (WorkerCell.sha256 == expected_sha256, Rin finding #2 wire
    integrity).
  - ``LocalJobDirBundleTransport`` size-cap pre-flight fails loud
    (mirrors the wire transport's BundleTooLargeError path).
  - ``LocalJobDirBundleTransport`` SHA pre-flight rejects skew
    (CodeSHARejectedError, Q1 loud skew rejection).
  - ``detect_local_node`` tri-state semantics:
      * ``True``  → force local path (no hostname check).
      * ``False`` → force remote path.
      * ``None``  → hostname == expected → local; else remote.
  - ``run_matrix_remote.main()`` with ``--transport=jobdir``:
      * ``--local-node``                  → LocalJobDirBundleTransport.
      * ``--no-local-node``               → JobDirBundleTransport.
      * ``--local-node`` + hostname match → log line naming transport.
      * default (no flag) + hostname != expected → gateway wire path.
  - The remote path is unchanged (card AC #2) — the wire transport's
    push_bundle still calls the gateway's terminal.upload.

DELEG-REF: 79793579-37f1-4636-8979-99b5c9ac35c7
HR5: targeted tests only — these tests scope to offload-transport
unit tests; the full pytest suite is Craig-gated and is NOT run here.
"""

from __future__ import annotations

import contextlib
import hashlib
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from offload.jobdir_transport import (
    JobDirBundleTransport,
    LocalJobDirBundleTransport,
    detect_local_node,
    fetch_descriptor_done_sentinel_local,
)
from offload.transport import (
    BundleTooLargeError,
    CodeSHARejectedError,
    WorkerCell,
    WorktreeUnreachableError,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_bundle(tmp_path: Path, payload: bytes = b"local staging test bundle\n") -> Path:
    """Write a test bundle to a stable source file under tmp_path.

    The push_bundle contract reads ``bundle_path`` as a file (Q1 SHA
    pre-flight streams the file), so we keep the fixture as a file.
    """
    src = tmp_path / "src" / "bundle.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(payload)
    return src


# ── detect_local_node tri-state semantics ──────────────────────────────────


def test_detect_local_node_force_true_ignores_hostname() -> None:
    """--local-node (True) → local path regardless of hostname."""
    assert detect_local_node_function(True, "any-hostname") is True
    assert detect_local_node_function(True, "") is True


def test_detect_local_node_force_false_ignores_hostname() -> None:
    """--no-local-node (False) → remote path even when hostname matches."""
    assert detect_local_node_function(False, "ava-worker-local") is False
    assert detect_local_node_function(False, "any-hostname") is False


def test_detect_local_node_auto_detect_hostname_match() -> None:
    """Omitted (None) + hostname == expected → local."""
    assert detect_local_node_function(None, "ava-worker-local") is True


def test_detect_local_node_auto_detect_hostname_mismatch() -> None:
    """Omitted (None) + hostname != expected → remote (gateway wire)."""
    assert detect_local_node_function(None, "laptop-craig") is False


def test_detect_local_node_auto_detect_hostname_case_insensitive() -> None:
    """Hostname comparison is case-insensitive (OS may return mixed case)."""
    assert detect_local_node_function(None, "AVA-WORKER-LOCAL") is True
    assert detect_local_node_function(None, "Ava-Worker-Local") is True


def test_detect_local_node_auto_detect_hostname_with_whitespace() -> None:
    """Hostname comparison tolerates trailing whitespace."""
    assert detect_local_node_function(None, "  ava-worker-local  ") is True


def test_detect_local_node_hostname_fn_raises_falls_back_to_false() -> None:
    """Hostname lookup raising → auto-detect falls back to False (remote)."""

    def boom() -> str:
        raise OSError("hostname lookup failed")

    assert detect_local_node(local_node_flag=None, hostname_fn=boom) is False


def test_detect_local_node_custom_expected_name() -> None:
    """--local-node-name overrides the default expected node name."""
    # default name is 'ava-worker-local', so this hostname DOESN'T auto-fire.
    assert detect_local_node_function(None, "my-custom-worker", expected="my-custom-worker") is True
    assert detect_local_node_function(None, "ava-worker-local", expected="my-custom-worker") is False


# Small adapter to make the tests above compact and readable.
def detect_local_node_function(
    flag: bool | None, hostname: str, expected: str = "ava-worker-local"
) -> bool:
    return detect_local_node(
        local_node_flag=flag,
        expected_node_name=expected,
        hostname_fn=lambda: hostname,
    )


# ── LocalJobDirBundleTransport.push_bundle ─────────────────────────────────


def test_local_push_writes_directly_to_incoming_dir(tmp_path: Path) -> None:
    """Bundle lands at {job_dir}/{run_id}/incoming/{name} on the local FS."""
    bundle = _write_bundle(tmp_path, payload=b"hello local staging\n")
    sha = _sha256(bundle.read_bytes())
    transport = LocalJobDirBundleTransport(
        node="ava-worker-local",
        job_dir=str(tmp_path / "jobs"),
    )

    cell = transport.push_bundle("r1", bundle, sha)

    expected = tmp_path / "jobs" / "r1" / "incoming" / "bundle.json"
    assert expected.is_file(), f"bundle should land at {expected}"
    assert expected.read_bytes() == b"hello local staging\n"
    assert cell.path == str(expected)
    assert cell.sha256 == sha


def test_local_push_preserves_sha_contract(tmp_path: Path) -> None:
    """WorkerCell.sha256 == expected_sha256 (Rin finding #2 wire integrity)."""
    bundle = _write_bundle(tmp_path, payload=b"sha-contract-check\n")
    sha = _sha256(bundle.read_bytes())
    transport = LocalJobDirBundleTransport(job_dir=str(tmp_path / "jobs"))

    cell = transport.push_bundle("r-sha", bundle, sha)
    assert isinstance(cell, WorkerCell)
    assert cell.sha256 == sha


def test_local_push_never_invokes_subprocess(tmp_path: Path) -> None:
    """No subprocess.run / no openclaw nodes invoke (card AC #1).

    The local path's architectural invariant is: zero gateway
    dependency. We assert subprocess.run was NEVER called, regardless
    of how many FS ops the transport performs.
    """
    bundle = _write_bundle(tmp_path, payload=b"no-wire-invocation\n")
    sha = _sha256(bundle.read_bytes())
    transport = LocalJobDirBundleTransport(job_dir=str(tmp_path / "jobs"))

    with mock.patch("subprocess.run") as m:
        cell = transport.push_bundle("r-novoke", bundle, sha)

    assert m.call_count == 0, "LocalJobDirBundleTransport must NOT call subprocess.run"
    # Sanity: the push succeeded anyway.
    assert cell.sha256 == sha
    assert Path(cell.path).is_file()


def test_local_push_sha_skew_raises_codesharejected(tmp_path: Path) -> None:
    """Local descriptor SHA != expected → CodeSHARejectedError (no FS write)."""
    bundle = _write_bundle(tmp_path, payload=b"sha-skew\n")
    wrong_sha = "0" * 64
    transport = LocalJobDirBundleTransport(job_dir=str(tmp_path / "jobs"))

    with mock.patch("subprocess.run") as m:
        with pytest.raises(CodeSHARejectedError) as excinfo:
            transport.push_bundle("r-skew", bundle, wrong_sha)

    assert "pre-flight" in str(excinfo.value)
    assert m.call_count == 0
    # No file should have been written.
    assert list((tmp_path / "jobs").rglob("*")) == []


def test_local_push_size_cap_raises_bundle_too_large(tmp_path: Path) -> None:
    """Local size-cap pre-flight → BundleTooLargeError (no FS write)."""
    transport = LocalJobDirBundleTransport(job_dir=str(tmp_path / "jobs"))
    # Allocate just over the 16 MB cap; the gateway source defines the
    # exact threshold (MAX_TERMINAL_UPLOAD_BYTES = 16 MB) and we share
    # that constant via OpenClawNodeBundleTransport.MAX_UPLOAD_BYTES.
    too_big = tmp_path / "too-big.json"
    too_big.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
    sha = _sha256(too_big.read_bytes())

    with mock.patch("subprocess.run") as m:
        with pytest.raises(BundleTooLargeError) as excinfo:
            transport.push_bundle("r-big", too_big, sha)

    assert "exceeds gateway cap" in str(excinfo.value)
    assert "16 MB" in str(excinfo.value)
    assert m.call_count == 0
    assert list((tmp_path / "jobs").rglob("*")) == []


def test_local_push_creates_incoming_dir_idempotently(tmp_path: Path) -> None:
    """Re-staging into the same run_id is safe (mkdir is idempotent)."""
    bundle = _write_bundle(tmp_path, payload=b"first-staging\n")
    sha = _sha256(bundle.read_bytes())
    transport = LocalJobDirBundleTransport(job_dir=str(tmp_path / "jobs"))

    # First staging creates the dirs.
    cell1 = transport.push_bundle("r-idem", bundle, sha)
    # Second staging re-creates the same dirs (mkdir(exist_ok=True)).
    bundle2 = _write_bundle(tmp_path, payload=b"second-staging\n")
    bundle2 = tmp_path / "src" / "bundle2.json"
    bundle2.write_bytes(b"second-staging\n")
    sha2 = _sha256(bundle2.read_bytes())
    cell2 = transport.push_bundle("r-idem", bundle2, sha2)

    incoming = tmp_path / "jobs" / "r-idem" / "incoming"
    assert incoming.is_dir()
    assert (incoming / "bundle.json").read_bytes() == b"first-staging\n"
    assert (incoming / "bundle2.json").read_bytes() == b"second-staging\n"
    assert cell1.sha256 == sha
    assert cell2.sha256 == sha2


# ── LocalJobDirBundleTransport.fetch_output ────────────────────────────────


def test_local_fetch_output_reads_done_filesystem(tmp_path: Path) -> None:
    """fetch_output reads {job_dir}/{run_id}/done/{cell_id}.output.json."""
    transport = LocalJobDirBundleTransport(job_dir=str(tmp_path / "jobs"))
    output_bytes = b'{"return_pct": 5.0, "trades": 10}'
    done_dir = tmp_path / "jobs" / "r-fetch" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "c1.output.json").write_bytes(output_bytes)

    result = transport.fetch_output("r-fetch", "c1")
    assert result == output_bytes


def test_local_fetch_output_returns_none_when_missing(tmp_path: Path) -> None:
    """fetch_output returns None when the cell hasn't finished yet."""
    transport = LocalJobDirBundleTransport(job_dir=str(tmp_path / "jobs"))

    with mock.patch("subprocess.run") as m:
        result = transport.fetch_output("r-pending", "c-pending")

    assert result is None
    assert m.call_count == 0  # no wire even on the failure path


def test_local_fetch_output_via_exec_alias(tmp_path: Path) -> None:
    """fetch_output_via_exec is a local-FS alias (no exec wire)."""
    transport = LocalJobDirBundleTransport(job_dir=str(tmp_path / "jobs"))
    output_bytes = b'{"return_pct": 7.5}'
    done_dir = tmp_path / "jobs" / "r-exec" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "c2.output.json").write_bytes(output_bytes)

    with mock.patch("subprocess.run") as m:
        result = transport.fetch_output_via_exec("r-exec", "c2")

    assert result == output_bytes
    assert m.call_count == 0


# ── fetch_descriptor_done_sentinel_local ───────────────────────────────────


def test_local_sentinel_returns_true_when_done_file_exists(tmp_path: Path) -> None:
    """fetch_descriptor_done_sentinel_local reads .done from local FS."""
    done_dir = tmp_path / "jobs" / "r-sentinel" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "c-sentinel.done").write_text("")

    with mock.patch("subprocess.run") as m:
        result = fetch_descriptor_done_sentinel_local(
            run_id="r-sentinel",
            cell_id="c-sentinel",
            job_dir=str(tmp_path / "jobs"),
        )

    assert result is True
    assert m.call_count == 0


def test_local_sentinel_returns_false_when_done_file_missing(tmp_path: Path) -> None:
    """No .done sentinel → False (cell still in flight)."""
    with mock.patch("subprocess.run") as m:
        result = fetch_descriptor_done_sentinel_local(
            run_id="r-pending",
            cell_id="c-pending",
            job_dir=str(tmp_path / "jobs-no-such-dir"),
        )

    assert result is False
    assert m.call_count == 0


# ── LocalJobDirBundleTransport vs JobDirBundleTransport contract ───────────


def test_local_transport_is_subclass_of_jobdir_transport() -> None:
    """LocalJobDirBundleTransport inherits from JobDirBundleTransport.

    Subclassing preserves API parity (push_bundle / fetch_output /
    fetch_output_via_exec signatures match), which keeps the matrix
    driver's transport dispatch path-transport-agnostic.
    """
    assert issubclass(LocalJobDirBundleTransport, JobDirBundleTransport)


def test_local_transport_does_not_instantiate_wire() -> None:
    """LocalJobDirBundleTransport does NOT carry an OpenClaw wire object.

    The parent's __init__ builds an OpenClawNodeBundleTransport;
    the local class bypasses it to avoid carrying unused state
    (and any accidental wire call from a future change).
    """
    transport = LocalJobDirBundleTransport(job_dir="/tmp/jobs")  # noqa: S108 — descriptive test fixture; not a FS write target
    # The local-only flag is set explicitly (parent sets it via _wire).
    assert getattr(transport, "_local_only", False) is True
    # The wire object is intentionally absent.
    assert not hasattr(transport, "_wire")


# ── run_matrix_remote.main() integration with --local-node ─────────────────
#


def _capture_init(cls):
    """Return a side-effect that records ``cls.__init__`` calls.

    The runner's ``isinstance(transport, JobDirBundleTransport)`` check
    must keep resolving to True for both transports — so we cannot
    replace the class with a Mock. Instead, wrap ``__init__`` so the
    call is recorded without altering class identity.
    """
    real_init = cls.__init__
    recorded: list[tuple[tuple, dict]] = []

    def capture(self, *args, **kwargs):
        recorded.append((args, kwargs))
        real_init(self, *args, **kwargs)

    capture.recorded = recorded  # type: ignore[attr-defined]
    return capture


def test_main_jobdir_local_node_flag_uses_local_transport(tmp_path: Path) -> None:
    """--local-node → LocalJobDirBundleTransport (no gateway call)."""
    from offload import run_matrix_remote as rmr

    cap_local = _capture_init(LocalJobDirBundleTransport)
    cap_remote = _capture_init(JobDirBundleTransport)

    # fetch_output returns deterministic bytes so the polling loop in
    # main() succeeds in unit tests without a real worker_runner.
    fake_output = b'{"exit_code": 0, "scorecard": {"return_pct": 1.0}}'

    with mock.patch.object(LocalJobDirBundleTransport, "__init__", cap_local), \
         mock.patch.object(JobDirBundleTransport, "__init__", cap_remote), \
         mock.patch.object(LocalJobDirBundleTransport, "fetch_output", return_value=fake_output):
        rc = rmr.main(
            [
                "--matrix", "smoke",
                "--run-id", "smoke-local",
                "--output-root", str(tmp_path / "out"),
                "--transport", "jobdir",
                "--local-node",
                "--worker-node", "ava-worker-local",
            ]
        )

    assert rc == 0
    assert len(cap_local.recorded) == 1, \
        f"LocalJobDirBundleTransport should have been instantiated once, got {cap_local.recorded}"
    assert cap_remote.recorded == [], \
        "JobDirBundleTransport (gateway wire) must NOT be instantiated under --local-node"


def test_main_jobdir_no_local_node_uses_remote_transport(tmp_path: Path) -> None:
    """--no-local-node → JobDirBundleTransport (gateway wire path).

    The wire transport's push_bundle is mocked to raise so the dispatch
    loop falls through to local-fallback without exercising the actual
    gateway round-trip (which would fail in unit tests anyway).
    """
    from offload import run_matrix_remote as rmr

    cap_local = _capture_init(LocalJobDirBundleTransport)
    cap_remote = _capture_init(JobDirBundleTransport)

    with mock.patch.object(LocalJobDirBundleTransport, "__init__", cap_local), \
         mock.patch.object(JobDirBundleTransport, "__init__", cap_remote):
        # Mock JobDirBundleTransport.push_bundle to raise immediately so
        # the runner falls into the local-fallback path without calling
        # the real wire transport (which would hit the real gateway).
        with mock.patch.object(
            JobDirBundleTransport,
            "push_bundle",
            side_effect=WorktreeUnreachableError("unit-test stub"),
        ):
            rc = rmr.main(
                [
                    "--matrix", "smoke",
                    "--run-id", "smoke-remote",
                    "--output-root", str(tmp_path / "out"),
                    "--transport", "jobdir",
                    "--no-local-node",
                    "--worker-node", "ava-worker-local",
                ]
            )

    assert rc == 0
    assert len(cap_remote.recorded) == 1, \
        f"JobDirBundleTransport should have been instantiated once, got {cap_remote.recorded}"
    assert cap_local.recorded == [], \
        "LocalJobDirBundleTransport must NOT be instantiated under --no-local-node"


def test_main_jobdir_omit_flag_uses_remote_when_hostname_mismatch(tmp_path: Path) -> None:
    """Omitted --local-node + hostname mismatch → JobDirBundleTransport."""
    from offload import run_matrix_remote as rmr

    cap_local = _capture_init(LocalJobDirBundleTransport)
    cap_remote = _capture_init(JobDirBundleTransport)

    with mock.patch.object(LocalJobDirBundleTransport, "__init__", cap_local), \
         mock.patch.object(JobDirBundleTransport, "__init__", cap_remote), \
         mock.patch.object(rmr, "detect_local_node", return_value=False) as det:
        # Mock JobDirBundleTransport.push_bundle so we don't actually try
        # the gateway wire (which would fail in unit tests).
        with mock.patch.object(
            JobDirBundleTransport,
            "push_bundle",
            side_effect=WorktreeUnreachableError("unit-test stub"),
        ):
            rc = rmr.main(
                [
                    "--matrix", "smoke",
                    "--run-id", "smoke-host-mismatch",
                    "--output-root", str(tmp_path / "out"),
                    "--transport", "jobdir",
                    "--worker-node", "ava-worker-local",
                ]
            )

    assert rc == 0
    assert det.called, "auto-detect path must consult detect_local_node"
    assert len(cap_remote.recorded) == 1, "JobDirBundleTransport expected under hostname mismatch"
    assert cap_local.recorded == [], "LocalJobDirBundleTransport must NOT be used under hostname mismatch"


def test_main_jobdir_omit_flag_uses_local_when_hostname_match(tmp_path: Path) -> None:
    """Omitted --local-node + hostname match → LocalJobDirBundleTransport."""
    from offload import run_matrix_remote as rmr

    cap_local = _capture_init(LocalJobDirBundleTransport)
    cap_remote = _capture_init(JobDirBundleTransport)

    fake_output = b'{"exit_code": 0, "scorecard": {"return_pct": 1.0}}'

    with mock.patch.object(LocalJobDirBundleTransport, "__init__", cap_local), \
         mock.patch.object(JobDirBundleTransport, "__init__", cap_remote), \
         mock.patch.object(LocalJobDirBundleTransport, "fetch_output", return_value=fake_output), \
         mock.patch.object(rmr, "detect_local_node", return_value=True) as det:
        rc = rmr.main(
            [
                "--matrix", "smoke",
                "--run-id", "smoke-host-match",
                "--output-root", str(tmp_path / "out"),
                "--transport", "jobdir",
                "--worker-node", "ava-worker-local",
            ]
        )

    assert rc == 0
    assert det.called, "auto-detect path must consult detect_local_node"
    assert len(cap_local.recorded) == 1, "LocalJobDirBundleTransport expected under hostname match"
    assert cap_remote.recorded == [], "JobDirBundleTransport must NOT be used under hostname match"


def test_main_jobdir_local_path_logs_transport_choice(tmp_path: Path, capsys) -> None:
    """--local-node prints a one-line log naming the chosen transport."""
    from offload import run_matrix_remote as rmr

    cap_local = _capture_init(LocalJobDirBundleTransport)
    cap_remote = _capture_init(JobDirBundleTransport)

    fake_output = b'{"exit_code": 0, "scorecard": {"return_pct": 1.0}}'

    with mock.patch.object(LocalJobDirBundleTransport, "__init__", cap_local), \
         mock.patch.object(JobDirBundleTransport, "__init__", cap_remote), \
         mock.patch.object(LocalJobDirBundleTransport, "fetch_output", return_value=fake_output):
        rc = rmr.main(
            [
                "--matrix", "smoke",
                "--run-id", "smoke-log",
                "--output-root", str(tmp_path / "out"),
                "--transport", "jobdir",
                "--local-node",
                "--worker-node", "ava-worker-local",
            ]
        )

    assert rc == 0
    captured = capsys.readouterr()
    # Card AC #3: a clear log line naming which transport was used.
    assert "[run_matrix_remote] transport: local-node-direct" in captured.err
    assert "bypassing gateway" in captured.err


def test_main_jobdir_remote_path_logs_transport_choice(tmp_path: Path, capsys) -> None:
    """--no-local-node prints a one-line log naming the gateway wire transport."""
    from offload import run_matrix_remote as rmr

    cap_local = _capture_init(LocalJobDirBundleTransport)
    cap_remote = _capture_init(JobDirBundleTransport)

    with mock.patch.object(LocalJobDirBundleTransport, "__init__", cap_local), \
         mock.patch.object(JobDirBundleTransport, "__init__", cap_remote):
        with mock.patch.object(
            JobDirBundleTransport,
            "push_bundle",
            side_effect=WorktreeUnreachableError("unit-test stub"),
        ):
            rc = rmr.main(
                [
                    "--matrix", "smoke",
                    "--run-id", "smoke-log-remote",
                    "--output-root", str(tmp_path / "out"),
                    "--transport", "jobdir",
                    "--no-local-node",
                    "--worker-node", "ava-worker-local",
                ]
            )

    assert rc == 0
    captured = capsys.readouterr()
    assert "[run_matrix_remote] transport: gateway-wire" in captured.err


# ── Wire-path preservation: existing remote behavior is unchanged ──────────


def test_remote_transport_still_invokes_subprocess_on_push(tmp_path: Path) -> None:
    """Card AC #2: gateway wire path behavior is unchanged.

    JobDirBundleTransport.push_bundle still calls the wire transport's
    _invoke_terminal_upload — i.e. subprocess.run IS called on the
    remote path. This is the regression guard: if a future change
    accidentally short-circuits the remote transport to the local
    path, this test will fail.
    """
    transport = JobDirBundleTransport(node="ava-worker-local", job_dir=str(tmp_path / "jobs"))
    bundle = _write_bundle(tmp_path, payload=b"remote-wire-still-works\n")
    sha = _sha256(bundle.read_bytes())

    # Subprocess returns success but the response is malformed;
    # we just want to confirm subprocess.run was called.
    cp: subprocess.CompletedProcess = subprocess.CompletedProcess(args=[], returncode=0)
    cp.stdout = "ok"
    cp.stderr = ""

    with mock.patch("subprocess.run", return_value=cp) as m:
        # We expect either a CalledProcessError (response missing
        # payload.path) or a successful cell — what matters is that
        # subprocess.run was called (the wire fired).
        with contextlib.suppress(Exception):
            # any failure path is fine; only the wire call matters
            transport.push_bundle("r-wire-guard", bundle, sha)

    assert m.call_count >= 1, "remote transport must invoke subprocess.run on push"
