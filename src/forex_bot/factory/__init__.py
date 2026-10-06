"""Strategy Factory package (SFA-3 — tournament front door landed).

SFA-1 landed the wiring skeleton (template contract + bridge + pipeline
config + spread costs).  SFA-2 added:

* :mod:`.validation_runner` — batch WF + DSR + spread-cost gate end-to-end
* :mod:`.storage`           — ``research.duckdb`` persistence for verdicts
* :mod:`.strategies`        — concrete ``ISignalStrategy`` implementations
                               that resolve the SFA-1 deferred
                               ``usdjpy_d1_trend`` placeholder

SFA-3 layers on:

* :mod:`.templates`         — registry-backed pass-through templates
                               used by the tournament front door
                               (``tournament.front_door``).

Re-exports from SFA-1/SFA-2 (template contract, bridge, pipeline
config, spread costs, registry, validation runner, storage) remain
available unchanged.  Sprint D 1 (card 64c6598f) layers on
:mod:`.meta_labeling` (per-trade features + calibrated meta-classifier)
plus the :func:`rank_candidates_with_meta_gate` extension to the
risk-adjusted ranker.
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
from forex_bot.factory.risk_adjusted_ranking import (
    BHResult,
    DEFAULT_FDR_ALPHA,
    MIN_CELL_TRIALS,
    MIN_TRIAL_BARS,
    RankedCandidate,
    RawReturnSortRemoved,
    RiskAdjustedRankingError,
    benjamini_hochberg,
    one_sample_t_pvalue,
    rank_candidates_by_trial_returns,
    rank_from_trial_return_store,
    # Sprint D 1 (card 64c6598f): per-trial meta-label gating.
    DEFAULT_META_GATE_THRESHOLD,
    rank_candidates_with_meta_gate,
    rank_from_trial_return_store_with_meta_gate,
)
from forex_bot.factory.meta_labeling import (
    CalibratedMetaClassifier,
    META_FEATURE_NAMES,
    MIN_META_TRADES,
    MetaLabeledTrade,
    MetaLabelingError,
    MetaLabelingShapeError,
    MetaTradeContext,
    build_meta_features,
    consume_meta_confidence_per_trial,
    evaluate_meta_label_calibration,
    fit_meta_classifier,
    predict_meta_probability,
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
from forex_bot.factory.templates import RegistryBackedTemplate
from forex_bot.factory.template import (
    ARCHETYPE_AFFINITY,
    ParamKind,
    ParamSpec,
    StrategyTemplate,
)
from forex_bot.factory.validation_runner import (
    INSUFFICIENT_DATA_THRESHOLD,
    PBO_CEILING_NOT_APPLICABLE,
    CandidateSpec,
    TrialReturnStore,
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
    "PBO_CEILING_NOT_APPLICABLE",
    "TrialReturnStore",
    "ValidationRunner",
    "ValidationVerdict",
    "run_validation_batch",
    # SFA-2: storage
    "FactoryVerdictStore",
    # SFA-2: deferred strategy implementation
    "USDJPYD1TrendConfig",
    "USDJPYD1TrendStrategy",
    # SFA-3: registry-backed pass-through template
    "RegistryBackedTemplate",
    # Sprint C 1b.3 (card cc90a6b6): risk-adjusted ranking — BH-FDR
    # primary, raw-return sort removed (loud-failure shim in
    # ``tournament.scorecard.rank_scorecard_rows``).
    "BHResult",
    "DEFAULT_FDR_ALPHA",
    "MIN_CELL_TRIALS",
    "MIN_TRIAL_BARS",
    "RankedCandidate",
    "RawReturnSortRemoved",
    "RiskAdjustedRankingError",
    "benjamini_hochberg",
    "one_sample_t_pvalue",
    "rank_candidates_by_trial_returns",
    "rank_from_trial_return_store",
    # Sprint D 1 (card 64c6598f): per-trial meta-label gating.
    "DEFAULT_META_GATE_THRESHOLD",
    "rank_candidates_with_meta_gate",
    "rank_from_trial_return_store_with_meta_gate",
    # Sprint D 1 (card 64c6598f): per-trade features + calibrated
    # meta-classifier.
    "CalibratedMetaClassifier",
    "META_FEATURE_NAMES",
    "MIN_META_TRADES",
    "MetaLabeledTrade",
    "MetaLabelingError",
    "MetaLabelingShapeError",
    "MetaTradeContext",
    "build_meta_features",
    "consume_meta_confidence_per_trial",
    "evaluate_meta_label_calibration",
    "fit_meta_classifier",
    "predict_meta_probability",
]
