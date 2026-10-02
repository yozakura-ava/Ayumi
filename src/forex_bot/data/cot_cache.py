"""Weekly cache for CFTC COT data with versioning.

COT reports are published weekly (Friday for Tuesday close). The cache
stores parsed reports as JSON with version tags, enabling:
  - Fast lookups without re-downloading
  - Staleness detection (weekly cadence)
  - Versioned format changes (Legacy vs Disaggregated schema)

Cache layout:
  <cache_dir>/cot_<format>_<year>.json
  <cache_dir>/cot_meta.json
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from data.cot_fetcher import COTFormat, COTPositioning

logger = logging.getLogger(__name__)

# COT reports are published weekly. Allow re-fetch after 4 days to
# cover the Friday release + weekend gap.
STALE_THRESHOLD_DAYS = 4

CACHE_VERSION = 2  # bump when schema changes


@dataclass
class CacheEntry:
    """Versioned cache entry for a year of COT data."""

    version: int
    format: str
    year: str
    fetched_at: str  # ISO timestamp
    record_count: int
    records: list[dict] = field(default_factory=list)


class COTCache:
    """Disk-based weekly cache for parsed COT reports.

    Usage:
        cache = COTCache("/path/to/cache")
        cached = cache.get(COTFormat.LEGACY, "2026")
        if cached is None:
            records = fetcher.fetch_legacy("2026")
            cache.put(COTFormat.LEGACY, "2026", records)
    """

    def __init__(self, cache_dir: str = "data/cot_cache"):
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = self._dir / "cot_meta.json"
        self._meta = self._load_meta()

    # ── Public API ────────────────────────────────────────────────────────────

    def get(
        self,
        fmt: COTFormat,
        year: str,
    ) -> Optional[list[COTPositioning]]:
        """Retrieve cached records for a given format and year.

        Returns None if not cached or stale. Returns list of
        COTPositioning if cache is valid.
        """
        path = self._entry_path(fmt, year)
        if not path.exists():
            return None

        try:
            entry = self._read_entry(path)
        except Exception as exc:
            logger.warning("Cache read failed for %s %s: %s", fmt.value, year, exc)
            return None

        if entry.version != CACHE_VERSION:
            logger.info(
                "Cache version mismatch (%d ≠ %d), invalidating",
                entry.version,
                CACHE_VERSION,
            )
            path.unlink(missing_ok=True)
            return None

        if self._is_stale(entry.fetched_at, year):
            logger.debug("Cache stale for %s %s", fmt.value, year)
            return None

        return [COTPositioning(**r) for r in entry.records]

    def put(
        self,
        fmt: COTFormat,
        year: str,
        records: list[COTPositioning],
    ) -> None:
        """Store parsed records in cache with version tag."""
        entry = CacheEntry(
            version=CACHE_VERSION,
            format=fmt.value,
            year=year,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            record_count=len(records),
            records=[self._serialise(r) for r in records],
        )

        path = self._entry_path(fmt, year)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(entry.__dict__, f, indent=2)

        self._update_meta(fmt, year, entry.fetched_at, entry.record_count)
        logger.info("Cached %d COT %s records for %s", len(records), fmt.value, year)

    def is_stale(self, fmt: COTFormat, year: str) -> bool:
        """Check if cached data needs refreshing."""
        path = self._entry_path(fmt, year)
        if not path.exists():
            return True
        try:
            entry = self._read_entry(path)
        except Exception:
            return True
        return self._is_stale(entry.fetched_at, year)

    def cleanup(self, max_age_days: int = 365) -> int:
        """Remove cache files older than max_age_days.

        Returns number of files removed.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        removed = 0
        for path in self._dir.glob("cot_*.json"):
            if path.name == "cot_meta.json":
                continue
            try:
                entry = self._read_entry(path)
                fetched = datetime.fromisoformat(entry.fetched_at)
                if fetched < cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                # Can't parse — remove corrupt file
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def get_status(self) -> dict:
        """Return cache summary for monitoring."""
        return {
            "version": CACHE_VERSION,
            "entries": self._meta.get("entries", {}),
            "cache_dir": str(self._dir),
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _entry_path(self, fmt: COTFormat, year: str) -> Path:
        return self._dir / f"cot_{fmt.value}_{year}.json"

    @staticmethod
    def _read_entry(path: Path) -> CacheEntry:
        with open(path) as f:
            data = json.load(f)
        return CacheEntry(**data)

    @staticmethod
    def _serialise(rec: COTPositioning) -> dict:
        """Serialise COTPositioning to dict for JSON storage.

        Excludes derived fields (net_position, total_open_interest)
        since __post_init__ recalculates them.
        """
        return {
            "market_name": rec.market_name,
            "report_date": rec.report_date,
            "format": rec.format if isinstance(rec.format, str) else rec.format.value,
            "non_comm_long": rec.non_comm_long,
            "non_comm_short": rec.non_comm_short,
            "non_comm_spread": rec.non_comm_spread,
            "comm_long": rec.comm_long,
            "comm_short": rec.comm_short,
        }

    @staticmethod
    def _is_stale(fetched_at: str, year: str) -> bool:
        """Check if the cached data is stale.

        Current year data is stale after STALE_THRESHOLD_DAYS (weekly cadence).
        Prior year data never goes stale (historical, finalised).
        """
        current_year = str(datetime.now(timezone.utc).year)
        if year != current_year:
            return False  # Historical data doesn't change

        try:
            fetched = datetime.fromisoformat(fetched_at)
        except ValueError:
            return True

        age = datetime.now(timezone.utc) - fetched
        return age.days >= STALE_THRESHOLD_DAYS

    # ── Metadata ──────────────────────────────────────────────────────────────

    def _load_meta(self) -> dict:
        if self._meta_path.exists():
            try:
                with open(self._meta_path) as f:
                    return json.load(f)
            except Exception:  # noqa: S110
                pass
        return {"entries": {}}

    def _update_meta(
        self,
        fmt: COTFormat,
        year: str,
        fetched_at: str,
        count: int,
    ) -> None:
        key = f"{fmt.value}_{year}"
        self._meta["entries"][key] = {
            "fetched_at": fetched_at,
            "record_count": count,
            "version": CACHE_VERSION,
        }
        with open(self._meta_path, "w") as f:
            json.dump(self._meta, f, indent=2)
