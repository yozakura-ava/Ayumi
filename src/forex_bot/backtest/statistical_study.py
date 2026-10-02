from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .engine import Bar, SessionType, determine_session


@dataclass(frozen=True)
class GoNoGoCriteria:
    metric: str
    threshold: float
    operator: str = ">="
    weight: float = 1.0

    def evaluate(self, value: float) -> bool:
        ops = {
            ">=": lambda v, t: v >= t,
            "<=": lambda v, t: v <= t,
            ">": lambda v, t: v > t,
            "<": lambda v, t: v < t,
            "==": lambda v, t: abs(v - t) < 1e-9,
        }
        fn = ops.get(self.operator)
        if fn is None:
            raise ValueError(f"Unknown operator: {self.operator}")
        return fn(value, self.threshold)


@dataclass
class CriterionResult:
    metric: str
    value: float
    threshold: float
    operator: str
    passed: bool
    weight: float


@dataclass
class StatisticalStudyResult:
    question: str
    test_period_start: str
    test_period_end: str
    instrument: str
    timeframe: str
    sample_size: int
    results: dict[str, Any] = field(default_factory=dict)
    pass_fail: list[CriterionResult] = field(default_factory=list)
    go_nogo: bool = False
    notes: str = ""

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "test_period": f"{self.test_period_start} to {self.test_period_end}",
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "sample_size": self.sample_size,
            "results": self.results,
            "pass": self.go_nogo,
            "criteria": [
                {
                    "metric": c.metric,
                    "value": c.value,
                    "threshold": c.threshold,
                    "operator": c.operator,
                    "passed": c.passed,
                    "weight": c.weight,
                }
                for c in self.pass_fail
            ],
            "notes": self.notes,
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())


class StatisticalStudy(ABC):
    def __init__(
        self,
        question_id: str,
        instrument: str = "EURUSD",
        timeframe: str = "H1",
        go_nogo_criteria: list[GoNoGoCriteria] | None = None,
    ):
        self.question_id = question_id
        self.instrument = instrument
        self.timeframe = timeframe
        self.go_nogo_criteria = go_nogo_criteria or []

    @abstractmethod
    def analyze(self, bars: list[Bar]) -> dict[str, Any]:
        pass

    def run(self, bars: list[Bar]) -> StatisticalStudyResult:
        if not bars:
            return StatisticalStudyResult(
                question=self.question_id,
                test_period_start="",
                test_period_end="",
                instrument=self.instrument,
                timeframe=self.timeframe,
                sample_size=0,
                results={},
                go_nogo=False,
                notes="No data provided",
            )

        raw_results = self.analyze(bars)
        sample_size = raw_results.pop("sample_size", len(bars))

        criterion_results = []
        for criterion in self.go_nogo_criteria:
            value = raw_results.get(criterion.metric, 0.0)
            passed = criterion.evaluate(value)
            criterion_results.append(
                CriterionResult(
                    metric=criterion.metric,
                    value=value,
                    threshold=criterion.threshold,
                    operator=criterion.operator,
                    passed=passed,
                    weight=criterion.weight,
                )
            )

        total_weight = sum(c.weight for c in self.go_nogo_criteria)
        passed_weight = sum(c.weight for c in criterion_results if c.passed)
        go_nogo = total_weight > 0 and passed_weight / total_weight >= 0.5

        notes_parts = []
        for cr in criterion_results:
            status = "PASS" if cr.passed else "FAIL"
            notes_parts.append(f"{cr.metric}: {cr.value:.4f} {cr.operator} {cr.threshold} -> {status}")
        notes = "; ".join(notes_parts)

        return StatisticalStudyResult(
            question=self.question_id,
            test_period_start=str(bars[0].time),
            test_period_end=str(bars[-1].time),
            instrument=self.instrument,
            timeframe=self.timeframe,
            sample_size=sample_size,
            results=raw_results,
            pass_fail=criterion_results,
            go_nogo=go_nogo,
            notes=notes,
        )

    def filter_by_session(self, bars: list[Bar], session: SessionType) -> list[Bar]:
        return [b for b in bars if determine_session(b.time) == session]

    def filter_by_day_of_week(self, bars: list[Bar], day: int) -> list[Bar]:
        return [b for b in bars if b.time.weekday() == day]

    def filter_by_date_range(self, bars: list[Bar], start: datetime, end: datetime) -> list[Bar]:
        return [b for b in bars if start <= b.time <= end]

    def group_by_session(self, bars: list[Bar]) -> dict[SessionType, list[Bar]]:
        groups: dict[SessionType, list[Bar]] = {s: [] for s in SessionType}
        for b in bars:
            groups[determine_session(b.time)].append(b)
        return groups

    def group_by_day(self, bars: list[Bar]) -> dict[str, list[Bar]]:
        groups: dict[str, list[Bar]] = {}
        for b in bars:
            day_key = b.time.strftime("%Y-%m-%d")
            groups.setdefault(day_key, []).append(b)
        return groups

    @staticmethod
    def compute_atr(bars: list[Bar], period: int = 14) -> list[float]:
        atr_values = []
        for i in range(len(bars)):
            lookback = min(period, i + 1)
            if lookback < 2:
                atr_values.append(0.0001)
                continue
            tr_sum = 0.0
            count = 0
            for k in range(i - lookback + 1, i + 1):
                if k > 0:
                    tr = max(
                        bars[k].high - bars[k].low,
                        abs(bars[k].high - bars[k - 1].close),
                        abs(bars[k].low - bars[k - 1].close),
                    )
                    tr_sum += tr
                    count += 1
            atr_values.append(tr_sum / count if count > 0 else 0.0001)
        return atr_values

    @staticmethod
    def pip_value(price: float) -> float:
        if price >= 50:
            return 0.01
        elif price >= 1:
            return 0.0001
        return 0.00000001

    @staticmethod
    def price_to_pips(price: float, ref_price: float) -> float:
        pv = StatisticalStudy.pip_value(ref_price)
        return price / pv
