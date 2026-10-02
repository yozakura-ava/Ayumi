from __future__ import annotations

from datetime import timedelta

from ..engine import TradeDirection
from .models import ICTMarketState, LiquidityPool, LiquiditySweep


class LiquiditySweepDetector:
    def __init__(
        self,
        sweep_wick_ratio: float = 0.6,
        pool_lookback: int = 50,
        sweep_atr_multiplier: float = 1.5,
        sweep_validity_bars: int = 5,
    ):
        self._sweep_wick_ratio = sweep_wick_ratio
        self._pool_lookback = pool_lookback
        self._sweep_atr_multiplier = sweep_atr_multiplier
        self._sweep_validity_bars = sweep_validity_bars

    def update_liquidity_pools(self, state: ICTMarketState):
        bars = state.bars
        if len(bars) < 5:
            return

        day_start = max(0, len(bars) - 32)
        state.day_high = max(b.high for b in bars[day_start:])
        state.day_low = min(b.low for b in bars[day_start:])

        state.liquidity_pools.clear()

        start_idx = max(0, len(bars) - self._pool_lookback)
        for i in range(start_idx, len(bars)):
            if 0 < i < len(bars) - 1:
                if bars[i].high >= bars[i - 1].high and bars[i].high >= bars[i + 1].high:
                    touches = 1
                    for j in range(i + 1, len(bars)):
                        if bars[j].high >= bars[i].high - (bars[i].high * 0.0001):
                            touches += 1
                        else:
                            break
                    state.liquidity_pools.append(
                        LiquidityPool(
                            level=bars[i].high,
                            touches=touches,
                            is_high=True,
                            is_day_high=abs(bars[i].high - state.day_high) < bars[i].high * 0.0001,
                            bar_index=i,
                        )
                    )

                if bars[i].low <= bars[i - 1].low and bars[i].low <= bars[i + 1].low:
                    touches = 1
                    for j in range(i + 1, len(bars)):
                        if bars[j].low <= bars[i].low + (bars[i].low * 0.0001):
                            touches += 1
                        else:
                            break
                    state.liquidity_pools.append(
                        LiquidityPool(
                            level=bars[i].low,
                            touches=touches,
                            is_high=False,
                            is_day_low=abs(bars[i].low - state.day_low) < bars[i].low * 0.0001,
                            bar_index=i,
                        )
                    )

    def detect_sweeps(self, state: ICTMarketState):
        bars = state.bars
        if len(bars) < 5 or state.atr == 0:
            return

        current = bars[-1]
        previous = bars[-2] if len(bars) > 1 else current

        for pool in state.liquidity_pools:
            if abs(pool.bar_index - (len(bars) - 1)) > self._sweep_validity_bars:
                continue

            if pool.is_high:
                if current.high > pool.level and previous.high <= pool.level:
                    wick_length = current.high - max(current.open, current.close)
                    total_range = current.high - current.low
                    if total_range > 0 and wick_length / total_range > self._sweep_wick_ratio:
                        sweep_distance = current.high - pool.level
                        if state.atr * 0.5 < sweep_distance < state.atr * self._sweep_atr_multiplier:
                            state.recent_sweeps.append(
                                LiquiditySweep(
                                    time=current.time,
                                    sweep_level=pool.level,
                                    sweep_high=current.high,
                                    sweep_low=current.low,
                                    rejection_body=abs(current.close - current.open),
                                    swept_high=True,
                                    session=state.current_session,
                                    strength=self._calculate_sweep_strength(sweep_distance, state.atr, pool.touches),
                                    implied_direction=TradeDirection.SHORT,
                                )
                            )
            else:
                if current.low < pool.level and previous.low >= pool.level:
                    wick_length = min(current.open, current.close) - current.low
                    total_range = current.high - current.low
                    if total_range > 0 and wick_length / total_range > self._sweep_wick_ratio:
                        sweep_distance = pool.level - current.low
                        if state.atr * 0.5 < sweep_distance < state.atr * self._sweep_atr_multiplier:
                            state.recent_sweeps.append(
                                LiquiditySweep(
                                    time=current.time,
                                    sweep_level=pool.level,
                                    sweep_high=current.high,
                                    sweep_low=current.low,
                                    rejection_body=abs(current.close - current.open),
                                    swept_high=False,
                                    session=state.current_session,
                                    strength=self._calculate_sweep_strength(sweep_distance, state.atr, pool.touches),
                                    implied_direction=TradeDirection.LONG,
                                )
                            )

        self._cleanup_old(state)

    def _calculate_sweep_strength(self, sweep_distance: float, atr: float, touches: float) -> float:
        distance_score = min(1.0, sweep_distance / atr)
        touch_score = min(1.0, touches / 3.0)
        return distance_score * 0.6 + touch_score * 0.4

    def _cleanup_old(self, state: ICTMarketState):
        if not state.bars:
            return
        cutoff = state.bars[-1].time - timedelta(hours=4)
        state.recent_sweeps = [s for s in state.recent_sweeps if s.time >= cutoff]
