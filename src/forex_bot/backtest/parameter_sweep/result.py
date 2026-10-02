from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SweepRow:
    params: dict[str, Any] = field(default_factory=dict)
    win_rate: float = 0.0
    max_dd: float = 0.0
    total_return: float = 0.0
    sharpe_ratio: float = 0.0
    trade_count: int = 0
    profit_factor: float = 0.0

    def get(self, key: str, default: float = 0.0) -> float:
        if key in self.params:
            return float(self.params[key])
        if hasattr(self, key):
            return float(getattr(self, key))
        return default


@dataclass
class SweepResult:
    rows: list[SweepRow] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def top_n(self, n: int, metric: str = "sharpe_ratio", ascending: bool = False) -> list[SweepRow]:
        sorted_rows = self.sort_by(metric, ascending=ascending)
        return sorted_rows[:n]

    def filter(self, predicate: Callable[[SweepRow], bool]) -> SweepResult:
        return SweepResult(rows=[r for r in self.rows if predicate(r)])

    def sort_by(self, metric: str, ascending: bool = False) -> list[SweepRow]:
        return sorted(
            self.rows,
            key=lambda row: row.get(metric, 0.0),
            reverse=not ascending,
        )

    def best(self, metric: str = "sharpe_ratio") -> SweepRow | None:
        sorted_rows = self.sort_by(metric)
        return sorted_rows[0] if sorted_rows else None
