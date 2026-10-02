"""Market hours utility for forex trading.

Forex market is closed from Friday 22:00 UTC through Sunday 21:00 UTC.
During market close, the cTrader feed stops sending ticks, which would
otherwise trigger the heartbeat-based kill switch.

Use this to gate kill switch triggers and feed health checks.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Single source of truth for forex market-close boundaries.
# Closed window: Friday 22:00 UTC (inclusive) through Sunday 21:00 UTC (exclusive).
# The previous implementation incorrectly kept the market closed through Monday
# 20:59 UTC, producing 24 phantom "closed" hours per weekend (Sun 21:00 UTC –
# Mon 20:59 UTC). It also closed 5 minutes early on Friday (21:55 instead of
# 22:00). Card: ee53643d (fix-weekend-market-closed).
_WEEKEND_CLOSE_HOUR_UTC = 22  # Friday on/after this hour → closed
_WEEKEND_OPEN_HOUR_UTC = 21  # Sunday before this hour → closed


def is_forex_market_closed(now: datetime | None = None) -> bool:
    """Return True if the forex market is currently closed.

    Forex hours: closes Friday 22:00 UTC, opens Sunday 21:00 UTC.

    Parameters
    ----------
    now:
        Optional UTC datetime to evaluate. Defaults to ``datetime.now(timezone.utc)``.
        Exposed for unit-test injection of boundary timestamps; production code
        should call without arguments.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    weekday = now.weekday()  # Mon=0, Tue=1, ..., Fri=4, Sat=5, Sun=6
    hour = now.hour

    # Friday at or after 22:00 UTC → market closed for the weekend.
    if weekday == 4 and hour >= _WEEKEND_CLOSE_HOUR_UTC:
        return True
    # Saturday → market closed all day.
    if weekday == 5:
        return True
    # Sunday before 21:00 UTC → still closed (reopens exactly at 21:00).
    if weekday == 6 and hour < _WEEKEND_OPEN_HOUR_UTC:
        return True
    # All other times (Sun ≥21, Mon–Thu, Fri <22) → market open.
    return False
