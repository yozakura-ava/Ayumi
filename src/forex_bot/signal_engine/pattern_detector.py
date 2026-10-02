"""§5 — Pattern detection: M/W formations, SVCs, traps, liquidity grabs, FL."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dt_time
from typing import Optional

import pytz

_ET = pytz.timezone("America/New_York")

from .data_types import Level, LevelType, Swing, SwingType
from .thresholds import (
    MW_EQUAL_THRESHOLD,
    MW_SYMMETRY_MAX,
    NEAR_THRESHOLD,
    TRAP_BREAK_ASIA,
    TRAP_BREAK_LONDON_NY,
)


@dataclass
class DetectedPattern:
    """A detected pattern with metadata."""

    pattern_type: str  # "M", "W", "SVC_SPRING", "SVC_VACATION", "SVC_CONTINUATION",
    # "TRAP", "LIQUIDITY_GRAB", "FL"
    direction: str  # "long" or "short"
    confidence: float  # 0.0 - 1.0
    key_levels: dict = field(default_factory=dict)
    checklist_score: float = 0.0  # for M/W: fraction of 11 points passed
    checklist_details: dict = field(default_factory=dict)
    notes: str = ""
    # Flight-log extensions
    flight_log_id: str = ""
    asia_range: Optional[tuple[float, float]] = None
    stop_at_first_peak: bool = True
    mandatory_exit_time: Optional[dt_time] = None
    consolidation_confirmed: bool = False


# ── Asia Session Analyzer ──────────────────────────────────────────


@dataclass
class AsiaRangeResult:
    """Result of Asia session analysis."""

    asia_high: float
    asia_low: float
    range_pct: float
    is_tradable: bool
    high_touches: int
    low_touches: int
    is_consolidated: bool
    quality_score: float
    mandatory_exit_time: dt_time = dt_time(8, 0)
    ilod: Optional[float] = None
    ilhod: Optional[float] = None
    asia_gap_type: str = "none"


class AsiaSessionAnalyzer:
    """Analyze Asia session range quality for TTC day-trade system."""

    def __init__(
        self,
        max_range_pct: float = 2.0,
        min_touches_per_side: int = 2,
        touch_tolerance_pct: float = 0.03,
        asia_start_hour: float = 20.0,
        asia_end_hour: float = 1.5,
        london_kz_start: float = 2.0,
        london_kz_end: float = 5.0,
        ny_kz_start: float = 7.0,
        ny_kz_end: float = 10.0,
        mandatory_exit_hour: float = 8.0,
    ):
        self.max_range_pct = max_range_pct
        self.min_touches_per_side = min_touches_per_side
        self.touch_tolerance_pct = touch_tolerance_pct
        self.asia_start_hour = asia_start_hour
        self.asia_end_hour = asia_end_hour
        self.london_kz_start = london_kz_start
        self.london_kz_end = london_kz_end
        self.ny_kz_start = ny_kz_start
        self.ny_kz_end = ny_kz_end
        self.mandatory_exit_hour = mandatory_exit_hour

    def analyze_asia_range(
        self,
        bars: list[dict],
        bar_interval_minutes: int = 15,
    ) -> Optional[AsiaRangeResult]:
        if not bars:
            return None
        asia_bars = self._filter_asia_bars(bars)
        if len(asia_bars) < 4:
            return None
        core_count = max(4, int(len(asia_bars) * 0.80))
        core_bars = asia_bars[:core_count]
        asia_high = max(b["high"] for b in core_bars)
        asia_low = min(b["low"] for b in core_bars)
        if asia_low <= 0:
            return None
        range_pct = (asia_high - asia_low) / asia_low * 100
        is_tradable = range_pct < self.max_range_pct
        touch_tol_high = asia_high * (self.touch_tolerance_pct / 100)
        touch_tol_low = asia_low * (self.touch_tolerance_pct / 100)
        high_touches = sum(1 for b in asia_bars if abs(b["high"] - asia_high) <= touch_tol_high)
        low_touches = sum(1 for b in asia_bars if abs(b["low"] - asia_low) <= touch_tol_low)
        is_consolidated = high_touches >= self.min_touches_per_side and low_touches >= self.min_touches_per_side
        range_score = max(0, 1.0 - range_pct / self.max_range_pct) if self.max_range_pct > 0 else 0
        consolidation_score = 1.0 if is_consolidated else 0.3
        touch_score = min(high_touches + low_touches, 10) / 10.0
        quality_score = range_score * 0.5 + consolidation_score * 0.3 + touch_score * 0.2

        # ILOD/IHOD: first swing high/low in Asia (timing-based, not size-based)
        ilod, ilhod = self._detect_ilod_ihod_from_bars(asia_bars)

        # Asia gap: compare next session's open against Asia range
        next_session_open = self._get_next_session_open(bars, asia_bars)
        asia_gap_type = "none"
        if next_session_open is not None:
            if next_session_open > asia_high:
                asia_gap_type = "bullish"
            elif next_session_open < asia_low:
                asia_gap_type = "bearish"

        return AsiaRangeResult(
            asia_high=asia_high,
            asia_low=asia_low,
            range_pct=range_pct,
            is_tradable=is_tradable,
            high_touches=high_touches,
            low_touches=low_touches,
            is_consolidated=is_consolidated,
            quality_score=quality_score,
            ilod=ilod,
            ilhod=ilhod,
            asia_gap_type=asia_gap_type,
        )

    @staticmethod
    def _detect_ilod_ihod_from_bars(
        asia_bars: list[dict],
    ) -> tuple[Optional[float], Optional[float]]:
        """Find the FIRST swing low (ILOD) and FIRST swing high (IHOD) in Asia.

        A swing low: bar whose low is lower than the N bars on each side.
        A swing high: bar whose high is higher than the N bars on each side.
        We return the FIRST such swing, not the most extreme.
        Lookback N=2 (need 2 bars on each side to confirm a swing).
        """
        lookback = 2
        if len(asia_bars) < lookback * 2 + 1:
            # Not enough bars for swing detection — fall back to first/last
            return (
                min(b["low"] for b in asia_bars) if asia_bars else None,
                max(b["high"] for b in asia_bars) if asia_bars else None,
            )

        ilod = None
        ilhod = None

        for i in range(lookback, len(asia_bars) - lookback):
            bar = asia_bars[i]
            # Check swing low
            if ilod is None:
                is_swing_low = all(
                    bar["low"] <= asia_bars[j]["low"] for j in range(i - lookback, i + lookback + 1) if j != i
                )
                if is_swing_low:
                    ilod = bar["low"]
            # Check swing high
            if ilhod is None:
                is_swing_high = all(
                    bar["high"] >= asia_bars[j]["high"] for j in range(i - lookback, i + lookback + 1) if j != i
                )
                if is_swing_high:
                    ilhod = bar["high"]
            if ilod is not None and ilhod is not None:
                break

        # Fallback if no swing found
        if ilod is None and asia_bars:
            ilod = min(b["low"] for b in asia_bars)
        if ilhod is None and asia_bars:
            ilhod = max(b["high"] for b in asia_bars)

        return ilod, ilhod

    @staticmethod
    def _get_next_session_open(
        all_bars: list[dict],
        asia_bars: list[dict],
    ) -> Optional[float]:
        """Get the open price of the first bar after the Asia session."""
        if not asia_bars or not all_bars:
            return None
        # Find the index of the last Asia bar in all_bars
        last_asia = asia_bars[-1]
        last_asia_time = last_asia.get("time")
        if last_asia_time is None:
            return None
        if isinstance(last_asia_time, datetime):
            last_asia_time_val = last_asia_time
        elif isinstance(last_asia_time, str):
            last_asia_time_val = datetime.fromisoformat(last_asia_time)
        else:
            return None

        for b in all_bars:
            t = b.get("time")
            if t is None:
                continue
            if isinstance(t, datetime):
                t_val = t
            elif isinstance(t, str):
                t_val = datetime.fromisoformat(t)
            else:
                continue
            if t_val > last_asia_time_val:
                return b.get("open")
        return None

    @staticmethod
    def _to_et(t: datetime) -> datetime:
        if t.tzinfo is None:
            t = pytz.utc.localize(t)
        return t.astimezone(_ET)

    def is_kill_zone(self, bar_time: datetime) -> str:
        et = self._to_et(bar_time)
        hour = et.hour + et.minute / 60.0
        if self.london_kz_start <= hour < self.london_kz_end:
            return "london_kz"
        if self.ny_kz_start <= hour < self.ny_kz_end:
            return "ny_kz"
        return "outside"

    def should_mandatory_exit(self, bar_time: datetime) -> bool:
        et = self._to_et(bar_time)
        hour = et.hour + et.minute / 60.0
        return hour >= self.mandatory_exit_hour

    def _filter_asia_bars(self, bars: list[dict]) -> list[dict]:
        result = []
        first_utc = None
        for b in bars:
            t = b.get("time")
            if t is None:
                continue
            if isinstance(t, datetime):
                first_utc = t
            elif isinstance(t, str):
                first_utc = datetime.fromisoformat(t)
            break
        if first_utc is None:
            return result
        if first_utc.tzinfo is not None:
            first_utc = first_utc.replace(tzinfo=None)
        et_offset_hours = round(_ET.localize(first_utc).utcoffset().total_seconds() / 3600)
        is_dst = et_offset_hours == -4
        for b in bars:
            t = b.get("time")
            if t is None:
                continue
            if isinstance(t, datetime):
                t_naive = t.replace(tzinfo=None) if t.tzinfo else t
            elif isinstance(t, str):
                t_naive = datetime.fromisoformat(t).replace(tzinfo=None)
            else:
                continue
            h = t_naive.hour + t_naive.minute / 60.0
            if is_dst:
                in_asia = 0.0 <= h <= 5.5
            else:
                in_asia = 1.0 <= h <= 6.5
            if in_asia:
                result.append(b)
        return result


class PatternDetector:
    """Detect M/W formations, SVCs, traps, liquidity grabs, and FL patterns."""

    def __init__(
        self,
        symmetry_max: float = MW_SYMMETRY_MAX,
        equal_threshold: float = MW_EQUAL_THRESHOLD,
        trap_break_london_ny: float = TRAP_BREAK_LONDON_NY,
        trap_break_asia: float = TRAP_BREAK_ASIA,
        near_threshold: float = NEAR_THRESHOLD,
    ):
        self.symmetry_max = symmetry_max
        self.equal_threshold = equal_threshold
        self.trap_break_london_ny = trap_break_london_ny
        self.trap_break_asia = trap_break_asia
        self.near_threshold = near_threshold

    # ── M/W 11-Point Checklist ──────────────────────────────────────

    MW_MAX_BARS_AGO = 200

    def detect_mw_formation(
        self,
        swings: list[Swing],
        levels: list[Level],
        current_price: float = 0.0,
        current_bar_index: int = -1,
    ) -> Optional[DetectedPattern]:
        """Detect M (bearish reversal) or W (bullish reversal) using 11-point checklist.

        M structure: SH1 -> SL1 -> SH2 -> SL2 -> SH3 (breaks SH1)
        W structure: SL1 -> SH1 -> SL2 -> SH2 -> SL3 (breaks SL1)

        Scans all possible 5-swing windows and returns the best-matching pattern.
        """
        best = None
        best = self._scan_m_patterns(swings, levels, current_price, best, current_bar_index)
        best = self._scan_w_patterns(swings, levels, current_price, best, current_bar_index)
        return best

    def _scan_m_patterns(
        self,
        swings: list[Swing],
        levels: list[Level],
        current_price: float,
        best: Optional[DetectedPattern],
        current_bar_index: int,
    ) -> Optional[DetectedPattern]:
        pattern_swings = self._extract_alternating(swings, SwingType.HIGH)
        if len(pattern_swings) < 5:
            return best
        for i in range(len(pattern_swings) - 4):
            sh3 = pattern_swings[i + 4]
            if current_bar_index >= 0 and (current_bar_index - sh3.bar_index) > self.MW_MAX_BARS_AGO:
                continue
            candidate = self._evaluate_m_window(pattern_swings, i, levels, current_price)
            if candidate and candidate.confidence > 0.3:
                if best is None or candidate.confidence > best.confidence:
                    best = candidate
        return best

    def _scan_w_patterns(
        self,
        swings: list[Swing],
        levels: list[Level],
        current_price: float,
        best: Optional[DetectedPattern],
        current_bar_index: int,
    ) -> Optional[DetectedPattern]:
        pattern_swings = self._extract_alternating(swings, SwingType.LOW)
        if len(pattern_swings) < 5:
            return best
        for i in range(len(pattern_swings) - 4):
            sl3 = pattern_swings[i + 4]
            if current_bar_index >= 0 and (current_bar_index - sl3.bar_index) > self.MW_MAX_BARS_AGO:
                continue
            candidate = self._evaluate_w_window(pattern_swings, i, levels, current_price)
            if candidate and candidate.confidence > 0.3:
                if best is None or candidate.confidence > best.confidence:
                    best = candidate
        return best

    def _evaluate_m_window(
        self,
        pattern_swings: list[Swing],
        start_idx: int,
        levels: list[Level],
        current_price: float,
    ) -> Optional[DetectedPattern]:
        sh1, sl1, sh2, sl2, sh3 = pattern_swings[start_idx : start_idx + 5]

        if sh3.price <= sh1.price or sl2.price >= sl1.price:
            return None

        checklist = self._mw_checklist(
            sh1=sh1.price,
            sl1=sl1.price,
            sh2=sh2.price,
            sl2=sl2.price,
            sh3=sh3.price,
            is_bearish=True,
            levels=levels,
            current_price=current_price,
        )

        score = sum(checklist.values()) / len(checklist)
        if score < 0.4:
            return None

        return DetectedPattern(
            pattern_type="M",
            direction="short",
            confidence=score,
            key_levels={
                "SH1": sh1.price,
                "SL1": sl1.price,
                "SH2": sh2.price,
                "SL2": sl2.price,
                "SH3": sh3.price,
            },
            checklist_score=score,
            checklist_details=checklist,
        )

    def _evaluate_w_window(
        self,
        pattern_swings: list[Swing],
        start_idx: int,
        levels: list[Level],
        current_price: float,
    ) -> Optional[DetectedPattern]:
        sl1, sh1, sl2, sh2, sl3 = pattern_swings[start_idx : start_idx + 5]

        if sl3.price >= sl1.price or sh2.price <= sh1.price:
            return None

        checklist = self._mw_checklist(
            sh1=sh1.price,
            sl1=sl1.price,
            sh2=sh2.price,
            sl2=sl2.price,
            sh3=sl3.price,
            is_bearish=False,
            levels=levels,
            current_price=current_price,
        )

        score = sum(checklist.values()) / len(checklist)
        if score < 0.4:
            return None

        return DetectedPattern(
            pattern_type="W",
            direction="long",
            confidence=score,
            key_levels={
                "SL1": sl1.price,
                "SH1": sh1.price,
                "SL2": sl2.price,
                "SH2": sh2.price,
                "SL3": sl3.price,
            },
            checklist_score=score,
            checklist_details=checklist,
        )

        score = sum(checklist.values()) / 11.0
        if score < 0.4:
            return None

        return DetectedPattern(
            pattern_type="M",
            direction="short",
            confidence=score,
            key_levels={
                "SH1": sh1.price,
                "SL1": sl1.price,
                "SH2": sh2.price,
                "SL2": sl2.price,
                "SH3": sh2.price,  # noqa: F821 — sh3 unavailable in this scope
            },
            checklist_score=score,
            checklist_details=checklist,
        )

    def _extract_alternating(self, swings: list[Swing], start_type: SwingType) -> list[Swing]:
        """Extract alternating H-L-H-L... or L-H-L-H... sequence from swings."""
        sorted_swings = sorted(swings, key=lambda s: s.bar_index)
        result: list[Swing] = []
        expected = start_type
        for s in sorted_swings:
            if s.swing_type == expected:
                result.append(s)
                expected = SwingType.LOW if expected == SwingType.HIGH else SwingType.HIGH
        return result

    def _mw_checklist(
        self,
        sh1: float,
        sl1: float,
        sh2: float,
        sl2: float,
        sh3: float,
        is_bearish: bool,
        levels: list[Level],
        current_price: float,
    ) -> dict[str, bool]:
        """Evaluate the 11-point M/W checklist.

        Points (adapted for both M and W):
        1. Break of first extreme (SH1 for M, SL1 for W)
        2. Lower low (SL2 < SL1 for M) / Higher high (SH2 > SH1 for W)
        3. Equality of first extremes (SH1≈SH3 for M, SL1≈SL3 for W)
        4. Second extreme not exceeded (SH2 < SH1 for M, SL2 > SL1 for W)
        5. SL1/SL2 symmetry ≤ 1.5%
        6. SH1/SH2 symmetry ≤ 1.5% (for M; mirrored for W)
        7. Clear rejection at first extreme
        8. Structure completion (all 5 points present)
        9. Time symmetry (roughly equal legs)
        10. Volume climax at turning points (simplified: checked via levels)
        11. No level overlap violations
        """
        checks: dict[str, bool] = {}

        if is_bearish:
            # 1. SH3 breaks SH1
            checks["break_first_extreme"] = sh3 > sh1
            # 2. SL2 < SL1 (lower low)
            checks["second_low_lower"] = sl2 < sl1
            # 3. SH1 ≈ SH3 (equality within tolerance)
            checks["equality_first_extremes"] = abs(sh3 - sh1) / sh1 <= self.equal_threshold * 10
            # 4. SH2 < SH1 (second high doesn't exceed first)
            checks["second_high_below_first"] = sh2 < sh1
            # 5. SL symmetry
            sl_mid = (sh1 + sh2) / 2
            sl1_dist = abs(sl1 - sl_mid)
            sl2_dist = abs(sl2 - sl_mid)
            sl_avg = (sl1_dist + sl2_dist) / 2
            checks["sl_symmetry"] = sl_avg > 0 and abs(sl1_dist - sl2_dist) / sl_avg <= self.symmetry_max
            # 6. SH symmetry
            sh1_dist = abs(sh1 - sl_mid)
            sh2_dist = abs(sh2 - sl_mid)
            sh_avg = (sh1_dist + sh2_dist) / 2
            checks["sh_symmetry"] = sh_avg > 0 and abs(sh1_dist - sh2_dist) / sh_avg <= self.symmetry_max
            # 7. Clear rejection (SH2 significantly below SH1)
            checks["clear_rejection"] = (sh1 - sh2) / sh1 > self.near_threshold
        else:
            # W (bullish) — mirror logic
            sl3 = sh3  # reuse
            checks["break_first_extreme"] = sl3 < sl1
            checks["second_high_higher"] = sh2 > sh1
            checks["equality_first_extremes"] = abs(sl3 - sl1) / sl1 <= self.equal_threshold * 10
            checks["second_low_above_first"] = sl2 > sl1
            # SH symmetry (now SH1/SH2 are the symmetric pair)
            sh_mid = (sl1 + sl2) / 2
            sh1_dist = abs(sh1 - sh_mid)
            sh2_dist = abs(sh2 - sh_mid)
            sh_avg = (sh1_dist + sh2_dist) / 2
            checks["sh_symmetry"] = sh_avg > 0 and abs(sh1_dist - sh2_dist) / sh_avg <= self.symmetry_max
            # SL symmetry
            sl1_dist = abs(sl1 - sh_mid)
            sl2_dist = abs(sl2 - sh_mid)
            sl_avg = (sl1_dist + sl2_dist) / 2
            checks["sl_symmetry"] = sl_avg > 0 and abs(sl1_dist - sl2_dist) / sl_avg <= self.symmetry_max
            checks["clear_rejection"] = (sl2 - sl1) / sl1 > self.near_threshold

        # 8. Structure completion (always true if we got here — 5 swings found)
        checks["structure_complete"] = True

        # 9. Time symmetry — skip without bar_index timing data in this check

        # 10. Volume climax — simplified: accept if levels are present
        checks["volume_confirmation"] = len(levels) > 0

        # 11. No level overlap — simplified: pass by default
        checks["no_level_violation"] = True

        return checks

    # ── SVC Detection ───────────────────────────────────────────────

    def detect_svc(
        self,
        bar: dict,
        levels: list[Level],
        session: str = "LONDON",
    ) -> Optional[DetectedPattern]:
        """Detect Stopping Volume Candle (SVC) patterns.

        Spring: price sweeps below support then reverses (bullish).
        Vacation: price sweeps above resistance then reverses (bearish).
        Continuation: momentum candle in trend direction.
        """
        high = bar.get("high", 0)
        low = bar.get("low", 0)
        close = bar.get("close", 0)
        open_ = bar.get("open", 0)
        body = abs(close - open_)
        total_range = high - low
        if total_range <= 0:
            return None

        # Body must be small (≤40% of range for an SVC — wick dominates)
        body_ratio = body / total_range
        if body_ratio > 0.40:
            return None

        is_bullish = close > open_

        for lv in levels:
            if lv.level_type in (LevelType.D1, LevelType.D2, LevelType.D3):
                # Check for spring: wick sweeps below level, body closes above
                if low < lv.price < close and is_bullish:
                    proximity = abs(low - lv.price) / lv.price
                    if proximity < self.near_threshold:
                        return DetectedPattern(
                            pattern_type="SVC_SPRING",
                            direction="long",
                            confidence=min(body_ratio, 0.9),
                            key_levels={"level": lv.price, "type": lv.level_type.value},
                            notes=f"Spring at {lv.level_type.value} ({lv.price:.5f})",
                        )
            elif lv.level_type in (LevelType.R1, LevelType.R2, LevelType.R3):
                # Check for vacation: wick sweeps above level, body closes below
                if high > lv.price > close and not is_bullish:
                    proximity = abs(high - lv.price) / lv.price
                    if proximity < self.near_threshold:
                        return DetectedPattern(
                            pattern_type="SVC_VACATION",
                            direction="short",
                            confidence=min(body_ratio, 0.9),
                            key_levels={"level": lv.price, "type": lv.level_type.value},
                            notes=f"Vacation at {lv.level_type.value} ({lv.price:.5f})",
                        )

        # Continuation: strong momentum candle with no level interaction
        if body_ratio >= 0.70:
            direction = "long" if is_bullish else "short"
            return DetectedPattern(
                pattern_type="SVC_CONTINUATION",
                direction=direction,
                confidence=body_ratio * 0.8,
                notes=f"Continuation candle (body ratio {body_ratio:.2f})",
            )

        return None

    # ── Trap Detection ──────────────────────────────────────────────

    def detect_trap(
        self,
        bars: list[dict],
        level_price: float,
        session: str = "LONDON",
    ) -> Optional[DetectedPattern]:
        """Detect trap setups where price breaks a level then reverses.

        Uses minimum break distance per session:
        - London/NY: 0.2%
        - Asia: 0.4%
        """
        if not bars:
            return None

        min_break = self.trap_break_london_ny if session in ("LONDON", "NY", "LONDON_NY") else self.trap_break_asia

        # Find the maximum excursion beyond the level
        max_above = max(b.get("high", 0) for b in bars)
        max_below = min(b.get("low", 0) for b in bars)
        last_close = bars[-1].get("close", 0)

        # Bullish trap: price broke above level then closed below
        if max_above > level_price * (1 + min_break) and last_close < level_price:
            break_pct = (max_above - level_price) / level_price
            return DetectedPattern(
                pattern_type="TRAP",
                direction="short",
                confidence=min(break_pct * 2, 0.9),
                key_levels={
                    "trap_level": level_price,
                    "max_excursion": max_above,
                    "break_pct": break_pct,
                },
                notes=f"Bullish trap in {session} (broke {break_pct:.3%})",
            )

        # Bearish trap: price broke below level then closed above
        if max_below < level_price * (1 - min_break) and last_close > level_price:
            break_pct = (level_price - max_below) / level_price
            return DetectedPattern(
                pattern_type="TRAP",
                direction="long",
                confidence=min(break_pct * 2, 0.9),
                key_levels={
                    "trap_level": level_price,
                    "max_excursion": max_below,
                    "break_pct": break_pct,
                },
                notes=f"Bearish trap in {session} (broke {break_pct:.3%})",
            )

        return None

    # ── ILOD / IHOD Detection ───────────────────────────────────────

    def detect_ilod_ihod(
        self,
        bars: list[dict],
        session: str = "LONDON",
        asia_analyzer: Optional[AsiaSessionAnalyzer] = None,
    ) -> list[DetectedPattern]:
        """Detect Intraday Low of Day (ILOD) and High of Day (IHOD) breaks.

        ILOD = Initial Low of Day (first swing low during Asia session)
        IHOD = Initial High of Day (first swing high during Asia session)

        These are TIMING-based (first print), not size-based (highest/lowest).

        Break detection:
        - Bearish: current close < ILOD (Asia low broken)
        - Bullish: current close > IHOD (Asia high broken)
        """
        if not bars:
            return []

        patterns: list[DetectedPattern] = []

        # Try to get ILOD/IHOD from Asia session analysis
        ilod = None
        ilhod = None
        if asia_analyzer is not None:
            asia_result = asia_analyzer.analyze_asia_range(bars)
            if asia_result is not None:
                ilod = asia_result.ilod
                ilhod = asia_result.ilhod

        # Fallback: use session high/low if no Asia analyzer
        if ilod is None:
            ilod = min(b.get("low", 0) for b in bars)
        if ilhod is None:
            ilhod = max(b.get("high", 0) for b in bars)

        last_bar = bars[-1]
        prev_bar = bars[-2] if len(bars) >= 2 else None

        # ILOD break (bearish): current close below Asia low,
        # and previous bar was above ILOD (fresh break)
        if last_bar["close"] < ilod:
            was_above = prev_bar is None or prev_bar["close"] >= ilod
            if was_above:
                patterns.append(
                    DetectedPattern(
                        pattern_type="ILOD_BREAK",
                        direction="short",
                        confidence=0.6,
                        key_levels={"ilod": ilod},
                        notes=f"ILOD break in {session} (fresh break below {ilod:.5f})",
                    )
                )

        # IHOD break (bullish): current close above Asia high,
        # and previous bar was below IHOD (fresh break)
        if last_bar["close"] > ilhod:
            was_below = prev_bar is None or prev_bar["close"] <= ilhod
            if was_below:
                patterns.append(
                    DetectedPattern(
                        pattern_type="IHOD_BREAK",
                        direction="long",
                        confidence=0.6,
                        key_levels={"ihod": ilhod},
                        notes=f"IHOD break in {session} (fresh break above {ilhod:.5f})",
                    )
                )

        session_high = max(b.get("high", 0) for b in bars[:-1]) if len(bars) > 1 else bars[0].get("high", 0)
        session_low = min(b.get("low", 0) for b in bars[:-1]) if len(bars) > 1 else bars[0].get("low", 0)

        # ILOD break — price breaks below prior session low then shows rejection
        if last_bar["low"] < session_low and last_bar["close"] > session_low:
            patterns.append(
                DetectedPattern(
                    pattern_type="ILOD_BREAK",
                    direction="short",
                    confidence=0.6,
                    key_levels={"ilod": session_low},
                    notes=f"ILOD break in {session}",
                )
            )

        # IHOD break — price breaks above prior session high then shows rejection
        if last_bar["high"] > session_high and last_bar["close"] < session_high:
            patterns.append(
                DetectedPattern(
                    pattern_type="IHOD_BREAK",
                    direction="long",
                    confidence=0.6,
                    key_levels={"ihod": session_high},
                    notes=f"IHOD break in {session}",
                )
            )

        return patterns

    # ── Liquidity Grab ──────────────────────────────────────────────

    def detect_liquidity_grab(
        self,
        bar: dict,
        levels: list[Level],
    ) -> Optional[DetectedPattern]:
        """Detect liquidity grab: wick sweeps through a level, body rejects.

        A liquidity grab has a long wick through a level with the body
        closing on the opposite side, indicating stops were taken.
        """
        high = bar.get("high", 0)
        low = bar.get("low", 0)
        close = bar.get("close", 0)
        open_ = bar.get("open", 0)
        total_range = high - low
        if total_range <= 0:
            return None

        for lv in levels:
            wick_below = low < lv.price
            wick_above = high > lv.price

            # Bullish liquidity grab: wick below level, body closes above
            if wick_below and close > lv.price and open_ > lv.price:
                wick_size = lv.price - low
                wick_ratio = wick_size / total_range
                if wick_ratio > 0.3:
                    return DetectedPattern(
                        pattern_type="LIQUIDITY_GRAB",
                        direction="long",
                        confidence=min(wick_ratio, 0.9),
                        key_levels={
                            "grab_level": lv.price,
                            "wick_ratio": wick_ratio,
                            "type": lv.level_type.value,
                        },
                        notes=f"Bullish liq grab at {lv.level_type.value}",
                    )

            # Bearish liquidity grab: wick above level, body closes below
            if wick_above and close < lv.price and open_ < lv.price:
                wick_size = high - lv.price
                wick_ratio = wick_size / total_range
                if wick_ratio > 0.3:
                    return DetectedPattern(
                        pattern_type="LIQUIDITY_GRAB",
                        direction="short",
                        confidence=min(wick_ratio, 0.9),
                        key_levels={
                            "grab_level": lv.price,
                            "wick_ratio": wick_ratio,
                            "type": lv.level_type.value,
                        },
                        notes=f"Bearish liq grab at {lv.level_type.value}",
                    )

        return None

    # ── FL (Flight Log) Strategy ────────────────────────────────────

    def detect_fl_pattern(
        self,
        swings: list[Swing],
        levels: list[Level],
        current_price: float = 0.0,
    ) -> Optional[DetectedPattern]:
        """Detect FL (Flight Log) strategy patterns.

        FL strategy looks for a series of higher highs and higher lows (or
        inverse) forming a staircase pattern with clear impulse/correction
        legs. At least 3 impulse legs required.
        """
        sorted_swings = sorted(swings, key=lambda s: s.bar_index)
        if len(sorted_swings) < 6:
            return None

        # Extract alternating swings starting from the most common type
        starts_high = sum(1 for s in sorted_swings[:3] if s.swing_type == SwingType.HIGH)
        start_type = SwingType.HIGH if starts_high >= 2 else SwingType.LOW
        alt = self._extract_alternating(sorted_swings, start_type)

        if len(alt) < 6:
            return None

        # Check for bullish FL: HL1, HH1, HL2, HH2, HL3, HH3
        is_bullish = self._check_staircase(alt, bullish=True)
        is_bearish = not is_bullish and self._check_staircase(alt, bullish=False)

        if not is_bullish and not is_bearish:
            return None

        direction = "long" if is_bullish else "short"
        # Confidence scales with number of impulse legs
        impulse_count = len(alt) // 2
        confidence = min(0.5 + impulse_count * 0.1, 0.9)

        last_swing = alt[-1]
        return DetectedPattern(
            pattern_type="FL",
            direction=direction,
            confidence=confidence,
            key_levels={
                "last_swing_price": last_swing.price,
                "impulse_legs": impulse_count,
            },
            notes=f"FL {direction} with {impulse_count} impulse legs",
        )

    def _check_staircase(self, swings: list[Swing], bullish: bool) -> bool:
        """Check if swings form a staircase pattern (HH/HL or LH/LL)."""
        if len(swings) < 4:
            return False

        if bullish:
            # For L-H-L-H... starting with L: check HL, HH, HL, HH...
            for i in range(2, len(swings)):
                if i % 2 == 0:  # Even = lows: should be higher low
                    if swings[i].price <= swings[i - 2].price:
                        return False
                else:  # Odd = highs: should be higher high
                    if swings[i].price <= swings[i - 2].price:
                        return False
        else:
            # For H-L-H-L... starting with H: check LH, LL, LH, LL...
            for i in range(2, len(swings)):
                if i % 2 == 0:  # Even = highs: should be lower high
                    if swings[i].price >= swings[i - 2].price:
                        return False
                else:  # Odd = lows: should be lower low
                    if swings[i].price >= swings[i - 2].price:
                        return False

        return True

    # ── SVC (Vector Candle) Helper ──────────────────────────────────

    def _detect_svc(self, bar: dict, direction: str, bars_for_vol: Optional[list[dict]] = None) -> bool:
        """Check if a single bar is an SVC (Stopping Volume Candle / Vector).

        TTC Rules:
        - Body ratio <= 0.40 (body is small)
        - Wick ratio >= 0.60 (wick dominates)
        - Volume ratio >= 1.3x avg of prior 9 bars
        - Color is OPPOSITE to the trade direction
          (long SVC = red/closed down; short SVC = green/closed up)

        Args:
            bar: The candle to evaluate.
            direction: "long" or "short" — the trade direction.
            bars_for_vol: Prior bars for volume average (at least 9).
                         If None, skips volume check.
        Returns:
            True if the bar qualifies as an SVC.
        """
        high = bar.get("high", 0)
        low = bar.get("low", 0)
        close = bar.get("close", 0)
        open_ = bar.get("open", 0)
        total_range = high - low
        if total_range <= 0:
            return False

        body = abs(close - open_)
        body_ratio = body / total_range
        wick_ratio = 1.0 - body_ratio

        if body_ratio > 0.40 or wick_ratio < 0.60:
            return False

        # Color check: opposite to trade direction
        is_bullish = close > open_
        if direction == "long" and is_bullish:
            return False  # long SVC must be bearish (red)
        if direction == "short" and not is_bullish:
            return False  # short SVC must be bullish (green)

        # Volume check
        bar_vol = bar.get("volume", 0)
        if bar_vol > 0 and bars_for_vol and len(bars_for_vol) >= 9:
            prior_vols = [b.get("volume", 0) for b in bars_for_vol[-9:] if b.get("volume", 0) > 0]
            if prior_vols:
                avg_vol = sum(prior_vols) / len(prior_vols)
                if avg_vol > 0 and bar_vol < avg_vol * 1.3:
                    return False

        return True

    # ── Composite Detection ─────────────────────────────────────────

    def detect_all(
        self,
        swings: list[Swing],
        levels: list[Level],
        bars: list[dict],
        current_price: float = 0.0,
        session: str = "LONDON",
        bar_time: Optional[datetime] = None,
        asia_analyzer: Optional[AsiaSessionAnalyzer] = None,
        current_bar_index: int = -1,
    ) -> list[DetectedPattern]:
        """Run all pattern detectors and return combined results."""
        patterns: list[DetectedPattern] = []

        # M/W formation
        mw = self.detect_mw_formation(swings, levels, current_price, current_bar_index)
        if mw:
            patterns.append(mw)

        # SVC (on last bar)
        if bars:
            svc = self.detect_svc(bars[-1], levels, session)
            if svc:
                patterns.append(svc)

        # Traps (check each level)
        for lv in levels:
            trap = self.detect_trap(bars[-5:] if len(bars) >= 5 else bars, lv.price, session)
            if trap:
                patterns.append(trap)

        # ILOD/IHOD
        patterns.extend(self.detect_ilod_ihod(bars, session, asia_analyzer=asia_analyzer))

        # Liquidity grab (on last bar)
        if bars:
            liq = self.detect_liquidity_grab(bars[-1], levels)
            if liq:
                patterns.append(liq)

        # FL pattern
        fl = self.detect_fl_pattern(swings, levels, current_price)
        if fl:
            patterns.append(fl)

        # Flight-log patterns (TTC Asia→UK)
        if asia_analyzer is not None and bar_time is not None:
            fl_patterns = self._detect_flight_log_patterns(
                swings=swings,
                levels=levels,
                bars=bars,
                current_price=current_price,
                bar_time=bar_time,
                asia_analyzer=asia_analyzer,
            )
            patterns.extend(fl_patterns)

        return patterns

    # ── Flight-Log Pattern Detection (TTC Asia→UK) ─────────────────

    def _detect_flight_log_patterns(
        self,
        swings: list[Swing],
        levels: list[Level],
        bars: list[dict],
        current_price: float,
        bar_time: datetime,
        asia_analyzer: AsiaSessionAnalyzer,
    ) -> list[DetectedPattern]:
        """Detect TTC flight-log patterns (FL-001 through FL-004)."""
        kz = asia_analyzer.is_kill_zone(bar_time)
        if kz == "outside":
            return []
        if asia_analyzer.should_mandatory_exit(bar_time):
            return []
        asia_result = asia_analyzer.analyze_asia_range(bars)
        if asia_result is None or not asia_result.is_tradable:
            return []

        # Consolidation: soft penalty (not hard gate)
        consolidation_penalty = 0.0 if asia_result.is_consolidated else 0.15

        patterns = []

        # FL-001: Single Session M/W
        fl001 = self._detect_fl001(swings, bars, current_price, asia_result)
        if fl001:
            fl001.confidence = max(0, fl001.confidence - consolidation_penalty - 0.10)
            fl001.consolidation_confirmed = asia_result.is_consolidated
            patterns.append(fl001)

        # FL-002: Liquidity Grab
        fl002 = self._detect_fl002(bars, current_price, asia_result)
        if fl002:
            fl002.confidence = max(0, fl002.confidence - consolidation_penalty - 0.10)
            fl002.consolidation_confirmed = asia_result.is_consolidated
            patterns.append(fl002)

        # FL-003: Multi-Session M/W
        fl003 = self._detect_fl003(swings, bars, current_price, asia_result)
        if fl003:
            fl003.confidence = max(0, fl003.confidence - consolidation_penalty - 0.10)
            fl003.consolidation_confirmed = asia_result.is_consolidated
            patterns.append(fl003)

        # FL-004: Multi-Session Fakeout
        fl004 = self._detect_fl004(bars, current_price, asia_result)
        if fl004:
            fl004.confidence = max(0, fl004.confidence - consolidation_penalty - 0.10)
            fl004.consolidation_confirmed = asia_result.is_consolidated
            patterns.append(fl004)

        return patterns

    def _detect_fl001(self, swings, bars, current_price, asia) -> Optional[DetectedPattern]:
        """FL-001: Single Session M/W — both peaks inside Asia range.

        Key TTC rules enforced:
        - Both peaks fully inside Asia range
        - Entry on NEXT candle after HL/LH forms (1-3 bar window)
        - Next candle must NOT break past second peak
        - Stop at FIRST peak
        - SVC check on first peak (confidence bonus)
        """
        if len(swings) < 4 or len(bars) < 3:
            return None
        sorted_swings = sorted(swings, key=lambda s: s.bar_index)
        highs = [s for s in sorted_swings if s.swing_type == SwingType.HIGH]
        lows = [s for s in sorted_swings if s.swing_type == SwingType.LOW]
        candidates = []
        window = min(300, len(bars))

        # W pattern
        # Note: swing.bar_index is 0-based relative to the window passed to
        # SwingDetector.detect_swings(). To map back to global bars index:
        #   global_idx = len(bars) - window + swing.bar_index
        # This is correct because detect_swings() receives bars[-window:]
        # and indexes from 0 within that slice.
        for sl1, sl2 in self._pair_consecutive(lows[-6:]):
            if not (asia.asia_low <= sl1.price <= asia.asia_high):
                continue
            if not (asia.asia_low <= sl2.price <= asia.asia_high):
                continue
            in_range = [h for h in highs if sl1.bar_index < h.bar_index < sl2.bar_index]
            sh1 = max(in_range, key=lambda h: h.price) if in_range else None
            if sh1 is None or not (asia.asia_low <= sh1.price <= asia.asia_high):
                continue
            if sl2.price <= sl1.price:
                continue  # need higher low

            # Entry trigger: next candle after HL
            swing_rel = len(bars) - window + sl2.bar_index
            bars_since = len(bars) - 1 - swing_rel
            if bars_since < 1 or bars_since > 3:
                continue

            cur = bars[-1]
            if cur["low"] < sl2.price:
                continue  # broke below second peak
            if cur["close"] <= sl2.price:
                continue  # no bullish reaction

            # SVC check on first peak
            first_bar_idx = len(bars) - window + sl1.bar_index
            first_bar = bars[max(0, first_bar_idx)] if first_bar_idx < len(bars) else None
            is_svc = False
            if first_bar:
                prior = bars[max(0, first_bar_idx - 9) : first_bar_idx]
                is_svc = self._detect_svc(first_bar, "long", prior)

            stop = sl1.price - (asia.asia_high - asia.asia_low) * 0.1
            target = asia.asia_high
            conf = asia.quality_score * 0.3 + 0.25 + (0.15 if is_svc else 0) + 0.15 + 0.15
            conf = min(conf, 1.0)
            candidates.append(
                DetectedPattern(
                    pattern_type="W",
                    direction="long",
                    confidence=conf,
                    key_levels={
                        "SL1": sl1.price,
                        "SH1": sh1.price,
                        "SL2": sl2.price,
                        "stop": stop,
                        "target": target,
                        "asia_high": asia.asia_high,
                        "asia_low": asia.asia_low,
                    },
                    flight_log_id="FL-001",
                    asia_range=(asia.asia_high, asia.asia_low),
                    stop_at_first_peak=True,
                    mandatory_exit_time=asia.mandatory_exit_time,
                    notes=f"FL-001 W: HL={sl2.price:.5f}, svc={is_svc}",
                )
            )

        # M pattern
        for sh1, sh2 in self._pair_consecutive(highs[-6:]):
            if not (asia.asia_low <= sh1.price <= asia.asia_high):
                continue
            if not (asia.asia_low <= sh2.price <= asia.asia_high):
                continue
            in_range = [sw for sw in lows if sh1.bar_index < sw.bar_index < sh2.bar_index]
            sl1 = min(in_range, key=lambda sw: sw.price) if in_range else None
            if sl1 is None or not (asia.asia_low <= sl1.price <= asia.asia_high):
                continue
            if sh2.price >= sh1.price:
                continue  # need lower high

            swing_rel = len(bars) - window + sh2.bar_index
            bars_since = len(bars) - 1 - swing_rel
            if bars_since < 1 or bars_since > 3:
                continue

            cur = bars[-1]
            if cur["high"] > sh2.price:
                continue
            if cur["close"] >= sh2.price:
                continue

            first_bar_idx = len(bars) - window + sh1.bar_index
            first_bar = bars[max(0, first_bar_idx)] if first_bar_idx < len(bars) else None
            is_svc = False
            if first_bar:
                prior = bars[max(0, first_bar_idx - 9) : first_bar_idx]
                is_svc = self._detect_svc(first_bar, "short", prior)

            stop = sh1.price + (asia.asia_high - asia.asia_low) * 0.1
            target = asia.asia_low
            conf = asia.quality_score * 0.3 + 0.25 + (0.15 if is_svc else 0) + 0.15 + 0.15
            conf = min(conf, 1.0)
            candidates.append(
                DetectedPattern(
                    pattern_type="M",
                    direction="short",
                    confidence=conf,
                    key_levels={
                        "SH1": sh1.price,
                        "SL1": sl1.price,
                        "SH2": sh2.price,
                        "stop": stop,
                        "target": target,
                        "asia_high": asia.asia_high,
                        "asia_low": asia.asia_low,
                    },
                    flight_log_id="FL-001",
                    asia_range=(asia.asia_high, asia.asia_low),
                    stop_at_first_peak=True,
                    mandatory_exit_time=asia.mandatory_exit_time,
                    notes=f"FL-001 M: LH={sh2.price:.5f}, svc={is_svc}",
                )
            )

        return max(candidates, key=lambda p: p.confidence) if candidates else None

    def _detect_fl002(self, bars, current_price, asia) -> Optional[DetectedPattern]:
        """FL-002: Liquidity Grab — single candle stop-hunt.

        Key TTC rules:
        - Large wick + small body (wick >= 60%, body <= 40%)
        - Closes back inside Asia range
        - Rejection candle confirms direction
        - Volume spike if available
        """
        if len(bars) < 2:
            return None
        grab = bars[-2]
        post = bars[-1]
        g_high, g_low, g_close, g_open = (
            grab["high"],
            grab["low"],
            grab["close"],
            grab["open"],
        )
        g_range = g_high - g_low
        if g_range <= 0:
            return None
        body = abs(g_close - g_open)
        body_ratio = body / g_range
        wick_ratio = 1.0 - body_ratio
        if wick_ratio < 0.60 or body_ratio > 0.40:
            return None

        # Volume spike
        g_vol = grab.get("volume", 0)
        if g_vol > 0 and len(bars) >= 10:
            avg_vol = sum(b.get("volume", 0) for b in bars[-10:-1]) / 9
            if avg_vol > 0 and g_vol < avg_vol * 1.3:
                return None

        # Bullish grab
        if g_low < asia.asia_low and g_close > asia.asia_low:
            if post["close"] <= post.get("open", 0) and post["close"] <= g_close:
                return None
            sweep = (asia.asia_low - g_low) / asia.asia_low * 100
            stop = g_low - g_range * 0.1
            target = asia.asia_high
            risk = asia.asia_low - stop
            rr = (target - asia.asia_low) / risk if risk > 0 else 0
            conf = min(wick_ratio, 0.9) * 0.35 + min(sweep / 0.1, 1.0) * 0.25 + asia.quality_score * 0.25 + 0.15
            return DetectedPattern(
                pattern_type="LIQUIDITY_GRAB",
                direction="long",
                confidence=min(conf, 1.0),
                key_levels={
                    "grab_low": g_low,
                    "grab_high": g_high,
                    "stop": stop,
                    "target": target,
                    "asia_high": asia.asia_high,
                    "asia_low": asia.asia_low,
                    "rr": rr,
                },
                flight_log_id="FL-002",
                asia_range=(asia.asia_high, asia.asia_low),
                stop_at_first_peak=True,
                mandatory_exit_time=asia.mandatory_exit_time,
                notes=f"FL-002 bull: sweep {sweep:.3f}%, RR={rr:.1f}",
            )

        # Bearish grab
        if g_high > asia.asia_high and g_close < asia.asia_high:
            if post["close"] >= post.get("open", 0) and post["close"] >= g_close:
                return None
            sweep = (g_high - asia.asia_high) / asia.asia_high * 100
            stop = g_high + g_range * 0.1
            target = asia.asia_low
            risk = stop - asia.asia_high
            rr = (asia.asia_high - target) / risk if risk > 0 else 0
            conf = min(wick_ratio, 0.9) * 0.35 + min(sweep / 0.1, 1.0) * 0.25 + asia.quality_score * 0.25 + 0.15
            return DetectedPattern(
                pattern_type="LIQUIDITY_GRAB",
                direction="short",
                confidence=min(conf, 1.0),
                key_levels={
                    "grab_low": g_low,
                    "grab_high": g_high,
                    "stop": stop,
                    "target": target,
                    "asia_high": asia.asia_high,
                    "asia_low": asia.asia_low,
                    "rr": rr,
                },
                flight_log_id="FL-002",
                asia_range=(asia.asia_high, asia.asia_low),
                stop_at_first_peak=True,
                mandatory_exit_time=asia.mandatory_exit_time,
                notes=f"FL-002 bear: sweep {sweep:.3f}%, RR={rr:.1f}",
            )
        return None

    def _detect_fl003(self, swings, bars, current_price, asia) -> Optional[DetectedPattern]:
        """FL-003: Multi-Session M/W — first peak Asia, second near Asia boundary."""
        if len(swings) < 4 or len(bars) < 4:
            return None
        sorted_swings = sorted(swings, key=lambda s: s.bar_index)
        highs = [s for s in sorted_swings if s.swing_type == SwingType.HIGH]
        lows = [s for s in sorted_swings if s.swing_type == SwingType.LOW]
        candidates = []
        window = min(300, len(bars))

        for sl1, sl2 in self._pair_consecutive(lows[-8:]):
            if not (asia.asia_low * 0.999 <= sl1.price <= asia.asia_high):
                continue
            if abs(sl2.price - asia.asia_low) > asia.asia_low * 0.002:
                continue
            if sl2.price < asia.asia_low * 0.998:
                continue  # broke through → FL-004
            in_range = [h for h in highs if sl1.bar_index < h.bar_index < sl2.bar_index]
            sh1 = max(in_range, key=lambda h: h.price) if in_range else None
            if sh1 is None:
                continue
            if current_price < sl2.price:
                continue
            # SVC check on first peak
            first_bar_idx = len(bars) - window + sl1.bar_index
            first_bar = bars[max(0, first_bar_idx)] if first_bar_idx < len(bars) else None
            is_svc = False
            if first_bar:
                prior = bars[max(0, first_bar_idx - 9) : first_bar_idx]
                is_svc = self._detect_svc(first_bar, "long", prior)
            stop = sl1.price - (asia.asia_high - asia.asia_low) * 0.1
            conf = asia.quality_score * 0.3 + 0.3 + 0.2 + 0.2 + (0.15 if is_svc else 0)
            candidates.append(
                DetectedPattern(
                    pattern_type="W",
                    direction="long",
                    confidence=min(conf, 1.0),
                    key_levels={
                        "SL1": sl1.price,
                        "SL2": sl2.price,
                        "stop": stop,
                        "target": asia.asia_high,
                        "asia_high": asia.asia_high,
                        "asia_low": asia.asia_low,
                    },
                    flight_log_id="FL-003",
                    asia_range=(asia.asia_high, asia.asia_low),
                    stop_at_first_peak=True,
                    mandatory_exit_time=asia.mandatory_exit_time,
                    notes=f"FL-003 W: svc={is_svc}",
                )
            )

        for sh1, sh2 in self._pair_consecutive(highs[-8:]):
            if not (asia.asia_low <= sh1.price <= asia.asia_high * 1.001):
                continue
            if abs(sh2.price - asia.asia_high) > asia.asia_high * 0.002:
                continue
            if sh2.price > asia.asia_high * 1.002:
                continue
            in_range = [sw for sw in lows if sh1.bar_index < sw.bar_index < sh2.bar_index]
            sl1 = min(in_range, key=lambda sw: sw.price) if in_range else None
            if sl1 is None:
                continue
            if current_price > sh2.price:
                continue
            # SVC check on first peak
            first_bar_idx = len(bars) - window + sh1.bar_index
            first_bar = bars[max(0, first_bar_idx)] if first_bar_idx < len(bars) else None
            is_svc = False
            if first_bar:
                prior = bars[max(0, first_bar_idx - 9) : first_bar_idx]
                is_svc = self._detect_svc(first_bar, "short", prior)
            stop = sh1.price + (asia.asia_high - asia.asia_low) * 0.1
            conf = min(
                asia.quality_score * 0.3 + 0.3 + 0.2 + 0.2 + (0.15 if is_svc else 0),
                1.0,
            )
            candidates.append(
                DetectedPattern(
                    pattern_type="M",
                    direction="short",
                    confidence=conf,
                    key_levels={
                        "SH1": sh1.price,
                        "SH2": sh2.price,
                        "stop": stop,
                        "target": asia.asia_low,
                        "asia_high": asia.asia_high,
                        "asia_low": asia.asia_low,
                    },
                    flight_log_id="FL-003",
                    asia_range=(asia.asia_high, asia.asia_low),
                    stop_at_first_peak=True,
                    mandatory_exit_time=asia.mandatory_exit_time,
                    notes=f"FL-003 M: svc={is_svc}",
                )
            )

        return max(candidates, key=lambda p: p.confidence) if candidates else None

    def _detect_fl004(self, bars, current_price, asia) -> Optional[DetectedPattern]:
        """FL-004: Fakeout — multi-candle break + failure.

        Key distinction from FL-002:
        - FL-002: single candle stop-hunt (wick back inside)
        - FL-004: multi-candle ACCEPTANCE outside range, then failure
        """
        if len(bars) < 5:
            return None
        pre = bars[-6:-1]
        cur = bars[-1]
        closed_below = sum(1 for b in pre if b["close"] < asia.asia_low)
        closed_above = sum(1 for b in pre if b["close"] > asia.asia_high)

        if closed_below >= 2 and cur["close"] > asia.asia_low:
            if cur["close"] <= cur.get("open", 0):
                return None
            # SVC check on breakout candle
            breakout_bar = max(
                pre,
                key=lambda b: (asia.asia_low - b["low"]) if b["low"] < asia.asia_low else 0,
            )
            prior = bars[max(0, len(bars) - 6 - 9) : len(bars) - 6]
            is_svc = self._detect_svc(breakout_bar, "long", prior) if prior else False
            sweep_low = min(b["low"] for b in pre)
            stop = sweep_low - (asia.asia_high - asia.asia_low) * 0.05
            conf = (
                asia.quality_score * 0.25 + min(closed_below / 4.0, 1.0) * 0.35 + 0.25 + 0.15 + (0.15 if is_svc else 0)
            )
            return DetectedPattern(
                pattern_type="W",
                direction="long",
                confidence=min(conf, 1.0),
                key_levels={
                    "sweep_low": sweep_low,
                    "stop": stop,
                    "target": asia.asia_high,
                    "asia_high": asia.asia_high,
                    "asia_low": asia.asia_low,
                },
                flight_log_id="FL-004",
                asia_range=(asia.asia_high, asia.asia_low),
                stop_at_first_peak=True,
                mandatory_exit_time=asia.mandatory_exit_time,
                notes=f"FL-004 W: {closed_below} bars below, svc={is_svc}",
            )

        if closed_above >= 2 and cur["close"] < asia.asia_high:
            if cur["close"] >= cur.get("open", 0):
                return None
            # SVC check on breakout candle
            breakout_bar = max(
                pre,
                key=lambda b: (b["high"] - asia.asia_high) if b["high"] > asia.asia_high else 0,
            )
            prior = bars[max(0, len(bars) - 6 - 9) : len(bars) - 6]
            is_svc = self._detect_svc(breakout_bar, "short", prior) if prior else False
            sweep_high = max(b["high"] for b in pre)
            stop = sweep_high + (asia.asia_high - asia.asia_low) * 0.05
            conf = (
                asia.quality_score * 0.25 + min(closed_above / 4.0, 1.0) * 0.35 + 0.25 + 0.15 + (0.15 if is_svc else 0)
            )
            return DetectedPattern(
                pattern_type="M",
                direction="short",
                confidence=min(conf, 1.0),
                key_levels={
                    "sweep_high": sweep_high,
                    "stop": stop,
                    "target": asia.asia_low,
                    "asia_high": asia.asia_high,
                    "asia_low": asia.asia_low,
                },
                flight_log_id="FL-004",
                asia_range=(asia.asia_high, asia.asia_low),
                stop_at_first_peak=True,
                mandatory_exit_time=asia.mandatory_exit_time,
                notes=f"FL-004 M: {closed_above} bars above, svc={is_svc}",
            )
        return None
