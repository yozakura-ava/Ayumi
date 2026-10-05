"""Strategy Factory package (SFA-2 — validation runner shipped).

SFA-1 landed the wiring skeleton (template contract + bridge + pipeline
config + spread costs).  SFA-2 layers on top:

* :mod:`.validation_runner` — batch WF + DSR + spread-cost gate end-to-end
* :mod:`.storage`           — ``research.duckdb`` persistence for verdicts
* :mod:`.strategies`        — concrete ``ISignalStrategy`` implementations
                               that resolve the SFA-1 deferred
                               ``usdjpy_d1_trend`` placeholder

Re-exports from SFA-1 (template contract, bridge, pipeline config,
spread costs, registry) remain available unchanged.
"""

from __future__ import annotations

from forex_bot.factory.bridge import (
    BridgeError,
    build_strategies_for_registry,
    build_strategy_from_template,
)
from forex_bot.factory.pipeline_config import (
    DSRConfig,
    OOSConfig,
    PBOConfig,
    PipelineConfig,
    RegimeGatingConfig,
    TradeCountConfig,
    WalkForwardWindowConfig,
    compute_dsr_n_trials,
    default_pipeline_config,
)
from forex_bot.factory.registry import FactoryRegistry, default_factory_registry
from forex_bot.factory.spread_costs import (
    COMMISSION_PER_LOT_USD,
    PIP_SLIPPAGE,
    SpreadCosts,
    SpreadCostTable,
    default_spread_costs,
)
from forex_bot.factory.storage import FactoryVerdictStore
from forex_bot.factory.strategies import USDJPYD1TrendConfig, USDJPYD1TrendStrategy
from forex_bot.factory.template import (
    ARCHETYPE_AFFINITY,
    ParamKind,
    ParamSpec,
    StrategyTemplate,
)
from forex_bot.factory.validation_runner import (
    INSUFFICIENT_DATA_THRESHOLD,
    CandidateSpec,
    ValidationRunner,
    ValidationVerdict,
    run_validation_batch,
)

__all__ = [
    # SFA-1: template contract
    "ARCHETYPE_AFFINITY",
    "ParamKind",
    "ParamSpec",
    "StrategyTemplate",
    # SFA-1: bridge
    "BridgeError",
    "build_strategies_for_registry",
    "build_strategy_from_template",
    # SFA-1: pipeline config (§4)
    "DSRConfig",
    "OOSConfig",
    "PBOConfig",
    "PipelineConfig",
    "RegimeGatingConfig",
    "TradeCountConfig",
    "WalkForwardWindowConfig",
    "compute_dsr_n_trials",
    "default_pipeline_config",
    # SFA-1: registry
    "FactoryRegistry",
    "default_factory_registry",
    # SFA-1: spread costs
    "COMMISSION_PER_LOT_USD",
    "PIP_SLIPPAGE",
    "SpreadCostTable",
    "SpreadCosts",
    "default_spread_costs",
    # SFA-2: validation runner
    "CandidateSpec",
    "INSUFFICIENT_DATA_THRESHOLD",
    "ValidationRunner",
    "ValidationVerdict",
    "run_validation_batch",
    # SFA-2: storage
    "FactoryVerdictStore",
    # SFA-2: deferred strategy implementation
    "USDJPYD1TrendConfig",
    "USDJPYD1TrendStrategy",
]
