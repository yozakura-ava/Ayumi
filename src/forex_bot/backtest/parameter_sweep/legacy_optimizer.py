"""Legacy strategy factory — creates strategies by name with custom parameters.

Used by run_live_paper.py to instantiate strategies from parameter configs.
"""

from __future__ import annotations

from backtest.strategy_legacy import ISignalStrategy
from strategies.session_range_mean_reversion import (
    SessionRangeMeanReversionStrategy,
    SessionRangeMRConfig,
)


def legacy_strategy_factory(
    name: str,
    params: dict,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
) -> ISignalStrategy:
    """Create a strategy instance by name with custom parameters.

    Args:
        name: Strategy name (e.g., "session_range_mr")
        params: Dict of parameter overrides
        symbol: Trading symbol (used for pip value calculation)
        timeframe: Bar timeframe

    Returns:
        ISignalStrategy instance
    """
    if name == "session_range_mr":
        return _make_srm(params, symbol)
    else:
        raise ValueError(f"Unknown legacy strategy: {name}")


def _make_srm(params: dict, symbol: str) -> SessionRangeMeanReversionStrategy:
    """Create a SessionRangeMR strategy with custom parameters."""
    defaults = SessionRangeMRConfig()
    overrides = {}

    if "session_range_min_pips" in params:
        overrides["session_range_min_pips"] = params["session_range_min_pips"]
    if "session_range_sl_fraction" in params:
        overrides["session_range_sl_fraction"] = params["session_range_sl_fraction"]
    if "atr_period" in params:
        overrides["atr_period"] = int(params["atr_period"])
    if "atr_sl_multiplier" in params:
        overrides["atr_sl_multiplier"] = params["atr_sl_multiplier"]
    if "atr_tp_multiplier" in params:
        overrides["atr_tp_multiplier"] = params["atr_tp_multiplier"]
    if "rsi_period" in params:
        overrides["rsi_period"] = int(params["rsi_period"])
    if "hard_cap_sl_pips" in params:
        overrides["hard_cap_sl_pips"] = params["hard_cap_sl_pips"]
    if "tp1_rr" in params:
        overrides["tp1_rr"] = params["tp1_rr"]
    if "tp2_rr" in params:
        overrides["tp2_rr"] = params["tp2_rr"]
    if "ema_trend_period" in params:
        overrides["ema_trend_period"] = int(params["ema_trend_period"])

    config = SessionRangeMRConfig(**overrides) if overrides else defaults
    return SessionRangeMeanReversionStrategy(config)
