"""ConfluenceWrappedStrategy — adapter wrapping old ISignalStrategy implementations
with the TTC confluence scoring pipeline (GateValidator + ConfluenceScorer).

This gives old strategies the TTC quality filters without modifying their internals:
  old strategy logic (entry triggers) + TTC gates + confluence scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from signal_engine import ConfluenceScorer, GateValidator
from signal_engine.data_types import HTFPhase, HTFState, SessionState

from ..engine import Bar, MarketState, StrategySignal, TradeDirection
from ..strategy_legacy import ISignalStrategy


@dataclass(frozen=True)
class ConfluenceWrapperConfig:
    min_confidence: float = 0.3
    use_gates: bool = True
    boost_weights: dict[str, float] = field(default_factory=dict)


class ConfluenceWrappedStrategy(ISignalStrategy):
    """Wrap any ISignalStrategy with TTC gate + confluence filtering."""

    def __init__(
        self,
        inner: ISignalStrategy,
        symbol: str = "EURUSD",
        timeframe: str = "M15",
        config: ConfluenceWrapperConfig | None = None,
    ):
        self._inner = inner
        self.symbol = symbol
        self.timeframe = timeframe
        self._config = config or ConfluenceWrapperConfig()
        self._gate_validator = GateValidator()
        self._confluence_scorer = ConfluenceScorer()

    @property
    def name(self) -> str:
        return f"ConfluenceWrapped({self._inner.name})"

    def reset(self) -> None:
        if hasattr(self._inner, "reset"):
            self._inner.reset()

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        # Step 1: Get signal from inner strategy
        signal = self._inner.evaluate(state)
        if signal is None:
            return None

        bars = state.bars
        direction = "long" if signal.direction == TradeDirection.LONG else "short"

        # Step 2: Build candidate dict for gates + confluence
        candidate = self._build_candidate(signal, direction, bars)

        # Step 3: Build minimal HTF/Session state from bars
        htf_state = self._build_htf_state(bars, direction)
        session_state = self._build_session_state(bars)

        # Step 4: Gate validation
        if self._config.use_gates:
            gate_result = self._gate_validator.validate(
                candidate=candidate,
                htf_state=htf_state,
                session_state=session_state,
                levels=[],
            )
            if not gate_result.passed:
                return None

        # Step 5: Confluence scoring
        htf_dict = {"alignment_score": htf_state.alignment_score} if htf_state else {}
        session_dict = {
            "phase_score": session_state.phase_score if session_state else 0.0,
            "kill_zone_active": session_state.kill_zone_active if session_state else False,
        }
        conf_score, _ = self._confluence_scorer.score(
            candidate=candidate,
            htf_state=htf_dict,
            session_state=session_dict,
        )

        # Blend inner confidence with confluence score
        blended = signal.confidence * 0.6 + conf_score * 0.4

        if blended < self._config.min_confidence:
            return None

        return StrategySignal(
            direction=signal.direction,
            confidence=blended,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            rationale=f"[conf={blended:.2f}] {signal.rationale}",
        )

    def _build_candidate(self, signal: StrategySignal, direction: str, bars: list[Bar]) -> dict:
        return {
            "direction": direction,
            "symbol": self.symbol,
            "confidence": signal.confidence,
            "pattern_type": "wrapped",
            "level_proximity_pct": self._estimate_level_proximity(bars),
            "ema_distance_pct": self._estimate_ema_distance(bars),
            "volume_ratio": self._volume_ratio(bars),
        }

    @staticmethod
    def _estimate_level_proximity(bars: list[Bar]) -> float:
        if len(bars) < 20:
            return 1.0
        recent = bars[-20:]
        price = recent[-1].close
        high = max(b.high for b in recent)
        low = min(b.low for b in recent)
        rng = high - low
        if rng <= 0:
            return 1.0
        dist_to_mid = abs(price - (high + low) / 2) / rng
        return 1.0 - dist_to_mid

    @staticmethod
    def _estimate_ema_distance(bars: list[Bar]) -> float:
        if len(bars) < 21:
            return 1.0
        ema20 = sum(b.close for b in bars[-21:-1]) / 20
        price = bars[-1].close
        return abs(price - ema20) / price

    @staticmethod
    def _volume_ratio(bars: list[Bar]) -> float:
        if len(bars) < 21:
            return 0.0
        avg = sum(b.volume for b in bars[-21:-1]) / 20
        if avg <= 0:
            return 0.0
        return bars[-1].volume / avg

    @staticmethod
    def _build_htf_state(bars: list[Bar], direction: str) -> HTFState | None:
        if len(bars) < 50:
            return None
        ema50 = sum(b.close for b in bars[-50:]) / 50
        ema20 = sum(b.close for b in bars[-20:]) / 20
        slope = (ema20 - ema50) / ema50 if ema50 > 0 else 0.0
        alignment = slope * 100  # normalize to ~[-1, 1]
        alignment = max(-1.0, min(1.0, alignment))

        if abs(slope) < 0.001:
            phase = HTFPhase.CONSOLIDATING
        elif (direction == "long" and slope > 0) or (direction == "short" and slope < 0):
            phase = HTFPhase.ALIGNED
        else:
            phase = HTFPhase.CONFLICTING

        return HTFState(
            ema_slope=slope,
            alignment_score=alignment,
            phase=phase,
            range_size=0.0,
        )

    @staticmethod
    def _build_session_state(bars: list[Bar]) -> SessionState | None:
        if not bars:
            return None
        hour = bars[-1].time.hour
        # Simple session scoring
        if 7 <= hour < 12:
            phase_score = 0.8
            kill_zone = 7 <= hour < 9
        elif 12 <= hour < 17:
            phase_score = 0.7
            kill_zone = 12 <= hour < 14
        elif 0 <= hour < 7:
            phase_score = 0.4
            kill_zone = False
        else:
            phase_score = 0.3
            kill_zone = False

        return SessionState(
            session_name="auto",
            kill_zone_active=kill_zone,
            phase_score=phase_score,
            directional_bias=None,
        )
