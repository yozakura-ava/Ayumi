from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class TradeDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class SessionType(StrEnum):
    ASIAN = "asian"
    LONDON = "london"
    NY_AM = "ny_am"
    NY_PM = "ny_pm"
    OUTSIDE = "outside"


class TradeOutcome(StrEnum):
    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"
    OPEN = "open"


class ExitReason(StrEnum):
    TAKE_PROFIT_1 = "take_profit_1"
    TAKE_PROFIT_2 = "take_profit_2"
    TAKE_PROFIT_3 = "take_profit_3"
    STOP_LOSS = "stop_loss"
    SIGNAL_FLIP = "signal_flip"
    END_OF_DATA = "end_of_data"
    MAX_DAILY_LOSS = "max_daily_loss"
    TIME_STOP = "time_stop"
    MOMENTUM_REVERSAL = "momentum_reversal"
    WEEKEND_CLOSE = "weekend_close"
    TRAILING_STOP = "trailing_stop"


@dataclass(frozen=True)
class BarPeriod:
    minutes: int

    @classmethod
    def M15(cls) -> "BarPeriod":
        return cls(15)

    @classmethod
    def H1(cls) -> "BarPeriod":
        return cls(60)

    @classmethod
    def H4(cls) -> "BarPeriod":
        return cls(240)

    @classmethod
    def D1(cls) -> "BarPeriod":
        return cls(1440)


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    period: BarPeriod = field(default_factory=BarPeriod.H1)
    spread_pips: float = 0.0


@dataclass
class MarketState:
    bars: list[Bar]
    current_session: SessionType = SessionType.OUTSIDE
    # Optional H4 bars for cross-timeframe filtering (backward-compatible).
    # When provided, strategies can use H4 trend alignment as a confluence filter.
    h4_bars: list[Bar] | None = None

    @property
    def latest_bar(self) -> Bar:
        return self.bars[-1]

    @property
    def atr(self) -> float:
        if len(self.bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(self.bars) - 14, len(self.bars)):
            if i > 0:
                tr = max(
                    self.bars[i].high - self.bars[i].low,
                    max(
                        abs(self.bars[i].high - self.bars[i - 1].close),
                        abs(self.bars[i].low - self.bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14


@dataclass
class StrategySignal:
    direction: TradeDirection
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    rationale: str
    is_volatile: bool = False


@dataclass
class SimulatedTrade:
    entry_bar_index: int
    exit_bar_index: int | None = None
    direction: TradeDirection = TradeDirection.NEUTRAL
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    take_profit_3: float = 0.0
    exit_price: float = 0.0
    lot_size: float = 0.0
    risk_amount: float = 0.0
    pips: float = 0.0
    profit_loss: float = 0.0
    outcome: TradeOutcome = TradeOutcome.OPEN
    exit_reason: ExitReason | None = None
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    confidence_score: float = 0.0
    confluence_count: int = 0
    rationale: str = ""
