from strategies.session_range_mean_reversion import SessionRangeMeanReversionStrategy
from strategies.volatility_squeeze import VolatilitySqueezeStrategy

from .amalgamation import (
    AmalgamatedBacktestEngine,
    AmalgamationConfig,
    AmalgamationEngine,
    ComponentExtractor,
    ComponentProfile,
    ConfidenceMethod,
    ExtractionResult,
    VotingMethod,
)
from .data_loader import CsvDataLoader
from .enhanced_engine import EnhancedBacktestEngine
from .grid_strategy import GridStrategy
from .hybrid_strategy import HybridConfig, HybridStrategy
from .ict_smc import (
    ICTMarketState,
    ICTSMCStrategy,
    SignalConfluenceEngine,
)
from .multi_strategy_engine import MultiStrategyBacktestEngine, MultiStrategyConfig
from .pattern_detector import (
    ConsolidationFilter,
    ConsolidationMetrics,
    MWPattern,
    MWPatternDetector,
)
from .stat_arb import StatArbBacktestResult, StatArbStrategy
from .statistical_study import (
    CriterionResult,
    GoNoGoCriteria,
    StatisticalStudy,
    StatisticalStudyResult,
)
from .strategies import (
    BBStrategy,
    CommodityMeanReversionStrategy,
    CommodityTrendStrategy,
    HighConvictionStrategy,
    ISignalStrategy,
    KeltnerChannelBreakoutStrategy,
    MACrossStrategy,
    MomentumBreakoutStrategy,
    ROCMStrategy,
    RSIStrategy,
    SRBreakoutStrategy,
    SupertrendRSIBlendStrategy,
)
from .trade_management import (
    ManagedTrade,
    TradeAction,
    TradeManagementConfig,
    TradeManager,
)
from .types import (
    DEFAULT_SPREAD_PIPS,
    PAIR_SPREAD_PIPS,
    BacktestConfig,
    BacktestEngine,
    BacktestMetrics,
    Bar,
    BarPeriod,
    ExitReason,
    MarketState,
    SessionType,
    SimulatedTrade,
    StrategyBacktestResult,
    StrategySignal,
    TradeDirection,
    TradeOutcome,
    determine_session,
    get_spread_for_pair,
)

try:
    from .parameter_sweep import (
        ParameterGrid,
        SweepResult,
        SweepRow,
        SweepRunner,
        to_csv,
        to_json,
    )
except ImportError:
    ParameterGrid = None
    SweepResult = None
    SweepRow = None
    SweepRunner = None
    to_csv = None
    to_json = None
try:
    from ml.mean_reversion import MLMeanReversionStrategy
except ImportError:
    MLMeanReversionStrategy = None
__all__ = [
    "Bar",
    "BarPeriod",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestMetrics",
    "ExitReason",
    "MarketState",
    "SessionType",
    "SimulatedTrade",
    "StrategySignal",
    "TradeDirection",
    "TradeOutcome",
    "StrategyBacktestResult",
    "determine_session",
    "get_spread_for_pair",
    "PAIR_SPREAD_PIPS",
    "DEFAULT_SPREAD_PIPS",
    "CsvDataLoader",
    "ISignalStrategy",
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
    "SessionRangeMeanReversionStrategy",
    "GridStrategy",
    "StatArbStrategy",
    "StatArbBacktestResult",
    "VolatilitySqueezeStrategy",
    "MultiStrategyConfig",
    "MultiStrategyBacktestEngine",
    "AmalgamationConfig",
    "AmalgamationEngine",
    "AmalgamatedBacktestEngine",
    "ComponentExtractor",
    "ComponentProfile",
    "ExtractionResult",
    "VotingMethod",
    "ConfidenceMethod",
    "ICTSMCStrategy",
    "SignalConfluenceEngine",
    "ICTMarketState",
    "EnhancedBacktestEngine",
    "TradeManagementConfig",
    "TradeManager",
    "ManagedTrade",
    "TradeAction",
    "HybridStrategy",
    "HybridConfig",
    "MLMeanReversionStrategy",
    "StatisticalStudy",
    "StatisticalStudyResult",
    "GoNoGoCriteria",
    "CriterionResult",
    "ConsolidationFilter",
    "ConsolidationMetrics",
    "MWPattern",
    "MWPatternDetector",
]
