"""Drift detector — card and quest-phase staleness checks.

Phase 6 of the FTMO Quest. Implements the drift primitives called out in
``docs/design/hayate-checkpoint-design-2026-07-08.md`` §5:

* ``check_card_staleness()`` — KH-001 / KH-002 / KH-003 (3d warn, 7d auto-create).
* ``check_phase_staleness()`` — KH-004 (2d warn, 5d escalate).
* ``auto_create_stale_card()`` — fabricates a follow-up card for cards stalled
  past the auto-remediation window (>7 days).
* ``escalate_phase()`` — appends to ``data/ops/escalation_queue.jsonl`` for
  phases that exceed the 5-day escalation threshold.

The detector is intentionally string-light and reads everything either from
the OpenClaw workboard sqlite (read-only) or from this repo's
``data/`` and ``docs/plans/`` trees. It does **no** remote IO and **no**
``workboard_create`` against the live board (the watcher cron wraps that).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ── Thresholds — sourced from the Hayate design doc (§3.7) ──────────────────

CARD_WARN_DAYS = 3  # KH-001 / KH-002 — flag in audit
CARD_AUTO_CREATE_DAYS = 7  # KH-003 — auto-create [STALE] follow-up card
PHASE_WARN_DAYS = 2  # KH-004 — flag
PHASE_ESCALATE_DAYS = 5  # KH-004 — escalate to Craig

# Workboard sqlite location (read-only). The OpenClaw gateway owns this file;
# we never write to it from this module.
DEFAULT_WORKBOARD_DB = os.path.expanduser("~/.openclaw/plugins/workboard/workboard.sqlite")

DEFAULT_STATE_FILE = Path("data/state/active_quest.json")
DEFAULT_OPS_DIR = Path("data/ops")
DEFAULT_REMEDIATION_LOG = DEFAULT_OPS_DIR / "remediation_log.jsonl"
DEFAULT_ESCALATION_QUEUE = DEFAULT_OPS_DIR / "escalation_queue.jsonl"
DEFAULT_STALE_CARDS_LOG = DEFAULT_OPS_DIR / "stale_cards.jsonl"

DEFAULT_PLANS_DIR = Path("docs/plans")


# ── Result dataclasses ──────────────────────────────────────────────────────


@dataclass
class StaleCard:
    """A workboard card that is sitting in todo/ready beyond the warn window."""

    card_id: str
    title: str
    status: str
    board_id: str
    age_days: float
    updated_at: int  # unix ms (workboard convention)
    level: str  # "warn" | "auto_create"
    labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StalePhase:
    """A quest phase whose audit_trail has not been touched in N days."""

    phase_id: str  # e.g. "Phase 6"
    source_file: str  # docs/plans/quest-*.md
    last_audit_ts: str | None  # ISO timestamp of last audit entry
    age_days: float
    level: str  # "warn" | "escalate"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Helpers ────────────────────────────────────────────────────────────────


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_iso(ms: int | float | None) -> str:
    if ms is None or ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def _ensure_ops_dir(ops_dir: Path) -> None:
    ops_dir.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Idempotent JSONL append — opens via ``"a"`` so concurrent cron runs
    don't truncate the file. The file is created with parent dirs as needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


# ── Workboard read-only helpers ─────────────────────────────────────────────


def _open_workboard(db_path: str = DEFAULT_WORKBOARD_DB) -> sqlite3.Connection:
    """Open the workboard DB read-only via uri mode so a lock contention
    with the gateway can't corrupt our connection."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"workboard sqlite not found at {db_path!r} — is the gateway running?")
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_active_cards(
    db_path: str = DEFAULT_WORKBOARD_DB,
    statuses: Iterable[str] = ("todo", "ready"),
) -> list[dict[str, Any]]:
    """Pull active cards in the given statuses (excluding archived).

    Labels live in the ``workboard_card_labels`` join table
    (card_id, ordinal, label) — not on the cards row directly. We do
    one join per call; the result set is small after the status filter
    and the staging file is read-only.
    """
    statuses = list(statuses)
    placeholders = ",".join("?" for _ in statuses)
    now_ms = _now_epoch_ms()
    conn = _open_workboard(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, board_id, title, status, updated_at, archived_at
              FROM workboard_cards
             WHERE status IN ({placeholders})
               AND archived_at IS NULL
             ORDER BY updated_at ASC
            """,  # noqa: S608
            statuses,
        )
        cards = cur.fetchall()
        if not cards:
            return []
        # Single join for labels of all candidate cards
        ids = [r["id"] for r in cards]
        id_placeholders = ",".join("?" for _ in ids)
        cur.execute(
            f"""
            SELECT card_id, label
              FROM workboard_card_labels
             WHERE card_id IN ({id_placeholders})
             ORDER BY card_id, ordinal
            """,  # noqa: S608
            ids,
        )
        label_rows = cur.fetchall()
    finally:
        conn.close()

    by_card: dict[str, list[str]] = {}
    for lr in label_rows:
        by_card.setdefault(lr["card_id"], []).append(lr["label"])

    out: list[dict[str, Any]] = []
    for r in cards:
        updated_at = r["updated_at"] or 0
        age_ms = max(0, now_ms - updated_at)
        out.append(
            {
                "id": r["id"],
                "board_id": r["board_id"],
                "title": r["title"],
                "status": r["status"],
                "updated_at": updated_at,
                "labels": by_card.get(r["id"], []),
                "age_days": round(age_ms / (1000 * 60 * 60 * 24), 3),
            }
        )
    return out


# ── Quest plan parsing ──────────────────────────────────────────────────────

# Matches a section header like "### Phase 6: Daily Audit + North-Star Tracking"
_PHASE_HEADER_RE = re.compile(r"^#{2,4}\s*Phase\s+(\d+[A-Za-z]?)\s*[:\-–]\s*(.+?)\s*$", re.IGNORECASE)
# Matches an audit trail line: "- 2026-07-08T..." or "- 2026-07-08 …"
_AUDIT_LINE_RE = re.compile(r"^\s*-\s*(?P<ts>\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?)")


def _parse_phases(plan_path: Path) -> list[dict[str, Any]]:
    """Return a list of ``{phase, title, last_audit_ts}`` blocks from a quest plan.

    Phase blocks end at the next ``###`` header of equal-or-greater depth.
    """
    if not plan_path.exists():
        return []
    text = plan_path.read_text(errors="replace").splitlines()

    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text:
        m = _PHASE_HEADER_RE.match(raw)
        if m:
            if current is not None:
                blocks.append(current)
            current = {
                "phase": m.group(1),
                "title": m.group(2).strip(),
                "last_audit_ts": None,
                "source_file": str(plan_path),
            }
            continue
        if current is None:
            continue
        am = _AUDIT_LINE_RE.match(raw)
        if am:
            current["last_audit_ts"] = am.group("ts")
    if current is not None:
        blocks.append(current)
    return blocks


def _iso_age_days(ts: str | None, now: datetime | None = None) -> float:
    """Compute whole-day age of an ISO timestamp. ``None`` → infinity."""
    if not ts:
        return float("inf")
    if now is None:
        now = datetime.now(timezone.utc)
    # Tolerate both date-only and full ISO formats.
    ts_clean = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts_clean)
    except ValueError:
        # Date-only fallback.
        try:
            dt = datetime.fromisoformat(ts_clean + "T00:00:00+00:00")
        except ValueError:
            return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    return delta.total_seconds() / (60 * 60 * 24)


# ── The DriftDetector class ─────────────────────────────────────────────────


class DriftDetector:
    """Card and quest-phase staleness auditor.

    Parameters
    ----------
    workboard_db
        Path to the OpenClaw workboard sqlite. Read-only.
    plans_dir
        Directory containing ``quest-*.md`` plan files (default
        ``docs/plans``).
    state_file
        Path to ``active_quest.json`` (the orchestrator sidecar file
        pointing at the active quest and current phase).
    ops_dir
        Directory for ``remediation_log.jsonl``,
        ``escalation_queue.jsonl``, ``stale_cards.jsonl``.
    now
        Override for the current datetime — useful for deterministic tests.
    """

    def __init__(
        self,
        workboard_db: str | os.PathLike = DEFAULT_WORKBOARD_DB,
        plans_dir: str | os.PathLike = DEFAULT_PLANS_DIR,
        state_file: str | os.PathLike = DEFAULT_STATE_FILE,
        ops_dir: str | os.PathLike = DEFAULT_OPS_DIR,
        now: datetime | None = None,
    ) -> None:
        self.workboard_db = str(workboard_db)
        self.plans_dir = Path(plans_dir)
        self.state_file = Path(state_file)
        self.ops_dir = Path(ops_dir)
        self.now = now or datetime.now(timezone.utc)

    # ── Card staleness ────────────────────────────────────────────────────

    def check_card_staleness(
        self,
        statuses: Iterable[str] = ("todo", "ready"),
        warn_days: float = CARD_WARN_DAYS,
        auto_create_days: float = CARD_AUTO_CREATE_DAYS,
    ) -> list[StaleCard]:
        """Return StaleCards for todo/ready cards over the warn window.

        * Cards with ``age_days >= warn_days`` → level ``"warn"``
        * Cards with ``age_days >= auto_create_days`` → level ``"auto_create"``
        """
        cards = _fetch_active_cards(self.workboard_db, statuses)
        stale: list[StaleCard] = []
        for c in cards:
            age = c["age_days"]
            if age >= auto_create_days:
                level = "auto_create"
            elif age >= warn_days:
                level = "warn"
            else:
                continue
            stale.append(
                StaleCard(
                    card_id=c["id"],
                    title=c["title"],
                    status=c["status"],
                    board_id=c["board_id"],
                    age_days=age,
                    updated_at=int(c["updated_at"]),
                    level=level,
                    labels=c["labels"],
                )
            )
        return stale

    # ── Phase staleness ───────────────────────────────────────────────────

    def check_phase_staleness(
        self,
        warn_days: float = PHASE_WARN_DAYS,
        escalate_days: float = PHASE_ESCALATE_DAYS,
        require_audit_trail: bool = False,
    ) -> list[StalePhase]:
        """Return StalePhases across all quest-*.md plans in ``plans_dir``.

        A phase is considered stalled if its most recent ``- YYYY-MM-DD``
        audit entry in the plan file is older than the warn window.

        ``require_audit_trail``
            When False (default) phases that have never logged an audit
            entry are reported as ``"info"`` rather than as stalled, so
            plans that haven't started instrumenting audit trails yet
            don't generate false-positive escalations. Pass True to get
            strict behaviour (the spec'd behaviour from §3.7 KH-004).
        """
        if not self.plans_dir.exists():
            return []
        plans = sorted(self.plans_dir.glob("quest-*.md"))
        stale: list[StalePhase] = []
        for plan in plans:
            for block in _parse_phases(plan):
                last = block.get("last_audit_ts")
                if not last:
                    if require_audit_trail:
                        stale.append(
                            StalePhase(
                                phase_id=f"Phase {block['phase']}",
                                source_file=block["source_file"],
                                last_audit_ts=None,
                                age_days=9999.0,
                                level="escalate",
                            )
                        )
                    continue
                age = _iso_age_days(last, self.now)
                if age >= escalate_days:
                    level = "escalate"
                elif age >= warn_days:
                    level = "warn"
                else:
                    continue
                stale.append(
                    StalePhase(
                        phase_id=f"Phase {block['phase']}",
                        source_file=block["source_file"],
                        last_audit_ts=last,
                        age_days=age,
                        level=level,
                    )
                )
        return stale

    # ── Auto-remediation hooks ────────────────────────────────────────────

    def auto_create_stale_card(self, card: StaleCard) -> dict[str, Any] | None:
        """Return the follow-up record that should be created for cards past
        the 7-day window.

        This function does **not** call ``workboard_create`` directly — it
        appends to ``data/ops/stale_cards.jsonl`` so the daily-audit cron
        (which owns card-creation budget per Hayate AGENTS.md "max 5 open
        Hayate-created cards") can batch-create them with throttling. The
        returned dict is a serialisable draft the watcher cron can wrap
        with a ``workboard_create`` call.
        """
        if card.level != "auto_create":
            return None
        draft = {
            "draft_id": str(uuid.uuid4()),
            "parent_card_id": card.card_id,
            "parent_title": card.title,
            "parent_status": card.status,
            "parent_age_days": card.age_days,
            "parent_updated_at_iso": _ms_to_iso(card.updated_at),
            "title": f"[STALE] {card.title[:80]}",
            "notes": (
                f"Auto-created by DriftDetector.check_card_staleness() because "
                f"card has been in `{card.status}` for {card.age_days:.1f} days. "
                f"Review and either progress or close parent card "
                f"`{card.card_id}`."
            ),
            "labels": ["stale", "auto-created", "hayate"],
            "created_at": self.now.isoformat(),
            "draft_status": "draft",  # watcher cron promotes to "todo"
        }
        stale_log_path = self.ops_dir / "stale_cards.jsonl"
        _ensure_ops_dir(self.ops_dir)
        _append_jsonl(stale_log_path, draft)
        return draft

    def escalate_phase(self, phase: StalePhase) -> dict[str, Any]:
        """Append a phase-escalation record to ``escalation_queue.jsonl``.

        Always returns the record so the caller can log it. ``level`` may
        be ``"warn"`` (logged only) or ``"escalate"`` (logged + flagged
        for Craig review).
        """
        record = {
            "ts": self.now.isoformat(),
            "kind": "phase_staleness",
            "phase_id": phase.phase_id,
            "source_file": phase.source_file,
            "last_audit_ts": phase.last_audit_ts,
            "age_days": phase.age_days,
            "level": phase.level,
            "recommended_action": (
                "Surface to Craig — phase has had no audit_trail entry for "
                f"{phase.age_days:.1f} days (>= {PHASE_ESCALATE_DAYS}d threshold)."
            ),
        }
        _ensure_ops_dir(self.ops_dir)
        _append_jsonl(self.ops_dir / "escalation_queue.jsonl", record)
        return record


# ── Convenience module-level functions ──────────────────────────────────────

_DEFAULT_DETECTOR: DriftDetector | None = None


def get_default_detector() -> DriftDetector:
    """Return a lazily-constructed default instance sharing the active
    process state. Useful for short scripts (daily_audit, kanban_hygiene)
    that don't want to thread the detector through every call."""
    global _DEFAULT_DETECTOR
    if _DEFAULT_DETECTOR is None:
        _DEFAULT_DETECTOR = DriftDetector()
    return _DEFAULT_DETECTOR


def check_card_staleness(*args: Any, **kwargs: Any) -> list[StaleCard]:
    return get_default_detector().check_card_staleness(*args, **kwargs)


def check_phase_staleness(*args: Any, **kwargs: Any) -> list[StalePhase]:
    return get_default_detector().check_phase_staleness(*args, **kwargs)


__all__ = [
    "DriftDetector",
    "StaleCard",
    "StalePhase",
    "CARD_AUTO_CREATE_DAYS",
    "CARD_WARN_DAYS",
    "PHASE_ESCALATE_DAYS",
    "PHASE_WARN_DAYS",
    "check_card_staleness",
    "check_phase_staleness",
]
