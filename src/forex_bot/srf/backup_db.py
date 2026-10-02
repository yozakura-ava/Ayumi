"""SRF Phase 3 — DuckDB nightly backup.

Simple file-level backup of research.duckdb. Run nightly before the
top-K re-evaluation cron.

Usage:
    python -m srf.backup_db
    python -m srf.backup_db --keep 7
"""

from __future__ import annotations

import argparse
import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DB_PATH = PROJECT_ROOT / "data" / "research" / "research.duckdb"
BACKUP_DIR = PROJECT_ROOT / "data" / "research" / "backups"
DEFAULT_KEEP = 30  # days


def backup_db(keep_days: int = DEFAULT_KEEP) -> dict:
    """Copy research.duckdb to backups/ with timestamp.

    Cleans up backups older than keep_days.
    """
    if not DB_PATH.exists():
        logger.warning("research.duckdb not found — skipping backup")
        return {"status": "no_db", "backed_up": False}

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"research_{timestamp}.duckdb"

    shutil.copy2(DB_PATH, backup_path)
    logger.info("Backup created: %s (%.1f MB)", backup_path, backup_path.stat().st_size / 1e6)

    # Cleanup old backups
    cutoff = datetime.now().timestamp() - (keep_days * 86400)
    cleaned = 0
    for f in BACKUP_DIR.glob("research_*.duckdb"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            cleaned += 1
            logger.info("Removed old backup: %s", f.name)

    return {
        "status": "ok",
        "backed_up": True,
        "backup_path": str(backup_path),
        "size_mb": round(backup_path.stat().st_size / 1e6, 1),
        "old_backups_removed": cleaned,
    }


def main():
    parser = argparse.ArgumentParser(description="SRF DuckDB Backup")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="Days to keep backups")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = backup_db(args.keep)
    print(f"Backup: {result}")


if __name__ == "__main__":
    main()
