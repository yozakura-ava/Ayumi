from dataclasses import dataclass
from enum import Enum

from ..engine import Bar, ExitReason, TradeDirection


class ExitTier(Enum):
    NONE = 0
    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3


class PartialExitAction(Enum):
    NO_ACTION = "no_action"
    PARTIAL_CLOSE = "partial_close"
    FULL_CLOSE = "full_close"
    MOVE_SL_TO_BREAKEVEN = "move_sl_breakeven"
    ENABLE_TRAIL = "enable_trail"


@dataclass
class TierState:
    cumulative_closed_pct: float = 0.0
    sl_moved_to_breakeven: bool = False
    trail_enabled: bool = False
    highest_tier_reached: ExitTier = ExitTier.NONE
    remaining_pct: float = 1.0


@dataclass
class PartialExitResult:
    action: PartialExitAction
    close_pct: float = 0.0
    exit_price: float = 0.0
    new_sl: float | None = None
    reason: ExitReason = ExitReason.TAKE_PROFIT_1
    tier_reached: ExitTier = ExitTier.NONE


class PartialExitManager:
    def __init__(
        self,
        enabled: bool = True,
        tiers: list[tuple[float, float, bool]] | None = None,
        final_trail: bool = True,
    ):
        self.enabled = enabled
        self.tiers = tiers or [
            (0.5, 1.0, True),
            (0.75, 2.0, False),
        ]
        self.final_trail = final_trail

    def create_state(self) -> TierState:
        return TierState(remaining_pct=1.0)

    def evaluate(
        self,
        bar: Bar,
        state: TierState,
        direction: TradeDirection,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        tp3: float,
        current_sl: float,
    ) -> PartialExitResult:
        if not self.enabled:
            return PartialExitResult(action=PartialExitAction.NO_ACTION)

        current_rr = self._calculate_rr(bar, direction, entry_price, stop_loss)

        for i, (close_pct, rr_target, move_sl) in enumerate(self.tiers):
            tier = ExitTier(i + 1)

            if state.highest_tier_reached.value >= tier.value:
                continue

            if current_rr < rr_target:
                continue

            hit = self._check_tp_hit(bar, direction, tp1, tp2, tp3, tier)
            if not hit:
                continue

            return self._process_tier_hit(
                bar,
                state,
                direction,
                tier,
                close_pct,
                move_sl,
                entry_price,
                stop_loss,
                tp1,
                tp2,
                tp3,
            )

        if state.highest_tier_reached.value >= ExitTier.TIER_2.value and self.final_trail:
            if not state.trail_enabled:
                state.trail_enabled = True
                return PartialExitResult(
                    action=PartialExitAction.ENABLE_TRAIL,
                    tier_reached=state.highest_tier_reached,
                )

        return PartialExitResult(action=PartialExitAction.NO_ACTION)

    def _check_tp_hit(
        self,
        bar: Bar,
        direction: TradeDirection,
        tp1: float,
        tp2: float,
        tp3: float,
        tier: ExitTier,
    ) -> bool:
        if direction == TradeDirection.LONG:
            if tier == ExitTier.TIER_1:
                return bar.high >= tp1
            elif tier == ExitTier.TIER_2:
                return bar.high >= tp2
            else:
                return bar.high >= tp3
        else:
            if tier == ExitTier.TIER_1:
                return bar.low <= tp1
            elif tier == ExitTier.TIER_2:
                return bar.low <= tp2
            else:
                return bar.low <= tp3

    def _process_tier_hit(
        self,
        bar: Bar,
        state: TierState,
        direction: TradeDirection,
        tier: ExitTier,
        close_pct: float,
        move_sl: bool,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        tp3: float,
    ) -> PartialExitResult:
        tp_price = self._get_tp_price(direction, tp1, tp2, tp3, tier)
        actual_close_pct = close_pct - state.cumulative_closed_pct
        is_last_tier = tier.value == len(self.tiers)

        if is_last_tier:
            state.cumulative_closed_pct = close_pct
            state.remaining_pct = 1.0 - close_pct
            state.highest_tier_reached = tier

            if self.final_trail:
                return PartialExitResult(
                    action=PartialExitAction.PARTIAL_CLOSE,
                    close_pct=actual_close_pct,
                    exit_price=tp_price,
                    reason=self._tier_to_exit_reason(tier),
                    tier_reached=tier,
                )
            else:
                return PartialExitResult(
                    action=PartialExitAction.FULL_CLOSE,
                    close_pct=1.0,
                    exit_price=tp_price,
                    reason=self._tier_to_exit_reason(tier),
                    tier_reached=tier,
                )

        state.cumulative_closed_pct = close_pct
        state.remaining_pct = 1.0 - close_pct
        state.highest_tier_reached = tier

        actions = []
        new_sl = None
        if move_sl and not state.sl_moved_to_breakeven:
            state.sl_moved_to_breakeven = True
            new_sl = entry_price
            actions.append(PartialExitAction.MOVE_SL_TO_BREAKEVEN)

        actions.append(PartialExitAction.PARTIAL_CLOSE)

        result = PartialExitResult(
            action=PartialExitAction.PARTIAL_CLOSE,
            close_pct=actual_close_pct,
            exit_price=tp_price,
            new_sl=new_sl,
            reason=self._tier_to_exit_reason(tier),
            tier_reached=tier,
        )
        return result

    def _get_tp_price(
        self,
        direction: TradeDirection,
        tp1: float,
        tp2: float,
        tp3: float,
        tier: ExitTier,
    ) -> float:
        if tier == ExitTier.TIER_1:
            return tp1
        elif tier == ExitTier.TIER_2:
            return tp2
        return tp3

    @staticmethod
    def _tier_to_exit_reason(tier: ExitTier) -> ExitReason:
        mapping = {
            ExitTier.TIER_1: ExitReason.TAKE_PROFIT_1,
            ExitTier.TIER_2: ExitReason.TAKE_PROFIT_2,
            ExitTier.TIER_3: ExitReason.TAKE_PROFIT_3,
        }
        return mapping.get(tier, ExitReason.TAKE_PROFIT_1)

    @staticmethod
    def _calculate_rr(bar: Bar, direction: TradeDirection, entry_price: float, stop_loss: float) -> float:
        risk = abs(entry_price - stop_loss)
        if risk == 0:
            return 0.0
        if direction == TradeDirection.LONG:
            reward = bar.close - entry_price
        else:
            reward = entry_price - bar.close
        return reward / risk
