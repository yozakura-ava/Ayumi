from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..engine import Bar


@dataclass
class VolumeDeltaResult:
    current_delta: float
    rolling_avg_delta: float
    delta_ratio: float
    is_high_volume: bool
    is_low_volume: bool
    volume_percentile: float


class VolumeDeltaAnalyzer:
    def __init__(
        self,
        rolling_window: int = 20,
        high_volume_percentile: float = 75.0,
        low_volume_percentile: float = 25.0,
    ):
        self._window = rolling_window
        self._high_pct = high_volume_percentile
        self._low_pct = low_volume_percentile

    def analyze(self, bars: List[Bar]) -> Optional[VolumeDeltaResult]:
        if len(bars) < self._window + 1:
            return None

        recent = bars[-self._window :]
        current = bars[-1]

        current_delta = current.close - current.open

        deltas = [b.close - b.open for b in recent]
        rolling_avg = sum(deltas) / len(deltas)

        abs_avg = sum(abs(d) for d in deltas) / len(deltas)
        delta_ratio = current_delta / abs_avg if abs_avg > 0 else 0.0

        volumes = [b.volume for b in recent]
        current_vol = current.volume
        sorted_vols = sorted(volumes)
        rank = sum(1 for v in sorted_vols if v < current_vol)
        tied = sum(1 for v in sorted_vols if v == current_vol)
        rank += tied // 2
        volume_percentile = (rank / len(sorted_vols)) * 100.0

        return VolumeDeltaResult(
            current_delta=current_delta,
            rolling_avg_delta=rolling_avg,
            delta_ratio=delta_ratio,
            is_high_volume=volume_percentile >= self._high_pct,
            is_low_volume=volume_percentile <= self._low_pct,
            volume_percentile=volume_percentile,
        )

    def score_sweep_conviction(self, result: VolumeDeltaResult, sweep_direction: str) -> float:
        if result is None:
            return 0.5

        score = 0.5

        if sweep_direction == "long":
            if result.current_delta > 0:
                score += 0.2
            if result.is_high_volume:
                score += 0.2
            elif result.is_low_volume:
                score -= 0.3
        elif sweep_direction == "short":
            if result.current_delta < 0:
                score += 0.2
            if result.is_high_volume:
                score += 0.2
            elif result.is_low_volume:
                score -= 0.3

        return max(0.0, min(1.0, score))

    def score_fvg_strength(self, result: VolumeDeltaResult) -> float:
        if result is None:
            return 0.5

        score = 0.5

        if result.is_high_volume:
            score += 0.3
        elif result.is_low_volume:
            score -= 0.3

        if abs(result.delta_ratio) > 1.5:
            score += 0.2

        return max(0.0, min(1.0, score))

    def overall_volume_score(self, result: VolumeDeltaResult) -> float:
        if result is None:
            return 0.5

        if result.is_high_volume:
            return 0.8
        if result.is_low_volume:
            return 0.2
        return 0.5
