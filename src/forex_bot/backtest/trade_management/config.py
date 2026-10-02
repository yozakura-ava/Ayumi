from dataclasses import dataclass, field
from enum import Enum


class TrailingStopMethod(Enum):
    ATR = "atr"
    STEP = "step"
    TIME = "time"
    PARABOLIC_SAR = "parabolic_sar"


@dataclass
class PartialExitConfig:
    enabled: bool = True
    tiers: list[tuple] = field(
        default_factory=lambda: [
            (0.5, 1.0, True),
            (0.75, 2.0, False),
        ]
    )
    final_trail: bool = True


@dataclass
class TrailingStopConfig:
    enabled: bool = True
    method: TrailingStopMethod = TrailingStopMethod.ATR
    atr_multiplier: float = 1.5
    step_pips: float = 10.0
    time_tighten_bars: int = 24
    time_tighten_pct: float = 0.5
    sar_af_start: float = 0.02
    sar_af_increment: float = 0.02
    sar_af_max: float = 0.2
    only_after_tier: int = 0


@dataclass
class SessionFilterConfig:
    enabled: bool = True
    allow_entry_sessions: list[str] = field(
        default_factory=lambda: [
            "london",
            "london_open",
            "ny_open",
            "ny_am",
            "ny_pm",
            "asian",
        ]
    )
    hold_through_sessions: bool = True
    weekend_close_hour_utc: int = 21
    weekend_close_minute_utc: int = 55
    news_buffer_bars: int = 2
    news_buffer_on_entry: bool = True


@dataclass
class ExitRefinementConfig:
    enabled: bool = True
    max_bars_to_tp1: int = 48
    momentum_exit_enabled: bool = True
    momentum_lookback: int = 5
    momentum_reversal_threshold: float = 0.6
    spread_filter_enabled: bool = True
    max_spread_atr_pct: float = 0.15


@dataclass
class TradeManagementConfig:
    partial_exit: PartialExitConfig = field(default_factory=PartialExitConfig)
    trailing_stop: TrailingStopConfig = field(default_factory=TrailingStopConfig)
    session_filter: SessionFilterConfig = field(default_factory=SessionFilterConfig)
    exit_refinement: ExitRefinementConfig = field(default_factory=ExitRefinementConfig)
    pair: str = "EURUSD"
    timeframe_minutes: int = 60

    @classmethod
    def conservative(cls, pair: str = "EURUSD", timeframe_minutes: int = 60) -> "TradeManagementConfig":
        return cls(
            partial_exit=PartialExitConfig(
                enabled=True,
                tiers=[(0.5, 1.0, True), (0.75, 1.5, False)],
                final_trail=True,
            ),
            trailing_stop=TrailingStopConfig(
                enabled=True,
                method=TrailingStopMethod.ATR,
                atr_multiplier=2.0,
                step_pips=15.0,
                time_tighten_bars=36,
                time_tighten_pct=0.4,
                only_after_tier=0,
            ),
            session_filter=SessionFilterConfig(
                enabled=True,
                news_buffer_bars=6,
            ),
            exit_refinement=ExitRefinementConfig(
                max_bars_to_tp1=36,
                momentum_reversal_threshold=0.5,
                max_spread_atr_pct=0.1,
            ),
            pair=pair,
            timeframe_minutes=timeframe_minutes,
        )

    @classmethod
    def aggressive(cls, pair: str = "EURUSD", timeframe_minutes: int = 60) -> "TradeManagementConfig":
        return cls(
            partial_exit=PartialExitConfig(
                enabled=True,
                tiers=[(0.4, 0.75, True), (0.7, 1.5, False)],
                final_trail=True,
            ),
            trailing_stop=TrailingStopConfig(
                enabled=True,
                method=TrailingStopMethod.ATR,
                atr_multiplier=1.0,
                step_pips=5.0,
                time_tighten_bars=12,
                time_tighten_pct=0.6,
                only_after_tier=0,
            ),
            session_filter=SessionFilterConfig(
                enabled=True,
                news_buffer_bars=2,
            ),
            exit_refinement=ExitRefinementConfig(
                max_bars_to_tp1=24,
                momentum_reversal_threshold=0.7,
                max_spread_atr_pct=0.2,
            ),
            pair=pair,
            timeframe_minutes=timeframe_minutes,
        )

    @classmethod
    def ftmo(cls, pair: str = "EURUSD", timeframe_minutes: int = 60) -> "TradeManagementConfig":
        return cls(
            partial_exit=PartialExitConfig(
                enabled=True,
                tiers=[(0.5, 1.0, True), (0.75, 2.0, False)],
                final_trail=True,
            ),
            trailing_stop=TrailingStopConfig(
                enabled=True,
                method=TrailingStopMethod.ATR,
                atr_multiplier=1.5,
                step_pips=10.0,
                time_tighten_bars=24,
                time_tighten_pct=0.5,
                only_after_tier=1,
            ),
            session_filter=SessionFilterConfig(
                enabled=True,
                news_buffer_bars=4,
                news_buffer_on_entry=True,
            ),
            exit_refinement=ExitRefinementConfig(
                max_bars_to_tp1=48,
                momentum_exit_enabled=True,
                momentum_reversal_threshold=0.6,
                spread_filter_enabled=True,
                max_spread_atr_pct=0.15,
            ),
            pair=pair,
            timeframe_minutes=timeframe_minutes,
        )
