from __future__ import annotations

from ..engine import Bar, SessionType, TradeDirection, determine_session
from .fvg import FVGDetector
from .h4_context import H4ContextModule
from .liquidity_sweep import LiquiditySweepDetector
from .market_structure import MarketStructureAnalyzer
from .models import (
    ConfluenceSignal,
    ICTMarketState,
    SignalStrength,
)
from .order_block import OrderBlockDetector
from .premium_discount import PremiumDiscountClassifier

MULTI_SESSION_BONUS = 0.08
SINGLE_SESSION_PENALTY = 0.03


def compute_session_span(bars: list[Bar]) -> tuple[int, bool]:
    if not bars:
        return (0, False)
    sessions_seen: set[SessionType] = set()
    for bar in bars:
        s = determine_session(bar.time)
        if s != SessionType.OUTSIDE:
            sessions_seen.add(s)
    count = len(sessions_seen)
    return (count, count >= 2)


class SignalConfluenceEngine:
    def __init__(
        self,
        min_confidence: float = 0.55,
        structure_weight: float = 0.30,
        ob_weight: float = 0.25,
        fvg_weight: float = 0.15,
        sweep_weight: float = 0.15,
        pd_weight: float = 0.10,
        session_weight: float = 0.05,
        h4_weight: float = 0.15,
        default_sl_multiplier: float = 3.0,
        tp1_rr: float = 1.0,
        tp2_rr: float = 2.0,
        tp3_rr: float = 3.0,
        multi_session_weight_bonus: float = MULTI_SESSION_BONUS,
        single_session_penalty: float = SINGLE_SESSION_PENALTY,
    ):
        self._min_confidence = min_confidence
        self._structure_weight = structure_weight
        self._ob_weight = ob_weight
        self._fvg_weight = fvg_weight
        self._sweep_weight = sweep_weight
        self._pd_weight = pd_weight
        self._session_weight = session_weight
        self._h4_weight = h4_weight
        self._default_sl_multiplier = default_sl_multiplier
        self._tp1_rr = tp1_rr
        self._tp2_rr = tp2_rr
        self._tp3_rr = tp3_rr
        self._multi_session_bonus = multi_session_weight_bonus
        self._single_session_penalty = single_session_penalty

        self._structure_analyzer = MarketStructureAnalyzer()
        self._ob_detector = OrderBlockDetector()
        self._fvg_detector = FVGDetector()
        self._sweep_detector = LiquiditySweepDetector()
        self._pd_classifier = PremiumDiscountClassifier()
        self._h4_module = H4ContextModule()

    def evaluate(self, state: ICTMarketState, h4_bars: list[Bar] | None = None) -> ConfluenceSignal | None:
        self._structure_analyzer.analyze(state)
        self._ob_detector.detect(state)
        self._fvg_detector.detect(state)
        self._sweep_detector.update_liquidity_pools(state)
        self._sweep_detector.detect_sweeps(state)
        self._pd_classifier.classify(state)

        h4_context = None
        if h4_bars is not None and state.atr > 0:
            h4_context = self._h4_module.analyze(h4_bars, state.latest_bar.close, state.atr)

        if state.atr == 0:
            return None

        session_span_count, is_multi_session = compute_session_span(state.bars)
        session_span_adjustment = self._multi_session_bonus if is_multi_session else -self._single_session_penalty

        bullish_scores = self._component_scores(state, TradeDirection.LONG, h4_context)
        bearish_scores = self._component_scores(state, TradeDirection.SHORT, h4_context)

        bullish_total = min(1.0, bullish_scores["total"] + session_span_adjustment)
        bearish_total = min(1.0, bearish_scores["total"] + session_span_adjustment)

        if bullish_total > bearish_total and bullish_total >= self._min_confidence:
            direction = TradeDirection.LONG
            confidence = bullish_total
            component_scores = bullish_scores
        elif bearish_total > bullish_total and bearish_total >= self._min_confidence:
            direction = TradeDirection.SHORT
            confidence = bearish_total
            component_scores = bearish_scores
        else:
            return None

        spread = (state.latest_bar.high - state.latest_bar.low) * 0.1
        entry = state.latest_bar.close + spread if direction == TradeDirection.LONG else state.latest_bar.close - spread

        sl, tp1, tp2, tp3 = self._calculate_levels(state, direction, entry)

        risk = abs(entry - sl)
        if risk == 0:
            return None

        rr = abs(tp2 - entry) / risk

        rationale = self._build_rationale(state, direction, confidence, h4_context, is_multi_session)

        return ConfluenceSignal(
            direction=direction,
            strength=self._classify_strength(confidence),
            confidence_score=confidence,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            signal_time=state.latest_bar.time,
            rationale=rationale,
            has_order_block=self._has_ob_confluence(state, direction),
            has_fvg=self._has_fvg_confluence(state, direction),
            has_liquidity_sweep=self._has_sweep_confluence(state, direction),
            has_premium_discount_confluence=self._has_pd_confluence(state, direction),
            has_structure_alignment=state.structure_bias == direction,
            confluence_count=self._count_confluences(state, direction, h4_context),
            risk_reward_ratio=rr,
            structure_score=component_scores["structure"],
            ob_score=component_scores["ob"],
            fvg_score=component_scores["fvg"],
            liq_sweep_score=component_scores["sweep"],
            pd_zone_score=component_scores["pd"],
            session_score=component_scores["session"],
            session_span_count=session_span_count,
            is_multi_session=is_multi_session,
        )

    def _component_scores(
        self,
        state: ICTMarketState,
        direction: TradeDirection,
        h4_context=None,
    ) -> dict:
        structure_score = self._score_structure(state, direction)
        ob_score = self._score_order_blocks(state, direction)
        fvg_score = self._score_fvg(state, direction)
        sweep_score = self._score_sweeps(state, direction)
        pd_score = self._score_premium_discount(state, direction)
        session_score = self._score_session(state)

        total = (
            structure_score * self._structure_weight
            + ob_score * self._ob_weight
            + fvg_score * self._fvg_weight
            + sweep_score * self._sweep_weight
            + pd_score * self._pd_weight
            + session_score * self._session_weight
        )

        if h4_context is not None:
            h4_score = h4_context.bullish_score if direction == TradeDirection.LONG else h4_context.bearish_score
            total += h4_score * self._h4_weight

        return {
            "structure": structure_score,
            "ob": ob_score,
            "fvg": fvg_score,
            "sweep": sweep_score,
            "pd": pd_score,
            "session": session_score,
            "total": min(1.0, total),
        }

    def _calculate_directional_score(
        self,
        state: ICTMarketState,
        direction: TradeDirection,
        h4_context=None,
    ) -> float:
        return self._component_scores(state, direction, h4_context)["total"]

    def _score_structure(self, state: ICTMarketState, direction: TradeDirection) -> float:
        if state.structure_bias != direction:
            return 0.1

        strength = self._structure_analyzer.get_structure_strength(state)
        bonus = 0.0

        recent_breaks = [sb for sb in state.structure_breaks if sb.direction == direction]
        if recent_breaks:
            last_break = max(recent_breaks, key=lambda sb: sb.time)
            if last_break.is_choch:
                bonus += 0.15
            bonus += last_break.break_strength * 0.15

        return min(1.0, strength + bonus)

    def _score_order_blocks(self, state: ICTMarketState, direction: TradeDirection) -> float:
        ob = self._ob_detector.get_most_relevant(state, direction)
        if ob is None:
            return 0.0

        score = ob.strength * 0.7
        if ob.age <= 2:
            score += 0.2
        elif ob.age <= 4:
            score += 0.1

        current_price = state.latest_bar.close
        ob_mid = (ob.top + ob.bottom) / 2
        distance = abs(current_price - ob_mid) / state.atr

        if distance <= 1.0:
            score += 0.2
        elif distance <= 2.0:
            score += 0.1

        return min(1.0, score)

    def _score_fvg(self, state: ICTMarketState, direction: TradeDirection) -> float:
        fvg = self._fvg_detector.get_nearest_unfilled(state, direction, state.latest_bar.close)
        if fvg is None:
            return 0.0

        score = 0.4
        if fvg.age <= 3:
            score += 0.2
        elif fvg.age <= 8:
            score += 0.1

        normalized_size = fvg.size / state.atr
        if normalized_size > 1.5:
            score += 0.2
        elif normalized_size > 0.5:
            score += 0.1

        if fvg.is_filled:
            score *= 0.5

        return min(1.0, score)

    def _score_sweeps(self, state: ICTMarketState, direction: TradeDirection) -> float:
        recent_sweeps = [s for s in state.recent_sweeps if s.implied_direction == direction]
        if not recent_sweeps:
            return 0.0

        best_sweep = max(recent_sweeps, key=lambda s: s.strength)
        score = best_sweep.strength * 0.5

        if best_sweep.session in (SessionType.LONDON, SessionType.NY_AM):
            score += 0.2

        sweep_age = (state.latest_bar.time - best_sweep.time).total_seconds() / 60
        if sweep_age <= 30:
            score += 0.3
        elif sweep_age <= 90:
            score += 0.15

        return min(1.0, score)

    def _score_premium_discount(self, state: ICTMarketState, direction: TradeDirection) -> float:
        if state.pd_zone is None:
            return 0.3

        pd = state.pd_zone
        if direction == TradeDirection.LONG and pd.is_in_discount:
            return 0.7 + pd.zone_strength * 0.3
        if direction == TradeDirection.SHORT and pd.is_in_premium:
            return 0.7 + pd.zone_strength * 0.3
        if pd.is_in_equilibrium:
            return 0.4
        return 0.1

    def _score_session(self, state: ICTMarketState) -> float:
        session_scores = {
            SessionType.LONDON: 0.8,
            SessionType.NY_AM: 0.9,
            SessionType.NY_PM: 0.6,
            SessionType.OUTSIDE: 0.1,
        }
        return session_scores.get(state.current_session, 0.1)

    def _calculate_levels(
        self, state: ICTMarketState, direction: TradeDirection, entry: float
    ) -> tuple[float, float, float, float]:
        atr = state.atr

        if direction == TradeDirection.LONG:
            sl = entry - atr * self._default_sl_multiplier
        else:
            sl = entry + atr * self._default_sl_multiplier

        risk = abs(entry - sl)

        if direction == TradeDirection.LONG:
            tp1 = entry + risk * self._tp1_rr
            tp2 = entry + risk * self._tp2_rr
            tp3 = entry + risk * self._tp3_rr
        else:
            tp1 = entry - risk * self._tp1_rr
            tp2 = entry - risk * self._tp2_rr
            tp3 = entry - risk * self._tp3_rr

        return (sl, tp1, tp2, tp3)

    def _build_rationale(
        self,
        state: ICTMarketState,
        direction: TradeDirection,
        confidence: float,
        h4_context=None,
        is_multi_session: bool = False,
    ) -> str:
        lines = [f"{direction.value.upper()} signal (confidence: {confidence:.2f})"]

        if state.structure_bias == direction:
            lines.append("- Structure aligned")

        recent_breaks = [sb for sb in state.structure_breaks if sb.direction == direction]
        if recent_breaks:
            last_break = max(recent_breaks, key=lambda sb: sb.time)
            if last_break.is_choch:
                lines.append("- CHoCH detected (high conviction)")

        if self._has_ob_confluence(state, direction):
            lines.append("- Order block confluence")
        if self._has_fvg_confluence(state, direction):
            lines.append("- Fair Value Gap present")
        if self._has_sweep_confluence(state, direction):
            lines.append("- Liquidity swept")

        if self._has_pd_confluence(state, direction):
            if direction == TradeDirection.LONG:
                lines.append("- Price in discount zone")
            else:
                lines.append("- Price in premium zone")

        if is_multi_session:
            lines.append("- Multi-session formation (higher quality)")

        if h4_context is not None:
            h4_confluences = (
                h4_context.confluence_count_bullish
                if direction == TradeDirection.LONG
                else h4_context.confluence_count_bearish
            )
            if h4_confluences > 0:
                lines.append(f"- H4 context confirmation ({h4_confluences} zone{'s' if h4_confluences > 1 else ''})")

        lines.append(f"- Confluence count: {self._count_confluences(state, direction, h4_context)}")
        return "\n".join(lines)

    def _has_ob_confluence(self, state: ICTMarketState, direction: TradeDirection) -> bool:
        return self._ob_detector.get_most_relevant(state, direction) is not None

    def _has_fvg_confluence(self, state: ICTMarketState, direction: TradeDirection) -> bool:
        return self._fvg_detector.get_nearest_unfilled(state, direction, state.latest_bar.close) is not None

    def _has_sweep_confluence(self, state: ICTMarketState, direction: TradeDirection) -> bool:
        return any(
            s.implied_direction == direction and (state.latest_bar.time - s.time).total_seconds() / 60 <= 120
            for s in state.recent_sweeps
        )

    def _has_pd_confluence(self, state: ICTMarketState, direction: TradeDirection) -> bool:
        if state.pd_zone is None:
            return False
        if direction == TradeDirection.LONG:
            return state.pd_zone.is_in_discount
        return state.pd_zone.is_in_premium

    def _count_confluences(
        self,
        state: ICTMarketState,
        direction: TradeDirection,
        h4_context=None,
    ) -> int:
        count = 0
        if state.structure_bias == direction:
            count += 1
        if self._has_ob_confluence(state, direction):
            count += 1
        if self._has_fvg_confluence(state, direction):
            count += 1
        if self._has_sweep_confluence(state, direction):
            count += 1
        if self._has_pd_confluence(state, direction):
            count += 1
        if state.current_session != SessionType.OUTSIDE:
            count += 1
        if h4_context is not None:
            h4_confluences = (
                h4_context.confluence_count_bullish
                if direction == TradeDirection.LONG
                else h4_context.confluence_count_bearish
            )
            if h4_confluences > 0:
                count += 1
        return count

    @staticmethod
    def _classify_strength(confidence: float) -> SignalStrength:
        if confidence >= 0.85:
            return SignalStrength.VERY_STRONG
        if confidence >= 0.70:
            return SignalStrength.STRONG
        if confidence >= 0.55:
            return SignalStrength.MODERATE
        return SignalStrength.WEAK
