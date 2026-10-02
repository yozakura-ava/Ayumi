"""§4.4-4.6 — HTF phase analysis, multi-timeframe alignment, and dual-mechanism reconciliation."""

from __future__ import annotations

from typing import Optional

import numpy as np

from .data_types import HTFPhase, HTFState
from .thresholds import BOARDROOM_RANGE


class HTFAnalyzer:
    """Analyze higher-timeframe context for gating lower-timeframe signals."""

    def __init__(self):
        pass

    def analyze_phase(
        self,
        tf_data: dict,
        bars_closed: bool = True,
    ) -> HTFState:
        """Evaluate HTF phase from a single timeframe's data.

        Args:
            tf_data: dict with keys:
                - highs: array of high prices
                - lows: array of low prices
                - closes: array of close prices
                - ema_50: optional array of 50 EMA values
                - atr: optional float (ATR for volatility context)
            bars_closed: if False, return neutral (bar still forming)

        Returns:
            HTFState with phase, alignment_score, ema_slope, range_size.
        """
        if not bars_closed:
            return HTFState(HTFPhase.NEUTRAL, 0.0, 0.0, 0.0)

        highs = np.asarray(tf_data.get("highs", []))
        lows = np.asarray(tf_data.get("lows", []))
        closes = np.asarray(tf_data.get("closes", []))
        ema_50 = np.asarray(tf_data["ema_50"]) if "ema_50" in tf_data else None

        if len(highs) < 20:
            return HTFState(HTFPhase.NEUTRAL, 0.0, 0.0, 0.0)

        # Range size over last 20 bars
        recent_high = float(np.max(highs[-20:]))
        recent_low = float(np.min(lows[-20:]))
        range_size = (recent_high - recent_low) / recent_low if recent_low > 0 else 0.0

        # EMA slope (normalized — change per bar as fraction of price)
        ema_slope = 0.0
        if ema_50 is not None and len(ema_50) >= 20:
            slope_raw = ema_50[-1] - ema_50[-20]
            ema_slope = slope_raw / (20 * ema_50[-1]) if ema_50[-1] > 0 else 0.0

        # Phase determination
        phase = self._determine_phase(range_size, ema_slope, highs, lows, closes)

        # Alignment score — placeholder; full MTF scoring uses analyze_htf_alignment
        alignment_score = 0.0

        return HTFState(phase, alignment_score, ema_slope, range_size)

    def _determine_phase(
        self,
        range_size: float,
        ema_slope: float,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
    ) -> HTFPhase:
        """Classify the HTF phase."""
        # Consolidating: tight range + flat EMA
        if range_size <= BOARDROOM_RANGE and abs(ema_slope) < 0.0001:
            return HTFPhase.CONSOLIDATING

        # Exhaustion: at period extremes — check if recent high is near
        # the overall high (simplified proxy for R3/D3)
        if len(highs) >= 50:
            period_high = float(np.max(highs))
            period_low = float(np.min(lows))
            current_price = float(closes[-1])
            total_range = period_high - period_low
            if total_range > 0:
                near_high = (period_high - current_price) / total_range < 0.05
                near_low = (current_price - period_low) / total_range < 0.05
                if near_high or near_low:
                    return HTFPhase.EXHAUSTION

        # Directional: trending with EMA slope
        if abs(ema_slope) >= 0.0001:
            return HTFPhase.ALIGNED

        return HTFPhase.NEUTRAL

    def analyze_htf_alignment(self, mtf_data: dict) -> float:
        """Score multi-timeframe alignment (0.0-1.0) per §4.5.

        Args:
            mtf_data: dict mapping timeframe names to their direction
                e.g. {"D1": "bullish", "H4": "bullish", "H1": "bullish", "M15": "bearish"}

        Returns:
            Alignment score: 1.0 (4/4), 0.75 (3/4 incl H4), 0.50 (2/4 incl H1+H4),
            0.25 (2/4 LTFs only), 0.0 (0-1/4).
        """
        if not mtf_data:
            return 0.0

        # Determine dominant direction from D1 and H4
        htf_directions = []
        for tf in ("D1", "H4"):
            if tf in mtf_data:
                htf_directions.append(mtf_data[tf])

        if not htf_directions:
            return 0.0

        # Use the first available HTF direction as reference
        dominant = htf_directions[0]
        all_tfs = ["D1", "H4", "H1", "M15"]
        agreeing = [tf for tf in all_tfs if tf in mtf_data and mtf_data[tf] == dominant]
        total = len([tf for tf in all_tfs if tf in mtf_data])

        if total == 0:
            return 0.0

        count = len(agreeing)

        # H4 must be included for high scores
        h4_agrees = "H4" in agreeing
        h1_agrees = "H1" in agreeing

        if count >= 4:
            return 1.0
        if count == 3 and h4_agrees:
            return 0.75
        if count == 2 and h4_agrees and h1_agrees:
            return 0.50
        if count == 2 and not h4_agrees:
            return 0.25
        if count <= 1:
            return 0.0
        return 0.0

    def reconcile_dual_mechanism(
        self,
        htf_phase: HTFPhase,
        ltf_pattern_direction: str,
        htf_direction: Optional[str] = None,
    ) -> dict:
        """Reconcile HTF phase with LTF pattern signals per §4.4.

        Returns dict with:
        - htf_phase: the resolved phase (aligned/conflicting/consolidating/exhaustion/neutral)
        - htf_modifier: numeric modifier for confluence scorer (-0.25 to +0.15)
        - allowed: whether the LTF pattern should proceed
        """
        if htf_phase == HTFPhase.CONSOLIDATING:
            return {
                "htf_phase": HTFPhase.CONSOLIDATING,
                "htf_modifier": -0.15,
                "allowed": True,  # degraded but not blocked
            }

        if htf_phase == HTFPhase.EXHAUSTION:
            # Reversal setups favored at exhaustion
            return {
                "htf_phase": HTFPhase.EXHAUSTION,
                "htf_modifier": 0.10,
                "allowed": True,
            }

        if htf_phase == HTFPhase.NEUTRAL:
            return {
                "htf_phase": HTFPhase.NEUTRAL,
                "htf_modifier": -0.05,
                "allowed": True,
            }

        # ALIGNED — check if LTF direction matches HTF
        if htf_direction and htf_direction != ltf_pattern_direction:
            return {
                "htf_phase": HTFPhase.CONFLICTING,
                "htf_modifier": -0.25,
                "allowed": True,  # gates handle hard fails
            }

        return {
            "htf_phase": HTFPhase.ALIGNED,
            "htf_modifier": 0.15,
            "allowed": True,
        }
