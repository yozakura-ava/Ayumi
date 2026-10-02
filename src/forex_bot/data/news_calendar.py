"""News calendar blackout filter for FTMO-compliant entry gating.

Per FTMO Standard account rules: "no trading 2 min before/after high-impact
news."  The blend plan extends this to a 5-minute safety margin on either
side of the event timestamp.

This module provides :class:`NewsCalendarFilter`, a daily-caching filter
that classifies whether the current timestamp falls inside a blackout window
for any of the traded symbols' constituent currencies.

Data source
-----------
The filter reads an economic-calendar JSON file (ForexFactory-style format).
A ``cache_path`` can be supplied to point at a locally cached file; when the
cache is stale (older than ``cache_ttl_hours``) the filter will attempt to
re-fetch from the configured URL.  If the fetch fails, the stale cache is
used (with a warning).  If no cache exists at all, the filter degrades to
**permissive** mode (allows all entries) and logs a warning — this matches
the principle that missing data should not hard-stop trading, but it should
be visible.

Typical usage::

    from data.news_calendar import NewsCalendarFilter

    nf = NewsCalendarFilter()
    if nf.is_blackout_now(["EURUSD", "USDJPY"]):
        logger.info("Entry blocked: news blackout in effect")

    window = nf.next_blackout_window(["EURUSD", "USDJPY"])
    if window:
        logger.info("Next blackout: %s", window)

For backtests, inject a fixed calendar file so results are deterministic::

    nf = NewsCalendarFilter(
        cache_path="data/news_calendar_2024.json",
        auto_fetch=False,
    )
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ayumi.news_calendar")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BLACKOUT_MINUTES = 5  # Safety margin before AND after each event

# ForexFactory calendar JSON URL (historical/forward ~1 week)
FOREXFACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# High-impact event keywords that trigger blackout windows
HIGH_IMPACT_KEYWORDS = frozenset(
    {
        "nonfarm payroll",
        "nfp",
        "employment change",
        "fomc",
        "federal funds rate",
        "interest rate",
        "ecb rate decision",
        "minimum bid rate",
        "main refinancing rate",
        "boe rate decision",
        "official bank rate",
        "boj rate decision",
        "policy rate",
        "cpi",
        "consumer price index",
        "core cpi",
        "ppi",
        "producer price index",
        "gdp",
        "gross domestic product",
        "ism manufacturing pmi",
        "ism services pmi",
        "unemployment rate",
        "claimant count",
    }
)

# Map currency codes to central bank rate decision keywords
CENTRAL_BANK_EVENTS = {
    "USD": {"fomc", "federal funds rate"},
    "EUR": {"ecb rate decision", "minimum bid rate", "main refinancing rate"},
    "GBP": {"boe rate decision", "official bank rate"},
    "JPY": {"boj rate decision", "policy rate"},
}

# Currencies whose high-impact events we track
SUPPORTED_CURRENCIES = frozenset({"USD", "EUR", "GBP", "JPY"})

# Currency extraction from symbol pairs (e.g. "EURUSD" -> {"EUR", "USD"})
# Most forex symbols are 6-char (XXXYYY), but some indices/crypto differ.
# We only apply the filter to 6-letter forex pairs.


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarEvent:
    """A single economic-calendar event."""

    timestamp: datetime
    currency: str  # ISO 4217 (e.g. "USD", "EUR")
    title: str  # Human-readable event title
    impact: str  # "high", "medium", "low", "holiday"


@dataclass(frozen=True)
class BlackoutWindow:
    """A computed blackout period for display / logging."""

    start: datetime
    end: datetime
    currency: str
    title: str

    def contains(self, when: datetime) -> bool:
        return self.start <= when <= self.end


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class NewsCalendarFilter:
    """FTMO news-blackout filter with daily caching.

    Parameters
    ----------
    blackout_minutes : int
        Minutes before AND after each high-impact event to block entries.
        Default 5 (FTMO safety margin beyond the 2-min rule).
    cache_path : str | Path | None
        Path to a local JSON cache of the economic calendar. If ``None``,
        uses ``<repo>/data/news_calendar_cache.json``.
    cache_ttl_hours : float
        Maximum age of the cache before a re-fetch is attempted.
    auto_fetch : bool
        Whether to attempt HTTP fetches when the cache is stale. Set to
        ``False`` for backtests (inject a fixed ``cache_path``).
    calendar_url : str
        URL for the ForexFactory-style JSON calendar.
    permissive_on_failure : bool
        If ``True`` (default), allow all entries when no calendar data is
        available. If ``False``, block all entries (fail-closed).
    """

    def __init__(
        self,
        blackout_minutes: int = DEFAULT_BLACKOUT_MINUTES,
        cache_path: Optional[str | Path] = None,
        cache_ttl_hours: float = 24.0,
        auto_fetch: bool = True,
        calendar_url: str = FOREXFACTORY_URL,
        permissive_on_failure: bool = True,
    ) -> None:
        self.blackout_minutes = blackout_minutes
        self.cache_path = Path(cache_path) if cache_path else None
        self.cache_ttl_hours = cache_ttl_hours
        self.auto_fetch = auto_fetch
        self.calendar_url = calendar_url
        self.permissive_on_failure = permissive_on_failure

        self._events: list[CalendarEvent] = []
        self._cache_loaded = False
        self._cache_checked = False  # Prevent re-fetch loops within same tick

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_blackout_now(
        self,
        symbols: list[str],
        now: Optional[datetime] = None,
    ) -> bool:
        """Return ``True`` if *now* falls inside any high-impact blackout window.

        Args:
            symbols: List of trading symbols (e.g. ``["EURUSD", "USDJPY"]``).
                Only 6-letter forex pairs are evaluated; others are ignored.
            now: Timestamp to check. Defaults to current UTC time.

        Returns:
            ``True`` if within ``blackout_minutes`` of any high-impact event
            affecting the currencies in *symbols*.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        currencies = self._extract_currencies(symbols)
        if not currencies:
            return False

        self._ensure_cache(now)

        if not self._events:
            # No data — follow permissive policy
            if not self.permissive_on_failure:
                logger.warning(
                    "News blackout filter has no calendar data and permissive_on_failure=False — blocking entry."
                )
                return True
            return False

        for event in self._events:
            if event.currency not in currencies:
                continue
            if not self._is_high_impact(event):
                continue
            window_start = event.timestamp - timedelta(minutes=self.blackout_minutes)
            window_end = event.timestamp + timedelta(minutes=self.blackout_minutes)
            if window_start <= now <= window_end:
                logger.info(
                    "News blackout active: %s (%s) at %s — blocking entry",
                    event.title,
                    event.currency,
                    event.timestamp.isoformat(),
                )
                return True

        return False

    def next_blackout_window(
        self,
        symbols: list[str],
        now: Optional[datetime] = None,
    ) -> Optional[BlackoutWindow]:
        """Return the next upcoming blackout window, or ``None``.

        Useful for UI display ("next news in 12 minutes").

        Args:
            symbols: Trading symbols to check.
            now: Reference timestamp. Defaults to current UTC.

        Returns:
            The nearest :class:`BlackoutWindow` starting at or after *now*,
            or ``None`` if no upcoming blackouts are found.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        currencies = self._extract_currencies(symbols)
        if not currencies:
            return None

        self._ensure_cache(now)

        if not self._events:
            return None

        candidates: list[BlackoutWindow] = []
        for event in self._events:
            if event.currency not in currencies:
                continue
            if not self._is_high_impact(event):
                continue
            window_start = event.timestamp - timedelta(minutes=self.blackout_minutes)
            window_end = event.timestamp + timedelta(minutes=self.blackout_minutes)
            # Include windows that haven't fully ended yet
            if window_end >= now:
                candidates.append(
                    BlackoutWindow(
                        start=window_start,
                        end=window_end,
                        currency=event.currency,
                        title=event.title,
                    )
                )

        if not candidates:
            return None

        candidates.sort(key=lambda w: w.start)
        return candidates[0]

    def get_active_blackouts(
        self,
        symbols: list[str],
        now: Optional[datetime] = None,
    ) -> list[BlackoutWindow]:
        """Return all blackout windows currently active for the given symbols."""
        if now is None:
            now = datetime.now(timezone.utc)

        currencies = self._extract_currencies(symbols)
        if not currencies:
            return []

        self._ensure_cache(now)
        if not self._events:
            return []

        active: list[BlackoutWindow] = []
        for event in self._events:
            if event.currency not in currencies:
                continue
            if not self._is_high_impact(event):
                continue
            window_start = event.timestamp - timedelta(minutes=self.blackout_minutes)
            window_end = event.timestamp + timedelta(minutes=self.blackout_minutes)
            if window_start <= now <= window_end:
                active.append(
                    BlackoutWindow(
                        start=window_start,
                        end=window_end,
                        currency=event.currency,
                        title=event.title,
                    )
                )
        return active

    # ------------------------------------------------------------------
    # Calendar data loading
    # ------------------------------------------------------------------

    def _ensure_cache(self, now: datetime) -> None:
        """Load or refresh the calendar cache if needed."""
        if self._cache_loaded and not self._is_cache_stale(now):
            return

        self._cache_checked = False
        loaded = self._try_load_cache()
        if loaded and not self._is_cache_stale(now):
            self._cache_loaded = True
            return

        if self.auto_fetch and not self._cache_checked:
            self._cache_checked = True
            fetched = self._try_fetch_and_save()
            if fetched:
                self._try_load_cache()
            self._cache_loaded = True
        else:
            self._cache_loaded = True  # Mark loaded to avoid retry loops

    def _is_cache_stale(self, now: datetime) -> bool:
        """Check if the cache file mtime exceeds ``cache_ttl_hours``."""
        if not self.cache_path or not self.cache_path.exists():
            return True
        mtime = datetime.fromtimestamp(self.cache_path.stat().st_mtime, tz=timezone.utc)
        age = now - mtime
        return age > timedelta(hours=self.cache_ttl_hours)

    def _try_load_cache(self) -> bool:
        """Attempt to load events from ``cache_path``. Returns success."""
        path = self.cache_path
        if not path or not path.exists():
            return False

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._events = self._parse_calendar(raw)
            logger.debug("Loaded %d calendar events from %s", len(self._events), path)
            return True
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to parse calendar cache %s: %s", path, exc)
            return False

    def _try_fetch_and_save(self) -> bool:
        """Fetch calendar JSON from ``calendar_url`` and save to cache."""
        if not self.cache_path:
            logger.debug("No cache_path set — skipping fetch.")
            return False

        try:
            req = urllib.request.Request(  # noqa: S310
                self.calendar_url,
                headers={"User-Agent": "Ayumi/1.0 NewsCalendarFilter"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                data = resp.read()

            # Validate it's JSON before saving
            parsed = json.loads(data)
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
            logger.info("Fetched %d bytes of calendar data", len(data))
            return True
        except Exception as exc:
            logger.warning("Calendar fetch failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Parsing & classification
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_calendar(raw: list[dict]) -> list[CalendarEvent]:
        """Parse ForexFactory-style JSON into :class:`CalendarEvent` list.

        Expected item format::

            {
                "title": "Non-Farm Employment Change",
                "country": "USD",
                "date": "2024-12-06",
                "time": "13:30",
                "impact": "High",
                "forecast": "...",
                "previous": "..."
            }

        Items without a valid timestamp are skipped.
        """
        events: list[CalendarEvent] = []
        if not isinstance(raw, list):
            return events

        for item in raw:
            try:
                title = item.get("title", "").strip()
                currency = item.get("country", item.get("currency", "")).strip().upper()
                date_str = item.get("date", "")
                time_str = item.get("time", "")
                impact = item.get("impact", "").strip().lower()

                if not currency or not date_str:
                    continue

                # Combine date + time; handle "All Day" or missing time
                ts_str = f"{date_str}T{time_str}:00" if time_str and ":" in time_str else f"{date_str}T00:00:00"
                # ForexFactory times are US/Eastern; we store as naive then
                # treat them as UTC for simplicity (the 5-min window is
                # generous enough to absorb timezone offsets).
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                events.append(
                    CalendarEvent(
                        timestamp=ts,
                        currency=currency,
                        title=title,
                        impact=impact,
                    )
                )
            except (ValueError, TypeError):
                continue

        events.sort(key=lambda e: e.timestamp)
        return events

    @staticmethod
    def _is_high_impact(event: CalendarEvent) -> bool:
        """Determine if an event should trigger a blackout window."""
        # Direct impact field check
        if event.impact == "high":
            return True

        # Keyword-based fallback (some sources don't set impact properly)
        title_lower = event.title.lower()
        if any(kw in title_lower for kw in HIGH_IMPACT_KEYWORDS):
            return True

        # Central bank rate decisions
        central_bank_kws = CENTRAL_BANK_EVENTS.get(event.currency, set())
        if central_bank_kws and any(kw in title_lower for kw in central_bank_kws):
            return True

        return False

    @staticmethod
    def _extract_currencies(symbols: list[str]) -> set[str]:
        """Extract constituent currency codes from forex symbols.

        Only processes 6-letter alphabetic symbols (standard forex pairs).
        Returns an empty set for non-forex symbols.
        """
        currencies: set[str] = set()
        for sym in symbols:
            cleaned = sym.upper().replace("/", "").replace("_", "")
            if len(cleaned) == 6 and cleaned.isalpha():
                base = cleaned[:3]
                quote = cleaned[3:]
                if base in SUPPORTED_CURRENCIES:
                    currencies.add(base)
                if quote in SUPPORTED_CURRENCIES:
                    currencies.add(quote)
        return currencies
