"""Multi-layer confidence scoring for trade signals.

Pipeline: Strategy Score → Confluence Boost → Gate Validator → Final Score
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from confidence.gates import (
    GateCheck,
    GateConfig,
    SessionGate,
    SpreadGate,
    VolatilityGate,
)


@dataclass
class ConfidenceResult:
    """Output of the confidence engine."""

    final_score: float
    strategy_score: float
    confluence_boost: float
    gates_passed: list[str] = field(default_factory=list)
    gates_failed: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""


class ConfidenceEngine:
    """Multi-layer confidence scoring for trade signals.

    Pipeline: Strategy Score → Confluence Boost → Gate Validator → Final Score

    The engine takes raw strategy signals and produces a final confidence
    score that the ProfileRouter uses to route to Sniper/Swarm/Rejected.
    """

    def __init__(
        self,
        gate_config: Optional[GateConfig] = None,
        confluence_detector=None,
    ):
        self._gate_config = gate_config or GateConfig()
        self._confluence_detector = confluence_detector
        self._gates = [
            SpreadGate(self._gate_config),
            SessionGate(self._gate_config),
            VolatilityGate(self._gate_config),
        ]

    def add_gate(self, gate) -> None:
        """Add a custom gate to the pipeline."""
        self._gates.append(gate)

    def score(
        self,
        raw_confidence: float,
        *,
        symbol: str = "",
        direction: str = "long",
        spread: float = 0.0,
        atr: float = 0.0,
        hour_utc: int = 0,
        confluences: Optional[list[dict]] = None,
    ) -> ConfidenceResult:
        """Run the full confidence pipeline.

        Args:
            raw_confidence: Strategy's raw confidence (0.0-1.0).
            symbol: Trading symbol for gate checks.
            direction: "long" or "short".
            spread: Current spread in pips.
            atr: Current ATR value.
            hour_utc: Current hour in UTC (0-23) for session gate.
            confluences: List of confluence signals. Each dict has:
                - "strategy": strategy name
                - "direction": "long" or "short"
                - "timeframe": e.g. "H1", "D1"

        Returns:
            ConfidenceResult with final score and gate details.
        """
        # Layer 1: Clamp strategy score
        strategy_score = max(0.0, min(1.0, raw_confidence))

        # Layer 2: Confluence boost (explicit confluences + detector)
        confluence_boost = self._calc_confluence_boost(confluences, direction, strategy_score)

        if self._confluence_detector is not None:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            result = self._confluence_detector.get_confluence(symbol, direction, now)
            if result.confluence_score > 0:
                confluence_boost = min(confluence_boost + result.confluence_score * 0.15, 0.30)

        # Layer 3: Gate validation
        gates_passed: list[str] = []
        gates_failed: list[str] = []
        gate_context = {
            "symbol": symbol,
            "direction": direction,
            "spread": spread,
            "atr": atr,
            "hour_utc": hour_utc,
        }

        for gate in self._gates:
            result: GateCheck = gate.check(gate_context)
            if result.passed:
                gates_passed.append(result.gate_name)
                strategy_score = min(1.0, strategy_score + result.boost)
            else:
                gates_failed.append(result.gate_name)
                return ConfidenceResult(
                    final_score=0.0,
                    strategy_score=strategy_score,
                    confluence_boost=confluence_boost,
                    gates_passed=gates_passed,
                    gates_failed=gates_failed,
                    blocked=True,
                    block_reason=result.reason,
                )

        final_score = min(1.0, strategy_score + confluence_boost)

        return ConfidenceResult(
            final_score=final_score,
            strategy_score=strategy_score,
            confluence_boost=confluence_boost,
            gates_passed=gates_passed,
            gates_failed=gates_failed,
        )

    def _calc_confluence_boost(
        self,
        confluences: Optional[list[dict]],
        direction: str,
        current_score: float,
    ) -> float:
        """Calculate confluence boost from agreeing signals."""
        if not confluences:
            return 0.0

        boost = 0.0
        seen_strategies: set[str] = set()
        seen_timeframes: set[str] = set()

        for c in confluences:
            name = c.get("strategy", "")
            c_dir = c.get("direction", "")
            tf = c.get("timeframe", "")

            # Strategy agreement boost (up to 0.10 total)
            if name and name not in seen_strategies and c_dir == direction:
                boost += 0.05
                seen_strategies.add(name)

            # Timeframe alignment boost (up to 0.05 total)
            if tf and tf not in seen_timeframes and c_dir == direction:
                boost += 0.025
                seen_timeframes.add(tf)

        # Cap boosts
        boost = min(boost, 0.15)

        # Final score can't exceed 1.0
        if current_score + boost > 1.0:
            boost = max(0.0, 1.0 - current_score)

        return boost
