"""Git-log guard for Hayate L2/L3 escalation decisions."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class CommitInfo:
    """One commit touching a guarded path."""

    commit_hash: str
    author: str
    authored_at: str
    subject: str
    path: str

    @property
    def short_hash(self) -> str:
        return self.commit_hash[:12]


def _default_project_root() -> Path:
    # src/forex_bot/monitoring/git_guard.py -> project root
    return Path(__file__).resolve().parents[3]


def _normalize_path(path: str, repo_root: Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(repo_root.resolve())
        except ValueError:
            return str(candidate)
    return str(candidate).replace(os.sep, "/")


def git_log_recent_changes(
    path: str,
    days: int = 7,
    *,
    repo_root: Path | str | None = None,
) -> list[CommitInfo]:
    """Return commits touching ``path`` within the last ``days`` days.

    Hayate uses this for L2/L3 guards: if a troubled code path changed
    recently, escalate rather than attempting an automatic code-adjacent fix.
    """

    if days < 0:
        raise ValueError("days must be non-negative")

    root = Path(repo_root).resolve() if repo_root is not None else _default_project_root()
    pathspec = _normalize_path(path, root)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    proc = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(root),
            "log",
            f"--since={since}",
            "--format=%H%x1f%an%x1f%aI%x1f%s",
            "--",
            pathspec,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed for {pathspec!r}: {proc.stderr.strip()}")

    commits: list[CommitInfo] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", maxsplit=3)
        if len(parts) != 4:
            continue
        commit_hash, author, authored_at, subject = parts
        commits.append(
            CommitInfo(
                commit_hash=commit_hash,
                author=author,
                authored_at=authored_at,
                subject=subject,
                path=pathspec,
            )
        )
    return commits
