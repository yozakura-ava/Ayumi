from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DynamicSizingConfig:
    min_multiplier: float = 0.5
    max_multiplier: float = 1.5
    loss_reduction: float = 0.1
    win_increase: float = 0.1
    max_streak_impact: float = 0.5


_STANDARD_LOT_SIZE = 100_000
_DEFAULT_CONFIG = DynamicSizingConfig()


def fixed_fractional(
    account_balance: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
) -> float:
    if account_balance <= 0:
        return 0.0
    if risk_pct <= 0:
        return 0.0
    stop_distance = abs(entry_price - stop_loss)
    if stop_distance == 0:
        return 0.0
    risk_amount = account_balance * (risk_pct / 100.0)
    risk_per_lot = stop_distance * _STANDARD_LOT_SIZE
    return risk_amount / risk_per_lot


def kelly_criterion(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
) -> float:
    if avg_loss <= 0:
        return 0.0
    b = avg_win / avg_loss
    kelly = (b * win_rate - (1.0 - win_rate)) / b
    if kelly <= 0:
        return 0.0
    return min(kelly / 2.0, 0.5)


def dynamic_sizing(
    base_size: float,
    recent_pnl: float,
    win_streak: int,
    loss_streak: int,
    config: DynamicSizingConfig | None = None,
) -> float:
    cfg = config or _DEFAULT_CONFIG
    if base_size <= 0:
        return 0.0

    pnl_impact = 0.0
    if recent_pnl > 0:
        pnl_impact = cfg.win_increase
    elif recent_pnl < 0:
        pnl_impact = -cfg.loss_reduction

    streak_impact = 0.0
    if win_streak > 0:
        streak_impact = min(win_streak * cfg.win_increase, cfg.max_streak_impact)
    elif loss_streak > 0:
        streak_impact = max(-loss_streak * cfg.loss_reduction, -cfg.max_streak_impact)

    total_impact = pnl_impact + streak_impact
    multiplier = 1.0 + total_impact
    multiplier = max(cfg.min_multiplier, min(cfg.max_multiplier, multiplier))

    return base_size * multiplier


def check_position_limits(
    current_positions: dict[str, float],
    new_pair: str,
    max_per_pair: float,
    max_total: float,
) -> bool:
    current_pair_size = current_positions.get(new_pair, 0.0)
    if current_pair_size >= max_per_pair:
        return False

    total_exposure = sum(current_positions.values())
    if total_exposure >= max_total:
        return False

    return True
