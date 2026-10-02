"""§7 — Confluence scorer. Weights multiple booster factors for signal confidence.

Implements all 14 booster factors from spec §7.1 (signal-confidence-engine-v2.3.md)
plus pattern_type as a bonus factor (uses the 5% slack in the weight budget).

Weight total: 0.95 (spec) + 0.05 (pattern_type slack) = 1.00
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .thresholds import (
    EMA_TOUCH_THRESHOLD,
    PERIOD_EXTREME_T1,
    PERIOD_EXTREME_T2,
    PERIOD_EXTREME_T3,
)

# ──────────────────────────────────────────────────────────────────────
# Spec §7.1 Forex Weight Table (total 0.95, 5% slack)
# ──────────────────────────────────────────────────────────────────────

WEIGHT_MTF_ALIGNMENT = 0.14  # §4.5 multi-TF agreement
WEIGHT_MULTI_SESSION = 0.10  # 3+ sessions = 1.0
WEIGHT_SVC_PRESENT = 0.10  # SVC at entry = 1.0
WEIGHT_HITS_TO_LEVEL = 0.08  # hit count → score mapping
WEIGHT_HITS_WITH_VOLUME = 0.05  # increasing vol per hit = 1.0
WEIGHT_NEAR_PERIOD_EXTREME = 0.08  # §1.2 proximity tiers
WEIGHT_HTF_NOT_CONSOLIDATING = 0.08  # H4 not consolidating = 1.0
WEIGHT_KILL_ZONE = 0.06  # in kill zone = 1.0
WEIGHT_SESSION_OVERLAP = 0.04  # in overlap = 1.0
WEIGHT_SESSION_PHASE = 0.04  # opening/mid/closing
WEIGHT_DAY_OF_WEEK = 0.04  # §8.3 weekly model
WEIGHT_ASIA_CONTROL = 0.03  # tight+consolidating = 1.0
WEIGHT_EMA_BOUNCE = 0.05  # 50 EMA touch + rejection = 1.0
WEIGHT_DXY_CORRELATION = 0.06  # DXY agrees = 1.0

# Bonus factor (uses the 5% slack)
WEIGHT_PATTERN_TYPE = 0.05


@dataclass
class BoosterResult:
    name: str
    score: float  # 0.0 to 1.0
    weight: float
    raw_detail: str = ""


class ConfluenceScorer:
    """Score confluence factors for qualified candidates.

    Only runs after gates pass. Each booster contributes a weighted
    score toward the total confluence confidence.

    All 14 spec §7.1 boosters + pattern_type bonus = 15 total boosters.
    """

    def score(
        self,
        candidate: dict,
        htf_state: Optional[dict] = None,
        session_state: Optional[dict] = None,
        mtf_state: Optional[dict] = None,
        dxy_state: Optional[dict] = None,
    ) -> tuple[float, list[BoosterResult]]:
        """Calculate total confluence score and per-booster breakdown.

        Returns (total_score, list of BoosterResult).

        Parameters
        ----------
        candidate : dict
            Candidate setup data. Keys used:
            - direction: "long" | "short"
            - level_proximity_pct: float
            - ema_distance_pct: float
            - volume_ratio: float (current vol / avg vol)
            - pattern_type: str
            - multi_session_count: int (number of sessions spanned)
            - hits_to_level: int (number of times level has been tested)
            - hits_volume_trend: str ("increasing" | "flat" | "decreasing")
            - htf_consolidating: bool (H4 in consolidation)
            - svc_present: bool (SVC candle at entry)
            - asia_range_pct: float (Asia session range %)
            - asia_trending: bool (Asia session trending)
            - ema_rejection_candle: bool (50 EMA touch + rejection close)

        htf_state : dict, optional
            - alignment_score: float (-1 to 1)

        session_state : dict, optional
            - phase_score: float (0-1)
            - kill_zone_active: bool
            - session_overlap: bool
            - day_of_week: str ("monday".."friday")
            - phase: str ("opening" | "mid" | "closing")

        mtf_state : dict, optional
            - tf_agreement_count: int (how many TFs agree)
            - includes_htf: bool (agreement includes H4+)

        dxy_state : dict, optional
            - agrees: bool (DXY direction agrees with trade)
            - flat: bool (DXY is flat/unknown)
        """
        if htf_state is None:
            htf_state = {}
        if session_state is None:
            session_state = {}
        if mtf_state is None:
            mtf_state = {}
        if dxy_state is None:
            dxy_state = {}

        boosters: list[BoosterResult] = []

        # ── 14 spec §7.1 boosters ──────────────────────────────────
        boosters.append(self._mtf_alignment_booster(mtf_state, candidate))
        boosters.append(self._multi_session_booster(candidate))
        boosters.append(self._svc_present_booster(candidate))
        boosters.append(self._hits_to_level_booster(candidate))
        boosters.append(self._hits_with_volume_booster(candidate))
        boosters.append(self._near_period_extreme_booster(candidate))
        boosters.append(self._htf_not_consolidating_booster(candidate))
        boosters.append(self._kill_zone_booster(session_state))
        boosters.append(self._session_overlap_booster(session_state))
        boosters.append(self._session_phase_booster(session_state))
        boosters.append(self._day_of_week_booster(session_state))
        boosters.append(self._asia_control_booster(candidate))
        boosters.append(self._ema_bounce_booster(candidate))
        boosters.append(self._dxy_correlation_booster(dxy_state))

        # ── Bonus: pattern_type (uses 5% slack) ───────────────────
        boosters.append(self._pattern_type_booster(candidate))

        total = sum(b.score * b.weight for b in boosters)
        return total, boosters

    # ──────────────────────────────────────────────────────────────
    # Individual boosters — spec §7.1 order
    # ──────────────────────────────────────────────────────────────

    def _mtf_alignment_booster(self, mtf_state: dict, candidate: dict) -> BoosterResult:
        """Score based on multi-timeframe agreement (§4.5)."""
        # Prefer structured MTF data; fall back to HTF alignment score
        if "tf_agreement_count" in mtf_state:
            count = mtf_state.get("tf_agreement_count", 0)
            includes_htf = mtf_state.get("includes_htf", False)

            if count == 4:  # All 4 TFs agree
                score = 1.0
            elif count >= 3 and includes_htf:
                score = 0.75
            elif count >= 2 and includes_htf:
                score = 0.50
            elif count >= 2:
                score = 0.25
            else:
                score = 0.0
        else:
            # Fallback: use HTF alignment score from candidate/htf_state
            direction = candidate.get("direction")
            alignment = mtf_state.get("alignment_score", candidate.get("htf_alignment_score", 0.0))
            if direction == "long":
                score = max(0.0, alignment)
            else:
                score = max(0.0, -alignment)

        return BoosterResult(
            "mtf_alignment",
            score,
            WEIGHT_MTF_ALIGNMENT,
            f"tf_agreement={mtf_state.get('tf_agreement_count', 'n/a')}",
        )

    def _multi_session_booster(self, candidate: dict) -> BoosterResult:
        """Score based on how many trading sessions the formation spans."""
        session_count = candidate.get("multi_session_count", 1)

        if session_count >= 3:
            score = 1.0
        elif session_count == 2:
            score = 0.7
        else:
            score = 0.2

        return BoosterResult(
            "multi_session",
            score,
            WEIGHT_MULTI_SESSION,
            f"sessions={session_count}",
        )

    def _svc_present_booster(self, candidate: dict) -> BoosterResult:
        """Score based on presence of Stopping Volume Candle at entry."""
        svc = candidate.get("svc_present", False)
        # Also check via volume_ratio as proxy if svc_present not explicit
        if not svc and candidate.get("volume_ratio", 0.0) >= 1.5:
            svc = True

        score = 1.0 if svc else 0.0
        return BoosterResult(
            "svc_present",
            score,
            WEIGHT_SVC_PRESENT,
            f"svc={'yes' if svc else 'no'}",
        )

    def _hits_to_level_booster(self, candidate: dict) -> BoosterResult:
        """Score based on number of times the level has been tested."""
        hits = candidate.get("hits_to_level", 0)

        if hits >= 3:
            score = 1.0
        elif hits == 2:
            score = 0.7
        elif hits == 1:
            score = 0.4
        else:
            score = 0.0

        return BoosterResult(
            "hits_to_level",
            score,
            WEIGHT_HITS_TO_LEVEL,
            f"hits={hits}",
        )

    def _hits_with_volume_booster(self, candidate: dict) -> BoosterResult:
        """Score based on volume trend across level hits."""
        trend = candidate.get("hits_volume_trend", "flat").lower()

        if trend == "increasing":
            score = 1.0
        elif trend == "flat":
            score = 0.3
        else:  # decreasing
            score = 0.1

        return BoosterResult(
            "hits_with_volume",
            score,
            WEIGHT_HITS_WITH_VOLUME,
            f"trend={trend}",
        )

    def _near_period_extreme_booster(self, candidate: dict) -> BoosterResult:
        """Score based on distance to key HiW/LoW levels (§1.2 proximity tiers)."""
        proximity_pct = candidate.get("level_proximity_pct", 1.0)

        if proximity_pct <= PERIOD_EXTREME_T1:
            score = 1.0
        elif proximity_pct <= PERIOD_EXTREME_T2:
            score = 0.7
        elif proximity_pct <= PERIOD_EXTREME_T3:
            score = 0.3
        else:
            score = 0.0

        return BoosterResult(
            "near_period_extreme",
            score,
            WEIGHT_NEAR_PERIOD_EXTREME,
            f"proximity={proximity_pct:.4f}",
        )

    def _htf_not_consolidating_booster(self, candidate: dict) -> BoosterResult:
        """Score based on H4 NOT being in consolidation (§4.4)."""
        # Check explicit flag; fall back to boardroom proxy
        htf_consolidating = candidate.get("htf_consolidating", None)
        if htf_consolidating is None:
            # Fallback: use boardroom data as proxy
            in_boardroom = candidate.get("in_boardroom", False)
            boardroom_bars = candidate.get("boardroom_bars", 0)
            if in_boardroom and boardroom_bars >= 20:
                htf_consolidating = True
            elif in_boardroom:
                htf_consolidating = True  # any boardroom = consolidating
            else:
                htf_consolidating = False

        score = 0.0 if htf_consolidating else 1.0
        return BoosterResult(
            "htf_not_consolidating",
            score,
            WEIGHT_HTF_NOT_CONSOLIDATING,
            f"consolidating={htf_consolidating}",
        )

    def _kill_zone_booster(self, session_state: dict) -> BoosterResult:
        """Score based on whether current time is in a session kill zone."""
        kz_active = session_state.get("kill_zone_active", False)
        score = 1.0 if kz_active else 0.0
        return BoosterResult(
            "kill_zone",
            score,
            WEIGHT_KILL_ZONE,
            f"active={kz_active}",
        )

    def _session_overlap_booster(self, session_state: dict) -> BoosterResult:
        """Score based on whether current time is in a session overlap."""
        overlap = session_state.get("session_overlap", False)
        score = 1.0 if overlap else 0.0
        return BoosterResult(
            "session_overlap",
            score,
            WEIGHT_SESSION_OVERLAP,
            f"overlap={overlap}",
        )

    def _session_phase_booster(self, session_state: dict) -> BoosterResult:
        """Score based on session phase (opening/mid/closing) (§8.2)."""
        # Prefer structured phase string; fall back to phase_score
        phase = session_state.get("phase", "").lower()
        if phase == "opening":
            score = 1.0
        elif phase == "mid":
            score = 0.5
        elif phase == "closing":
            score = 0.2
        else:
            # Fallback: derive from phase_score + kill_zone
            phase_score = session_state.get("phase_score", 0.0)
            kz_active = session_state.get("kill_zone_active", False)
            kz_bonus = 0.3 if kz_active else 0.0
            score = min(1.0, phase_score + kz_bonus)

        return BoosterResult("session_phase", score, WEIGHT_SESSION_PHASE)

    def _day_of_week_booster(self, session_state: dict) -> BoosterResult:
        """Score based on weekly structural model (§8.3).

        Note: The §8.3 modifiers (Monday -0.10, Wednesday +0.05) are additive
        offsets to the FINAL confidence score, NOT this weight. This booster
        captures session-phase timing quality per the weekly model.
        """
        dow = session_state.get("day_of_week", "").lower()

        dow_scores = {
            "monday": 0.2,  # Fake move day — low conviction
            "tuesday": 0.7,  # True trend day
            "wednesday": 0.9,  # Midweek reversal window — best
            "thursday": 0.5,  # Typical trading day
            "friday": 0.1,  # Unpredictable — skip
        }
        score = dow_scores.get(dow, 0.3)

        return BoosterResult(
            "day_of_week",
            score,
            WEIGHT_DAY_OF_WEEK,
            f"dow={dow}",
        )

    def _asia_control_booster(self, candidate: dict) -> BoosterResult:
        """Score based on Asia session range quality (§8.4).

        Tight range + consolidating = high score (good for Asia→UK setups).
        Trending = low score.
        """
        asia_range_pct = candidate.get("asia_range_pct", None)
        asia_trending = candidate.get("asia_trending", None)

        if asia_range_pct is not None and asia_trending is not None:
            if asia_range_pct < 0.02 and not asia_trending:
                score = 1.0  # tight + consolidating
            elif asia_trending:
                score = 0.0  # trending — suppresses Asia→UK setups
            else:
                score = 0.5  # neutral
        else:
            score = 0.5  # unknown — neutral

        return BoosterResult(
            "asia_control",
            score,
            WEIGHT_ASIA_CONTROL,
            f"range={asia_range_pct}, trending={asia_trending}",
        )

    def _ema_bounce_booster(self, candidate: dict) -> BoosterResult:
        """Score based on 50 EMA touch with rejection candle close.

        Price touches 50 EMA (within 0.2%) and closes away with a
        rejection candle = 1.0. No touch = 0.0.
        """
        # Explicit flag takes priority (only when actually set to True/False)
        if candidate.get("ema_rejection_candle") is not None:
            score = 1.0 if candidate["ema_rejection_candle"] else 0.0
            return BoosterResult(
                "ema_bounce",
                score,
                WEIGHT_EMA_BOUNCE,
                f"rejection_candle={candidate['ema_rejection_candle']}",
            )

        # Fallback: use ema_distance_pct as proxy
        ema_distance = candidate.get("ema_distance_pct", 1.0)
        if ema_distance <= EMA_TOUCH_THRESHOLD:
            score = 1.0  # At the EMA — treat as bounce
        elif ema_distance <= EMA_TOUCH_THRESHOLD * 3:
            score = 0.5  # Near the EMA
        else:
            score = 0.0

        return BoosterResult(
            "ema_bounce",
            score,
            WEIGHT_EMA_BOUNCE,
            f"ema_dist={ema_distance:.4f}",
        )

    def _dxy_correlation_booster(self, dxy_state: dict) -> BoosterResult:
        """Score based on DXY direction agreement (forex only, §1.3).

        DXY direction agrees with trade direction = 1.0,
        opposes = 0.0, flat/unknown = 0.5.
        """
        if dxy_state.get("agrees"):
            score = 1.0
        elif dxy_state.get("opposes"):
            score = 0.0
        else:
            score = 0.5  # flat/unknown

        return BoosterResult(
            "dxy_correlation",
            score,
            WEIGHT_DXY_CORRELATION,
            f"agrees={dxy_state.get('agrees', 'n/a')}",
        )

    # ──────────────────────────────────────────────────────────────
    # Bonus booster (not in spec §7.1 weight table)
    # ──────────────────────────────────────────────────────────────

    def _pattern_type_booster(self, candidate: dict) -> BoosterResult:
        """Score based on pattern type strength.

        Stronger reversal patterns get higher base scores.
        Uses the 5% weight slack from spec §7.1.
        """
        pattern = candidate.get("pattern_type", "").upper()

        pattern_scores = {
            "M": 0.9,
            "W": 0.9,
            "SVC": 0.7,
            "OB": 0.6,
            "FVG": 0.5,
            "LIQ_SWEEP": 0.6,
        }
        score = pattern_scores.get(pattern, 0.3)

        return BoosterResult(
            "pattern_type",
            score,
            WEIGHT_PATTERN_TYPE,
            f"pattern={pattern}",
        )
