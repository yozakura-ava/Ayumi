"""§8 — Session definitions, phase scoring, weekly model, and Asia rules."""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional

import pytz

_ET = pytz.timezone("America/New_York")

# Session definitions (UTC times)
SESSIONS = {
    "ASIA": (time(0, 0), time(7, 0)),
    "LONDON": (time(7, 0), time(16, 0)),
    "NY": (time(12, 0), time(21, 0)),
}

# Kill zones (first 90 minutes of each session) — static defaults (EDT)
# NY KZ is DST-aware: see get_ny_kz_hours()
KILL_ZONES = {
    "ASIA": (time(0, 0), time(1, 30)),
    "LONDON": (time(7, 0), time(8, 30)),
    "NY": (time(12, 30), time(14, 0)),
}


def _is_dst(utc_dt: datetime) -> bool:
    """Check if a UTC datetime falls in US Eastern DST.

    Approximation: DST runs from 2nd Sunday March to 1st Sunday November.
    Uses the first bar's ET offset to determine DST vs EST.
    """
    if utc_dt.tzinfo is None:
        utc_dt = pytz.utc.localize(utc_dt)
    et = utc_dt.astimezone(_ET)
    # ET offset of -4h = EDT, -5h = EST
    return et.utcoffset().total_seconds() == -4 * 3600


def get_ny_kz_hours(is_dst: bool) -> tuple[time, time]:
    """Return NY kill zone UTC hours adjusted for DST.

    EDT (UTC-4): NY KZ 8:30-10:00 ET = 12:30-14:00 UTC
    EST (UTC-5): NY KZ 8:30-10:00 ET = 13:30-15:00 UTC
    """
    if is_dst:
        return (time(12, 30), time(14, 0))
    else:
        return (time(13, 30), time(15, 0))


# Overlaps
OVERLAPS = {
    "ASIA_LONDON": (time(7, 0), time(8, 0)),
    "LONDON_NY": (time(12, 0), time(16, 0)),
}

# Weekly confidence modifiers (§8.3)
WEEKLY_MODIFIERS = {
    0: -0.10,  # Monday
    1: 0.05,  # Tuesday
    2: 0.05,  # Wednesday
    3: 0.0,  # Thursday
    4: -0.10,  # Friday
    5: 0.0,  # Saturday
    6: 0.0,  # Sunday
}


class SessionAnalyzer:
    """Session-aware scoring for trade timing and quality."""

    def get_current_session(self, utc_dt: datetime) -> str:
        """Return the active session name(s) for a UTC datetime."""
        t = utc_dt.time()
        active = []

        for name, (start, end) in SESSIONS.items():
            if start <= t < end:
                active.append(name)

        if not active:
            return "OUTSIDE"

        # Check overlaps first (higher priority)
        for name, (start, end) in OVERLAPS.items():
            if start <= t < end:
                return name

        return active[0]

    def _get_kill_zones(self, utc_dt: datetime) -> dict[str, tuple[time, time]]:
        """Return KILL_ZONES with NY adjusted for DST."""
        kz = dict(KILL_ZONES)
        kz["NY"] = get_ny_kz_hours(_is_dst(utc_dt))
        return kz

    def is_kill_zone(self, utc_dt: datetime) -> bool:
        """Check if the given time falls within any session's kill zone."""
        t = utc_dt.time()
        for _, (start, end) in self._get_kill_zones(utc_dt).items():
            if start <= t < end:
                return True
        return False

    def get_kill_zone_name(self, utc_dt: datetime) -> Optional[str]:
        """Return which session's kill zone is active, or None."""
        t = utc_dt.time()
        for name, (start, end) in self._get_kill_zones(utc_dt).items():
            if start <= t < end:
                return name
        return None

    def score_session_phase(
        self,
        session: str,
        htf_phase: str = "neutral",
        price_action: Optional[dict] = None,
    ) -> dict:
        """Score session phase quality per §8.2.

        Args:
            session: session name
            htf_phase: HTF phase string
            price_action: optional dict with keys like 'asia_range_pct',
                'asia_high', 'asia_low', 'asia_direction' for Asia logic

        Returns:
            dict with phase_score, kill_zone_active, directional_bias
        """
        result: dict = {
            "phase_score": 0.5,
            "kill_zone_active": False,
            "directional_bias": None,
        }

        if session == "OUTSIDE":
            result["phase_score"] = 0.0
            return result

        # Phase scoring based on where we are in the session
        # Opening = 1.0, Mid = 0.5, Closing = 0.2
        result["phase_score"] = self._calculate_phase_score(session)

        # Asia control detection (§8.4)
        if price_action and "asia_range_pct" in price_action:
            asia_range = price_action.get("asia_range_pct", 0.1)
            if asia_range < 0.02:
                # Tight range — Asia control = consolidating
                result["asia_control_score"] = 1.0
                result["directional_bias"] = price_action.get("asia_direction")
            elif asia_range > 0.02:
                result["asia_control_score"] = 0.0
                result["directional_bias"] = price_action.get("asia_direction")

        return result

    def _calculate_phase_score(self, session: str) -> float:
        """Simplified phase score — in real usage, pass utc_dt for precise timing."""
        return 0.5  # Mid-session default

    def score_session_phase_with_time(
        self,
        session: str,
        utc_dt: datetime,
        htf_phase: str = "neutral",
        price_action: Optional[dict] = None,
        bar_closed: bool = True,
    ) -> dict:
        """Score with precise time for phase calculation."""
        if not bar_closed:
            return {
                "phase_score": 0.0,
                "kill_zone_active": self.is_kill_zone(utc_dt),
                "directional_bias": None,
                "bar_closed": False,
            }

        result: dict = {
            "phase_score": 0.5,
            "kill_zone_active": self.is_kill_zone(utc_dt),
            "directional_bias": None,
        }

        if session == "OUTSIDE":
            result["phase_score"] = 0.0
            return result

        t = utc_dt.time()
        session_times = SESSIONS.get(session)
        if session_times is None:
            return result

        start, end = session_times
        session_duration = (
            datetime.combine(utc_dt.date(), end) - datetime.combine(utc_dt.date(), start)
        ).total_seconds()

        elapsed = (datetime.combine(utc_dt.date(), t) - datetime.combine(utc_dt.date(), start)).total_seconds()

        if session_duration <= 0:
            return result

        pct_elapsed = elapsed / session_duration

        # Opening: first 90 min (adjusted by session duration)
        opening_pct = min(1.5 * 3600 / session_duration, 0.25)
        closing_pct = 0.20  # last 20%

        if pct_elapsed <= opening_pct:
            result["phase_score"] = 1.0
        elif pct_elapsed >= (1.0 - closing_pct):
            result["phase_score"] = 0.2
        else:
            result["phase_score"] = 0.5

        # Asia directional inference (§8.4)
        if price_action:
            asia_range = price_action.get("asia_range_pct", 0.1)
            if asia_range < 0.02:
                result["asia_control_score"] = 1.0
                result["directional_bias"] = price_action.get("asia_direction")
            else:
                result["asia_control_score"] = 0.0

        return result

    def get_weekly_modifier(self, utc_dt: datetime) -> float:
        """Return the day-of-week confidence modifier per §8.3."""
        # Monday=0, Friday=4
        return WEEKLY_MODIFIERS.get(utc_dt.weekday(), 0.0)

    def check_ny_open_manipulation(
        self,
        bars_before_ny: list[dict],
        ny_open_bar: dict,
    ) -> dict:
        """§8.5 NY open manipulation detection.

        The first 30-60 min of NY often includes stop-hunts and fakeouts.
        Returns dict with manipulation_detected and notes.
        """
        result = {"manipulation_detected": False, "notes": ""}

        if not bars_before_ny:
            return result

        # Check for wick-heavy candles in the pre-NY / early NY window
        wick_count = 0
        for bar in bars_before_ny[-6:]:  # Last ~6 bars before NY open
            body = abs(bar.get("close", 0) - bar.get("open", 0))
            total_range = bar.get("high", 0) - bar.get("low", 0)
            if total_range > 0 and body / total_range < 0.5:
                wick_count += 1

        # Early NY bar wick check
        ny_body = abs(ny_open_bar.get("close", 0) - ny_open_bar.get("open", 0))
        ny_range = ny_open_bar.get("high", 0) - ny_open_bar.get("low", 0)
        ny_wicky = ny_range > 0 and ny_body / ny_range < 0.5

        if wick_count >= 3 or ny_wicky:
            result["manipulation_detected"] = True
            result["notes"] = (
                "Wick-heavy candles detected around NY open. Do not trade the manipulation — wait for it to fail."
            )

        return result
