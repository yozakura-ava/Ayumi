"""§9 — Stop loss and take profit placement.

ATR-based stop placement scaled to entry timeframe with minimum RR enforcement.
Priority-ordered SL rules (§9.1), target rules (§9.2),
and trailing SL logic for lock-in behavior (§9.3).
"""

from __future__ import annotations  # noqa: I001

DEFAULT_RR_RATIO = 2.0
SPREAD_BUFFER_PIPS = 0.5

ATR_SL_MULTIPLIERS = {
    "M5": 1.0,
    "M15": 1.5,
    "H1": 2.0,
    "H4": 2.5,
    "D1": 3.0,
}

ATR_TP_MULTIPLIERS = {
    "M5": {"tp1": 1.0, "tp2": 1.5, "tp3": 2.5},
    "M15": {"tp1": 1.5, "tp2": 2.5, "tp3": 3.5},
    "H1": {"tp1": 2.0, "tp2": 3.0, "tp3": 4.0},
    "H4": {"tp1": 2.5, "tp2": 3.5, "tp3": 5.0},
    "D1": {"tp1": 3.0, "tp2": 4.5, "tp3": 6.0},
}

MIN_RR_BY_TF = {
    "M5": 1.2,
    "M15": 1.5,
    "H1": 1.5,
    "H4": 1.3,
    "D1": 1.2,
}

DEFAULT_TF = "M15"

MAX_SL_ATR_MULT = 4.0


class StopTargetCalculator:
    """Calculate stop loss and take profit levels from structure context.

    Uses ATR-based stop placement scaled to the entry timeframe, with
    minimum R:R enforcement to prevent negative-expectancy trades.
    Falls back to structure-based SL when ATR is unavailable.
    """

    def __init__(
        self,
        rr_ratio: float = DEFAULT_RR_RATIO,
        pip_size: float = 0.0001,
        timeframe: str = DEFAULT_TF,
    ):
        self.rr_ratio = rr_ratio
        self.pip_size = pip_size
        self.timeframe = timeframe.upper()
        self._sl_mult = ATR_SL_MULTIPLIERS.get(self.timeframe, 1.5)
        self._tp_mults = ATR_TP_MULTIPLIERS.get(self.timeframe, ATR_TP_MULTIPLIERS[DEFAULT_TF])
        self._min_rr = MIN_RR_BY_TF.get(self.timeframe, 1.5)

    def calculate(
        self,
        direction: str,
        entry: float,
        context: dict,
        spread: float = 0.0,
    ) -> dict:
        """Return stop_loss, take_profit, tp_levels, and trailing config.

        Context keys:
            atr: ATR value (required for ATR-based stops)
            sl2 (long): second swing low below entry (fallback)
            sh2 (short): second swing high above entry (fallback)
            r2, r3: demand/take-profit levels for longs
            d2, d3: supply/take-profit levels for shorts
        """
        atr = context.get("atr", 0.0)
        sl = self._place_stop_loss(direction, entry, context, spread, atr)
        tp, tp_levels = self._place_take_profit(direction, entry, sl, context, atr, spread)
        trailing = self._trailing_config(direction, entry, sl, tp, context, atr)

        return {
            "stop_loss": sl,
            "take_profit": tp,
            "tp_levels": tp_levels,
            "trailing": trailing,
            "risk_pips": abs(entry - sl) / self.pip_size,
            "reward_pips": abs(tp - entry) / self.pip_size,
            "rr_ratio": abs(tp - entry) / abs(entry - sl) if sl != entry else 0.0,
        }

    def _place_stop_loss(
        self,
        direction: str,
        entry: float,
        context: dict,
        spread: float,
        atr: float,
    ) -> float:
        """ATR-based SL scaled to entry timeframe.

        SL distance = ATR * timeframe_multiplier. Capped at MAX_SL_ATR_MULT * ATR.
        Falls back to structure-based SL when ATR is not available.
        """
        buffer = max(SPREAD_BUFFER_PIPS * self.pip_size, spread)

        if atr > 0:
            atr_sl_distance = atr * self._sl_mult
            atr_sl_distance = min(atr_sl_distance, atr * MAX_SL_ATR_MULT)

            if direction == "long":
                sl = entry - atr_sl_distance - buffer
                structure_sl = context.get("sl2")
                if structure_sl is not None:
                    structure_sl -= buffer
                    sl = max(sl, structure_sl)
                sl = min(sl, entry - 5 * self.pip_size)
            else:
                sl = entry + atr_sl_distance + buffer
                structure_sl = context.get("sh2")
                if structure_sl is not None:
                    structure_sl += buffer
                    sl = min(sl, structure_sl)
                sl = max(sl, entry + 5 * self.pip_size)
        else:
            sl = self._structure_fallback_sl(direction, entry, context, buffer)

        return round(sl, 5)

    def _structure_fallback_sl(
        self,
        direction: str,
        entry: float,
        context: dict,
        buffer: float,
    ) -> float:
        """Structure-based SL fallback when ATR is not available."""
        if direction == "long":
            sl2 = context.get("sl2", entry - 50 * self.pip_size)
            sl = min(sl2, entry - 20 * self.pip_size) - buffer
            sl = min(sl, entry - 10 * self.pip_size)
        else:
            sh2 = context.get("sh2", entry + 50 * self.pip_size)
            sl = max(sh2, entry + 20 * self.pip_size) + buffer
            sl = max(sl, entry + 10 * self.pip_size)
        return round(sl, 5)

    def _place_take_profit(
        self,
        direction: str,
        entry: float,
        sl: float,
        context: dict,
        atr: float,
        spread: float = 0.0,
    ) -> tuple[float, list[dict]]:
        """TP using ATR multiples, enforcing minimum RR ratio.

        Spread-compensated: min_reward = risk * min_rr + spread to account
        for the round-trip spread charged on exit (SL already includes
        entry-side spread buffer, but exit-side spread reduces TP gain).
        """
        risk = abs(entry - sl)
        min_reward = risk * self._min_rr + spread

        if atr > 0:
            tp_levels = self._atr_tp_levels(direction, entry, atr, context, min_reward)
        else:
            tp_levels = self._rr_tp_levels(direction, entry, risk, context, min_reward)

        tp = tp_levels[-1]["price"]
        return round(tp, 5), tp_levels

    def _atr_tp_levels(
        self,
        direction: str,
        entry: float,
        atr: float,
        context: dict,
        min_reward: float,
    ) -> list[dict]:
        """Generate TP levels at ATR multiples, enforcing minimum RR."""
        tp_levels = []

        for level_name, mult in self._tp_mults.items():
            distance = atr * mult
            if distance < min_reward:
                distance = min_reward

            if direction == "long":
                price = entry + distance
            else:
                price = entry - distance

            tp_levels.append(
                {
                    "level": level_name.upper(),
                    "price": round(price, 5),
                    "rr": round(distance / min_reward, 2) if min_reward > 0 else 0,
                    "atr_mult": mult,
                }
            )

        self._add_structure_levels(tp_levels, direction, entry, context)

        return tp_levels

    def _rr_tp_levels(
        self,
        direction: str,
        entry: float,
        risk: float,
        context: dict,
        min_reward: float,
    ) -> list[dict]:
        """RR-based TP levels fallback (no ATR)."""
        rr_targets = [1.0, 1.5, 2.0]
        tp_levels = []

        for level_name, rr_mult in zip(["tp1", "tp2", "tp3"], rr_targets):  # noqa: B905
            reward = risk * max(rr_mult, self._min_rr)
            if direction == "long":
                price = entry + reward
            else:
                price = entry - reward
            tp_levels.append(
                {
                    "level": level_name.upper(),
                    "price": round(price, 5),
                    "rr": round(rr_mult, 2),
                }
            )

        self._add_structure_levels(tp_levels, direction, entry, context)

        return tp_levels

    def _add_structure_levels(
        self,
        tp_levels: list[dict],
        direction: str,
        entry: float,
        context: dict,
    ) -> None:
        """Add structure levels (R2/R3/D2/D3) as reference if on correct side."""
        if direction == "long":
            for name in ("r2", "r3"):
                level_price = context.get(name)
                if level_price and level_price > entry:
                    existing_names = {t["level"].lower() for t in tp_levels}
                    if name not in existing_names:
                        tp_levels.append(
                            {
                                "level": name.upper(),
                                "price": round(level_price, 5),
                                "rr": 0.0,
                                "structure": True,
                            }
                        )
        else:
            for name in ("d2", "d3"):
                level_price = context.get(name)
                if level_price and level_price < entry:
                    existing_names = {t["level"].lower() for t in tp_levels}
                    if name not in existing_names:
                        tp_levels.append(
                            {
                                "level": name.upper(),
                                "price": round(level_price, 5),
                                "rr": 0.0,
                                "structure": True,
                            }
                        )

    def _trailing_config(
        self,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        context: dict,
        atr: float,
    ) -> dict:
        """Trailing stop logic for lock-in behavior.

        Once price moves 1R in favor, trail SL to breakeven.
        After 1.5R, trail by ATR (if available) or half remaining distance.
        """
        risk = abs(entry - sl)

        if direction == "long":
            breakeven_trigger = entry + risk
            trail_start = entry + risk * 1.5
        else:
            breakeven_trigger = entry - risk
            trail_start = entry - risk * 1.5

        trail_step = "half_remaining"
        if atr > 0:
            trail_step = "atr_half"

        return {
            "breakeven_trigger": round(breakeven_trigger, 5),
            "trail_start": round(trail_start, 5),
            "trail_step": trail_step,
            "atr_trail": atr if atr > 0 else context.get("atr"),
        }
