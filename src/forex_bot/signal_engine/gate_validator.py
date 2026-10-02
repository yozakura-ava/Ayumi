"""§6 — Gate validator: hard gates and quality threshold for signal candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .data_types import HTFPhase, HTFState, Level, LevelType, SessionState, Signal
from .thresholds import LEVEL_COMPLETION_RATIO, MW_SYMMETRY_MAX


@dataclass
class GateResult:
    """Result of gate validation."""

    passed: bool
    failed_gates: list[str] = field(default_factory=list)
    gate_details: dict[str, bool] = field(default_factory=dict)
    quality_score: float = 0.0
    notes: str = ""


class GateValidator:
    """Validate hard requirements before a signal enters confluence scoring.

    Hard gates (ALL must pass):
    - G1: M/W symmetry (SL1/SL2 ≤ 1.5%, SH1/SH2 ≤ 1.5%)
    - G2: Level completion (R3 ≥ 90% of R2, D3 ≥ 90% of D2)
    - G3: Session alignment (signal direction matches session bias)
    - G4: HTF trend alignment
    - G5: No conflicting signals on same pair
    """

    def __init__(
        self,
        quality_threshold: float = 0.6,
        symmetry_max: float = MW_SYMMETRY_MAX,
        level_completion_ratio: float = LEVEL_COMPLETION_RATIO,
    ):
        self.quality_threshold = quality_threshold
        self.symmetry_max = symmetry_max
        self.level_completion_ratio = level_completion_ratio
        # Track active signals per pair for G5
        self._active_signals: dict[str, Signal] = {}

    def validate(
        self,
        candidate: dict,
        htf_state: Optional[HTFState] = None,
        session_state: Optional[SessionState] = None,
        levels: Optional[list[Level]] = None,
    ) -> GateResult:
        """Validate all gates for a signal candidate.

        Args:
            candidate: dict with keys:
                - direction: "long" or "short"
                - symbol: pair name (e.g. "EURUSD")
                - confidence: float 0-1
                - pattern_type: str
                - key_levels: dict with optional SH1/SH2/SL1/SL2
            htf_state: HTF analysis result
            session_state: session context
            levels: detected levels for G2 check

        Returns:
            GateResult with passed status and details.
        """
        details: dict[str, bool] = {}
        failed: list[str] = []

        # G1: M/W symmetry
        g1_pass = self._check_symmetry(candidate.get("key_levels", {}))
        details["G1_symmetry"] = g1_pass
        if not g1_pass:
            failed.append("G1_symmetry")

        # G2: Level completion
        g2_pass = self._check_level_completion(levels or [])
        details["G2_level_completion"] = g2_pass
        if not g2_pass:
            failed.append("G2_level_completion")

        # G3: Session alignment
        g3_pass = self._check_session_alignment(
            candidate.get("direction", ""),
            session_state,
        )
        details["G3_session_alignment"] = g3_pass
        if not g3_pass:
            failed.append("G3_session_alignment")

        # G4: HTF trend alignment
        g4_pass = self._check_htf_alignment(
            candidate.get("direction", ""),
            htf_state,
        )
        details["G4_htf_alignment"] = g4_pass
        if not g4_pass:
            failed.append("G4_htf_alignment")

        # G5: No conflicting signals
        g5_pass = self._check_no_conflict(
            candidate.get("symbol", ""),
            candidate.get("direction", ""),
        )
        details["G5_no_conflict"] = g5_pass
        if not g5_pass:
            failed.append("G5_no_conflict")

        all_passed = len(failed) == 0
        confidence = candidate.get("confidence", 0.0)

        return GateResult(
            passed=all_passed and confidence >= self.quality_threshold,
            failed_gates=failed,
            gate_details=details,
            quality_score=confidence,
            notes="All gates passed" if all_passed else f"Failed: {', '.join(failed)}",
        )

    def _check_symmetry(self, key_levels: dict) -> bool:
        """G1: Check M/W structure symmetry.

        SL1/SL2 symmetry and SH1/SH2 symmetry must each be ≤ 1.5%.
        If the candidate doesn't have these levels (e.g. it's not an M/W),
        this gate passes.
        """
        sh1 = key_levels.get("SH1")
        sh2 = key_levels.get("SH2")
        sl1 = key_levels.get("SL1")
        sl2 = key_levels.get("SL2")

        # If no M/W levels present, gate passes (not an M/W pattern)
        if not all(v is not None for v in (sh1, sh2, sl1, sl2)):
            return True

        # M/W symmetry: left leg (SH1→SL1) vs right leg (SH2→SL2)
        # Both legs should be roughly equal in size
        left_leg = abs(sh1 - sl1)
        right_leg = abs(sh2 - sl2)
        avg_leg = (left_leg + right_leg) / 2
        if avg_leg > 0:
            leg_diff = abs(left_leg - right_leg) / avg_leg
            if leg_diff > self.symmetry_max:
                return False

        # Also check SH1-SL2 symmetry: the two highs should be roughly
        # equidistant from the center of the two lows
        center = (sl1 + sl2) / 2
        sh1_dist = abs(sh1 - center)
        sh2_dist = abs(sh2 - center)
        sh_avg = (sh1_dist + sh2_dist) / 2
        if sh_avg > 0:
            sh_diff = abs(sh1_dist - sh2_dist) / sh_avg
            if sh_diff > self.symmetry_max:
                return False

        return True

    def _check_level_completion(self, levels: list[Level]) -> bool:
        """G2: Verify R3/D3 magnitude is ≥ 90% of R2/D2.

        If no R3/D3 levels exist, this gate passes.
        """
        r2_mag = None
        r3_mag = None
        d2_mag = None
        d3_mag = None

        for lv in levels:
            if lv.level_type == LevelType.R2:
                r2_mag = lv.magnitude
            elif lv.level_type == LevelType.R3:
                r3_mag = lv.magnitude
            elif lv.level_type == LevelType.D2:
                d2_mag = lv.magnitude
            elif lv.level_type == LevelType.D3:
                d3_mag = lv.magnitude

        # Check R3 vs R2
        if r3_mag is not None and r2_mag is not None and r2_mag > 0:
            if r3_mag < r2_mag * self.level_completion_ratio:
                return False

        # Check D3 vs D2
        if d3_mag is not None and d2_mag is not None and d2_mag > 0:
            if d3_mag < d2_mag * self.level_completion_ratio:
                return False

        return True

    def _check_session_alignment(
        self,
        direction: str,
        session_state: Optional[SessionState],
    ) -> bool:
        """G3: Signal direction must match session directional bias.

        If no session bias is set, this gate passes (neutral).
        """
        if session_state is None or session_state.directional_bias is None:
            return True

        bias = session_state.directional_bias
        if bias == "bullish" and direction != "long":
            return False
        if bias == "bearish" and direction != "short":
            return False

        return True

    def _check_htf_alignment(
        self,
        direction: str,
        htf_state: Optional[HTFState],
    ) -> bool:
        """G4: HTF must not be in a conflicting state.

        - ALIGNED phase: direction must match EMA slope
        - CONSOLIDATING: pass (degraded, not blocked)
        - EXHAUSTION: pass (reversal setups OK)
        - NEUTRAL: pass
        - CONFLICTING: fail
        """
        if htf_state is None:
            return True

        if htf_state.phase == HTFPhase.CONFLICTING:
            return False

        if htf_state.phase == HTFPhase.ALIGNED:
            # Direction must match EMA slope
            if htf_state.ema_slope > 0 and direction != "long":
                return False
            if htf_state.ema_slope < 0 and direction != "short":
                return False

        return True

    def _check_no_conflict(
        self,
        symbol: str,
        direction: str,
    ) -> bool:
        """G5: No opposing active signal on the same pair."""
        if symbol not in self._active_signals:
            return True

        existing = self._active_signals[symbol]
        if existing.direction != direction:
            return False

        return True

    def register_signal(self, signal: Signal) -> None:
        """Register an active signal for conflict detection."""
        self._active_signals[signal.symbol] = signal

    def clear_signal(self, symbol: str) -> None:
        """Clear an active signal (e.g. on SL hit or TP reached)."""
        self._active_signals.pop(symbol, None)

    def clear_all(self) -> None:
        """Clear all active signals."""
        self._active_signals.clear()
