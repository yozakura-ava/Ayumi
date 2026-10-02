from __future__ import annotations

try:
    from strategies.grid import GridConfig, GridStrategyAdapter
except ImportError:
    GridConfig = None  # type: ignore
    GridStrategyAdapter = None  # type: ignore

from backtest.stat_arb import StatArbStrategy
from backtest.strategies import (
    BBStrategy,
    CommodityMeanReversionStrategy,
    CommodityTrendStrategy,
    HighConvictionStrategy,
    ISignalStrategy,
    KeltnerChannelBreakoutStrategy,
    MACrossStrategy,
    MomentumBreakoutStrategy,
    RegimeSwitchingRouter,
    ROCMStrategy,
    RSIStrategy,
    SRBreakoutStrategy,
)
from backtest.walk_forward_runner import register_strategy
from strategies.session_range_mean_reversion import (  # noqa: I001
    SessionRangeMeanReversionStrategy,
    SessionRangeMRWithRegimeFilter,
)
from strategies.volatility_squeeze import VolatilitySqueezeStrategy


def _make_ma_crossover() -> ISignalStrategy:
    return MACrossStrategy(fast_period=5, slow_period=13, atr_multiplier=2.0)


def _make_bollinger() -> ISignalStrategy:
    return BBStrategy(period=20, std_dev=2.0)


def _make_rsi() -> ISignalStrategy:
    return RSIStrategy(period=14, oversold=35, overbought=65)


def _make_sr_breakout() -> ISignalStrategy:
    return SRBreakoutStrategy(lookback=50, confirmation_bars=1, breakout_threshold=0.0001)


def _make_roc() -> ISignalStrategy:
    return ROCMStrategy(period=12, roc_threshold=0.3)


def _make_momentum() -> ISignalStrategy:
    return MomentumBreakoutStrategy(fast_period=9, slow_period=21, adx_threshold=25.0)


def _make_commodity_trend() -> ISignalStrategy:
    return CommodityTrendStrategy()


def _make_commodity_mean_reversion() -> ISignalStrategy:
    return CommodityMeanReversionStrategy()


def _make_keltner() -> ISignalStrategy:
    return KeltnerChannelBreakoutStrategy()


def _make_grid(pair: str = "EURUSD") -> ISignalStrategy:
    if GridStrategyAdapter is None or GridConfig is None:
        raise ImportError("strategies.grid is not available")
    return GridStrategyAdapter(GridConfig.ftmo(pair))


def _make_stat_arb() -> ISignalStrategy:
    return StatArbStrategy()


def _make_volatility_squeeze() -> ISignalStrategy:
    return VolatilitySqueezeStrategy()


def _make_session_range_mr() -> ISignalStrategy:
    return SessionRangeMeanReversionStrategy()


def _make_session_range_mr_with_regime_filter() -> ISignalStrategy:
    return SessionRangeMRWithRegimeFilter()


def _make_high_conviction() -> ISignalStrategy:
    return HighConvictionStrategy()


def _make_regime_router() -> ISignalStrategy:
    return RegimeSwitchingRouter()


def register_builtin_strategies(pair: str = "EURUSD") -> None:
    register_strategy("ma_crossover", _make_ma_crossover)
    register_strategy("bollinger", _make_bollinger)
    register_strategy("rsi", _make_rsi)
    register_strategy("sr_breakout", _make_sr_breakout)
    register_strategy("roc", _make_roc)
    register_strategy("momentum", _make_momentum)
    register_strategy("commodity_trend", _make_commodity_trend)
    register_strategy("commodity_mean_reversion", _make_commodity_mean_reversion)
    register_strategy("keltner", _make_keltner)
    register_strategy("grid", lambda: _make_grid(pair))
    register_strategy("stat_arb", _make_stat_arb)
    register_strategy("volatility_squeeze", _make_volatility_squeeze)
    register_strategy("session_range_mr", _make_session_range_mr)
    register_strategy("session_range_mr_with_regime", _make_session_range_mr_with_regime_filter)
    register_strategy("high_conviction", _make_high_conviction)
    register_strategy("regime_router", _make_regime_router)
