"""
Configurable session time definitions for the trading bot.
All times are UTC.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass(frozen=True)
class SessionDefinition:
    """Defines a trading session with start and end times."""

    start: time
    end: time


# Killzone session times (when major exchanges have high volume)
# Static defaults in UTC — use get_killzone_hours_for_date() for DST-aware values
class KillzoneHours:
    LONDON_OPEN_START = time(7, 0)
    LONDON_OPEN_END = time(9, 0)
    NY_OPEN_START = time(12, 0)
    NY_OPEN_END = time(14, 0)
    OVERLAP_START = time(13, 0)
    OVERLAP_END = time(16, 0)


# ---------------------------------------------------------------------------
# DST-aware killzone hours
# ---------------------------------------------------------------------------

_LONDON_TZ = ZoneInfo("Europe/London")
_NY_TZ = ZoneInfo("America/New_York")


def is_london_dst(d: date) -> bool:
    """Return True if London is in BST (DST) on the given date."""
    dt = datetime(d.year, d.month, d.day, 12, 0, tzinfo=_LONDON_TZ)
    return dt.dst() != timedelta(0)


def is_ny_dst(d: date) -> bool:
    """Return True if New York is in EDT (DST) on the given date."""
    dt = datetime(d.year, d.month, d.day, 12, 0, tzinfo=_NY_TZ)
    return dt.dst() != timedelta(0)


@dataclass(frozen=True)
class DSTAwareKillzoneHours:
    """Killzone hours in UTC, adjusted for DST on a given date.

    London Open killzone is 8:00–10:00 London local.
    In GMT (winter): 8–10 UTC.
    In BST (summer): 7–9 UTC.

    NY Open killzone is 8:00–10:00 NY local.
    In EST (winter): 13–15 UTC.
    In EDT (summer): 12–14 UTC.
    """

    LONDON_OPEN_START: time
    LONDON_OPEN_END: time
    NY_OPEN_START: time
    NY_OPEN_END: time
    OVERLAP_START: time
    OVERLAP_END: time


def get_killzone_hours_for_date(d: date) -> DSTAwareKillzoneHours:
    """Return killzone hours in UTC adjusted for London/NY DST status.

    Handles the spring/fall mismatch periods where one region has
    transitioned but the other has not (typically 1–2 weeks in
    March and November).
    """
    london_offset = 1 if is_london_dst(d) else 0  # London UTC offset
    ny_utc_offset = -4 if is_ny_dst(d) else -5  # NY UTC offset

    london_start = 8 - london_offset
    london_end = 10 - london_offset
    ny_start = 8 - ny_utc_offset  # 8 - (-4) = 12 summer, 8 - (-5) = 13 winter
    ny_end = 10 - ny_utc_offset  # 10 - (-4) = 14 summer, 10 - (-5) = 15 winter

    # Overlap: NY open to ~London afternoon close
    overlap_start = ny_start
    overlap_end = max(ny_end, min(ny_start + 4, 16))

    return DSTAwareKillzoneHours(
        LONDON_OPEN_START=time(london_start, 0),
        LONDON_OPEN_END=time(london_end, 0),
        NY_OPEN_START=time(ny_start, 0),
        NY_OPEN_END=time(ny_end, 0),
        OVERLAP_START=time(overlap_start, 0),
        OVERLAP_END=time(overlap_end, 0),
    )


# Session range mean reversion times
class SessionRangeHours:
    ASIAN_START = time(0, 0)
    ASIAN_END = time(7, 0)
    EARLY_LONDON_END = time(9, 0)
    LONDON_START = time(7, 0)
    LONDON_END = time(11, 0)
    NY_OPEN_START = time(12, 0)
    NY_OPEN_END = time(15, 0)
    LONDON_NY_OVERLAP_START = time(12, 0)
    LONDON_NY_OVERLAP_END = time(16, 0)
    NY_CLOSE_START = time(16, 0)
    NY_CLOSE_END = time(20, 0)


# Default killzone sessions - tuples of (start, end)
DEFAULT_KILLZONES = [
    (KillzoneHours.LONDON_OPEN_START, KillzoneHours.LONDON_OPEN_END),
    (KillzoneHours.NY_OPEN_START, KillzoneHours.NY_OPEN_END),
    (KillzoneHours.OVERLAP_START, KillzoneHours.OVERLAP_END),
]

# Default preferred sessions for strategy filtering
DEFAULT_PREFERRED_SESSIONS = {"london", "ny_am"}


class TradingSession(Enum):
    """Major forex trading sessions used for spread modelling.

    Boundaries are UTC and align with typical institutional hours.
    """

    ASIAN = "asian"
    LONDON = "london"
    NY_OVERLAP = "ny_overlap"
    NY_AFTERNOON = "ny_afternoon"
    OFF_HOURS = "off_hours"


# UTC time windows for each session
_SESSION_WINDOWS = {
    TradingSession.ASIAN: (time(0, 0), time(7, 0)),
    TradingSession.LONDON: (time(7, 0), time(12, 0)),
    TradingSession.NY_OVERLAP: (time(12, 0), time(16, 0)),
    TradingSession.NY_AFTERNOON: (time(16, 0), time(20, 0)),
    TradingSession.OFF_HOURS: (time(20, 0), time(23, 59)),
}


def get_trading_session(timestamp: pd.Timestamp) -> TradingSession:
    """Return the :class:`TradingSession` for *timestamp* (UTC).

    Parameters
    ----------
    timestamp:
        A timezone-aware or naive ``pd.Timestamp``.  If naive it is
        assumed to be UTC.

    Returns
    -------
    TradingSession
    """
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC")
    t = timestamp.time()

    for session, (start, end) in _SESSION_WINDOWS.items():
        if start <= t < end:
            return session

    # Handles the 23:59 → 00:00 boundary
    return TradingSession.ASIAN


# ---------------------------------------------------------------------------
# Session gating for signal pipeline (SRB-AYUMI-011 §4.2.1)
# ---------------------------------------------------------------------------

_ET = ZoneInfo("America/New_York")

# Module-level toggle (can be overridden at runtime)
SESSION_GATING_ENABLED: bool = True


def is_tradable_session(
    pair: str,
    utc_timestamp: datetime,
) -> tuple[bool, str]:
    """Check whether *pair* may be traded at *utc_timestamp*.

    Rules (SRB-AYUMI-011 §4.2.1):
    - **Allow** — London Open (02:00–06:00 ET), NY Overlap (08:00–12:00 ET)
    - **Allow (caution)** — NY Afternoon (12:00–17:00 ET)
    - **Block** — Asian session (18:00–02:00 ET) for non-JPY pairs

    Args:
        pair: Symbol such as ``"GBPUSD"`` or ``"USD/JPY"``.
        utc_timestamp: Timezone-aware UTC datetime.

    Returns:
        ``(allowed, reason)`` — *reason* is empty when allowed.
    """
    if not SESSION_GATING_ENABLED:
        return True, ""

    et_hour = utc_timestamp.astimezone(_ET).hour

    pair_norm = pair.upper().replace("/", "").replace("_", "")
    is_jpy = "JPY" in pair_norm

    # Asian session: 18:00–02:00 ET (wraps midnight)
    in_asian = et_hour >= 18 or et_hour < 2
    if in_asian and not is_jpy:
        return False, (f"Session blocked: Asian session for non-JPY pair {pair} (ET hour {et_hour:02d}:00)")

    return True, ""
