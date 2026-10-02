"""Cycle detection for repeated Hayate remediations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


class CycleDetector:
    """Read the append-only remediation log and escalate recurring patterns."""

    def __init__(
        self,
        remediation_log_path: Path | str | None = None,
        escalation_queue_path: Path | str | None = None,
        *,
        project_root: Path | str | None = None,
    ) -> None:
        root = Path(project_root).resolve() if project_root is not None else self._default_project_root()
        self.project_root = root
        self.remediation_log_path = (
            Path(remediation_log_path) if remediation_log_path is not None else root / "data/ops/remediation_log.jsonl"
        )
        self.escalation_queue_path = (
            Path(escalation_queue_path)
            if escalation_queue_path is not None
            else root / "data/ops/escalation_queue.jsonl"
        )

    @staticmethod
    def _default_project_root() -> Path:
        # src/forex_bot/monitoring/cycle_detector.py -> project root
        return Path(__file__).resolve().parents[3]

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")

    def append_remediation(self, pattern_name: str, result: dict[str, Any] | Any) -> None:
        """Append one remediation result to the wrapped log.

        ``result`` may be a plain dict or a dataclass-like object with
        ``__dict__``; this keeps the detector decoupled from the remediation
        action module while still sharing the same append-only JSONL stream.
        """

        payload = dict(result if isinstance(result, dict) else vars(result))
        payload["pattern_name"] = pattern_name
        self._append_jsonl(self.remediation_log_path, payload)

    def _iter_log_entries(self) -> Iterable[dict[str, Any]]:
        if not self.remediation_log_path.exists():
            return []

        entries: list[dict[str, Any]] = []
        with self.remediation_log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    entries.append(payload)
        return entries

    def _write_escalation(
        self,
        pattern_name: str,
        count: int,
        days: int,
        threshold: int,
    ) -> None:
        self._append_jsonl(
            self.escalation_queue_path,
            {
                "timestamp": self._utc_now().isoformat(),
                "pattern_name": pattern_name,
                "classification": "code-fix candidate",
                "status": "queued",
                "priority": "high",
                "reason": (
                    f"{pattern_name} remediated {count} times in the last {days} days; threshold is >{threshold}"
                ),
                "source": str(self.remediation_log_path),
            },
        )

    def check_recent_cycles(
        self,
        pattern_name: str,
        days: int = 7,
        threshold: int = 3,
    ) -> bool:
        """Return True when the same pattern was remediated > threshold times.

        A true result also appends a code-fix-candidate item to
        ``data/ops/escalation_queue.jsonl``.
        """

        if days < 0:
            raise ValueError("days must be non-negative")
        if threshold < 0:
            raise ValueError("threshold must be non-negative")

        cutoff = self._utc_now() - timedelta(days=days)
        count = 0
        for entry in self._iter_log_entries():
            if entry.get("pattern_name", entry.get("pattern")) != pattern_name:
                continue
            if entry.get("applied") is not True:
                continue
            timestamp = entry.get("timestamp")
            if not timestamp:
                continue
            try:
                seen_at = self._parse_timestamp(str(timestamp))
            except ValueError:
                continue
            if seen_at >= cutoff:
                count += 1

        if count > threshold:
            self._write_escalation(pattern_name, count, days, threshold)
            return True
        return False
