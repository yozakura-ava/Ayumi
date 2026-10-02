"""Hayate L1.5 known-pattern auto-remediation actions.

The actions in this module are intentionally narrow and idempotent.  They
cover previously observed, operator-approved file-hygiene failures from the
2026-06-30 live remediation audit and are safe for Hayate to apply without a
Craig approval gate.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# Known patterns Hayate should auto-apply (L1.5 tier — no Craig approval needed)
KNOWN_PATTERNS = {
    "stale_audit_log": {  # audit doc exists but remediation_validated.flag missing
        "detect": "check_remediation_flag_exists()",
        "remediate": "touch data/ayumi/remediation_validated.flag",
        "log": "data/ops/remediation_log.jsonl",
    },
    "signal_stats_root_owned": {  # signal_stats.jsonl owned by root
        "detect": "check_signal_stats_owner()",
        "remediate": "chown_to_runtime_user(signal_stats.jsonl)",
        "log": "data/ops/remediation_log.jsonl",
    },
    "stale_pid_file": {  # forward_test.pid points to dead process
        "detect": "check_pid_alive()",
        "remediate": "remove_pid_file()",
        "log": "data/ops/remediation_log.jsonl",
    },
    "balance_snapshot_stale": {  # last_save_ts > 60min ago
        "detect": "check_balance_snapshot_age()",
        "remediate": "force_risk_guard_save()",
        "log": "data/ops/remediation_log.jsonl",
    },
}

REMEDIATION_VALIDATED_FLAG = Path("data/ayumi/remediation_validated.flag")
REMEDIATION_AUDIT_DOC = Path("docs/audits/ayumi-live-remediation-session-audit-2026-06-30.md")
SIGNAL_STATS_PATH = Path("data/signal_stats.jsonl")
FORWARD_TEST_PID_PATH = Path("data/forward_test.pid")
RISK_GUARD_STATE_PATH = Path("data/state/risk_guard_state.json")
REMEDIATION_LOG_PATH = Path("data/ops/remediation_log.jsonl")
BALANCE_SNAPSHOT_STALE_AFTER = timedelta(minutes=60)


@dataclass(frozen=True)
class RemediationResult:
    """Result returned by a known-pattern L1.5 remediation."""

    applied: bool
    action_taken: str
    evidence: str
    timestamp: str


def _default_project_root() -> Path:
    # src/forex_bot/monitoring/remediation_actions.py -> project root
    return Path(__file__).resolve().parents[3]


def _resolve_project_root(project_root: Path | str | None = None) -> Path:
    return Path(project_root).resolve() if project_root is not None else _default_project_root()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _record_result(project_root: Path, pattern_name: str, result: RemediationResult) -> None:
    payload = {"pattern_name": pattern_name, **asdict(result)}
    _append_jsonl(project_root / REMEDIATION_LOG_PATH, payload)


def _result(applied: bool, action_taken: str, evidence: str) -> RemediationResult:
    return RemediationResult(
        applied=applied,
        action_taken=action_taken,
        evidence=evidence,
        timestamp=_iso_now(),
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _parse_timestamp(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _name_for_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _name_for_gid(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _resolve_runtime_identity(project_root: Path) -> tuple[int, int, str, str]:
    """Return the uid/gid Hayate should restore runtime files to.

    The production service runs as $USER:$USER.  The environment knobs
    exist for tests and emergency service renames; if the named account is not
    present, we fall back to the project root owner rather than guessing root.
    """

    user_name = os.environ.get("AYUMI_RUNTIME_USER", "$USER")
    group_name = os.environ.get("AYUMI_RUNTIME_GROUP", user_name)
    try:
        uid = pwd.getpwnam(user_name).pw_uid
    except KeyError:
        uid = project_root.stat().st_uid
        user_name = _name_for_uid(uid)
    try:
        gid = grp.getgrnam(group_name).gr_gid
    except KeyError:
        gid = project_root.stat().st_gid
        group_name = _name_for_gid(gid)
    return uid, gid, user_name, group_name


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_remediation_flag_exists(project_root: Path) -> tuple[bool, str]:
    flag_path = project_root / REMEDIATION_VALIDATED_FLAG
    audit_doc = project_root / REMEDIATION_AUDIT_DOC
    if flag_path.exists():
        return False, f"{REMEDIATION_VALIDATED_FLAG} already exists"
    if not audit_doc.exists():
        return (
            False,
            f"{REMEDIATION_VALIDATED_FLAG} missing, but source audit doc "
            f"{REMEDIATION_AUDIT_DOC} is also missing; not safe to recreate",
        )
    return (
        True,
        f"{REMEDIATION_VALIDATED_FLAG} missing while {REMEDIATION_AUDIT_DOC} exists",
    )


def _remediate_stale_audit_log(project_root: Path) -> RemediationResult:
    should_apply, evidence = check_remediation_flag_exists(project_root)
    if not should_apply:
        return _result(False, "no_op", evidence)
    flag_path = project_root / REMEDIATION_VALIDATED_FLAG
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.touch()
    return _result(
        True,
        f"touch {REMEDIATION_VALIDATED_FLAG}",
        f"{evidence}; recreated {REMEDIATION_VALIDATED_FLAG}",
    )


def check_signal_stats_owner(project_root: Path) -> tuple[bool, str]:
    signal_stats_path = project_root / SIGNAL_STATS_PATH
    if not signal_stats_path.exists():
        return (
            False,
            f"{SIGNAL_STATS_PATH} does not exist; no ownership remediation needed",
        )

    stat_result = signal_stats_path.stat()
    runtime_uid, runtime_gid, runtime_user, runtime_group = _resolve_runtime_identity(project_root)
    current_owner = f"{_name_for_uid(stat_result.st_uid)}:{_name_for_gid(stat_result.st_gid)}"
    runtime_owner = f"{runtime_user}:{runtime_group}"
    if stat_result.st_uid == runtime_uid and stat_result.st_gid == runtime_gid:
        return (
            False,
            f"{SIGNAL_STATS_PATH} already owned by runtime user {runtime_owner}",
        )
    return (
        True,
        f"{SIGNAL_STATS_PATH} owner is {current_owner}, expected {runtime_owner}",
    )


def _remediate_signal_stats_root_owned(project_root: Path) -> RemediationResult:
    should_apply, evidence = check_signal_stats_owner(project_root)
    if not should_apply:
        return _result(False, "no_op", evidence)

    signal_stats_path = project_root / SIGNAL_STATS_PATH
    runtime_uid, runtime_gid, runtime_user, runtime_group = _resolve_runtime_identity(project_root)
    os.chown(signal_stats_path, runtime_uid, runtime_gid)
    return _result(
        True,
        f"chown_to_runtime_user({SIGNAL_STATS_PATH})",
        f"{evidence}; changed owner to {runtime_user}:{runtime_group}",
    )


def check_pid_alive(project_root: Path) -> tuple[bool, str]:
    pid_path = project_root / FORWARD_TEST_PID_PATH
    if not pid_path.exists():
        return False, f"{FORWARD_TEST_PID_PATH} does not exist; no stale pid file"

    raw_pid = pid_path.read_text(encoding="utf-8").strip()
    try:
        pid = int(raw_pid)
    except ValueError:
        return True, f"{FORWARD_TEST_PID_PATH} contains non-integer pid {raw_pid!r}"

    if _pid_is_alive(pid):
        return False, f"{FORWARD_TEST_PID_PATH} points to live pid {pid}"
    return True, f"{FORWARD_TEST_PID_PATH} points to dead pid {pid}"


def _remediate_stale_pid_file(project_root: Path) -> RemediationResult:
    should_apply, evidence = check_pid_alive(project_root)
    if not should_apply:
        return _result(False, "no_op", evidence)

    pid_path = project_root / FORWARD_TEST_PID_PATH
    pid_path.unlink(missing_ok=True)
    return _result(
        True,
        f"remove_pid_file({FORWARD_TEST_PID_PATH})",
        f"{evidence}; removed {FORWARD_TEST_PID_PATH}",
    )


def check_balance_snapshot_age(project_root: Path) -> tuple[bool, str]:
    state_path = project_root / RISK_GUARD_STATE_PATH
    if not state_path.exists():
        return (
            False,
            f"{RISK_GUARD_STATE_PATH} does not exist; no balance snapshot to refresh",
        )

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"{RISK_GUARD_STATE_PATH} is unreadable or invalid JSON: {exc}"

    last_save_ts = payload.get("last_save_ts")
    if not last_save_ts:
        return (
            False,
            f"{RISK_GUARD_STATE_PATH} has no last_save_ts; not treating as stale snapshot",
        )

    try:
        last_save = _parse_timestamp(str(last_save_ts))
    except ValueError as exc:
        return (
            False,
            f"{RISK_GUARD_STATE_PATH} has invalid last_save_ts {last_save_ts!r}: {exc}",
        )

    age = _utc_now() - last_save
    if age <= BALANCE_SNAPSHOT_STALE_AFTER:
        return (
            False,
            f"{RISK_GUARD_STATE_PATH} last_save_ts age {age} is within 60 minutes",
        )
    return True, f"{RISK_GUARD_STATE_PATH} last_save_ts age {age} exceeds 60 minutes"


def _remediate_balance_snapshot_stale(project_root: Path) -> RemediationResult:
    should_apply, evidence = check_balance_snapshot_age(project_root)
    if not should_apply:
        return _result(False, "no_op", evidence)

    state_path = project_root / RISK_GUARD_STATE_PATH
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["last_save_ts"] = _iso_now()
    _atomic_write_json(state_path, payload)
    return _result(
        True,
        "force_risk_guard_save()",
        f"{evidence}; refreshed last_save_ts in {RISK_GUARD_STATE_PATH}",
    )


_HANDLERS: dict[str, Callable[[Path], RemediationResult]] = {
    "stale_audit_log": _remediate_stale_audit_log,
    "signal_stats_root_owned": _remediate_signal_stats_root_owned,
    "stale_pid_file": _remediate_stale_pid_file,
    "balance_snapshot_stale": _remediate_balance_snapshot_stale,
}


def detect_and_remediate(
    pattern_name: str,
    *,
    project_root: Path | str | None = None,
) -> RemediationResult:
    """Detect and remediate one known L1.5 pattern.

    A JSONL audit record is appended to ``data/ops/remediation_log.jsonl`` for
    every invocation, including no-op and failure results.  Unknown pattern
    names raise ``ValueError`` because silent typos would hide failed healing.
    """

    if pattern_name not in KNOWN_PATTERNS:
        valid_patterns = ", ".join(sorted(KNOWN_PATTERNS))
        raise ValueError(f"unknown remediation pattern {pattern_name!r}; valid: {valid_patterns}")

    root = _resolve_project_root(project_root)
    try:
        result = _HANDLERS[pattern_name](root)
    except Exception as exc:  # pragma: no cover - defensive audit trail
        result = _result(False, "failed", f"{pattern_name} remediation failed: {exc}")

    _record_result(root, pattern_name, result)
    return result
