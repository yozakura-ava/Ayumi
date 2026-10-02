from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GridPoint:
    params: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(tuple(sorted(self.params.items())))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GridPoint):
            return NotImplemented
        return self.params == other.params


class ParameterGrid:
    def __init__(self, param_space: dict[str, list[Any]]):
        if not param_space:
            raise ValueError("param_space must not be empty")
        self._param_space = param_space
        self._names = sorted(param_space.keys())
        self._value_lists = [param_space[n] for n in self._names]

    @property
    def param_names(self) -> list[str]:
        return list(self._names)

    @property
    def size(self) -> int:
        if not self._value_lists:
            return 0
        result = 1
        for vals in self._value_lists:
            result *= len(vals)
        return result

    def __len__(self) -> int:
        return self.size

    def __iter__(self) -> Iterator[GridPoint]:
        for combo in itertools.product(*self._value_lists):
            yield GridPoint(params=dict(zip(self._names, combo)))  # noqa: B905

    def to_list(self) -> list[GridPoint]:
        return list(self)
