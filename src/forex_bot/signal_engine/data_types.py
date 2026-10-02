"""Shared dataclasses for the TTC Signal Engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class LevelType(str, Enum):
    """All counted level types in the TTC system."""

    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    D1 = "D1"
    D2 = "D2"
    D3 = "D3"
    SWL = "SWL"  # Swing low (unclassified)
    SWH = "SWH"  # Swing high (unclassified)
    AOI = "AOI"  # Area of interest


class SwingType(str, Enum):
    HIGH = "high"
    LOW = "low"


class HTFPhase(str, Enum):
    ALIGNED = "aligned"
    CONFLICTING = "conflicting"
    CONSOLIDATING = "consolidating"
    EXHAUSTION = "exhaustion"
    NEUTRAL = "neutral"


@dataclass
class Swing:
    bar_index: int
    price: float
    swing_type: SwingType


@dataclass
class Level:
    price: float
    level_type: LevelType
    magnitude: float
    bar_index: int
    completed: bool = False
    unrecovered: bool = True  # price hasn't yet reclaimed this level


@dataclass
class HTFState:
    phase: HTFPhase
    alignment_score: float
    ema_slope: float
    range_size: float


@dataclass
class SessionState:
    session_name: str
    kill_zone_active: bool
    phase_score: float
    directional_bias: Optional[str] = None  # "bullish", "bearish", or None


@dataclass
class Signal:
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: float
    gates_passed: list[str] = field(default_factory=list)
    boosters_active: list[str] = field(default_factory=list)
    pattern_type: str = ""
    timeframe: str = "H1"
    timestamp: Optional[datetime] = None
    reversal_score: float = 0.0
    quality_score: float = 0.0
    setup_type: str = ""
