"""Backtest strategies package."""

# Import the formal ABC first (no circular deps)
# Load legacy strategies (ISignalStrategy re-exported from line above)
from ..strategy_legacy import (
    DEFAULT_LOD_HOD_STOP_BUFFER_PIPS,
    BBStrategy,
    CommodityMeanReversionStrategy,
    CommodityTrendStrategy,
    HighConvictionStrategy,
    KeltnerChannelBreakoutStrategy,
    MACrossStrategy,
    MomentumBreakoutStrategy,
    RegimeRouterConfig,
    RegimeSwitchingRouter,
    ROCMStrategy,
    RSIStrategy,
    SRBreakoutStrategy,
    SupertrendRSIBlendStrategy,
    apply_lod_hod_stop_buffer,
)
from .isignal_strategy import ISignalStrategy, StrategyConfig, Tick
from .scalper_strategy import ScalperStrategy

# TTSStrategy loaded last — depends on ISignalStrategy being already defined
from .tts_strategy import TTSStrategy

__all__ = [
    "ISignalStrategy",
    "Tick",
    "StrategyConfig",
    "TTSStrategy",
    "ScalperStrategy",
    "MACrossStrategy",
    "BBStrategy",
    "RSIStrategy",
    "SRBreakoutStrategy",
    "ROCMStrategy",
    "MomentumBreakoutStrategy",
    "CommodityTrendStrategy",
    "CommodityMeanReversionStrategy",
    "SupertrendRSIBlendStrategy",
    "KeltnerChannelBreakoutStrategy",
    "HighConvictionStrategy",
    "RegimeSwitchingRouter",
    "RegimeRouterConfig",
    "DEFAULT_LOD_HOD_STOP_BUFFER_PIPS",
    "apply_lod_hod_stop_buffer",
]
