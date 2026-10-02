from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..engine import Bar, TradeDirection


@dataclass
class SessionKillZone:
    name: str
    start_hour_utc: int
    end_hour_utc: int
    is_kill_zone: bool = True


DEFAULT_KILL_ZONES: list[SessionKillZone] = [
    SessionKillZone("london_open", 7, 9, True),
    SessionKillZone("london", 8, 12, False),
    SessionKillZone("ny_open", 12, 14, True),
    SessionKillZone("ny_am", 12, 16, False),
    SessionKillZone("ny_pm", 16, 20, False),
    SessionKillZone("asian", 0, 6, False),
]


@dataclass
class NewsEvent:
    time: datetime
    currency: str
    impact: str
    description: str = ""


@dataclass
class SessionFilterResult:
    allow_entry: bool = True
    force_close: bool = False
    reason: str = ""


class NewsEventSimulator:
    def __init__(self, events: list[NewsEvent] | None = None):
        self.events: dict[datetime, list[NewsEvent]] = {}
        if events:
            for event in events:
                dt = event.time
                if dt not in self.events:
                    self.events[dt] = []
                self.events[dt].append(event)

    def add_event(self, event: NewsEvent):
        dt = event.time
        if dt not in self.events:
            self.events[dt] = []
        self.events[dt].append(event)

    def has_high_impact_near(self, bar_time: datetime, buffer_hours: float = 1.0) -> bool:
        window_start = bar_time - timedelta(hours=buffer_hours)
        window_end = bar_time + timedelta(hours=buffer_hours)
        for event_time, events in self.events.items():
            if window_start <= event_time <= window_end:
                for event in events:
                    if event.impact in ("high", "high_impact"):
                        return True
        return False

    def get_impact_near(self, bar_time: datetime, buffer_hours: float = 1.0) -> list[NewsEvent]:
        window_start = bar_time - timedelta(hours=buffer_hours)
        window_end = bar_time + timedelta(hours=buffer_hours)
        result = []
        for event_time, events in self.events.items():
            if window_start <= event_time <= window_end:
                result.extend(events)
        return result


class SessionFilter:
    def __init__(
        self,
        enabled: bool = True,
        allow_entry_sessions: list[str] | None = None,
        hold_through_sessions: bool = True,
        weekend_close_hour_utc: int = 21,
        weekend_close_minute_utc: int = 55,
        news_buffer_bars: int = 2,
        news_buffer_on_entry: bool = True,
        news_simulator: NewsEventSimulator | None = None,
        kill_zones: list[SessionKillZone] | None = None,
    ):
        self.enabled = enabled
        self.allow_entry_sessions = set(
            allow_entry_sessions or ["london", "london_open", "ny_open", "ny_am", "ny_pm", "asian"]
        )
        self.hold_through_sessions = hold_through_sessions
        self.weekend_close_hour = weekend_close_hour_utc
        self.weekend_close_minute = weekend_close_minute_utc
        self.news_buffer_bars = news_buffer_bars
        self.news_buffer_on_entry = news_buffer_on_entry
        self.news_simulator = news_simulator
        self.kill_zones = kill_zones or DEFAULT_KILL_ZONES
        self._news_buffered_bars: set[datetime] = set()

    def check_entry(self, bar: Bar, pair: str = "EURUSD") -> SessionFilterResult:
        if not self.enabled:
            return SessionFilterResult(allow_entry=True)

        if self._is_weekend_close(bar.time):
            return SessionFilterResult(
                allow_entry=False,
                reason="Weekend close approaching, no new entries",
            )

        if self.news_buffer_on_entry and self.news_simulator:
            if self.news_simulator.has_high_impact_near(bar.time):
                return SessionFilterResult(
                    allow_entry=False,
                    reason="High-impact news event within buffer window",
                )

        session = self._get_session_name(bar.time)
        session_ok = any(
            session == allowed or (allowed == "london" and session == "london_open")
            for allowed in self.allow_entry_sessions
        )

        if not session_ok:
            return SessionFilterResult(
                allow_entry=False,
                reason=f"Session '{session}' not in allowed entry sessions",
            )

        return SessionFilterResult(allow_entry=True)

    def check_hold(self, bar: Bar, entry_time: datetime, direction: TradeDirection) -> SessionFilterResult:
        if not self.enabled:
            return SessionFilterResult(force_close=False)

        if self._is_weekend_close(bar.time):
            return SessionFilterResult(
                force_close=True,
                reason="Weekend close, force closing all positions",
            )

        if self.hold_through_sessions:
            return SessionFilterResult(force_close=False)

        current_session = self._get_session_name(bar.time)
        entry_session = self._get_session_name(entry_time)

        if current_session not in self.allow_entry_sessions and entry_session != current_session:
            return SessionFilterResult(
                force_close=True,
                reason=f"Current session '{current_session}' outside allowed hold sessions",
            )

        return SessionFilterResult(force_close=False)

    @staticmethod
    def _ensure_utc(time: datetime) -> datetime:
        if time.tzinfo is not None:
            return time.astimezone(timezone.utc)
        return time

    def is_kill_zone(self, bar_time: datetime) -> bool:
        bar_time = self._ensure_utc(bar_time)
        for kz in self.kill_zones:
            if kz.is_kill_zone and kz.start_hour_utc <= bar_time.hour < kz.end_hour_utc:
                return True
        return False

    def _get_session_name(self, time: datetime) -> str:
        time = self._ensure_utc(time)
        for kz in self.kill_zones:
            if kz.start_hour_utc <= time.hour < kz.end_hour_utc:
                return kz.name
        return "outside"

    def _is_weekend_close(self, time: datetime) -> bool:
        time = self._ensure_utc(time)
        if time.weekday() == 4:
            if time.hour > self.weekend_close_hour:
                return True
            if time.hour == self.weekend_close_hour and time.minute >= self.weekend_close_minute:
                return True
        if time.weekday() >= 5:
            return True
        return False
