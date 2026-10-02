"""ORB (Opening Range Breakout) Filter & Signal Prioritization.

Computes opening ranges per session (Asian, London, NY) and scores
signals by how strongly they align with the ORB structure:

    1. Breakout direction alignment (long above range high, short below range low)
    2. Distance from the opening range edge (further = stronger breakout)
    3. Volume confirmation vs session average volume

Returns a prioritized list of signals sorted by ORB score descending.

This module is additive — it does not modify existing signal engine core files.
It integrates via the existing ``Signal`` dataclass and ``SESSIONS`` config
from ``session_logic``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

from .data_types import Signal
from .session_logic import SESSIONS

logger = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────────────

# Opening range duration in minutes for each session.
DEFAULT_ORB_WINDOW_MINUTES: dict[str, int] = {
    "ASIA": 60,
    "LONDON": 60,
    "NY": 30,
}

# How far price must be from the ORB edge to count as a "clean" breakout,
# expressed as a fraction of the opening range width.
BREAKOUT_MIN_FRACTION = 0.10  # 10 % of OR width

# Volume ratio thresholds
VOLUME_STRONG = 1.5  # ≥ 1.5× session average
VOLUME_NORMAL = 1.0  # ≥ 1.0× session average

# Scoring weights (sum to 1.0)
W_DIRECTION = 0.45
W_DISTANCE = 0.30
W_VOLUME = 0.25


# ── Data Structures ─────────────────────────────────────────────────────────


@dataclass
class OpeningRange:
    """Represents the established opening range for a session."""

    session: str
    high: float
    low: float
    open_time: datetime
    close_time: datetime
    avg_volume: float = 0.0

    @property
    def width(self) -> float:
        """Absolute width of the opening range."""
        return self.high - self.low

    @property
    def mid(self) -> float:
        """Midpoint of the opening range."""
        return (self.high + self.low) / 2.0

    @property
    def is_valid(self) -> bool:
        """A range is valid if high > low and width > 0."""
        return self.high > self.low and self.width > 0.0


@dataclass
class ORBScore:
    """ORB alignment score for a single signal."""

    signal: Signal
    score: float  # 0.0 – 1.0
    direction_aligned: bool
    distance_score: float  # 0.0 – 1.0
    volume_score: float  # 0.0 – 1.0
    breakout_type: str = ""  # "breakout", "pseudo", "failure", "inside"
    detail: str = ""


# ── ORB Filter ──────────────────────────────────────────────────────────────


class ORBFilter:
    """Opening Range Breakout filter and signal prioritizer.

    Implements the IFilter interface for use in FilterChain.

    Usage::

        orbf = ORBFilter()
        rng = orbf.calculate_opening_range(session="LONDON", bars=session_bars)
        scored = orbf.prioritize(signals, opening_range=rng, current_volume=1.2)

    As a FilterChain stage::

        chain = FilterChain()
        chain.add(ORBFilter(min_score_threshold=0.3))
        chain.evaluate(signal_direction="LONG", entry_price=1.2500,
                       opening_range=rng, current_volume=1.2)
    """

    priority: int = 40  # runs after trend(10), atr(20), fvg(30)

    def __init__(
        self,
        orb_windows: Optional[dict[str, int]] = None,
        breakout_min_fraction: float = BREAKOUT_MIN_FRACTION,
        min_score_threshold: float = 0.3,
    ) -> None:
        self.orb_windows = orb_windows or dict(DEFAULT_ORB_WINDOW_MINUTES)
        self.breakout_min_fraction = breakout_min_fraction
        self.min_score_threshold = min_score_threshold

    @property
    def name(self) -> str:
        return "orb"

    def evaluate(
        self,
        signal_direction: str = "",
        entry_price: float = 0.0,
        opening_range: Optional[OpeningRange] = None,
        current_volume: float = 0.0,
    ) -> bool:
        """IFilter-compatible evaluate for FilterChain integration.

        Returns True if the ORB score meets or exceeds the threshold,
        or if no opening range is available (fail-open for live trading
        before session range is established).

        Args:
            signal_direction: "LONG" or "SHORT".
            entry_price: Entry price of the signal.
            opening_range: Pre-computed OpeningRange for the active session.
            current_volume: Volume of the current/bar (0 = unknown).
        """
        if opening_range is None or not opening_range.is_valid:
            # No range established yet — allow signal through
            return True

        # Build a lightweight Signal for scoring
        from .data_types import Signal as _Signal

        sig = _Signal(
            symbol="",
            direction=signal_direction.upper() or "LONG",
            entry_price=entry_price,
            stop_loss=0.0,
            take_profit=0.0,
            confidence=0.0,
            timestamp=datetime.now(timezone.utc),
        )
        score = self.score_signal(sig, opening_range, current_volume)
        passed = score.score >= self.min_score_threshold
        if not passed:
            logger.debug(
                "ORBFilter REJECT: dir=%s entry=%.5f score=%.3f threshold=%.3f type=%s",
                signal_direction,
                entry_price,
                score.score,
                self.min_score_threshold,
                score.breakout_type,
            )
        return passed

    # ── Opening Range Calculation ───────────────────────────────────────────

    def calculate_opening_range(
        self,
        session: str,
        bars: list[dict],
    ) -> Optional[OpeningRange]:
        """Compute the opening range for *session* from a list of OHLCV bars.

        Each bar dict is expected to have: ``high``, ``low``, ``open``,
        ``close``, ``volume``, and optionally ``timestamp`` (ISO string or
        ``datetime``).

        The first *N* bars of ``bars`` are used, where *N* is the configured
        ORB window for the session.  If fewer bars than *N* are provided we
        still compute the range from what's available (minimum 2 bars).

        Returns ``None`` when fewer than 2 bars are supplied.
        """
        if session not in self.orb_windows:
            # Unknown session — try a sane default
            window = 60
        else:
            window = self.orb_windows[session]

        if len(bars) < 2:
            return None

        window_bars = bars[:window]
        highs = [b["high"] for b in window_bars if "high" in b]
        lows = [b["low"] for b in window_bars if "low" in b]
        volumes = [b["volume"] for b in window_bars if "volume" in b]

        if not highs or not lows:
            return None

        or_high = max(highs)
        or_low = min(lows)
        avg_vol = sum(volumes) / len(volumes) if volumes else 0.0

        # Timestamps
        first_ts = self._parse_ts(window_bars[0].get("timestamp"))
        last_ts = self._parse_ts(window_bars[-1].get("timestamp"))
        if first_ts is None:
            first_ts = datetime.now(timezone.utc)
        if last_ts is None:
            last_ts = first_ts

        return OpeningRange(
            session=session,
            high=or_high,
            low=or_low,
            open_time=first_ts,
            close_time=last_ts,
            avg_volume=avg_vol,
        )

    # ── Signal Scoring ──────────────────────────────────────────────────────

    def score_signal(
        self,
        signal: Signal,
        opening_range: OpeningRange,
        current_volume: float = 0.0,
    ) -> ORBScore:
        """Score a single ``Signal`` against an ``OpeningRange``.

        *current_volume* is the volume of the breakout bar (or current bar).
        Pass 0 to skip volume confirmation.
        """
        if not opening_range.is_valid:
            return ORBScore(
                signal=signal,
                score=0.0,
                direction_aligned=False,
                distance_score=0.0,
                volume_score=0.0,
                breakout_type="invalid_range",
                detail="Opening range not valid (high <= low)",
            )

        direction = signal.direction.lower()
        entry = signal.entry_price
        or_width = opening_range.width

        # ── Direction alignment ──
        if direction == "long":
            direction_aligned = entry > opening_range.high
        elif direction == "short":
            direction_aligned = entry < opening_range.low
        else:
            direction_aligned = False

        # ── Distance score ──
        if direction == "long":
            ref_edge = opening_range.high
            distance = entry - ref_edge
        elif direction == "short":
            ref_edge = opening_range.low
            distance = ref_edge - entry
        else:
            ref_edge = opening_range.mid
            distance = 0.0

        # Normalise distance by OR width
        if or_width > 0:
            dist_fraction = max(0.0, distance / or_width)
        else:
            dist_fraction = 0.0

        # Beyond the min breakout fraction → full marks, then decay
        if dist_fraction >= self.breakout_min_fraction:
            distance_score = min(1.0, 0.5 + dist_fraction)
        elif dist_fraction > 0:
            # Pseudo-breakout (close to edge but not beyond min fraction)
            distance_score = dist_fraction / self.breakout_min_fraction * 0.5
        else:
            distance_score = 0.0

        distance_score = max(0.0, min(1.0, distance_score))

        # ── Volume score ──
        if current_volume > 0 and opening_range.avg_volume > 0:
            vol_ratio = current_volume / opening_range.avg_volume
            if vol_ratio >= VOLUME_STRONG:
                volume_score = 1.0
            elif vol_ratio >= VOLUME_NORMAL:
                volume_score = 0.6
            elif vol_ratio >= 0.7:
                volume_score = 0.3
            else:
                volume_score = 0.0
        else:
            # No volume data — neutral, doesn't penalise
            volume_score = 0.5

        # ── Breakout type ──
        if direction_aligned and dist_fraction >= self.breakout_min_fraction:
            breakout_type = "breakout"
        elif direction_aligned:
            breakout_type = "pseudo"
        elif opening_range.low <= entry <= opening_range.high:
            breakout_type = "inside"
        else:
            breakout_type = "failure"

        # ── Composite score ──
        dir_score = 1.0 if direction_aligned else 0.0
        composite = W_DIRECTION * dir_score + W_DISTANCE * distance_score + W_VOLUME * volume_score

        detail = (
            f"type={breakout_type} dir={'✓' if direction_aligned else '✗'} "
            f"dist_frac={dist_fraction:.3f} vol_score={volume_score:.2f}"
        )

        return ORBScore(
            signal=signal,
            score=round(composite, 4),
            direction_aligned=direction_aligned,
            distance_score=round(distance_score, 4),
            volume_score=round(volume_score, 4),
            breakout_type=breakout_type,
            detail=detail,
        )

    # ── Batch Prioritization ────────────────────────────────────────────────

    def prioritize(
        self,
        signals: list[Signal],
        opening_range: OpeningRange,
        current_volume: float = 0.0,
        min_score: float = 0.0,
    ) -> list[ORBScore]:
        """Score and sort *signals* by ORB alignment, best first.

        Signals below *min_score* are filtered out.
        """
        scored = [self.score_signal(sig, opening_range, current_volume) for sig in signals]
        scored = [s for s in scored if s.score >= min_score]
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored

    # ── Session Window Selection ────────────────────────────────────────────

    def get_session_window(self, session: str) -> tuple[time, time]:
        """Return the ``(start, end)`` UTC time window for *session*.

        Delegates to the existing ``SESSIONS`` dict so there is a single
        source of truth for session boundaries.
        """
        if session not in SESSIONS:
            raise ValueError(f"Unknown session '{session}'. Valid sessions: {list(SESSIONS.keys())}")
        return SESSIONS[session]

    def get_active_sessions(self, utc_dt: datetime) -> list[str]:
        """Return a list of sessions whose window contains *utc_dt*."""
        t = utc_dt.time()
        active: list[str] = []
        for name, (start, end) in SESSIONS.items():
            if start <= t < end:
                active.append(name)
        return active

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_ts(raw) -> Optional[datetime]:
        """Parse a timestamp from a bar dict value."""
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                return None
        return None
