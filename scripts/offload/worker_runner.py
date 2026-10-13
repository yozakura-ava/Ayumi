#!/usr/bin/env python3
"""Worker-side runner loop — persistent job-dir polling daemon (card 05fa0065).

Per the Phase 1a design (see card comment 2140c28c for the rationale): the
68-cell tournament matrix runs through a worker-side polling daemon instead
of port-8877. The daemon picks up job JSON files dropped by the gateway
into ``/tmp/ayumi-jobs/{run_id}/incoming/``, executes each cell via the
tournament harness with the FTMO cost model, and writes the result to
``done/{cell_id}.json`` for the gateway to fetch.

Lifecycle:
  1. Started once on ava-worker via ``exec(host=node)`` + ``nohup`` (the
     gateway's BundleTransport only does push+fetch; persistent execution
     needs a daemon, and a file-based polling daemon is the thinnest
     surface available).
  2. Atomically renames ``incoming/{cell_id}.json`` → ``processing/{cell_id}.json``
     before execution (so two concurrent workers never pick the same job).
  3. Writes ``done/{cell_id}.output.json`` (the scorecard + manifest) and
     a sentinel ``done/{cell_id}.done`` after each cell.
  4. On SIGTERM/SIGINT, finishes the in-flight cell and exits cleanly.

Output JSON schema (per cell_id):
  {
    "schema_version": 1,
    "cell_id": "<sha256:16>",
    "run_id": "matrix-2026-09-15",
    "strategy": "...",
    "symbol": "...",
    "timeframe": "...",
    "started_at": "<iso8601>",
    "finished_at": "<iso8601>",
    "wall_time_s": <float>,
    "compute_runtime": "cpu",
    "git_sha": "<worker HEAD sha>",
    "env_lock_hash": "<env lock hash>",
    "cost_model": {...},
    "scorecard": {
      "return_pct": ...,
      "max_dd_pct": ...,
      "daily_dd_breaches": ...,
      "total_dd_breaches": ...,
      "trade_count": ...
    },
    "run_meta": {"signals": ..., "trades": ...}
  }

Failure modes are fail-loud:
  - missing data DB → output.json has ``exit_code=1``, ``error="db_missing"``
  - TournamentEmptyWindow (zero bars) → ``exit_code=2``, ``error="empty_window"``
  - TournamentNoSignals (strategy emits 0 signals) → ``exit_code=3``,
    ``error="no_signals"`` (this is the c4b86732 fail-loud guard — the
    matrix will surface the strategy that silently died rather than
    pretend it succeeded)
  - Any other exception → ``exit_code=1``, ``error="<class>: <msg>"``

Usage (run ONCE on the worker):
  python3 scripts/offload/worker_runner.py \
      --run-id matrix-2026-09-15 \
      --job-dir /tmp/ayumi-jobs \
      --poll-interval 0.5

The matrix driver on the gateway is responsible for dropping job JSONs
and fetching outputs; the worker_runner only consumes + executes.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make ``tournament`` importable when invoked as a script on the worker.
# The matrix checkout puts these at the repo root; the worker_runner
# expects to be invoked with the repo's ``scripts/`` as the script
# parent (see --code-root arg).
_REPO_SRC_HINT = os.environ.get("AYUMI_REPO_ROOT", "")


def _ensure_import_paths(code_root: Path) -> None:
    """Inject ``code_root/src``, ``code_root/src/forex_bot``, and ``code_root/scripts`` onto sys.path.

    Worker is invoked with PYTHONPATH unset; we mutate sys.path here so
    ``import tournament`` (``src/``), ``import core`` / ``import strategies.core_types``
    (``src/forex_bot/`` — strategies live under the forex_bot package), and
    ``import offload.*`` (``scripts/``) resolve regardless of cwd. Mirrors the
    path block in ``scripts/run_tournament.py`` (lines 31-34) and the
    ``pythonpath`` declared in ``pytest.ini`` (``src/forex_bot src tests scripts``).
    Idempotent — repeated calls don't grow sys.path unboundedly.
    """
    candidates = [
        code_root / "src",
        code_root / "src" / "forex_bot",
        code_root / "scripts",
    ]
    for c in candidates:
        sp = str(c)
        if sp not in sys.path:
            sys.path.insert(0, sp)


# Single-instance guard (lock file). Two workers racing on the same
# job-dir would double-execute. We use ``flock(2)`` on a lock file in
# the job-dir root; second invocation exits non-zero with a clear message.
@contextlib.contextmanager
def _worker_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = lock_path.open("w")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Another worker_runner holds {lock_path}; refusing to double-execute"
            ) from exc
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


# The exact field set a CostModel requires. A payload must supply ALL of them
# (CostModel has no defaults, so a partial payload would raise TypeError) and
# NONE of anything else (an unexpected kwarg raises TypeError too).
_COST_MODEL_FIELDS: tuple[str, ...] = (
    "spread_pips",
    "commission_per_lot",
    "slippage_pips",
    "pip_value_per_lot",
    "pip_size",
)


def _cost_model_payload_error(symbol: str, payload: dict, canonical) -> str | None:
    """Return an error string if a gateway cost model cannot be trusted.

    Card c0f284b8, Rin iter-1 MEDIUM-2 then iter-2 MEDIUM-A/MEDIUM-B.

    A job-supplied ``cost_model_payload`` must never silently rescore a cell.
    This checks, in order:

    1. COMPLETENESS -- every :data:`_COST_MODEL_FIELDS` entry must be present.
       ``CostModel`` has no defaults, so a partial payload raised ``TypeError``
       at the construction call, which sits *before* the ``try:`` block: the
       exception escaped ``_execute_cell``, killed the worker daemon, and
       stranded the cell with no ``output.json`` and no ``.done`` sentinel.
    2. NO EXTRA KEYS -- an unexpected kwarg raises ``TypeError`` the same way.
    3. NUMERIC -- every field must parse as a float.
    4. VALUE -- every field must equal the Liora-sourced canonical model.
       This covers ``pip_value_per_lot`` and ``pip_size`` as well as the
       spread/commission/slippage trio: the two pip fields set ``qty_lots`` and
       multiply into every cost term, so a wrong value silently rescales cost
       (measured 18.4x on a realistic XAUUSD trade).

    Returns ``None`` only when the payload is safe to use.

    Hostile descriptors are rejected, not raised on.  ``_process_one_job`` has
    a ``try``/``finally`` with no ``except`` and ``main()`` has no handler, so
    ANY exception escaping this function kills the worker daemon and strands
    the cell in ``processing/`` with no ``output.json`` and no ``.done``
    sentinel.  Two classes reached that in iter-3: a non-dict ``cost_model``
    (``"cost_model": 5``) made ``f not in payload`` raise ``TypeError``, and an
    oversized JSON integer (>= ~309 digits) made ``float(raw)`` raise
    ``OverflowError``, which is not a ``ValueError`` subclass.

    When ``canonical`` is ``None`` (symbol absent from the Liora table) steps
    1-3 still apply so a malformed payload cannot crash the worker, but the
    VALUE comparison is skipped -- such a payload is trusted as-is.  That is a
    known fail-open, recorded as an open policy question for the orchestrator;
    the ``cost_not_modeled`` gate does NOT catch it, because a supplied model
    makes the harness model a real cost and mark trades ``cost_modeled=True``.
    """
    if not isinstance(payload, dict):
        return (
            f"gateway cost_model for {symbol} must be a JSON object, got "
            f"{type(payload).__name__} (card c0f284b8)."
        )
    missing = [f for f in _COST_MODEL_FIELDS if f not in payload]
    if missing:
        return (
            f"gateway cost_model_payload for {symbol} is missing required "
            f"field(s): {', '.join(missing)}. Refusing to build a CostModel "
            f"from a partial payload (card c0f284b8)."
        )
    unexpected = [k for k in payload if k not in _COST_MODEL_FIELDS]
    if unexpected:
        return (
            f"gateway cost_model_payload for {symbol} carries unexpected "
            f"field(s): {', '.join(sorted(unexpected))}. Expected exactly "
            f"{', '.join(_COST_MODEL_FIELDS)} (card c0f284b8)."
        )

    parsed: dict[str, float] = {}
    for field_name in _COST_MODEL_FIELDS:
        raw = payload[field_name]
        if isinstance(raw, bool):
            return (
                f"gateway cost_model_payload for {symbol}: {field_name}={raw!r} "
                f"is a bool, not a number (card c0f284b8)."
            )
        try:
            parsed[field_name] = float(raw)
        except OverflowError:
            # A JSON integer too large for a float. NOT a ValueError subclass,
            # so it escapes a (TypeError, ValueError) handler.
            return (
                f"gateway cost_model_payload for {symbol}: {field_name} is too "
                f"large to be a finite cost value (card c0f284b8)."
            )
        except (TypeError, ValueError):
            return (
                f"gateway cost_model_payload for {symbol}: {field_name}={raw!r} "
                f"is not numeric (card c0f284b8)."
            )

    if canonical is None:
        return None

    mismatches = []
    for field_name in _COST_MODEL_FIELDS:
        want = float(getattr(canonical, field_name))
        got = parsed[field_name]
        # NaN/inf never compare equal to a finite canonical value, so a single
        # `!=` rejects them without a special case.
        if got != want:
            mismatches.append(f"{field_name}={got!r} (Liora table: {want!r})")
    if mismatches:
        return (
            f"gateway cost_model_payload for {symbol} contradicts the Liora "
            f"spread-cost table: {'; '.join(mismatches)}. Refusing to score this "
            f"cell on a conflicting cost constant (card c0f284b8)."
        )
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically via a tmp file + ``os.replace``.

    POSIX-atomic on the same FS (the per-run done/ dir is one FS). Mirrors
    ``scripts/offload/manifest.atomic_write_manifest`` semantics so a
    crash mid-write leaves either the old file or the new file — never a
    truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    os.replace(tmp, path)


def _move_job_to_subdir(job_path: Path, target_dir: Path) -> Path:
    """Atomically move a job descriptor from incoming/ to processing/ or done/.

    Uses ``os.rename`` (POSIX-atomic on same FS). Returns the new path.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / job_path.name
    os.rename(job_path, target_path)
    return target_path


def _compute_cell_id(strategy: str, symbol: str, timeframe: str) -> str:
    """SHA256:16 hash matching ``scripts/offload/seed.cell_id``.

    Duplicated here so the worker can validate the descriptor's ``cell_id``
    field matches its (strategy, symbol, timeframe) before executing —
    catches gateway-side descriptor corruption early without importing
    the whole offload package.
    """
    payload = f"{strategy}|{symbol}|{timeframe}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _execute_cell(job: dict[str, Any], code_root: Path) -> dict[str, Any]:
    """Execute one tournament cell, returning the output JSON payload.

    Imports ``tournament`` lazily so the worker_runner can boot even when
    the harness isn't on sys.path yet (early debug path). The function is
    pure-functional over the job descriptor: no I/O outside what the
    harness reads (DuckDB via AYUMI_DUCKDB_PATH or job['data_db_path']).
    """
    strategy = str(job["strategy"])
    symbol = str(job["symbol"]).upper()
    timeframe = str(job["timeframe"]).upper()
    expected_cell_id = str(job["cell_id"])
    actual_cell_id = _compute_cell_id(strategy, symbol, timeframe)
    if actual_cell_id != expected_cell_id:
        return {
            "schema_version": 1,
            "cell_id": expected_cell_id,
            "exit_code": 1,
            "error": f"cell_id_mismatch: descriptor={expected_cell_id} computed={actual_cell_id}",
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "started_at": job.get("started_at") or _now_iso(),
            "finished_at": _now_iso(),
        }

    cost_model_payload = job.get("cost_model") or {}
    data_db_path = job.get("data_db_path")
    db_sha = job.get("data_db_sha")
    if not data_db_path:
        return {
            "schema_version": 1,
            "cell_id": expected_cell_id,
            "exit_code": 1,
            "error": "missing_data_db_path",
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "started_at": _now_iso(),
            "finished_at": _now_iso(),
        }

    # Honor AYUMI_DUCKDB_PATH so the harness's resolve_default_duckdb_path
    # uses our subset DB rather than the 53GB main DB. We override with
    # an absolute path so the harness never accidentally reads main-tree.
    os.environ["AYUMI_DUCKDB_PATH"] = str(data_db_path)

    from tournament.harness import (  # noqa: E402
        CostModel,
        TournamentEmptyWindow,
        TournamentHarness,
        TournamentNoSignals,
        cost_model_for,
        row_is_cost_modeled,
    )

    cm = cost_model_for(symbol)

    started_at = _now_iso()
    wall_start = time.monotonic()

    if cost_model_payload:
        # Card c0f284b8 (Rin iter-1 MEDIUM-2, iter-2 MEDIUM-A/MEDIUM-B): a
        # gateway-supplied cost model must be complete, well-formed, and in
        # agreement with the Liora single source of truth -- otherwise the cell
        # fails loudly instead of being silently rescored.
        payload_error = _cost_model_payload_error(symbol, cost_model_payload, cm)
        if payload_error:
            return {
                "schema_version": 1,
                "cell_id": expected_cell_id,
                "exit_code": 5,
                "error": payload_error,
                "strategy": strategy,
                "symbol": symbol,
                "timeframe": timeframe,
                "started_at": started_at,
                "finished_at": _now_iso(),
                "wall_time_s": round(time.monotonic() - wall_start, 3),
            }
        try:
            # Build from NORMALIZED floats, not the raw payload: the validator
            # already proved every field parses, and this stops a numeric
            # string ("3.0") from silently failing much later.
            cm = CostModel(**{
                f: float(cost_model_payload[f]) for f in _COST_MODEL_FIELDS
            })
        except (TypeError, ValueError, OverflowError) as exc:  # pragma: no cover - belt & braces
            # Unreachable while the validator above holds, but this call sits
            # OUTSIDE the try: block, so an escaping TypeError would kill the
            # worker daemon and strand the cell rather than failing one cell.
            return {
                "schema_version": 1,
                "cell_id": expected_cell_id,
                "exit_code": 5,
                "error": f"cost_model_payload_rejected: {exc} (card c0f284b8)",
                "strategy": strategy,
                "symbol": symbol,
                "timeframe": timeframe,
                "started_at": started_at,
                "finished_at": _now_iso(),
                "wall_time_s": round(time.monotonic() - wall_start, 3),
            }

    try:
        harness = TournamentHarness(
            strategy_ids=[strategy],
            db_path=Path(data_db_path),
            symbol=symbol,
            timeframe=timeframe,
            starting_equity=1.0,
            cost_model=cm,
        )
        scorecard = harness.run()
        wall_time = time.monotonic() - wall_start
        finished_at = _now_iso()
        if not scorecard.rows:
            return {
                "schema_version": 1,
                "cell_id": expected_cell_id,
                "exit_code": 4,
                "error": "no_scorecard_rows",
                "strategy": strategy,
                "symbol": symbol,
                "timeframe": timeframe,
                "started_at": started_at,
                "finished_at": finished_at,
                "wall_time_s": wall_time,
            }
        row = scorecard.rows[0]
        run_meta = scorecard.meta.get("run_meta", {}).get(strategy, {})

        # Card c0f284b8 (Rin iter-1 HIGH-1): a row whose costs could not be
        # modeled must never be emitted as a successful cell. Before this the
        # runner returned exit_code 0 with a cost-free return_pct for symbols
        # outside the cost table -- the silent cost-free row AC2 exists to
        # kill. The cell now fails loudly instead.
        if not row_is_cost_modeled(row):
            return {
                "schema_version": 1,
                "cell_id": expected_cell_id,
                "run_id": job.get("run_id", ""),
                "exit_code": 5,
                "error": (
                    "cost_not_modeled: this cell's costs could not be modeled, "
                    "so its return_pct is not comparable to cost-modeled cells. "
                    "Refusing to report it as a successful tournament result "
                    "(card c0f284b8)."
                ),
                "strategy": strategy,
                "symbol": symbol,
                "timeframe": timeframe,
                "started_at": started_at,
                "finished_at": finished_at,
                "wall_time_s": round(wall_time, 3),
                "compute_runtime": "cpu",
                "git_sha": job.get("git_sha", ""),
                "env_lock_hash": job.get("env_lock_hash", ""),
                "db_sha": db_sha,
                "cost_model": None if cm is None else {
                    "spread_pips": cm.spread_pips,
                    "commission_per_lot": cm.commission_per_lot,
                    "slippage_pips": cm.slippage_pips,
                    "pip_value_per_lot": cm.pip_value_per_lot,
                    "pip_size": cm.pip_size,
                },
                "cost_modeled": False,
                "run_meta": run_meta,
            }

        return {
            "schema_version": 1,
            "cell_id": expected_cell_id,
            "run_id": job.get("run_id", ""),
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "started_at": started_at,
            "finished_at": finished_at,
            "wall_time_s": round(wall_time, 3),
            "compute_runtime": "cpu",
            "git_sha": job.get("git_sha", ""),
            "env_lock_hash": job.get("env_lock_hash", ""),
            "db_sha": db_sha,
            "cost_model": (
                {
                    "spread_pips": cm.spread_pips,
                    "commission_per_lot": cm.commission_per_lot,
                    "slippage_pips": cm.slippage_pips,
                    "pip_value_per_lot": cm.pip_value_per_lot,
                    "pip_size": cm.pip_size,
                }
                if cm is not None
                else None
            ),
            "cost_modeled": row.cost_modeled,
            "scorecard": {
                "rank": row.rank,
                "return_pct": row.return_pct,
                "max_dd_pct": row.max_dd_pct,
                "daily_dd_breaches": row.daily_dd_breaches,
                "total_dd_breaches": row.total_dd_breaches,
                "trade_count": row.trade_count,
                "source": row.source,
                # Carried inside the scorecard block too, so a consumer
                # reading only this sub-object still sees the provenance.
                "cost_modeled": row.cost_modeled,
            },
            "run_meta": run_meta,
            "exit_code": 0,
        }
    except TournamentNoSignals as exc:
        return {
            "schema_version": 1,
            "cell_id": expected_cell_id,
            "exit_code": 3,
            "error": f"no_signals: {exc.detail}",
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "wall_time_s": round(time.monotonic() - wall_start, 3),
        }
    except TournamentEmptyWindow as exc:
        return {
            "schema_version": 1,
            "cell_id": expected_cell_id,
            "exit_code": 2,
            "error": f"empty_window: {exc.detail}",
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "wall_time_s": round(time.monotonic() - wall_start, 3),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "schema_version": 1,
            "cell_id": expected_cell_id,
            "exit_code": 1,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "wall_time_s": round(time.monotonic() - wall_start, 3),
        }


def _worker_git_sha(code_root: Path) -> str:
    """Capture the worker's code_root HEAD SHA at startup (Q4.e two-SHA split).

    Falls back to ``"unknown"`` if git is unavailable so the worker never
    fails on a misconfigured git. The value lands in every output.json's
    ``git_sha`` field so drift detection is mechanical (compare across
    cells, or against the gateway-side dispatcher's git_sha).
    """
    git_bin = shutil.which("git") or "/usr/bin/git"
    try:
        out = subprocess.run(  # noqa: S603 — args hardcoded; binary resolved via shutil.which w/ /usr/bin/git fallback
            [git_bin, "rev-parse", "HEAD"],
            cwd=str(code_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        return f"unknown:{type(exc).__name__}"


def _process_one_job(
    job_path: Path,
    *,
    processing_dir: Path,
    done_dir: Path,
    code_root: Path,
    log_path: Path,
) -> dict[str, Any] | None:
    """Pick up one job, execute it, persist output. Returns output dict or None."""
    cell_id_stem = job_path.stem  # cell_id is filename minus .json
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a")
    try:
        try:
            job = json.loads(job_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.write(
                f"[{_now_iso()}] WARN: dropping corrupt job {job_path.name}: "
                f"{type(exc).__name__}: {exc}\n"
            )
            # Quarantine: move to a corrupt/ subdir so we don't loop on it.
            corrupt_dir = job_path.parent.parent / "corrupt"
            corrupt_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(job_path), str(corrupt_dir / job_path.name))
            return None

        processing_path = _move_job_to_subdir(job_path, processing_dir)
        log.write(
            f"[{_now_iso()}] START cell_id={cell_id_stem} "
            f"strategy={job.get('strategy')} symbol={job.get('symbol')} "
            f"timeframe={job.get('timeframe')}\n"
        )
        log.flush()

        output = _execute_cell(job, code_root)
        output_path = done_dir / f"{cell_id_stem}.output.json"
        _atomic_write_json(output_path, output)
        # Sentinel file: gateway-side dispatcher can `ls done/{cell_id}.done`
        # via exec(host=node) to know the cell is fetchable.
        (done_dir / f"{cell_id_stem}.done").write_text(_now_iso())

        # Move the descriptor out of processing/ to done/ too — keeps the
        # full audit trail in one place per cell.
        done_desc = _move_job_to_subdir(processing_path, done_dir)
        log.write(
            f"[{_now_iso()}] DONE cell_id={cell_id_stem} "
            f"exit_code={output.get('exit_code')} "
            f"wall_time_s={output.get('wall_time_s', 'n/a')} "
            f"descriptor={done_desc.name}\n"
        )
        log.flush()
        return output
    finally:
        log.close()


def _list_incoming(incoming_dir: Path) -> list[Path]:
    """List incoming job files in deterministic order (cell_id ASC)."""
    if not incoming_dir.is_dir():
        return []
    return sorted(p for p in incoming_dir.iterdir() if p.is_file() and p.suffix == ".json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="worker_runner",
        description=(
            "Worker-side polling daemon for the 68-cell tournament matrix. "
            "Picks up job JSONs from --job-dir/incoming/, executes the cell "
            "via the tournament harness with the FTMO cost model, writes "
            "output JSON + .done sentinel to done/. Started once on the "
            "worker via exec(host=node) + nohup."
        ),
    )
    parser.add_argument(
        "--run-id", required=True,
        help="Run identifier; the worker only consumes jobs under {job-dir}/{run-id}/",
    )
    parser.add_argument(
        "--job-dir", default="/tmp/ayumi-jobs",  # noqa: S108 — descriptive default matching transport.py/live_smoke convention; worker-runtime scratch root is intentional
        help="Root directory for {incoming,processing,done}/ subdirs (default: /tmp/ayumi-jobs)",
    )
    parser.add_argument(
        "--code-root", required=True,
        help=(
            "Path to the Ayumi checkout on the worker (the worker_runner "
            "expects tournament + offload packages importable from "
            "{code-root}/src and {code-root}/scripts)."
        ),
    )
    parser.add_argument(
        "--poll-interval", type=float, default=0.5,
        help="Seconds between polls of incoming/ (default: 0.5)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Process all currently-pending jobs then exit (smoke-test mode).",
    )
    parser.add_argument(
        "--max-runtime-s", type=float, default=0,
        help="If > 0, exit cleanly after this many seconds (default: 0 = run forever).",
    )
    args = parser.parse_args(argv)

    code_root = Path(args.code_root).resolve()
    if not (code_root / "src" / "tournament").is_dir():
        print(
            f"FATAL: --code-root {code_root} has no src/tournament/; "
            f"check the path",
            file=sys.stderr,
        )
        return 10

    _ensure_import_paths(code_root)
    run_root = Path(args.job_dir) / args.run_id
    incoming_dir = run_root / "incoming"
    processing_dir = run_root / "processing"
    done_dir = run_root / "done"
    lock_path = run_root / "worker.lock"
    log_path = run_root / "worker.log"
    pid_path = run_root / "worker.pid"

    for d in (incoming_dir, processing_dir, done_dir):
        d.mkdir(parents=True, exist_ok=True)

    with _worker_lock(lock_path):
        pid_path.write_text(str(os.getpid()))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a")
        try:
            git_sha = _worker_git_sha(code_root)
            log.write(
                f"[{_now_iso()}] worker_runner start pid={os.getpid()} "
                f"run_id={args.run_id} code_root={code_root} "
                f"git_sha={git_sha} poll_interval={args.poll_interval}\n"
            )
            log.flush()

            # Graceful shutdown: track in-flight cell so SIGTERM lets it finish.
            stop_requested = False

            def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
                nonlocal stop_requested
                stop_requested = True
                log.write(
                    f"[{_now_iso()}] signal={signum} received; finishing "
                    f"in-flight cell then exiting\n"
                )
                log.flush()

            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)

            start_time = time.monotonic()
            idle_polls = 0
            while True:
                if stop_requested:
                    log.write(f"[{_now_iso()}] shutdown clean\n")
                    break
                if args.max_runtime_s > 0 and (time.monotonic() - start_time) >= args.max_runtime_s:
                    log.write(f"[{_now_iso()}] max-runtime reached; exit\n")
                    break
                jobs = _list_incoming(incoming_dir)
                if not jobs:
                    idle_polls += 1
                    if args.once:
                        log.write(f"[{_now_iso()}] --once: no jobs; exit\n")
                        break
                    time.sleep(args.poll_interval)
                    continue
                idle_polls = 0
                for jp in jobs:
                    if stop_requested:
                        break
                    _process_one_job(
                        jp,
                        processing_dir=processing_dir,
                        done_dir=done_dir,
                        code_root=code_root,
                        log_path=log_path,
                    )
            return 0
        finally:
            log.write(f"[{_now_iso()}] worker_runner exit\n")
            log.close()
            try:
                pid_path.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
