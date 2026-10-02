#!/usr/bin/env python3
"""Ayumi node-offload runner v1 — CLI dispatch loop (Q1+Q4+Q6).

Per card f9414fe1 + Tomoe brief: thin wrapper over OpenClaw node
transport (system.run + file transfer). v1 ships with a
Port8877StubTransport which raises on real invoke, driving the
local-fallback path. The BundleTransport ABC stays the contract —
a terminal.upload impl slots in when gateway 9.5 lands.

This is the cycle-2 dispatch skeleton. Cycle-1 helper modules
(manifest, transport, seed) are wired here:

- atomic_write_manifest() + per_cell_lock(): called per cell (Q4.b/c)
- lock_files_hash(): captured once at dispatch time (Q3)
- BundleTransport.push_bundle(): wrapped in try/except (Q1 skew ABORT,
  Q2 unreachable → local-fallback flag)
- Manifest dataclass: extended with bundle_files (Tomoe watch item 1)

CPU-first v1; GPU deferred (card AC5 out-of-scope). No touch to live
ayumi-forward-test service.

Deferred to subsequent cycles:
- cycle 3 — dispatch_skipped.py jsonl appender; scoring.py real local
  fallback execution; --resume skip logic
- cycle 4 — tests/offload/* (AC extensions, parallel-resume, DB-skip,
  forced-skew)
- cycle 5 — README.md (Tomoe watch item 2 wording on local-fallback honesty)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running this script directly (python3 scripts/offload/run_matrix_remote.py)
# without requiring `-m offload.run_matrix_remote`. Two sys.path mutations at
# module-load time: scripts/ for offload.* and src/ for tournament.* (the
# tournament package is imported lazily inside _tournament_matrix_spec,
# but src/ must be on sys.path when the runner fires --matrix=tournament).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from offload.dispatch_skipped import append_skipped
from offload.jobdir_transport import (
    JobDirBundleTransport,
    LocalJobDirBundleTransport,
    detect_local_node,
)
from offload.manifest import (
    Manifest,
    atomic_write_manifest,
    per_cell_lock,
)
from offload.scoring import score_cell_local
from offload.seed import cell_id, lock_files_hash
from offload.transport import (
    BundleTooLargeError,
    BundleTransport,
    BundleTransportError,
    CodeSHARejectedError,
    OpenClawNodeBundleTransport,
    Port8877StubTransport,
)

# v1 default output_root base (Q6(a) CLI > env > default). Subdir layout
# (e.g. {manifests,scorecards,logs}/) deferred to cycle 3.
DEFAULT_OUTPUT_ROOT = "data/offload"


# v1 smoke matrix — 1 strategy × 1 symbol × 2 TFs (card AC1 spec).
# Future impl: parse a matrix spec file (JSON/YAML); v1 ships inline.
SMOKE_MATRIX: list[tuple[str, str, str]] = [
    ("q1_mw_formation", "GBPUSD", "M5"),
    ("q1_mw_formation", "GBPUSD", "M15"),
]


# v1 tournament matrix (card 05fa0065) — 17 registered strategies
# × {XAUUSD, GBPUSD} × {M15, H1} = 68 cells. Strategy ids are loaded
# from tournament.harness.STRATEGY_CLASS_MAP at module load (the harness
# is the canonical registry) so this stays in sync with new strategy
# registrations automatically.
def _tournament_matrix_spec() -> tuple[list[str], list[str], list[str]]:
    """Return ``(strategy_ids, symbols, timeframes)`` for the tournament.

    Imported lazily so the runner can boot even when tournament.harness
    has transient import problems (the harness pulls in src/forex-bot/
    strategies/* which can fail on partial checkouts).
    """
    from tournament.harness import STRATEGY_CLASS_MAP

    return (
        list(STRATEGY_CLASS_MAP.keys()),
        ["XAUUSD", "GBPUSD"],
        ["M15", "H1"],
    )


TOURNAMENT_STRATEGIES, TOURNAMENT_SYMBOLS, TOURNAMENT_TIMEFRAMES = _tournament_matrix_spec()
TOURNAMENT_MATRIX: list[tuple[str, str, str]] = [
    (s, sym, tf)
    for s in TOURNAMENT_STRATEGIES
    for sym in TOURNAMENT_SYMBOLS
    for tf in TOURNAMENT_TIMEFRAMES
]


def _resolve_output_root(cli_root: str | None, repo_root: Path) -> Path:
    """--output-root precedence: CLI flag > $AYUMI_OFFLOAD_ROOT env > default (Q6)."""
    explicit = cli_root or os.environ.get("AYUMI_OFFLOAD_ROOT")
    if explicit:
        return Path(explicit)
    return repo_root / DEFAULT_OUTPUT_ROOT


def _coerce_local_flag(value: object) -> bool | None:
    """Coerce argparse's --local-node value into the tri-state expected by detect_local_node.

    argparse's ``const=True, nargs="?"`` yields three outcomes:
      * ``--local-node`` (no value)         → ``True``  (force local path).
      * ``--local-node=true``  /  ``=True``  → ``True``.
      * ``--local-node=false`` /  ``=False`` → ``False`` (force remote wire).
      * flag omitted                         → ``None``  (auto-detect via hostname).

    argparse normalizes ``=true``/``=false`` to Python bools (the
    ``choices=[True, False, "true", "false"]`` allows both); we map
    any truthy value to True, falsy to False, and treat the omitted
    case (caller passed ``None``) as the auto-detect signal.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
    # Unknown string → fall back to None so detect_local_node's
    # hostname path can still decide.
    return None


def _current_git_sha(repo_root: Path) -> str:
    """Pin dispatcher-side git_sha at dispatch time (Q4.e two-SHA split)."""
    git_bin = shutil.which("git") or "/usr/bin/git"
    out = subprocess.run(  # noqa: S603 — args hardcoded; binary resolved via shutil.which w/ /usr/bin/git fallback (lint false positive)
        [git_bin, "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _bundle_path(repo_root: Path) -> Path:
    """Local source dir we hand to the worker via push-bundle (Q2)."""
    return repo_root / "scripts" / "offload"


def _bundle_sha_and_files(bundle_path: Path) -> tuple[str, list[str]]:
    """SHA256 + filename list of the bundle (Tomoe watch item 1).

    sorted() keeps the SHA deterministic across filesystems and runs.
    ``__pycache__/`` is excluded — bytecode is a build artifact, not
    source; including it would cause spurious bundle drift across runs
    (mtime + platform-specific bytecode headers).
    """
    h = hashlib.sha256()
    names: list[str] = []
    if not bundle_path.is_dir():
        return h.hexdigest(), names
    for p in sorted(bundle_path.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(bundle_path)
        if any(part == "__pycache__" for part in rel.parts):
            continue
        names.append(str(rel))
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest(), names


def _run_one_cell(
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    args: argparse.Namespace,
    transport: BundleTransport,
    output_root: Path,
    git_sha: str,
    env_lock_hash_val: str,
    env_lock_files_names: list[str],
    bundle_path: Path,
    bundle_sha: str,
    bundle_files: list[str],
    current_db_sha: str | None = None,
) -> tuple[Manifest | None, str]:
    """Dispatch one cell; returns ``(manifest | None, status)``.

    Status values:
      - ``"dispatched"``       — remote path succeeded (Q2 happy)
      - ``"local_fallback"``   — transport errored; local score emitted (Q7)
      - ``"skipped_resume"``   — ``--resume`` hit a terminal manifest (Q4.b),
                                 OR per_cell_lock contention (Q4.c)

    Q1 loud skew (``CodeSHARejectedError``) → log to dispatch_skipped.jsonl
    + ABORT (re-raises; no atomic write; nothing recorded as "skipped").

    Q4.c lock contention (``per_cell_lock`` raises ``RuntimeError``) →
    caught at the function boundary; converted to skipped_resume + stderr
    warning so ``main()`` doesn't crash on parallel ``--resume``. The
    parallel-resume test in cycle 4b reproduces + verifies the wire end
    to end. Single-cell unit of ``per_cell_lock`` raising is covered in
    ``tests/offload/test_manifest.py::test_per_cell_lock_raises_runtime_error_on_contention``.
    """
    cid = cell_id(strategy, symbol, timeframe)
    cell_out = output_root / cid

    try:
        with per_cell_lock(cell_out):  # Q4.c; raises RuntimeError on contention
            manifest_path = cell_out / "manifest.json"
            draft_path = cell_out / "manifest.draft.json"

            # --resume: skip ONLY if a terminal manifest exists AND its
            # output is verifiable + SHA matches (Rin finding #4).
            # Tampered or missing output → fall through to fresh run +
            # log an audit row (output_hash_mismatch / output_path_missing).
            if args.resume:
                if manifest_path.is_file():
                    try:
                        prior = Manifest.from_dict(
                            json.loads(manifest_path.read_text())
                        )
                        if prior.finished_at is not None:
                            skip_verified = False
                            if prior.output_path and prior.output_hash:
                                out_p = Path(prior.output_path)
                                if out_p.is_file():
                                    actual = hashlib.sha256(
                                        out_p.read_bytes()
                                    ).hexdigest()
                                    if actual == prior.output_hash:
                                        skip_verified = True
                                    else:
                                        append_skipped(
                                            output_root,
                                            cell_id=cid,
                                            reason="output_hash_mismatch",
                                            expected=prior.output_hash,
                                            actual=actual,
                                            extra={
                                                "prior_manifest": str(manifest_path),
                                            },
                                        )
                                else:
                                    append_skipped(
                                        output_root,
                                        cell_id=cid,
                                        reason="output_path_missing",
                                        expected=prior.output_path,
                                        actual="<file not found>",
                                    )
                            # else: incomplete prior (no output_path/hash)
                            # → fall through to fresh run.
                            if skip_verified:
                                return prior, "skipped_resume"
                    except (json.JSONDecodeError, KeyError):
                        pass  # corrupt prior — fall through to fresh run
                elif draft_path.is_file():
                    # In-flight per Q4.b — refuse to clobber an in-progress cell.
                    return None, "skipped_resume"

            m = Manifest(
                cell_id=cid,
                seed=cid,                    # Q4 v1: cell_id == seed (binding)
                strategy=strategy,
                symbol=symbol,
                timeframe=timeframe,
                git_sha=git_sha,
                env_lock_hash=env_lock_hash_val,
                env_lock_files=env_lock_files_names,
                bundle_files=bundle_files,   # Tomoe watch item 1
                db_sha=current_db_sha,
                local_fallback=False,
            )

            try:
                if isinstance(transport, JobDirBundleTransport):
                    # Jobdir wire: push_bundle expects the per-cell job
                    # descriptor JSON (basename = cell_id), NOT the bundle
                    # directory. Write it, hash it, and let the transport
                    # pre-flight that hash.
                    descriptor = {
                        "schema_version": 1,
                        "cell_id": cid,
                        "seed": cid,
                        "run_id": args.run_id,
                        "strategy": strategy,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "git_sha": git_sha,
                        "env_lock_hash": env_lock_hash_val,
                        "data_db_path": args.data_db_path,
                        "data_db_sha": args.data_db_sha,
                    }
                    descriptor_path = cell_out / f"{cid}.json"
                    descriptor_path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor_path.write_text(
                        json.dumps(descriptor, indent=2, sort_keys=True)
                    )
                    push_path: Path = descriptor_path
                    push_sha = hashlib.sha256(
                        push_path.read_bytes()
                    ).hexdigest()
                else:
                    push_path = bundle_path
                    push_sha = bundle_sha
                worker_cell = transport.push_bundle(
                    args.run_id, push_path, push_sha
                )
            except CodeSHARejectedError:
                # Q1 loud skew: log row + ABORT. Log BEFORE re-raise so the
                # audit row is on disk even though no manifest is written.
                append_skipped(
                    output_root,
                    cell_id=cid,
                    reason="code_skew",
                    expected=bundle_sha,
                    actual="<worker-side; see logs>",
                    extra={"git_sha": git_sha, "env_lock_hash": env_lock_hash_val},
                )
                raise
            except BundleTooLargeError:
                # Rin finding #1: BundleTooLargeError must FAIL LOUD, never
                # trigger local fallback (Tomoe explicit at checkpoint 2
                # sign-off). Audit row + propagate so the runner exits
                # nonzero with NO fallback manifest written.
                append_skipped(
                    output_root,
                    cell_id=cid,
                    reason="bundle_too_large",
                    expected=bundle_sha,
                    actual=None,
                    extra={"bundle_size_exceeded": "see worker logs"},
                )
                raise
            except BundleTransportError:
                # Q2 unreachable → local-fallback score + flag (Q7 observability,
                # not control flow). Same scorecard schema as the remote path.
                m.local_fallback = True
                score_path = cell_out / "scorecard.json"
                score_cell_local(
                    strategy=strategy, symbol=symbol, timeframe=timeframe,
                    seed=cid, output_path=score_path,
                )
                m.finished_at = datetime.now(timezone.utc).isoformat()
                m.exit_code = 0
                m.output_path = str(score_path)
                status = "local_fallback"
            else:
                # Fix #2 (Rin): validate the worker's reported SHA matches what
                # we sent. Trust boundary; mismatch routes through
                # CodeSHARejectedError semantics (audit row + ABORT, no
                # fallback manifest).
                #
                # IMPORTANT: we compare against ``push_sha`` (the SHA the
                # transport verified inside push_bundle), NOT ``bundle_sha``
                # (the SHA of the bundle *directory*). For the jobdir path
                # the runner pushes a per-cell descriptor whose SHA equals
                # ``push_sha``, not the bundle directory SHA — the
                # original ``!= bundle_sha`` comparison was a pre-existing
                # bug that was hidden because the gateway wire raised
                # ``WorktreeUnreachableError`` before this check fired
                # (see card 79793579). With the local-node path the wire no
                # longer fails, so the post-push SHA check must consult the
                # same value the transport's own pre-flight checked.
                if worker_cell.sha256 != push_sha:
                    append_skipped(
                        output_root,
                        cell_id=cid,
                        reason="code_skew",
                        expected=push_sha,
                        actual=worker_cell.sha256,
                        extra={
                            "validation": "post-push_bundle",
                            "git_sha": git_sha,
                            "env_lock_hash": env_lock_hash_val,
                        },
                    )
                    raise CodeSHARejectedError(
                        f"WorkerCell.sha256={worker_cell.sha256!r} != "
                        f"expected {push_sha!r}"
                    )

                # Fix #3 (Rin): retrieve + persist + hash the worker's output.
                # The runner's terminalize contract: every successful dispatch
                # MUST carry output_path + output_hash (AC4).
                #
                # Jobdir wire: the worker_runner executes asynchronously —
                # poll for the done/ output (file.fetch first, exec-cat
                # fallback when the gateway denies file.fetch) until the
                # per-cell timeout expires.
                output_bytes: bytes | None = None
                if isinstance(transport, JobDirBundleTransport):
                    deadline = time.monotonic() + args.cell_timeout_s
                    while time.monotonic() < deadline:
                        output_bytes = transport.fetch_output(args.run_id, cid)
                        if output_bytes is None:
                            output_bytes = transport.fetch_output_via_exec(
                                args.run_id, cid
                            )
                        if output_bytes is not None:
                            break
                        time.sleep(args.poll_interval_s)
                else:
                    output_bytes = transport.fetch_output(args.run_id, cid)
                if output_bytes is None:
                    append_skipped(
                        output_root,
                        cell_id=cid,
                        reason="output_missing",
                        expected="non-empty bytes",
                        actual=None,
                    )
                    raise BundleTransportError(
                        f"fetch_output returned None for cell {cid!r} after "
                        f"successful push_bundle; refusing to terminalize "
                        f"without output_hash (Rin finding #3 / AC4)"
                    )

                output_hash = hashlib.sha256(output_bytes).hexdigest()
                output_path = cell_out / "output"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(output_bytes)

                # Jobdir wire: the worker writes a fail-loud JSON payload —
                # a non-zero exit_code is a cell failure (db_missing /
                # empty_window / no_signals / exception). Persist the output
                # for forensics, audit-row it, and leave the manifest
                # non-terminal so --resume re-runs the cell.
                if isinstance(transport, JobDirBundleTransport):
                    try:
                        worker_payload = json.loads(output_bytes.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        worker_payload = None
                    if (
                        not isinstance(worker_payload, dict)
                        or int(worker_payload.get("exit_code", 1) or 1) != 0
                    ):
                        err = (
                            worker_payload.get("error", "<unparsable output>")
                            if isinstance(worker_payload, dict)
                            else "<unparsable output>"
                        )
                        append_skipped(
                            output_root,
                            cell_id=cid,
                            reason="worker_error",
                            expected="exit_code=0",
                            actual=str(err)[:200],
                            extra={"output_path": str(output_path)},
                        )
                        m.output_path = str(output_path)
                        m.output_hash = output_hash
                        if isinstance(worker_payload, dict):
                            m.exit_code = int(worker_payload.get("exit_code", 1) or 1)
                        else:
                            m.exit_code = 1
                        # finished_at stays None → --resume retries the cell.
                        atomic_write_manifest(m, cell_out)
                        return m, "worker_error"

                m.output_path = str(output_path)
                m.output_hash = output_hash
                m.finished_at = datetime.now(timezone.utc).isoformat()
                m.exit_code = 0
                status = "dispatched"
                status = "dispatched"

            atomic_write_manifest(m, cell_out)  # Q4.b POSIX-atomic, inside the lock
            return m, status
    except RuntimeError as exc:
        # Q4.c "skip-with-warning": a parallel --resume is mid-dispatching
        # this cell. Don't propagate (would crash main()); convert to
        # skipped_resume so the loop continues. The lost cell will be
        # retried naturally on the next --resume invocation.
        print(
            f"warn: cell {cid!r} ({strategy}/{symbol}/{timeframe}): "
            f"lock contended by another runner; skipping (Q4.c "
            f"skip-with-warning): {exc}",
            file=sys.stderr,
        )
        return None, "skipped_resume"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_matrix_remote",
        description=(
            "Ayumi node-offload runner v1 — dispatches backtest/tournament "
            "matrix cells to ava-worker (CPU-first; GPU deferred). v1 ships "
            "with local-fallback path; BundleTransport ABC is wired so a "
            "terminal.upload impl slots in when gateway 9.5 lands."
        ),
    )
    parser.add_argument("--matrix", choices=["smoke", "tournament"], default="smoke")
    parser.add_argument(
        "--data-db-path", default=None,
        help=(
            "Path to the worker-local DuckDB data subset (jobdir transport). "
            "Embedded in every cell descriptor as 'data_db_path'. "
            "Required when --transport=jobdir and --matrix=tournament."
        ),
    )
    parser.add_argument(
        "--data-db-sha", default=None,
        help=(
            "SHA256 of the worker-local DuckDB subset (jobdir transport). "
            "Embedded in every cell descriptor as 'data_db_sha'; the "
            "matrix_driver records it in the manifest for drift detection."
        ),
    )
    parser.add_argument(
        "--strategies", default=None,
        help=(
            "Comma-separated strategy id filter (default: all registered "
            "strategies when --matrix=tournament). Useful for slicing "
            "the matrix for smoke runs (e.g. --strategies=london_breakout_retest)."
        ),
    )
    parser.add_argument(
        "--report-dir", default=None,
        help=(
            "If set, after the cell loop completes, write the matrix "
            "report to {report-dir}/{run-id}/ (matrix.json, matrix.md, "
            "verdict_memo.md). Reports filter v1_stub:true scorecards — "
            "they are NEVER ingested into the report (card 05fa0065 AC #2)."
        ),
    )
    parser.add_argument("--run-id", default="v1smoke")
    parser.add_argument(
        "--output-root", default=None,
        help=(
            "Dispatcher-side root for manifests + scorecards. Precedence: "
            "this flag > $AYUMI_OFFLOAD_ROOT env > <repo>/data/offload/<run-id>/."
        ),
    )
    parser.add_argument(
        "--resume", action="store_true",
        help=(
            "Skip cells whose terminal manifest already exists at "
            "{output_root}/{cell_id}/manifest.json (Q4.b). Does NOT yet "
            "detect db_sha drift (deferred to a follow-up; for now, any "
            "existing terminal manifest is reused as-is)."
        ),
    )
    parser.add_argument("--worker", default="ava-worker-local")
    parser.add_argument(
        "--cell-timeout-s", type=float, default=2700.0,
        help=(
            "(jobdir) Max seconds to wait for one cell's done/ output "
            "before giving up (default: 2700 = 45 min)."
        ),
    )
    parser.add_argument(
        "--poll-interval-s", type=float, default=5.0,
        help="(jobdir) Seconds between done/ output polls (default: 5).",
    )
    parser.add_argument(
        "--transport", choices=["stub", "node", "jobdir"], default="node",
        help=(
            "Which BundleTransport impl to dispatch through. "
            "'node' (default, c3134271): OpenClawNodeBundleTransport — "
            "real push/exec/fetch on the ava-worker-local node. "
            "'jobdir' (05fa0065): JobDirBundleTransport — uploads a "
            "per-cell job descriptor to /tmp/ayumi-jobs/{run_id}/incoming/ "
            "for the worker_runner daemon to consume (production path for "
            "the 68-cell matrix; explicitly NOT local-fallback). "
            "'stub' (legacy v1): Port8877StubTransport — raises on real "
            "invoke, drives local fallback (kept for the v1.0 contract "
            "until the node wire is fully smoke-validated)."
        ),
    )
    parser.add_argument(
        "--worker-node", default="ava-worker-local",
        help="Node name for OpenClawNodeBundleTransport (default: ava-worker-local).",
    )
    parser.add_argument(
        "--local-node",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "(jobdir) Bypass the gateway round-trip and stage bundles "
            "directly on the local filesystem. "
            "``--local-node``     → force the local path (no gateway call). "
            "``--no-local-node``  → force the gateway wire (default). "
            "Omitted              → auto-detect via OS hostname == "
            "--local-node-name (default: 'ava-worker-local'). "
            "Card 79793579: required when the runner executes ON the "
            "worker node itself; the node's CLI has no gateway "
            "credentials so ``openclaw nodes invoke`` fails."
        ),
    )
    parser.add_argument(
        "--local-node-name", default="ava-worker-local",
        help=(
            "(jobdir) Expected local node name for hostname-based "
            "auto-detection. Only consulted when --local-node is "
            "omitted. Default: 'ava-worker-local'."
        ),
    )
    parser.add_argument(
        "--simulate-transport", action="store_true",
        help=(
            "(legacy) Force Port8877StubTransport.simulate_success=True "
            "regardless of --transport. Overrides --transport=node."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]
    output_root = _resolve_output_root(args.output_root, repo_root) / args.run_id

    env_lock_hash_val, env_lock_files_names = lock_files_hash(repo_root)
    git_sha = _current_git_sha(repo_root)
    bundle_path = _bundle_path(repo_root)
    bundle_sha, bundle_files = _bundle_sha_and_files(bundle_path)

    # --simulate-transport (legacy) overrides --transport; otherwise --transport picks the impl.
    if args.simulate_transport:
        transport: BundleTransport = Port8877StubTransport(simulate_success=True)
    elif args.transport == "node":
        transport = OpenClawNodeBundleTransport(node=args.worker_node)
    elif args.transport == "jobdir":
        # Card 79793579: jobdir dispatches MAY be served by either
        # JobDirBundleTransport (gateway wire) or
        # LocalJobDirBundleTransport (direct FS, no gateway call).
        # Selection is deterministic: explicit --local-node wins over
        # hostname detection. We emit a one-line transport log so
        # operators can grep the chosen path from the run log without
        # reading the code.
        local_node_tri = _coerce_local_flag(args.local_node)
        use_local = detect_local_node(
            local_node_flag=local_node_tri,
            expected_node_name=args.local_node_name,
        )
        if use_local:
            transport = LocalJobDirBundleTransport(
                node=args.worker_node,
            )
            print(
                f"[run_matrix_remote] transport: local-node-direct "
                f"(bypassing gateway; node={args.worker_node!r}, "
                f"job_dir={LocalJobDirBundleTransport.__module__})",
                file=sys.stderr,
            )
        else:
            transport = JobDirBundleTransport(node=args.worker_node)
            print(
                f"[run_matrix_remote] transport: gateway-wire "
                f"(--transport=jobdir via nodes invoke; node={args.worker_node!r})",
                file=sys.stderr,
            )
    else:
        # 'stub' legacy path: raises on real invoke → drives local fallback.
        transport = Port8877StubTransport(simulate_success=False)

    # Matrix selection + per-strategy filter
    if args.matrix == "smoke":
        matrix: list[tuple[str, str, str]] = SMOKE_MATRIX
    else:
        matrix = TOURNAMENT_MATRIX
        if args.strategies:
            wanted = {s.strip() for s in args.strategies.split(",") if s.strip()}
            matrix = [
                (s, sym, tf)
                for s, sym, tf in matrix
                if s in wanted
            ]
            if not matrix:
                print(
                    f"ERROR: --strategies filter {sorted(wanted)} matched 0 cells; "
                    f"check the spelling",
                    file=sys.stderr,
                )
                return 4

    statuses: list[str] = []
    for strategy, symbol, timeframe in matrix:
        _m, status = _run_one_cell(
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            args=args,
            transport=transport,
            output_root=output_root,
            git_sha=git_sha,
            env_lock_hash_val=env_lock_hash_val,
            env_lock_files_names=env_lock_files_names,
            bundle_path=bundle_path,
            bundle_sha=bundle_sha,
            bundle_files=bundle_files,
        )
        statuses.append(status)

    # CLI summary (Q7 — observability; never silent).
    n_total = len(statuses)
    counts: dict[str, int] = {}
    for s in statuses:
        counts[s] = counts.get(s, 0) + 1
    breakdown = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"{n_total}/{n_total} cells: {breakdown}")

    # Report generation (card 05fa0065 AC #3 + #4): only when --report-dir
    # is set. Reports filter v1_stub:true scorecards so the report never
    # lies about a real (cost-aware) scorecard using a synthetic local-
    # fallback number. "no survivors" is a valid verdict and is reported
    # plainly — we never hedge.
    if args.report_dir:
        report_root = Path(args.report_dir).expanduser().resolve() / args.run_id
        generate_matrix_report(
            output_root=output_root,
            report_root=report_root,
            run_id=args.run_id,
            n_total=n_total,
            statuses=statuses,
            git_sha=git_sha,
            env_lock_hash_val=env_lock_hash_val,
            data_db_sha=getattr(args, "data_db_sha", None),
        )

    return 0


def _read_manifests(output_root: Path) -> list[dict]:
    """Read every ``{cell_id}/manifest.json`` under ``output_root``.

    Returns a list of dicts (manifest payload). Corrupt or missing files
    are skipped — callers must check the count against ``n_total`` to
    detect gaps (HR27 silent-success guard).
    """
    manifests: list[dict] = []
    if not output_root.is_dir():
        return manifests
    for cell_dir in sorted(output_root.iterdir()):
        if not cell_dir.is_dir():
            continue
        manifest_path = cell_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifests.append(json.loads(manifest_path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return manifests


def _filter_real_scorecards(manifests: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split manifests into real (jobdir-dispatched) and v1_stub.

    v1_stub:true scorecards come from the local-fallback path (resilience
    only) and are NEVER ingested into the matrix report (card 05fa0065
    AC #2). The Craig directive 6f2cd97b is explicit: a matrix run that
    reports local_fallback=true for any cell does not satisfy this card.
    """
    real: list[dict] = []
    stub: list[dict] = []
    for m in manifests:
        is_stub = bool(m.get("local_fallback", False))
        # The local_fallback path emits score_cell_local's v1_stub flag.
        # Belt-and-suspenders: also reject if v1_stub appears anywhere
        # in the manifest (e.g. embedded in the scorecard payload).
        if is_stub or m.get("v1_stub", False):
            stub.append(m)
        else:
            real.append(m)
    return real, stub


def _verdict_filter(
    rows: list[dict],
    *,
    max_dd_pct: float = 10.0,
    min_trades: int = 20,
) -> list[dict]:
    """Apply the FTMO survivor filter: max_dd<10% AND trade_count≥20.

    Returns the surviving rows (the cells that "survive realistic costs"
    per card 05fa0065 AC #4). The filter is intentionally strict — the
    deliverable either names survivors or says "no survivors" plainly,
    not a hedge.
    """
    survivors: list[dict] = []
    for r in rows:
        try:
            dd = float(r.get("max_dd_pct", 0.0))
            n_trades = int(r.get("trade_count", 0))
        except (TypeError, ValueError):
            continue
        if dd < max_dd_pct and n_trades >= min_trades:
            survivors.append(r)
    return survivors


def _rank_rows(rows: list[dict]) -> list[dict]:
    """Rank by return_pct DESC, trade_count DESC, strategy_id ASC.

    Mirrors tournament.scorecard.rank_scorecard_rows tie-breaking. NaN
    returns are coerced to -math.inf so they sort to the bottom
    (Python's sort has undefined behavior with NaN keys). Pure stdlib
    so the report works without importing the harness.
    """
    import math as _math

    def _safe_return(r: dict) -> float:
        try:
            v = float(r.get("return_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            return -_math.inf
        return v if not _math.isnan(v) else -_math.inf

    def _key(r: dict) -> tuple[float, int, str]:
        try:
            tc = int(r.get("trade_count", 0) or 0)
        except (TypeError, ValueError):
            tc = 0
        return (
            -_safe_return(r),
            -tc,
            str(r.get("strategy_id", "")),
        )
    return sorted(rows, key=_key)


def generate_matrix_report(
    *,
    output_root: Path,
    report_root: Path,
    run_id: str,
    n_total: int,
    statuses: list[str],
    git_sha: str,
    env_lock_hash_val: str,
    data_db_sha: str | None,
) -> None:
    """Build the matrix report from completed manifests.

    Writes three artifacts under ``report_root/``:
      - matrix.json       — ranked real scorecards + run metadata
      - matrix.md         — human-readable ranked table
      - verdict_memo.md   — survivor list under FTMO costs (or "no survivors")

    v1_stub scorecards are excluded from all three artifacts; their count
    is logged to stdout as a safety check. ``n_total`` vs filtered count
    is the gap detector — if filtered < n_total, the report notes the gap.
    """
    report_root.mkdir(parents=True, exist_ok=True)
    manifests = _read_manifests(output_root)
    real, stub = _filter_real_scorecards(manifests)
    real_rows = _rank_rows(real)
    survivors = _verdict_filter(real_rows)
    n_real = len(real)
    n_stub = len(stub)
    n_manifests = len(manifests)
    gap = n_total - n_manifests  # cells with no manifest at all
    status_counts: dict[str, int] = {}
    for s in statuses:
        status_counts[s] = status_counts.get(s, 0) + 1

    # ── matrix.json ──────────────────────────────────────────────────────
    matrix_payload = {
        "run_id": run_id,
        "git_sha": git_sha,
        "env_lock_hash": env_lock_hash_val,
        "data_db_sha": data_db_sha,
        "n_cells_requested": n_total,
        "n_manifests": n_manifests,
        "n_real_scorecards": n_real,
        "n_v1_stub_excluded": n_stub,
        "n_gap": gap,
        "status_counts": status_counts,
        "rows": [
            {
                "rank": i + 1,
                "cell_id": r.get("cell_id"),
                "strategy_id": r.get("strategy_id"),
                "symbol": r.get("symbol"),
                "timeframe": r.get("timeframe"),
                "return_pct": r.get("return_pct"),
                "max_dd_pct": r.get("max_dd_pct"),
                "daily_dd_breaches": r.get("daily_dd_breaches"),
                "total_dd_breaches": r.get("total_dd_breaches"),
                "trade_count": r.get("trade_count"),
                "git_sha": r.get("git_sha"),
                "env_lock_hash": r.get("env_lock_hash"),
                "compute_runtime": r.get("compute_runtime"),
                "db_sha": r.get("db_sha"),
            }
            for i, r in enumerate(real_rows)
        ],
        "survivors": [
            {
                "rank": r.get("rank"),
                "cell_id": r.get("cell_id"),
                "strategy_id": r.get("strategy_id"),
                "symbol": r.get("symbol"),
                "timeframe": r.get("timeframe"),
                "return_pct": r.get("return_pct"),
                "max_dd_pct": r.get("max_dd_pct"),
                "trade_count": r.get("trade_count"),
            }
            for r in survivors
        ],
    }
    (report_root / "matrix.json").write_text(
        json.dumps(matrix_payload, indent=2, sort_keys=True, default=str)
    )

    # ── matrix.md ─────────────────────────────────────────────────────────
    md_lines: list[str] = [
        f"# Tournament matrix — {run_id}",
        "",
        f"- git_sha: `{git_sha}`",
        f"- env_lock_hash: `{env_lock_hash_val}`",
        f"- data_db_sha: `{data_db_sha or 'unknown'}`",
        f"- cells requested: {n_total}",
        f"- manifests found: {n_manifests}",
        f"- real scorecards: {n_real}",
        f"- v1_stub excluded: {n_stub}",
        f"- gap (no manifest): {gap}",
        f"- status breakdown: {status_counts}",
        "",
        "## Ranked scorecards (under FTMO costs)",
        "",
        "| Rank | Strategy | Symbol | TF | Return% | MaxDD% | DailyBreaches | TotalBreaches | Trades |",
        "|------|----------|--------|----|---------|--------|---------------|---------------|--------|",
    ]
    for i, r in enumerate(real_rows, start=1):
        md_lines.append(
            f"| #{i} | {r.get('strategy_id','')} | {r.get('symbol','')} | "
            f"{r.get('timeframe','')} | "
            f"{float(r.get('return_pct', 0.0) or 0.0):+.2f} | "
            f"{float(r.get('max_dd_pct', 0.0) or 0.0):.2f} | "
            f"{r.get('daily_dd_breaches', 0)} | "
            f"{r.get('total_dd_breaches', 0)} | "
            f"{r.get('trade_count', 0)} |"
        )
    if not real_rows:
        md_lines.append("| _ | _no real scorecards_ | _ | _ | _ | _ | _ | _ | _ |")
    (report_root / "matrix.md").write_text("\n".join(md_lines) + "\n")

    # ── verdict_memo.md ──────────────────────────────────────────────────
    verdict_lines: list[str] = [
        f"# Verdict memo — {run_id}",
        "",
        "## Filter",
        "",
        "FTMO survivor rule (card 05fa0065 AC #4):",
        "  - `max_dd_pct < 10.0` (under FTMO-realistic costs)",
        "  - `trade_count >= 20` (statistical-confidence floor)",
        "",
        "v1_stub:true scorecards are excluded from this analysis (they come",
        "from the local-fallback path and are not real cost-aware scorecards).",
        "",
        "## Survivors",
        "",
    ]
    if survivors:
        verdict_lines.append(f"**{len(survivors)} survivor(s)** survive realistic costs:")
        verdict_lines.append("")
        for r in survivors:
            verdict_lines.append(
                f"- `#{r.get('rank')} {r.get('strategy_id')}` "
                f"on {r.get('symbol')}/{r.get('timeframe')} — "
                f"return {float(r.get('return_pct', 0.0) or 0.0):+.2f}%, "
                f"max DD {float(r.get('max_dd_pct', 0.0) or 0.0):.2f}%, "
                f"{r.get('trade_count')} trades"
            )
    else:
        verdict_lines.extend(
            [
                "**No survivors.**",
                "",
                "Zero cells satisfy `max_dd_pct < 10% AND trade_count >= 20`",
                "under FTMO-realistic costs. This IS the deliverable; we do not",
                "hedge. The matrix ran end-to-end (see matrix.json + matrix.md)",
                "and the field is empty.",
                "",
                f"- cells requested: {n_total}",
                f"- manifests found: {n_manifests}",
                f"- real scorecards: {n_real}",
                f"- v1_stub excluded: {n_stub}",
            ]
        )
    (report_root / "verdict_memo.md").write_text("\n".join(verdict_lines) + "\n")

    # Stdout summary so the operator can grep without opening the report.
    print(
        f"[report] {report_root}/matrix.json matrix.md verdict_memo.md "
        f"({n_real} real, {n_stub} stub excluded, {len(survivors)} survivors)"
    )


if __name__ == "__main__":
    sys.exit(main())
