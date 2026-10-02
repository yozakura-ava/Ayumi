from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .engine import Bar, SessionType, determine_session
from .statistical_study import StatisticalStudy


@dataclass(frozen=True)
class ConsolidationMetrics:
    duration_bars: int = 0
    duration_hours: float = 0.0
    range_pips: float = 0.0
    meets_min_duration: bool = False
    meets_max_range: bool = False
    passes_filter: bool = False


@dataclass(frozen=True)
class MWPattern:
    pattern_type: str
    left_shoulder_idx: int
    left_shoulder_price: float
    neckline_start_idx: int
    neckline_start_price: float
    valley_peak_idx: int
    valley_peak_price: float
    neckline_end_idx: int
    neckline_end_price: float
    right_shoulder_idx: int
    right_shoulder_price: float
    neckline_level: float
    depth_pips: float
    formation_start_time: datetime
    formation_end_time: datetime
    sessions: list[SessionType]
    bar_count: int
    atr_at_formation: float = 0.0
    consolidation: ConsolidationMetrics = field(default_factory=ConsolidationMetrics)

    @property
    def is_bullish(self) -> bool:
        return self.pattern_type == "W"

    @property
    def is_bearish(self) -> bool:
        return self.pattern_type == "M"

    @property
    def neckline_break_distance(self) -> float:
        if self.is_bullish:
            return self.left_shoulder_price - self.neckline_level
        return self.neckline_level - self.left_shoulder_price

    @property
    def is_multi_session(self) -> bool:
        return self.sessions_spanned_count() >= 2

    @property
    def session_span_quality_score(self) -> float:
        span = self.sessions_spanned_count()
        if span >= 3:
            return 1.0
        if span == 2:
            return 0.85
        return 0.55

    def sessions_spanned_count(self) -> int:
        unique = set(self.sessions)
        unique.discard(SessionType.OUTSIDE)
        return len(unique)


class ConsolidationFilter:
    def __init__(
        self,
        min_duration_hours: float = 4.0,
        max_range_pips: float = 20.0,
        bar_period_minutes: int = 60,
        enabled: bool = True,
    ):
        self.min_duration_hours = min_duration_hours
        self.max_range_pips = max_range_pips
        self.bar_period_minutes = bar_period_minutes
        self.enabled = enabled
        self.min_duration_bars = int(min_duration_hours * (60 / bar_period_minutes))

    def measure(self, bars: list[Bar], pattern_start_idx: int) -> ConsolidationMetrics:
        if not self.enabled or pattern_start_idx < 1:
            return ConsolidationMetrics()

        scan_start = max(0, pattern_start_idx - self.min_duration_bars * 3)
        pre_bars = bars[scan_start:pattern_start_idx]

        if len(pre_bars) < 2:
            return ConsolidationMetrics()

        pip_val = StatisticalStudy.pip_value(pre_bars[-1].close)
        pre_high = max(b.high for b in pre_bars)
        pre_low = min(b.low for b in pre_bars)
        range_pips = (pre_high - pre_low) / pip_val

        atr_values = StatisticalStudy.compute_atr(pre_bars)
        avg_atr = atr_values[-1] if atr_values else 0.0001
        tight_threshold = max(avg_atr * 0.75, pre_bars[-1].close * 0.0005)

        consolidation_start = pattern_start_idx
        for i in range(len(pre_bars) - 1, -1, -1):
            bar_range = pre_bars[i].high - pre_bars[i].low
            if bar_range > tight_threshold:
                consolidation_start = scan_start + i + 1
                break

        if consolidation_start == pattern_start_idx and len(pre_bars) >= self.min_duration_bars:
            consolidation_start = scan_start

        duration_bars = pattern_start_idx - consolidation_start
        duration_hours = duration_bars * (self.bar_period_minutes / 60.0)
        meets_min_duration = duration_bars >= self.min_duration_bars
        meets_max_range = range_pips <= self.max_range_pips
        passes_filter = meets_min_duration and meets_max_range

        return ConsolidationMetrics(
            duration_bars=duration_bars,
            duration_hours=round(duration_hours, 1),
            range_pips=round(range_pips, 1),
            meets_min_duration=meets_min_duration,
            meets_max_range=meets_max_range,
            passes_filter=passes_filter,
        )


class MWPatternDetector:
    def __init__(
        self,
        swing_lookback: int = 3,
        symmetry_tolerance: float = 0.30,
        min_depth_atr: float = 0.5,
        min_bar_span: int = 10,
        max_bar_span: int = 200,
        consolidation_filter: ConsolidationFilter | None = None,
    ):
        self.swing_lookback = swing_lookback
        self.symmetry_tolerance = symmetry_tolerance
        self.min_depth_atr = min_depth_atr
        self.min_bar_span = min_bar_span
        self.max_bar_span = max_bar_span
        self.consolidation_filter = consolidation_filter

    def detect(self, bars: list[Bar], atr_values: list[float] | None = None) -> list[MWPattern]:
        if len(bars) < self.swing_lookback * 2 + 5:
            return []

        if atr_values is None:
            atr_values = StatisticalStudy.compute_atr(bars)

        swing_highs = self._find_swing_highs(bars)
        swing_lows = self._find_swing_lows(bars)

        patterns: list[MWPattern] = []

        w_patterns = self._detect_w_patterns(bars, swing_lows, swing_highs, atr_values)
        m_patterns = self._detect_m_patterns(bars, swing_highs, swing_lows, atr_values)

        patterns.extend(w_patterns)
        patterns.extend(m_patterns)

        if self.consolidation_filter and self.consolidation_filter.enabled:
            patterns = [p for p in patterns if p.consolidation.passes_filter]

        patterns.sort(key=lambda p: p.formation_start_time)
        return self._remove_overlapping(patterns)

    def _find_swing_highs(self, bars: list[Bar]) -> list[tuple[int, float]]:
        results = []
        for i in range(self.swing_lookback, len(bars) - self.swing_lookback):
            is_high = True
            for j in range(1, self.swing_lookback + 1):
                if bars[i].high <= bars[i - j].high or bars[i].high <= bars[i + j].high:
                    is_high = False
                    break
            if is_high:
                results.append((i, bars[i].high))
        return results

    def _find_swing_lows(self, bars: list[Bar]) -> list[tuple[int, float]]:
        results = []
        for i in range(self.swing_lookback, len(bars) - self.swing_lookback):
            is_low = True
            for j in range(1, self.swing_lookback + 1):
                if bars[i].low >= bars[i - j].low or bars[i].low >= bars[i + j].low:
                    is_low = False
                    break
            if is_low:
                results.append((i, bars[i].low))
        return results

    def _detect_w_patterns(
        self,
        bars: list[Bar],
        swing_lows: list[tuple[int, float]],
        swing_highs: list[tuple[int, float]],
        atr_values: list[float],
    ) -> list[MWPattern]:
        patterns = []
        for i in range(1, len(swing_lows)):
            left_idx, left_price = swing_lows[i - 1]
            right_idx, right_price = swing_lows[i]

            span = right_idx - left_idx
            if span < self.min_bar_span or span > self.max_bar_span:
                continue

            avg_atr = atr_values[(left_idx + right_idx) // 2]
            depth = self._find_valley_between(bars, left_idx, right_idx, swing_highs) - left_price
            if avg_atr > 0 and depth / avg_atr < self.min_depth_atr:
                continue

            price_diff = abs(left_price - right_price)
            avg_price = (left_price + right_price) / 2
            symmetry = price_diff / avg_price if avg_price > 0 else 1.0

            if symmetry > self.symmetry_tolerance:
                continue

            peak_idx, peak_price = self._find_highest_between(bars, left_idx, right_idx)

            neckline_level = (left_price + right_price) / 2
            sessions = [determine_session(bars[k].time) for k in range(left_idx, right_idx + 1)]

            depth_pips = depth / StatisticalStudy.pip_value(bars[left_idx].close)

            consolidation = ConsolidationMetrics()
            if self.consolidation_filter:
                consolidation = self.consolidation_filter.measure(bars, left_idx)

            patterns.append(
                MWPattern(
                    pattern_type="W",
                    left_shoulder_idx=left_idx,
                    left_shoulder_price=left_price,
                    neckline_start_idx=left_idx,
                    neckline_start_price=left_price,
                    valley_peak_idx=peak_idx,
                    valley_peak_price=peak_price,
                    neckline_end_idx=right_idx,
                    neckline_end_price=right_price,
                    right_shoulder_idx=right_idx,
                    right_shoulder_price=right_price,
                    neckline_level=neckline_level,
                    depth_pips=depth_pips,
                    formation_start_time=bars[left_idx].time,
                    formation_end_time=bars[right_idx].time,
                    sessions=sessions,
                    bar_count=span + 1,
                    atr_at_formation=avg_atr,
                    consolidation=consolidation,
                )
            )

        return patterns

    def _detect_m_patterns(
        self,
        bars: list[Bar],
        swing_highs: list[tuple[int, float]],
        swing_lows: list[tuple[int, float]],
        atr_values: list[float],
    ) -> list[MWPattern]:
        patterns = []
        for i in range(1, len(swing_highs)):
            left_idx, left_price = swing_highs[i - 1]
            right_idx, right_price = swing_highs[i]

            span = right_idx - left_idx
            if span < self.min_bar_span or span > self.max_bar_span:
                continue

            avg_atr = atr_values[(left_idx + right_idx) // 2]
            depth = left_price - self._find_peak_between(bars, left_idx, right_idx, swing_lows)
            if avg_atr > 0 and depth / avg_atr < self.min_depth_atr:
                continue

            price_diff = abs(left_price - right_price)
            avg_price = (left_price + right_price) / 2
            symmetry = price_diff / avg_price if avg_price > 0 else 1.0

            if symmetry > self.symmetry_tolerance:
                continue

            peak_idx, peak_price = self._find_lowest_between(bars, left_idx, right_idx)

            neckline_level = (left_price + right_price) / 2
            sessions = [determine_session(bars[k].time) for k in range(left_idx, right_idx + 1)]

            depth_pips = depth / StatisticalStudy.pip_value(bars[left_idx].close)

            consolidation = ConsolidationMetrics()
            if self.consolidation_filter:
                consolidation = self.consolidation_filter.measure(bars, left_idx)

            patterns.append(
                MWPattern(
                    pattern_type="M",
                    left_shoulder_idx=left_idx,
                    left_shoulder_price=left_price,
                    neckline_start_idx=left_idx,
                    neckline_start_price=left_price,
                    valley_peak_idx=peak_idx,
                    valley_peak_price=peak_price,
                    neckline_end_idx=right_idx,
                    neckline_end_price=right_price,
                    right_shoulder_idx=right_idx,
                    right_shoulder_price=right_price,
                    neckline_level=neckline_level,
                    depth_pips=depth_pips,
                    formation_start_time=bars[left_idx].time,
                    formation_end_time=bars[right_idx].time,
                    sessions=sessions,
                    bar_count=span + 1,
                    atr_at_formation=avg_atr,
                    consolidation=consolidation,
                )
            )

        return patterns

    def _find_valley_between(
        self,
        bars: list[Bar],
        start: int,
        end: int,
        swing_highs: list[tuple[int, float]],
    ) -> float:
        relevant = [(idx, price) for idx, price in swing_highs if start < idx < end]
        if not relevant:
            return max(bars[i].high for i in range(start, end + 1))
        return max(price for _, price in relevant)

    def _find_peak_between(
        self,
        bars: list[Bar],
        start: int,
        end: int,
        swing_lows: list[tuple[int, float]],
    ) -> float:
        relevant = [(idx, price) for idx, price in swing_lows if start < idx < end]
        if not relevant:
            return min(bars[i].low for i in range(start, end + 1))
        return min(price for _, price in relevant)

    def _find_lowest_between(self, bars: list[Bar], start: int, end: int) -> tuple[int, float]:
        min_idx = start
        min_price = bars[start].low
        for i in range(start, end + 1):
            if bars[i].low < min_price:
                min_price = bars[i].low
                min_idx = i
        return min_idx, min_price

    def _find_highest_between(self, bars: list[Bar], start: int, end: int) -> tuple[int, float]:
        max_idx = start
        max_price = bars[start].high
        for i in range(start, end + 1):
            if bars[i].high > max_price:
                max_price = bars[i].high
                max_idx = i
        return max_idx, max_price

    def _remove_overlapping(self, patterns: list[MWPattern]) -> list[MWPattern]:
        if not patterns:
            return []
        non_overlapping = [patterns[0]]
        for p in patterns[1:]:
            last = non_overlapping[-1]
            if p.formation_start_time > last.formation_end_time:
                non_overlapping.append(p)
        return non_overlapping
