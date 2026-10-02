from __future__ import annotations

from dataclasses import dataclass

from .position_sizing import fixed_fractional
from .regime import VolatilityRegime, VolatilityThresholds, volatility_regime


@dataclass(frozen=True)
class VAPSConfig:
    lookback: int = 50
    low_multiplier: float = 1.25
    normal_multiplier: float = 1.0
    high_multiplier: float = 0.75
    extreme_multiplier: float = 0.5
    min_multiplier: float = 0.25
    max_multiplier: float = 1.5
    low_pctile: float = 25.0
    normal_pctile: float = 75.0
    high_pctile: float = 90.0


_DEFAULT_CONFIG = VAPSConfig()


def _regime_multiplier(
    regime: VolatilityRegime,
    config: VAPSConfig,
) -> float:
    multipliers = {
        VolatilityRegime.LOW: config.low_multiplier,
        VolatilityRegime.NORMAL: config.normal_multiplier,
        VolatilityRegime.HIGH: config.high_multiplier,
        VolatilityRegime.EXTREME: config.extreme_multiplier,
    }
    raw = multipliers[regime]
    return max(config.min_multiplier, min(config.max_multiplier, raw))


def vaps_regime(
    atr_series: list[float],
    config: VAPSConfig | None = None,
) -> tuple[VolatilityRegime, float, float]:
    cfg = config or _DEFAULT_CONFIG
    thresholds = VolatilityThresholds(
        low=cfg.low_pctile,
        normal=cfg.normal_pctile,
        high=cfg.high_pctile,
    )
    result = volatility_regime(atr_series, lookback=cfg.lookback, thresholds=thresholds)
    multiplier = _regime_multiplier(result.regime, cfg)
    return result.regime, result.percentile, multiplier


def vaps_size(
    account_balance: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    atr_series: list[float],
    config: VAPSConfig | None = None,
) -> tuple[float, VolatilityRegime, float]:
    cfg = config or _DEFAULT_CONFIG
    regime, percentile, multiplier = vaps_regime(atr_series, cfg)
    adjusted_risk_pct = risk_pct * multiplier
    adapted_lot = fixed_fractional(
        account_balance,
        adjusted_risk_pct,
        entry_price,
        stop_loss,
    )
    return adapted_lot, regime, percentile


def vaps_multiply(
    base_lot: float,
    atr_series: list[float],
    config: VAPSConfig | None = None,
) -> tuple[float, VolatilityRegime, float]:
    cfg = config or _DEFAULT_CONFIG
    regime, percentile, multiplier = vaps_regime(atr_series, cfg)
    adapted_lot = base_lot * multiplier
    return adapted_lot, regime, percentile
