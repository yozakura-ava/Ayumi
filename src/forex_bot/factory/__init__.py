"""Strategy Factory package (SFA-1 spine).

This package provides the *plug-in* contract that lets new strategy archetypes
be added without touching the rest of the pipeline.  It is intentionally narrow
in SFA-1 — only the wiring skeleton lands here:

* :mod:`.template`  — :class:`StrategyTemplate` ABC + :class:`ParamSpec`
* :mod:`.bridge`    — Template → ``ISignalStrategy`` adapter (re-uses the
                      existing ``strategies.registry`` strategies unchanged)
* :mod:`.pipeline_config` — §4 validation-pipeline config (WF windows, DSR
                            ``n_trials`` scaling, regime gating, min-trade
                            counts, OOS isolation, PBO threshold)
* :mod:`.spread_costs`    — mandatory spread/commission/slippage defaults
* :mod:`.registry`   — :class:`FactoryRegistry` (template registration)

SFA-1 is **wiring-only**: no new strategies are created, no validation logic is
re-implemented, and the existing ``src/forex_bot/strategies/registry.py``
remains the single source of truth for concrete strategies.
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
from forex_bot.factory.template import (
    ARCHETYPE_AFFINITY,
    ParamKind,
    ParamSpec,
    StrategyTemplate,
)

__all__ = [
    # template contract
    "ARCHETYPE_AFFINITY",
    "ParamKind",
    "ParamSpec",
    "StrategyTemplate",
    # bridge
    "BridgeError",
    "build_strategy_from_template",
    "build_strategies_for_registry",
    # pipeline config (§4)
    "DSRConfig",
    "OOSConfig",
    "PBOConfig",
    "PipelineConfig",
    "RegimeGatingConfig",
    "TradeCountConfig",
    "WalkForwardWindowConfig",
    "compute_dsr_n_trials",
    "default_pipeline_config",
    # registry
    "FactoryRegistry",
    "default_factory_registry",
    # spread costs
    "COMMISSION_PER_LOT_USD",
    "PIP_SLIPPAGE",
    "SpreadCostTable",
    "SpreadCosts",
    "default_spread_costs",
]
