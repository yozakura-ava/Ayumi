"""TTCXAUUSDStrategy — Pre-configured TTSStrategy adapter for XAUUSD M15.

Uses Optuna-optimized parameters from the XAUUSD M15 tuning study.
Monkeypatches module-level constants in tts_strategy.py before
constructing the strategy instance, following the same pattern as
ttc_strategy_factory() in backtest/parameter_sweep/ttc_optimizer.py.
"""

from __future__ import annotations

import logging

from backtest.strategies.isignal_strategy import ISignalStrategy
from backtest.strategies.tts_strategy import TTSStrategy

logger = logging.getLogger(__name__)

# Optuna best params for XAUUSD M15
_XAUUSD_M15_PARAMS = {
    "min_confidence": 0.5,
    "min_quality_score": 0.4,
    "mw_base_confidence": 0.45,
    "rsi_divergence_boost": 0.2,
    "htf_trend_aligned_boost": 0.1,
    "htf_opposing_penalty": -0.05,
    "kill_zone_active_boost": -0.15,
    "negative_weight": 0.25,
    "swing_lookback": 3,
    "history_bars": 50,
}

# Embargo recommendation for XAUUSD M15 walk-forward optimization.
# 96 M15 bars = 24 hours of trading data. This covers typical intraday
# autocorrelation decay for XAUUSD and should be applied as the embargo_bars
# parameter when running ttc_optimizer.run_ttc_optuna() for this pair.
# See ttc_optimizer.py module docstring for full embargo documentation.
RECOMMENDED_EMBARGO_BARS_M15 = 96

_CONST_MAP = {
    "mw_base_confidence": "MW_BASE_CONFIDENCE",
    "rsi_divergence_boost": "RSI_DIVERGENCE_BOOST",
    "htf_trend_aligned_boost": "HTF_TREND_ALIGNED_BOOST",
    "htf_opposing_penalty": "HTF_OPPOSING_PENALTY",
    "kill_zone_active_boost": "KILL_ZONE_ACTIVE_BOOST",
    "negative_weight": "NEGATIVE_WEIGHT",
    "swing_lookback": "SWING_LOOKBACK",
    "history_bars": "HISTORY_BARS",
}


class TTCXAUUSDStrategy(ISignalStrategy):
    """TTSStrategy pre-wired with Optuna-optimized XAUUSD M15 parameters.

    Embargo
    -------
    Walk-forward validation for this strategy MUST use ``embargo_bars=96``
    (24 hours of M15 data) to prevent autocorrelation leakage at train/test
    boundaries.  Callers should pass ``embargo_bars=TTCXAUUSDStrategy.EMBARGO_BARS_M15``
    to ``run_strategy()`` or ``WalkForwardValidator``.

    Risk Management Delegation
    --------------------------
    This adapter does not implement PF caps, drawdown limits, or position
    throttling. Those concerns belong to the portfolio risk manager in
    the execution layer, not the signal generator. See the PF-Cap Design
    Decision in TTSStrategy's docstring (backtest/strategies/tts_strategy.py).
    """

    name = "TTC XAUUSD M15"
    EMBARGO_BARS_M15: int = RECOMMENDED_EMBARGO_BARS_M15

    def __init__(self) -> None:
        import backtest.strategies.tts_strategy as tts_mod

        params = _XAUUSD_M15_PARAMS

        # Save originals
        originals: dict[str, object] = {}
        for param_name, const_name in _CONST_MAP.items():
            if param_name in params:
                originals[const_name] = getattr(tts_mod, const_name, None)
                setattr(tts_mod, const_name, params[param_name])

        try:
            self._strategy = TTSStrategy(
                symbol="XAUUSD",
                min_confidence=params.get("min_confidence", 0.20),
                min_quality_score=params.get("min_quality_score", 0.25),
                timeframe="M15",
            )
        finally:
            # Restore originals
            for const_name, orig_val in originals.items():
                setattr(tts_mod, const_name, orig_val)

    # Delegate ISignalStrategy interface to the inner strategy
    def evaluate(self, state: object) -> object:
        result = self._strategy.evaluate(state)
        if result is None:
            bars = getattr(state, "bars", [])
            latest = getattr(state, "latest_bar", None)
            if latest:
                logger.debug(
                    "TTC XAUUSD M15: no signal (bars=%d, time=%s)",
                    len(bars),
                    getattr(latest, "time", "?"),
                )
            else:
                logger.debug("TTC XAUUSD M15: no signal (bars=%d, no latest bar)", len(bars))
        return result
