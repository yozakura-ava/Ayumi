"""Signal Orchestrator — wires confidence → routing → sizing → execution."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from confidence.engine import ConfidenceEngine
from risk.profile_router import Profile, ProfileRouter
from risk.sl_position_sizer import SLPositionSizer

logger = logging.getLogger("ayumi.orchestrator")


@dataclass
class OrchestratorTradeSignal:
    """Raw signal from a strategy."""

    strategy_id: str
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: float  # Raw strategy confidence (0.0-1.0)
    timestamp: datetime
    metadata: dict = field(default_factory=dict)


@dataclass
class OrchestratedOrder:
    """Final order ready for execution."""

    signal: OrchestratorTradeSignal
    profile: Profile
    confidence_final: float
    lots: float
    risk_amount: float
    sl_distance_pips: float
    gates_passed: list[str]
    rejected: bool = False
    rejection_reason: str = ""


class SignalOrchestrator:
    """Wires together confidence → routing → sizing → execution."""

    def __init__(
        self,
        confidence_engine: ConfidenceEngine,
        profile_router: ProfileRouter,
        position_sizer: SLPositionSizer,
        account_balance: float = 10000.0,
    ):
        self._engine = confidence_engine
        self._router = profile_router
        self._sizer = position_sizer
        self._account_balance = account_balance

    def process_signal(self, signal: OrchestratorTradeSignal) -> OrchestratedOrder:
        """Full pipeline: signal → confidence → routing → sizing → order."""

        logger.info(
            "Signal received: strategy=%s symbol=%s direction=%s confidence=%.3f",
            signal.strategy_id,
            signal.symbol,
            signal.direction,
            signal.confidence,
        )

        # Step 1: Run through confidence engine
        confidence_result = self._engine.score(
            raw_confidence=signal.confidence,
            symbol=signal.symbol,
            direction=signal.direction,
            spread=signal.metadata.get("spread", 0.0),
            atr=signal.metadata.get("atr", 0.0),
            confluences=signal.metadata.get("confluences"),
        )

        if confidence_result.blocked:
            gates_str = ",".join(confidence_result.gates_failed)
            logger.warning(
                "Signal rejected: strategy=%s symbol=%s reason=confidence_gate gate=%s final_score=%.3f",
                signal.strategy_id,
                signal.symbol,
                gates_str,
                confidence_result.final_score,
            )
            return OrchestratedOrder(
                signal=signal,
                profile=Profile.SNIPER,
                confidence_final=confidence_result.final_score,
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=0.0,
                gates_passed=confidence_result.gates_passed,
                rejected=True,
                rejection_reason=confidence_result.block_reason,
            )

        # Step 2: Route to profile
        profile = self._router.route(confidence_result.final_score)
        if profile is None:
            logger.warning(
                "Signal rejected: strategy=%s symbol=%s reason=routing_threshold score=%.3f threshold=%.2f",
                signal.strategy_id,
                signal.symbol,
                confidence_result.final_score,
                ProfileRouter.SWARM_THRESHOLD,
            )
            return OrchestratedOrder(
                signal=signal,
                profile=Profile.SNIPER,
                confidence_final=confidence_result.final_score,
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=0.0,
                gates_passed=confidence_result.gates_passed,
                rejected=True,
                rejection_reason="Below minimum confidence threshold",
            )

        # Step 3: Size position
        logger.info(
            "Routing: strategy=%s symbol=%s → profile=%s",
            signal.strategy_id,
            signal.symbol,
            profile,
        )
        size_result = self._sizer.calculate(
            symbol=signal.symbol,
            entry_price=signal.entry_price,
            sl_price=signal.stop_loss,
            profile=profile,
        )

        if size_result.blocked:
            logger.warning(
                "Signal rejected: strategy=%s symbol=%s reason=sizing_block=%s "
                "sl_pips=%.1f daily_risk_remaining=%.2f open_risk=%.2f",
                signal.strategy_id,
                signal.symbol,
                size_result.block_reason,
                size_result.sl_distance_pips,
                self._sizer.daily_risk_remaining,
                self._sizer.open_risk,
            )
        else:
            logger.info(
                "Signal accepted: strategy=%s symbol=%s lots=%.4f risk=$%.2f sl=%.1fpips",
                signal.strategy_id,
                signal.symbol,
                size_result.lots,
                size_result.risk_amount,
                size_result.sl_distance_pips,
            )

        return OrchestratedOrder(
            signal=signal,
            profile=profile,
            confidence_final=confidence_result.final_score,
            lots=size_result.lots,
            risk_amount=size_result.risk_amount,
            sl_distance_pips=size_result.sl_distance_pips,
            gates_passed=confidence_result.gates_passed,
            rejected=size_result.blocked,
            rejection_reason=size_result.block_reason,
        )

    def update_balance(self, balance: float):
        """Update account balance across all components."""
        self._account_balance = balance
        self._sizer.update_balance(balance)
