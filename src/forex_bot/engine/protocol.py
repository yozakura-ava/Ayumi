from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.types import TradeDirection


@dataclass(frozen=True)
class CanonicalSignal:
    strategy_id: str
    symbol: str
    direction: TradeDirection
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None = None
    take_profit_3: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    rationale: str = ""
    metadata: dict = field(default_factory=dict)
