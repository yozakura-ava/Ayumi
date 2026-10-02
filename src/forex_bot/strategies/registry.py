"""Central registry of all active strategies with metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StrategyConfig:
    strategy_id: str
    name: str
    strategy_type: str  # "mean_reversion", "momentum", "trend", "breakout"
    symbols: list[str]  # which pairs it trades
    timeframes: list[str]  # which timeframes
    typical_confidence_range: tuple[float, float]  # (min, max) expected
    active: bool = True


class StrategyRegistry:
    """Central registry of all active strategies with metadata."""

    def __init__(self) -> None:
        self._strategies: dict[str, StrategyConfig] = {}

    def register(self, config: StrategyConfig) -> None:
        """Register a strategy. Overwrites if ID already exists."""
        self._strategies[config.strategy_id] = config

    def get(self, strategy_id: str) -> StrategyConfig | None:
        """Get strategy config by ID."""
        return self._strategies.get(strategy_id)

    def get_for_symbol(self, symbol: str) -> list[StrategyConfig]:
        """Get all strategies that trade a given symbol."""
        return [
            s for s in self._strategies.values() if s.active and symbol.upper() in [sym.upper() for sym in s.symbols]
        ]

    def get_all(self) -> list[StrategyConfig]:
        """Get all registered strategies."""
        return list(self._strategies.values())

    def get_all_active(self) -> list[StrategyConfig]:
        """Get all active strategies."""
        return [s for s in self._strategies.values() if s.active]


def default_registry() -> StrategyRegistry:
    """Create a registry pre-loaded with all known strategies."""
    reg = StrategyRegistry()

    strategies = [
        StrategyConfig(
            strategy_id="srmr_plus",
            name="Session Range Mean Reversion Plus",
            strategy_type="mean_reversion",
            symbols=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"],
            timeframes=["H1"],
            typical_confidence_range=(0.40, 0.75),
        ),
        StrategyConfig(
            strategy_id="bb_rsi_reversion",
            name="Bollinger + RSI Reversion",
            strategy_type="mean_reversion",
            symbols=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
            timeframes=["H1", "M15"],
            typical_confidence_range=(0.35, 0.70),
        ),
        StrategyConfig(
            strategy_id="killzone_momentum",
            name="Killzone Momentum",
            strategy_type="momentum",
            symbols=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"],
            timeframes=["M15", "H1"],
            typical_confidence_range=(0.45, 0.80),
        ),
        StrategyConfig(
            strategy_id="momentum",
            name="Raw Momentum",
            strategy_type="momentum",
            symbols=["EURUSD"],
            timeframes=["M15"],
            typical_confidence_range=(0.50, 0.80),
        ),
        StrategyConfig(
            strategy_id="mtf_filtered_momentum",
            name="Multi-Timeframe Filtered Momentum",
            strategy_type="momentum",
            symbols=["EURUSD", "GBPUSD", "USDJPY"],
            timeframes=["M15", "H1", "H4"],
            typical_confidence_range=(0.50, 0.85),
        ),
        StrategyConfig(
            strategy_id="session_range_mr_ict_filtered",
            name="ICT-Filtered Session Range MR",
            strategy_type="mean_reversion",
            symbols=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"],
            timeframes=["H1", "H4"],
            typical_confidence_range=(0.45, 0.80),
        ),
        StrategyConfig(
            strategy_id="usdjpy_d1_trend",
            name="USDJPY D1 Trend-Following",
            strategy_type="trend",
            symbols=["USDJPY"],
            timeframes=["D1"],
            typical_confidence_range=(0.55, 0.85),
        ),
        StrategyConfig(
            strategy_id="session_range_mean_reversion",
            name="Session Range Mean Reversion",
            strategy_type="mean_reversion",
            symbols=["EURUSD", "GBPUSD"],
            timeframes=["H1"],
            typical_confidence_range=(0.3, 0.7),
        ),
        StrategyConfig(
            strategy_id="volatility_squeeze",
            name="Volatility Squeeze",
            strategy_type="breakout",
            symbols=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"],
            timeframes=["H1", "M15"],
            typical_confidence_range=(0.40, 0.75),
        ),
        StrategyConfig(
            strategy_id="session_breakout_london",
            name="Session Breakout London",
            strategy_type="breakout",
            symbols=["GBPUSD", "EURUSD"],
            timeframes=["M15"],
            typical_confidence_range=(0.55, 0.90),
        ),
        StrategyConfig(
            strategy_id="session_breakout_ny",
            name="Session Breakout NY",
            strategy_type="breakout",
            symbols=["GBPUSD", "EURUSD"],
            timeframes=["M15"],
            typical_confidence_range=(0.55, 0.90),
        ),
        StrategyConfig(
            strategy_id="session_breakout_asian",
            name="Session Breakout Asian",
            strategy_type="breakout",
            symbols=["USDJPY"],
            timeframes=["M15"],
            typical_confidence_range=(0.55, 0.90),
        ),
        StrategyConfig(
            strategy_id="ttc_xauusd",
            name="TTC XAUUSD M15",
            strategy_type="momentum",
            symbols=["XAUUSD"],
            timeframes=["M15"],
            typical_confidence_range=(0.45, 0.80),
        ),
        StrategyConfig(
            strategy_id="donchian_atr_trend_v2",
            name="Donchian + ATR Trailing Trend v2",
            strategy_type="trend",
            symbols=["XAUUSD", "GBPUSD", "EURUSD"],
            timeframes=["M15", "H1"],
            typical_confidence_range=(0.45, 0.85),
        ),
        StrategyConfig(
            strategy_id="dual_tf_squeeze_pro",
            name="Dual-Timeframe Squeeze Pro",
            strategy_type="breakout",
            symbols=["XAUUSD", "GBPUSD"],
            timeframes=["M15", "H1"],
            typical_confidence_range=(0.40, 0.85),
        ),
    ]

    for s in strategies:
        reg.register(s)

    return reg
