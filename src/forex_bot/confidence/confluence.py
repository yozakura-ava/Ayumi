"""Confluence detection — measures when multiple strategies agree on a signal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.registry import StrategyRegistry


@dataclass
class ConfluenceResult:
    agreeing_strategies: list[str] = field(default_factory=list)
    agreeing_types: list[str] = field(default_factory=list)
    agreeing_timeframes: list[str] = field(default_factory=list)
    cross_type_agreement: bool = False
    confluence_score: float = 0.0


@dataclass
class _SignalRecord:
    strategy_id: str
    symbol: str
    direction: str
    confidence: float
    timestamp: datetime


class ConfluenceDetector:
    """Detects when multiple strategies agree on the same signal."""

    def __init__(self, registry: StrategyRegistry, window_minutes: int = 60) -> None:
        self._registry = registry
        self._window = timedelta(minutes=window_minutes)
        self._signals: list[_SignalRecord] = []

    def record_signal(
        self,
        strategy_id: str,
        symbol: str,
        direction: str,
        confidence: float,
        timestamp: datetime,
    ) -> None:
        """Record a signal for confluence tracking."""
        self._signals.append(
            _SignalRecord(
                strategy_id=strategy_id,
                symbol=symbol,
                direction=direction.lower(),
                confidence=confidence,
                timestamp=timestamp,
            )
        )
        # Prune old signals
        cutoff = timestamp - self._window
        self._signals = [s for s in self._signals if s.timestamp >= cutoff]

    def get_confluence(self, symbol: str, direction: str, timestamp: datetime) -> ConfluenceResult:
        """Check confluence for a given symbol/direction at a given time."""
        cutoff = timestamp - self._window
        direction = direction.lower()

        matching = [
            s
            for s in self._signals
            if s.symbol.upper() == symbol.upper() and s.direction == direction and s.timestamp >= cutoff
        ]

        if not matching:
            return ConfluenceResult()

        agreeing_ids: list[str] = []
        agreeing_types: list[str] = []
        agreeing_tfs: list[str] = []
        seen_ids: set[str] = set()
        seen_types: set[str] = set()
        seen_tfs: set[str] = set()

        for s in matching:
            if s.strategy_id not in seen_ids:
                seen_ids.add(s.strategy_id)
                agreeing_ids.append(s.strategy_id)

                config = self._registry.get(s.strategy_id)
                if config:
                    if config.strategy_type not in seen_types:
                        seen_types.add(config.strategy_type)
                        agreeing_types.append(config.strategy_type)
                    for tf in config.timeframes:
                        if tf not in seen_tfs:
                            seen_tfs.add(tf)
                            agreeing_tfs.append(tf)

        n_strategies = len(agreeing_ids)
        n_types = len(agreeing_types)
        cross_type = n_types > 1

        # Scoring
        if n_strategies <= 1:
            score = 0.0
        elif n_strategies == 2:
            score = 0.3 if n_types == 1 else 0.5
        else:  # 3+
            score = 0.7 if n_types == 1 else 0.9

        score = min(score, 1.0)

        return ConfluenceResult(
            agreeing_strategies=agreeing_ids,
            agreeing_types=agreeing_types,
            agreeing_timeframes=agreeing_tfs,
            cross_type_agreement=cross_type,
            confluence_score=score,
        )
