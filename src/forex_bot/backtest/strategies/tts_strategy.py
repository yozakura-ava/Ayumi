"""TTSStrategy — ISignalStrategy adapter for the Ayumi TTC signal engine.

Wires the full signal engine pipeline (Phase 1-4) into the backtest engine
via the ISignalStrategy interface, producing StrategySignal objects.

Enhanced with:
- ConfidenceBuilder: modular confidence cascade with confluence boosts
- TPManager: 3-level TP system (1R/1.5R/2R) with progressive SL management
- New confluence detections: Asia gap type, ILOD/IHOD boundary, VWAP rejection
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import numpy as np
from ml.per_symbol_configs import DEFAULT_SYMBOL_CONFIG, PER_SYMBOL_CONFIGS
from signal_engine import (
    ConfluenceScorer,
    GateValidator,
    PatternDetector,
    SessionAnalyzer,
    StopTargetCalculator,
    TPManager,
)
from signal_engine.data_types import HTFState, Level, SessionState, Swing
from signal_engine.htf_analyzer import HTFAnalyzer
from signal_engine.level_counter import LevelCounter
from signal_engine.pattern_detector import (
    AsiaRangeResult,
    AsiaSessionAnalyzer,
)
from signal_engine.swing_detector import SwingDetector
from utils.pip_value import pip_value_for_symbol

from ..engine import Bar, MarketState, StrategySignal, TradeDirection
from ..strategy_legacy import ISignalStrategy

logger = logging.getLogger(__name__)


# ── Confidence Cascade Constants ─────────────────────────────────

# Base confidence for M/W patterns
MW_BASE_CONFIDENCE = 0.30

# Confluence boost values
RSI_DIVERGENCE_BOOST = 0.10
HTF_TREND_ALIGNED_BOOST = 0.10
SVC_AT_PEAK_BOOST = 0.10
CONSOLIDATION_BOOST = 0.05
ASIA_GAP_FAVORABLE_BOOST = 0.05
ILOD_IHOD_AT_BOUNDARY_BOOST = 0.05
VWAP_REJECTION_BOOST = 0.10
KILL_ZONE_ACTIVE_BOOST = -0.05
HTF_OPPOSING_PENALTY = -0.15
NEGATIVE_WEIGHT = 1.0  # global multiplier for negative confluence magnitude (0=off, 1=full)

# New confluence boost constants
MFI_BOOST = 0.08
EMA_CROSS_BOOST = 0.08
BB_CONF_BOOST = 0.07
ADX_BOOST = 0.06
VOLUME_SPIKE_BOOST = 0.05
VWAP_DISTANCE_BOOST = 0.05
RSI_EXTREME_BOOST = 0.06
EMA_EXTENSION_BOOST = 0.07
EMA_CLUSTER_BOOST = 0.06

# 4H 200 EMA confluence
HTF_200EMA_BOOST = 0.10
HTF_200EMA_PENALTY = 0.08

# Negative confluence constants (reduce confidence when triggered)
RSI_OVERBOUGHT_NC = -0.05  # RSI > 70 for longs = overbought
RSI_OVERSOLD_NC = -0.05  # RSI < 30 for shorts = oversold
HTF_COUNTER_TREND_NC = -0.08  # HTF alignment opposes entry direction
LATE_KILL_ZONE_NC = -0.06  # UK session nearly over (near 8am NY)
VOLUME_DIVERGENCE_NC = -0.05  # Price up/down but volume not confirming
BB_SQUEEZE_NC = -0.04  # Bollinger bandwidth compressed = breakout risk
ADX_EXHAUSTION_NC = -0.05  # ADX > 40 but price stalling = weakening
VOLATILE_SESSION_NC = -0.06  # ATR at 90th percentile = high volatility session
VWAP_EXTREME_DISTANCE_NC = -0.04  # price far from VWAP = mean reversion risk
ASIA_RANGE_WIDE_NC = -0.05  # Asia range > 2% of price = low quality range
MFI_OVERBOUGHT_NC = -0.04  # MFI > 80 for longs
MFI_OVERSOLD_NC = -0.04  # MFI < 20 for shorts

# Pattern-type-specific base confidence
PATTERN_BASE_CONFIGS = {
    "mw_rejection": 0.30,
    "fv_ob_snd": 0.35,
    "fv_ob_bos": 0.32,
    "kill_zone_pattern": 0.28,
    "default": 0.30,
}


_CONFLUENCE_DENSITY_BUCKETS = [
    (0.40, 1.25),
    (0.27, 1.15),
    (0.13, 1.05),
    (0.00, 1.00),
]

_QUALITY_GATE_WEIGHT = 0.18
_CONFLUENCE_SCORER_WEIGHT = 0.18


class ConfidenceBuilder:
    """Modular confidence scoring — each confluence adds to the score.

    Finalize applies a multiplicative density bonus: trades with more
    independent positive confluences receive a compounding multiplier,
    naturally pushing high-confirmation setups into higher tiers.
    """

    _MAX_POSITIVE_CONFLUENCES = 15.0

    def __init__(self, base_confidence: float):
        self.score = base_confidence
        self.boosts_applied: list[tuple[str, float]] = []

    def add_boost(self, name: str, value: float) -> None:
        self.score += value
        self.boosts_applied.append((name, value))

    def finalize(self) -> float:
        positive_count = sum(1 for _, v in self.boosts_applied if v > 0)
        density = positive_count / self._MAX_POSITIVE_CONFLUENCES
        for threshold, multiplier in _CONFLUENCE_DENSITY_BUCKETS:
            if density >= threshold:
                return min(self.score * multiplier, 1.0)
        return min(self.score, 1.0)


class TTSStrategy(ISignalStrategy):
    """Multi-timeframe TTC/TBD signal strategy.

    Pair- and timeframe-aware FL pattern thresholds — see FL_CONFIDENCE_THRESHOLDS.


    Uses direct incremental swing/level detection for accurate per-bar
    state, then runs Phase 2-4 pattern → gate → confluence → SL/TP pipeline.

    Pipeline per bar:
        1. Session context (SessionAnalyzer)
        2. Incremental swing detection + level counting (SwingDetector + LevelCounter)
        3. Pattern detection (PatternDetector) — M/W 11-pt, SVC, traps, FL
        4. Gate validation (GateValidator) — hard gates
        5. Confluence scoring (ConfluenceScorer) — boosters
        6. SL/TP calculation (StopTargetCalculator)
        7. Output as StrategySignal

    PF-Cap Design Decision (card f5b6ebcd, Jul 2026)
    -------------------------------------------------
    This strategy does **not** implement a profit-factor cap (PF cap).
    A PF cap would suppress trading when rolling PF drops below a threshold.
    The decision is **not needed here** because:

    1. **Separation of concerns.** TTSStrategy is a *signal generator* — its
       job is to identify opportunities via pattern + confluence scoring.
       Making it stateful (tracking running PF) would couple signal detection
       to trade history, making backtests non-deterministic across position
       sizing configurations.
    2. **Risk management layer.** PF-based position throttling belongs in the
       portfolio/position-sizing layer (risk manager, not signal engine).
       The backtest engine already supports per-trade SL/TP via
       StopTargetCalculator, and the execution layer can apply PF caps.
    3. **Root cause of PF=0.** The 1/3-runs-PF=0 anomaly is not a missing
       PF cap — it's over-selective parameter combinations found by certain
       Optuna seeds. The fix is pruning PF=0 trials in the optimizer
       objective (see ttc_optimizer.py), not adding runtime PF gating.
    """

    SWING_LOOKBACK = 5  # N-bar swing detection
    HISTORY_BARS = 50  # how many bars to feed into swing detection (M15)

    # Per-pair, per-timeframe FL pattern confidence thresholds.
    # 0.0 = always suppress FL patterns (prefer generic M/W only).
    # Per-pair entry proximity thresholds (how far price can move from pattern level).
    ENTRY_PROXIMITY_PIPS: dict[str, float] = {
        "EURUSD": 0.0030,  # 30 forex pips
        "GBPUSD": 0.0030,
        "USDJPY": 0.030,  # 30 JPY pips
        "GBPJPY": 0.030,
        "XAUUSD": 3.0,  # 300 gold cents = $3.00
        "DEFAULT": 0.0030,
    }

    FL_CONFIDENCE_THRESHOLDS: dict[str, dict[str, float]] = {
        "EURUSD": {"M15": 0.60, "H1": 0.55, "H4": 0.55, "D1": 0.50},
        "GBPUSD": {"M15": 0.50, "H1": 0.50, "H4": 0.50, "D1": 0.50},
        "USDJPY": {"M15": 0.0, "H1": 0.0, "H4": 0.0, "D1": 0.0},  # always suppress FL
        "GBPJPY": {"M15": 0.50, "H1": 0.50, "H4": 0.50, "D1": 0.50},
        "XAUUSD": {
            "M15": 0.50,
            "H1": 0.55,
            "H4": 0.55,
            "D1": 0.50,
        },  # 0.50 for M15 (was 0.55)
        "DEFAULT": {"M15": 0.60, "H1": 0.55, "H4": 0.55, "D1": 0.50},
    }

    def __init__(
        self,
        symbol: str = "EURUSD",
        min_confidence: float = 0.20,
        min_quality_score: float = 0.25,
        lookback: int = 200,
        timeframe: str = "M15",
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.min_confidence = min_confidence
        self.min_quality_score = min_quality_score
        self.lookback = lookback
        self.timeframe = timeframe
        self._is_volatile_session = False  # set per-bar in evaluate

        # Load per-symbol, per-timeframe ML-optimized config
        self._symbol_config = PER_SYMBOL_CONFIGS.get(symbol.upper(), {}).get(timeframe, DEFAULT_SYMBOL_CONFIG)

        # Phase 2 components
        self._pattern_detector = PatternDetector()
        self._gate_validator = GateValidator(quality_threshold=min_quality_score)

        # Pair-specific Asia range thresholds (Bug 3 fix)
        self._asia_range_thresholds = self._get_asia_range_thresholds(symbol)
        # Phase 3 components
        self._confluence_scorer = ConfluenceScorer()
        # Pair-specific pip size — canonical via utils.pip_value (XAUUSD=0.1, JPY=0.01, FX=0.0001)
        pip_size = pip_value_for_symbol(symbol)
        self._stop_target = StopTargetCalculator(pip_size=pip_size, timeframe=timeframe)
        # Session
        self._session_analyzer = SessionAnalyzer()
        # Asia session analyzer (TTC flight-log system)
        self._asia_analyzer = AsiaSessionAnalyzer(max_range_pct=self._asia_range_thresholds)
        # HTF
        self._htf_analyzer = HTFAnalyzer()

        # Incremental swing detector.
        # NOTE: lookback=5 gives ~13 swing highs and ~14 swing lows from 300 bars
        # of real M15 data — sufficient for M/W detection. Higher lookback values
        # (>=10) start producing too few swings due to the flat, noisy M15 price
        # action requiring large lookback to register as a swing.
        self._swing_detector = SwingDetector(lookback=5)
        self._level_counter = LevelCounter()

        # Mutable state — rebuilt each evaluate() call with full history
        self._swing_highs: list[Swing] = []
        self._swing_lows: list[Swing] = []
        self._levels: list[Level] = []
        self._last_bar_idx = -1
        # Pattern dedup: don't re-signal the same M/W pattern on consecutive bars
        self._last_mw_pattern_key: str = ""
        self._last_signal_bar: int = -1

    @property
    def name(self) -> str:
        return f"TTC/TBD {self.symbol}"

    def initialize(self, config: dict | None = None) -> None:
        """Initialize strategy components.

        Called once before the first bar. TTSStrategy sets up all
        detection components in __init__, so this is mainly for
        logging and state validation.
        """
        super().initialize(config)
        logger.info(
            "TTSStrategy initialized: symbol=%s timeframe=%s min_confidence=%.2f",
            self.symbol,
            self.timeframe,
            self.min_confidence,
        )

    def reset(self) -> None:
        """Reset cached state between backtest runs."""
        self._swing_highs = []
        self._swing_lows = []
        self._levels = []
        self._last_bar_idx = -1
        self._last_mw_pattern_key = ""
        self._last_signal_bar = -1
        self._swing_detector = SwingDetector(lookback=5)
        self._level_counter = LevelCounter()

    def shutdown(self) -> None:
        """Clean up strategy resources after backtest/run completes."""
        logger.info(
            "TTSStrategy shutdown: %s (processed %d bars)",
            self.symbol,
            self._bars_processed,
        )
        super().shutdown()

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        """Evaluate current bar for a trading signal.

        Args:
            state: MarketState with bars up to and including the current bar.

        Returns:
            StrategySignal if all gate conditions pass and quality >= threshold,
            None otherwise.
        """
        bars = state.bars
        if len(bars) < self.HISTORY_BARS + 1:
            return None

        latest = bars[-1]
        bar_idx = len(bars) - 1

        # Mark high-volatility bars for reduced position sizing
        self._is_volatile_session = self._check_volatile_session(bars) < 0

        # Skip if already evaluated (batch mode dedup)
        if bar_idx == self._last_bar_idx:
            return None
        self._last_bar_idx = bar_idx

        # ── Step 1: Session context + HTF analysis ────────────────────
        session_state = self._get_session_state(latest.time)

        # Require active session (not OUTSIDE)
        if session_state.session_name == "OUTSIDE":
            return None

        htf_state = self._compute_htf_state(bars)

        # ── Step 2: Swing + level detection (fixed window to prevent look-ahead) ──
        # Fixed 300-bar window for M15 swing detection.
        # This gives ~10-15 swings which is enough for M/W + level detection.
        n = len(bars)
        window = min(300, n)
        recent = bars[-window:]
        highs = [b.high for b in recent]
        lows = [b.low for b in recent]
        self._swing_highs, self._swing_lows = self._swing_detector.detect_swings(highs, lows)
        self._levels = self._level_counter.detect_levels_with_tracking(self._swing_highs, self._swing_lows)

        if not self._levels:
            return None

        # ── Step 3: Pattern detection ──────────────────────────────────
        bars_dict = [
            {
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "open": b.open,
                "volume": b.volume,
            }
            for b in bars
        ]
        patterns = self._pattern_detector.detect_all(
            swings=self._swing_highs + self._swing_lows,
            levels=self._levels,
            bars=bars_dict,
            current_price=latest.close,
            session=session_state.session_name,
            bar_time=latest.time,
            asia_analyzer=self._asia_analyzer,
            current_bar_index=bar_idx,
        )
        if not patterns:
            return None

        # ── FL pattern priority rule (pair+TF aware) ──
        # Thresholds tuned per pair and timeframe — see FL_CONFIDENCE_THRESHOLDS.
        fl_threshold = self.FL_CONFIDENCE_THRESHOLDS.get(self.symbol, self.FL_CONFIDENCE_THRESHOLDS["DEFAULT"]).get(
            self.timeframe, 0.60
        )

        PATTERN_PRIORITY = {
            "M": 3,
            "W": 3,
            "TRAP": 2,
            "LIQUIDITY_GRAB": 2,
            "SVC_SPRING": 2,
            "SVC_VACATION": 2,
            "FL": 2,
            "ILOD_BREAK": 1,
            "IHOD_BREAK": 1,
            "SVC_CONTINUATION": 0,
        }
        best_pattern = max(
            patterns,
            key=lambda p: (PATTERN_PRIORITY.get(p.pattern_type, 0), p.confidence),
        )

        mw_patterns = [p for p in patterns if p.pattern_type in ("M", "W") and not p.flight_log_id]
        fl_patterns = [p for p in patterns if p.flight_log_id]

        if fl_threshold == 0.0:
            # Suppress all FL — always prefer generic M/W
            if mw_patterns:
                best_pattern = max(mw_patterns, key=lambda p: p.confidence)
            else:
                best_pattern = max(patterns, key=lambda p: p.confidence) if patterns else None
        elif mw_patterns and (not fl_patterns or max(p.confidence for p in fl_patterns) < fl_threshold):
            best_pattern = max(mw_patterns, key=lambda p: p.confidence)
        elif fl_patterns and max(p.confidence for p in fl_patterns) >= fl_threshold:
            fl_best = max(fl_patterns, key=lambda p: p.confidence)
            if getattr(fl_best, "consolidation_confirmed", False):
                best_pattern = fl_best
            else:
                best_pattern = max(mw_patterns, key=lambda p: p.confidence) if mw_patterns else fl_best
        else:
            best_pattern = max(patterns, key=lambda p: p.confidence) if patterns else None

        # ── Step 3b: Quality gates (pre-ConfidenceBuilder) ─────────────
        # Require minimum pattern confidence
        if best_pattern.confidence < 0.35:
            return None

        # For M/W patterns, require reasonable checklist
        if best_pattern.pattern_type in ("M", "W"):
            checklist = getattr(best_pattern, "checklist_score", 0.0)
            if checklist < 0.4:
                return None

            # Pattern dedup: don't re-signal the same M/W on consecutive bars
            kl = best_pattern.key_levels
            if best_pattern.pattern_type == "M":
                pattern_key = f"M:{kl.get('SH1', 0):.5f}:{kl.get('SH2', 0):.5f}"
            else:
                pattern_key = f"W:{kl.get('SL1', 0):.5f}:{kl.get('SL2', 0):.5f}"

            if pattern_key == self._last_mw_pattern_key and bar_idx - self._last_signal_bar <= 3:
                return None

            # Update dedup state
            self._last_mw_pattern_key = pattern_key
            self._last_signal_bar = bar_idx

        # ── Step 3c: ConfidenceBuilder cascade ─────────────────────────
        # Use per-symbol ML-optimized base confidence
        base_conf = self._symbol_config["base_confidence"]
        builder = ConfidenceBuilder(base_conf)

        top_confluences = self._symbol_config.get("top_confluences", [])

        # RSI divergence boost
        if best_pattern.pattern_type in ("M", "W"):
            if not top_confluences or "rsi_divergence" in top_confluences:
                rsi_boost = self._get_rsi_divergence_boost(best_pattern, bars)
                if rsi_boost > 0:
                    builder.add_boost("rsi_divergence", RSI_DIVERGENCE_BOOST)

        # HTF trend alignment boost
        if not top_confluences or "htf_trend_aligned" in top_confluences:
            if htf_state:
                htf_trend = (
                    "bullish"
                    if htf_state.ema_slope > 0.00003
                    else ("bearish" if htf_state.ema_slope < -0.00003 else None)
                )
                if htf_trend is not None:
                    aligned = (best_pattern.direction == "long" and htf_trend == "bullish") or (
                        best_pattern.direction == "short" and htf_trend == "bearish"
                    )
                    if aligned:
                        builder.add_boost("htf_trend_aligned", HTF_TREND_ALIGNED_BOOST)
                # Conflicting HTF: apply penalty instead of blocking
                if htf_trend is not None:
                    opposing = (best_pattern.direction == "long" and htf_trend == "bearish") or (
                        best_pattern.direction == "short" and htf_trend == "bullish"
                    )
                    if opposing:
                        builder.add_boost("htf_opposing", HTF_OPPOSING_PENALTY)
                # HTF phase penalties
                if htf_state.phase.value == "conflicting":
                    builder.add_boost("htf_conflicting", -0.10)
                elif htf_state.phase.value == "consolidating":
                    builder.add_boost("htf_consolidating", -0.05)

        # Kill zone boost
        if not top_confluences or "kill_zone_active" in top_confluences:
            if session_state.kill_zone_active:
                builder.add_boost("kill_zone_active", KILL_ZONE_ACTIVE_BOOST)

        # Consolidation boost (from FL pattern)
        if not top_confluences or "consolidation" in top_confluences:
            if getattr(best_pattern, "consolidation_confirmed", False):
                builder.add_boost("consolidation", CONSOLIDATION_BOOST)

        # SVC at peak boost
        if not top_confluences or "svc_at_peak" in top_confluences:
            if getattr(best_pattern, "flight_log_id", None) and "svc=" in best_pattern.notes:
                if "svc=True" in best_pattern.notes:
                    builder.add_boost("svc_at_peak", SVC_AT_PEAK_BOOST)

        # ── New confluence detections ─────────────────────────────────
        # Asia gap type boost
        if not top_confluences or "asia_gap_favorable" in top_confluences:
            asia_result = self._get_asia_result(bars)
            if asia_result and asia_result.asia_gap_type != "none":
                gap_favorable = (asia_result.asia_gap_type == "bullish" and best_pattern.direction == "long") or (
                    asia_result.asia_gap_type == "bearish" and best_pattern.direction == "short"
                )
                if gap_favorable:
                    builder.add_boost("asia_gap_favorable", ASIA_GAP_FAVORABLE_BOOST)
        else:
            asia_result = self._get_asia_result(bars)

        # ILOD/IHOD at boundary boost
        if not top_confluences or "ilod_ihod_at_boundary" in top_confluences:
            if asia_result and (asia_result.ilod is not None or asia_result.ilhod is not None):
                if self._is_price_near_boundary(latest.close, asia_result):
                    builder.add_boost("ilod_ihod_at_boundary", ILOD_IHOD_AT_BOUNDARY_BOOST)

        # VWAP rejection boost
        if not top_confluences or "vwap_rejection" in top_confluences:
            if self._check_vwap_rejection(bars, latest, best_pattern.direction):
                builder.add_boost("vwap_rejection", VWAP_REJECTION_BOOST)

        # ── New modular confluence detectors ──────────────────────────
        if not top_confluences or "mfi" in top_confluences:
            if self._check_mfi_confluence(bars, best_pattern.direction):
                builder.add_boost("mfi", MFI_BOOST)

        if not top_confluences or "ema_cross" in top_confluences:
            if self._check_ema_cross_confluence(bars, best_pattern.direction):
                builder.add_boost("ema_cross", EMA_CROSS_BOOST)

        if not top_confluences or "bollinger" in top_confluences:
            if self._check_bollinger_confluence(bars, latest, best_pattern.direction):
                builder.add_boost("bollinger", BB_CONF_BOOST)

        if not top_confluences or "adx" in top_confluences:
            if self._check_adx_confluence(bars, best_pattern.direction):
                builder.add_boost("adx", ADX_BOOST)

        if not top_confluences or "volume_spike" in top_confluences:
            if self._check_volume_spike_confluence(bars, latest, best_pattern.direction):
                builder.add_boost("volume_spike", VOLUME_SPIKE_BOOST)

        if not top_confluences or "vwap_distance" in top_confluences:
            if self._check_vwap_distance_confluence(bars, latest, best_pattern.direction):
                builder.add_boost("vwap_distance", VWAP_DISTANCE_BOOST)

        if not top_confluences or "rsi_extreme" in top_confluences:
            if self._check_rsi_extreme_confluence(bars, best_pattern.direction):
                builder.add_boost("rsi_extreme", RSI_EXTREME_BOOST)

        if not top_confluences or "ema_extension" in top_confluences:
            extension_boost = self._check_ema_extension_confluence(bars, best_pattern.direction)
            if extension_boost > 0:
                builder.add_boost("ema_extension", extension_boost)

        if not top_confluences or "ema_cluster" in top_confluences:
            cluster_boost = self._check_ema_cluster_confluence(bars, best_pattern.direction)
            if cluster_boost > 0:
                builder.add_boost("ema_cluster", cluster_boost)

        # 4H 200 EMA confluence
        if not top_confluences or "htf_200ema" in top_confluences:
            htf_200ema = self._check_htf_200ema_confluence(bars, best_pattern.direction)
            if htf_200ema != 0.0:
                name = "htf_200ema_aligned" if htf_200ema > 0 else "htf_200ema_fighting"
                builder.add_boost(name, htf_200ema)

        # ── Negative confluence (reduce confidence when triggered) ─────
        neg_weight = self._symbol_config.get("negative_weight", 1.0)
        if neg_weight > 0:
            neg_rsi = self._check_rsi_negative_confluence(bars, best_pattern.direction)
            if neg_rsi < 0:
                builder.add_boost("rsi_overbought_oversold", neg_rsi * neg_weight)
            neg_htf = self._check_htf_counter_trend(htf_state, best_pattern.direction)
            if neg_htf < 0:
                builder.add_boost("htf_counter_trend", neg_htf * neg_weight)
            neg_kz = self._check_late_kill_zone(session_state)
            if neg_kz < 0:
                builder.add_boost("late_kill_zone", neg_kz * neg_weight)
            neg_vol = self._check_volume_divergence(bars, best_pattern.direction)
            if neg_vol < 0:
                builder.add_boost("volume_divergence", neg_vol * neg_weight)
            neg_bb = self._check_bb_squeeze(bars, best_pattern.direction)
            if neg_bb < 0:
                builder.add_boost("bb_squeeze", neg_bb * neg_weight)
            neg_adx = self._check_adx_exhaustion(bars, best_pattern.direction)
            if neg_adx < 0:
                builder.add_boost("adx_exhaustion", neg_adx * neg_weight)
            neg_mfi = self._check_mfi_negative(bars, best_pattern.direction)
            if neg_mfi < 0:
                builder.add_boost("mfi_extreme", neg_mfi * neg_weight)
            neg_asia = self._check_asia_range_quality(asia_result, latest)
            if neg_asia < 0:
                builder.add_boost("asia_range_wide", neg_asia * neg_weight)

        # Require active session (not outside core hours)
        if session_state.phase_score < 0.2:
            return None

        # ── Step 4: Gate validation ───────────────────────────────────
        candidate = self._build_candidate(best_pattern, latest, bars)
        gate_result = self._gate_validator.validate(
            candidate=candidate,
            htf_state=htf_state,
            session_state=session_state,
            levels=self._levels,
        )
        if not gate_result.passed:
            return None

        quality_score = gate_result.quality_score
        if quality_score < self.min_quality_score:
            return None

        # Quality score as a proportional boost (scaled by _QUALITY_GATE_WEIGHT)
        quality_boost = quality_score * _QUALITY_GATE_WEIGHT
        builder.add_boost("quality_gate", quality_boost)

        # Confluence scorer boosters (legacy, additive)
        if not top_confluences or "confidence_score" in top_confluences:
            session_dict = {
                "phase_score": session_state.phase_score,
                "kill_zone_active": session_state.kill_zone_active,
            }
            htf_dict = {"alignment_score": htf_state.alignment_score} if htf_state else {}
            confluence_score, confluence_boosters = self._confluence_scorer.score(
                candidate=candidate,
                htf_state=htf_dict,
                session_state=session_dict,
            )
            # Add confluence score as a proportional boost (scaled by _CONFLUENCE_SCORER_WEIGHT)
            builder.add_boost("confluence_scorer", confluence_score * _CONFLUENCE_SCORER_WEIGHT)

        # Finalize confidence
        total_confidence = builder.finalize()

        if total_confidence < self.min_confidence:
            return None

        # ── Step 5: SL/TP calculation (TPManager 3-level system) ──────
        # Build context dict for stop calculation
        context: dict = {"atr": state.atr if state.atr > 0 else 0.0001}
        for lv in self._levels:
            if lv.level_type.value in ("R2", "R3", "D2", "D3"):
                context[lv.level_type.value.lower()] = lv.price
        for s in self._swing_lows[-3:]:
            if s.price < latest.close:
                context["sl2"] = s.price
                break
        for s in self._swing_highs[-3:]:
            if s.price > latest.close:
                context["sh2"] = s.price
                break

        # For M/W patterns: entry at the pattern's second peak/trough level
        if best_pattern.pattern_type in ("M", "W"):
            kl = best_pattern.key_levels
            proximity_threshold = self.ENTRY_PROXIMITY_PIPS.get(self.symbol, 0.0030)
            if best_pattern.pattern_type == "M":
                entry_price = kl.get("SH2", latest.close)
                if entry_price - latest.close > proximity_threshold:
                    return None
            else:  # W
                entry_price = kl.get("SL2", latest.close)
                if latest.close - entry_price > proximity_threshold:
                    return None
        else:
            entry_price = latest.close

        st_result = self._stop_target.calculate(
            direction=best_pattern.direction,
            entry=entry_price,
            context=context,
            spread=1.5 * self._stop_target.pip_size,
        )
        stop_price = st_result["stop_loss"]

        # C4: Use FL pattern's own stop when available
        if getattr(best_pattern, "flight_log_id", None):
            fl_stop = best_pattern.key_levels.get("stop")
            if fl_stop is not None:
                stop_price = fl_stop

        if stop_price is None:
            return None

        # Sanity check: SL on correct side of entry.
        # These were previously ``assert`` statements but asserts are stripped
        # under ``python -O``. A stop placed on the wrong side of entry is a
        # strategy-level bug that must surface on every Python invocation,
        # not only debug runs — converted to explicit raises.
        if best_pattern.direction == "long":
            if not stop_price < entry_price:
                raise ValueError(
                    f"SL ({stop_price:.5f}) must be below entry ({entry_price:.5f}) for long"
                )
        else:
            if not stop_price > entry_price:
                raise ValueError(
                    f"SL ({stop_price:.5f}) must be above entry ({entry_price:.5f}) for short"
                )

        # Use ATR-based TP levels from StopTargetCalculator
        tp_levels = st_result["tp_levels"]
        non_structure = [t for t in tp_levels if not t.get("structure")]
        if len(non_structure) >= 3:
            tp1_price = non_structure[0]["price"]
            tp2_price = non_structure[1]["price"]
            tp3_price = non_structure[2]["price"]
        elif len(non_structure) >= 1:
            tp1_price = non_structure[0]["price"]
            tp2_price = non_structure[-1]["price"]
            tp3_price = non_structure[-1]["price"]
        else:
            pip_size = self._stop_target.pip_size
            tp_mgr = TPManager(
                entry_price=entry_price,
                stop_price=stop_price,
                pip_size=pip_size,
                direction=best_pattern.direction,
            )
            tp1_price = tp_mgr.tp1_price
            tp2_price = tp_mgr.tp2_price
            tp3_price = tp_mgr.tp3_price

        direction = TradeDirection.LONG if best_pattern.direction == "long" else TradeDirection.SHORT

        rationale = (
            f"{self.name}: {best_pattern.pattern_type} "
            f"{best_pattern.direction} @ {latest.close:.5f}, "
            f"conf={total_confidence:.2f}, "
            f"boosts={builder.boosts_applied}"
        )

        return StrategySignal(
            direction=direction,
            confidence=total_confidence,
            entry_price=entry_price,
            stop_loss=stop_price,
            take_profit_1=tp1_price,
            take_profit_2=tp2_price,
            take_profit_3=tp3_price,
            rationale=rationale,
            is_volatile=self._is_volatile_session,
        )

    def _get_asia_result(self, bars: list[Bar]) -> Optional[AsiaRangeResult]:
        """Get Asia session analysis result from recent bars."""
        bars_dict = [
            {
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "open": b.open,
                "volume": b.volume,
                "time": b.time,
            }
            for b in bars
        ]
        return self._asia_analyzer.analyze_asia_range(bars_dict)

    @staticmethod
    def _is_price_near_boundary(price: float, asia: AsiaRangeResult) -> bool:
        """Check if price is within 0.05% of ILOD or IHOD."""
        tolerance = 0.0005
        if asia.ilod is not None and abs(price - asia.ilod) / asia.ilod < tolerance:
            return True
        if asia.ilhod is not None and abs(price - asia.ilhod) / asia.ilhod < tolerance:
            return True
        return False

    @staticmethod
    def _check_mfi_confluence(bars: list[Bar], direction: str) -> bool:
        """MFI(14) confluence: overbought for longs, oversold for shorts."""
        lookback = min(50, len(bars))
        if lookback < 20:
            return False
        recent = bars[-lookback:]
        typical = np.array([(b.high + b.low + b.close) / 3.0 for b in recent])
        volumes = np.array([b.volume for b in recent], dtype=float)
        mfi = TTSStrategy._compute_mfi(typical, volumes, 14)
        if np.isnan(mfi):
            return False
        if 40 <= mfi <= 60:
            return False  # neutral zone
        if direction == "long" and mfi > 80:
            return True
        if direction == "short" and mfi < 20:
            return True
        return False

    @staticmethod
    def _check_ema_cross_confluence(bars: list[Bar], direction: str) -> bool:
        """EMA(9)/EMA(21) crossover within last 3 bars."""
        lookback = min(50, len(bars))
        if lookback < 25:
            return False
        closes = np.array([b.close for b in bars[-lookback:]])
        ema9 = TTSStrategy._ema(closes, 9)
        ema21 = TTSStrategy._ema(closes, 21)
        # Check last 3 bars for crossover
        for i in range(-3, 0):
            if np.isnan(ema9[i]) or np.isnan(ema21[i]):
                continue
            if i == -len(closes):
                continue
            if np.isnan(ema9[i - 1]) or np.isnan(ema21[i - 1]):
                continue
            prev_diff = ema9[i - 1] - ema21[i - 1]
            curr_diff = ema9[i] - ema21[i]
            if direction == "long" and prev_diff <= 0 and curr_diff > 0:
                return True
            if direction == "short" and prev_diff >= 0 and curr_diff < 0:
                return True
        return False

    @staticmethod
    def _check_bollinger_confluence(bars: list[Bar], latest: Bar, direction: str) -> bool:
        """Bollinger Band(20, 2σ) bounce/rejection or middle band cross."""
        lookback = min(50, len(bars))
        if lookback < 22:
            return False
        closes = np.array([b.close for b in bars[-lookback:]])
        bb_upper, bb_mid, bb_lower = TTSStrategy._bollinger_bands(closes, 20, 2.0)
        if np.isnan(bb_upper) or np.isnan(bb_lower) or np.isnan(bb_mid):
            return False
        if direction == "long":
            # Lower band bounce: previous close below lower, current close above
            prev_close = bars[-2].close
            if prev_close < bb_lower and latest.close > bb_lower:
                return True
            # Middle band cross: previous close below, current above
            if prev_close < bb_mid and latest.close > bb_mid:
                return True
        else:
            prev_close = bars[-2].close
            if prev_close > bb_upper and latest.close < bb_upper:
                return True
            if prev_close > bb_mid and latest.close < bb_mid:
                return True
        return False

    @staticmethod
    def _check_adx_confluence(bars: list[Bar], direction: str) -> bool:
        """ADX(14) > 25 with directional DI alignment."""
        lookback = min(80, len(bars))
        if lookback < 30:
            return False
        recent = bars[-lookback:]
        highs = np.array([b.high for b in recent])
        lows = np.array([b.low for b in recent])
        closes = np.array([b.close for b in recent])
        adx, plus_di, minus_di = TTSStrategy._compute_adx(highs, lows, closes, 14)
        if np.isnan(adx):
            return False
        if adx < 25:
            return False  # ranging, no boost
        if direction == "long" and plus_di > minus_di:
            return True
        if direction == "short" and minus_di > plus_di:
            return True
        return False

    @staticmethod
    def _check_volume_spike_confluence(bars: list[Bar], latest: Bar, direction: str) -> bool:
        """Volume spike > 1.5x 20-bar average in trade direction."""
        lookback = min(50, len(bars))
        if lookback < 21:
            return False
        volumes = np.array([b.volume for b in bars[-lookback:]], dtype=float)
        vol_ma = np.mean(volumes[-21:-1])
        if vol_ma <= 0:
            return False
        if latest.volume < vol_ma * 1.5:
            return False
        # Check direction: bullish candle for long, bearish for short
        if direction == "long" and latest.close > latest.open:
            return True
        if direction == "short" and latest.close < latest.open:
            return True
        return False

    @staticmethod
    def _check_vwap_distance_confluence(bars: list[Bar], latest: Bar, direction: str) -> bool:
        """Price > 0.10% away from VWAP in trade direction."""
        lookback = min(30, len(bars))
        if lookback < 15:
            return False
        recent = bars[-lookback:]
        typical = np.array([(b.high + b.low + b.close) / 3.0 for b in recent])
        volumes = np.array([b.volume for b in recent], dtype=float)
        total_vol = volumes.sum()
        if total_vol <= 0:
            return False
        vwap = float(np.sum(typical * volumes) / total_vol)
        dist_pct = (latest.close - vwap) / vwap
        if direction == "long" and dist_pct > 0.001:
            return True
        if direction == "short" and dist_pct < -0.001:
            return True
        return False

    @staticmethod
    def _check_rsi_extreme_confluence(bars: list[Bar], direction: str) -> bool:
        """RSI(7) extreme: <30 for longs (oversold), >70 for shorts (overbought)."""
        lookback = min(50, len(bars))
        if lookback < 10:
            return False
        closes = np.array([b.close for b in bars[-lookback:]])
        rsi7 = TTSStrategy._rsi(closes, 7)
        if np.isnan(rsi7[-1]):
            return False
        if direction == "long" and rsi7[-1] < 30:
            return True
        if direction == "short" and rsi7[-1] > 70:
            return True
        return False

    def _check_ema_extension_confluence(self, bars: list[Bar], direction: str) -> float:
        """
        FL-006: 'Price has pulled away from moving averages' is an exhaustion signal.
        When price is extended far from key MAs, mean reversion becomes likely.

        If price is > 1.5x ATR away from BOTH 50 EMA and 200 EMA:
          → Counter-trend setup: apply EMA_EXTENSION_BOOST
        """
        lookback = min(251, len(bars))
        if lookback < 201:
            return 0.0
        recent = bars[-lookback:]
        closes = np.array([b.close for b in recent])
        ema50_series = TTSStrategy._ema(closes, 50)
        ema200_series = TTSStrategy._ema(closes, 200)

        if np.isnan(ema50_series[-1]) or np.isnan(ema200_series[-1]):
            return 0.0

        # Compute ATR(14) using Wilder's method
        tr = np.zeros(len(closes))
        for i in range(1, len(closes)):
            tr[i] = max(
                recent[i].high - recent[i].low,
                abs(recent[i].high - recent[i - 1].close),
                abs(recent[i].low - recent[i - 1].close),
            )
        atr_val = float(np.mean(tr[-15:-1]))  # simple 14-bar mean
        if atr_val == 0:
            return 0.0

        current = closes[-1]
        dist_ema50 = abs(current - ema50_series[-1]) / atr_val
        dist_ema200 = abs(current - ema200_series[-1]) / atr_val

        if dist_ema50 > 1.5 and dist_ema200 > 1.5:
            return EMA_EXTENSION_BOOST
        return 0.0

    def _check_ema_cluster_confluence(self, bars: list[Bar], direction: str) -> float:
        """
        When EMA(20), EMA(50), EMA(200) are clustered within 0.05% of each other,
        it indicates a consolidation zone that often breaks violently.
        Price breaking through an EMA cluster = momentum continuation.
        """
        lookback = min(251, len(bars))
        if lookback < 201:
            return 0.0
        recent = bars[-lookback:]
        closes = np.array([b.close for b in recent])
        ema20_series = TTSStrategy._ema(closes, 20)
        ema50_series = TTSStrategy._ema(closes, 50)
        ema200_series = TTSStrategy._ema(closes, 200)

        if np.isnan(ema20_series[-1]) or np.isnan(ema50_series[-1]) or np.isnan(ema200_series[-1]):
            return 0.0

        ema20 = float(ema20_series[-1])
        ema50 = float(ema50_series[-1])
        ema200 = float(ema200_series[-1])

        avg_ema = (ema20 + ema50 + ema200) / 3
        spread = max(ema20, ema50, ema200) - min(ema20, ema50, ema200)
        cluster_threshold = avg_ema * 0.0005  # 0.05%

        if spread < cluster_threshold:
            prev_close = recent[-2].close
            current = recent[-1].close
            if direction == "long" and current > avg_ema and prev_close <= avg_ema:
                return EMA_CLUSTER_BOOST
            elif direction == "short" and current < avg_ema and prev_close >= avg_ema:
                return EMA_CLUSTER_BOOST
        return 0.0

    def _check_htf_200ema_confluence(self, bars: list[Bar], direction: str) -> float:
        """Check if price is aligned with or against the 4H 200 EMA.

        Resamples M15 bars to H4 (every 16 bars) and computes 200 EMA.
        Returns boost/penalty value, or 0.0 if not enough data.
        """
        h4_bars = self._resample_to_h4(bars)
        if len(h4_bars) < 201:
            return 0.0
        h4_closes = np.array([b.close for b in h4_bars])
        ema_200 = self._ema(h4_closes, 200)
        if np.isnan(ema_200[-1]):
            return 0.0
        current_price = bars[-1].close
        if current_price > ema_200[-1]:
            return HTF_200EMA_BOOST if direction == "long" else -HTF_200EMA_PENALTY
        else:
            return HTF_200EMA_BOOST if direction == "short" else -HTF_200EMA_PENALTY

    # ── Negative confluence check methods ──────────────────────────

    def _compute_rsi(self, closes: list[float], period: int = 14) -> list[float]:
        """Compute RSI for a list of close prices."""
        if len(closes) < period + 1:
            return []
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0.0 for d in deltas]
        losses = [-d if d < 0 else 0.0 for d in deltas]
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return [100.0] * len(closes)
        rs = avg_gain / avg_loss
        rsi = [100.0 - (100.0 / (1.0 + rs))]
        for i in range(period, len(closes)):
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
            if avg_loss == 0:
                rsi.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi.append(100.0 - (100.0 / (1.0 + rs)))
        return rsi

    def _check_rsi_negative_confluence(self, bars: list[Bar], direction: str) -> float:
        """RSI overbought (>70) for longs or RSI oversold (<30) for shorts = negative."""
        if len(bars) < 15:
            return 0.0
        closes = [b.close for b in bars[-15:]]
        rsi = self._compute_rsi(closes, period=14)
        if not rsi:
            return 0.0
        current_rsi = rsi[-1]
        if direction == "long" and current_rsi > 70:
            return RSI_OVERBOUGHT_NC
        if direction == "short" and current_rsi < 30:
            return RSI_OVERSOLD_NC
        return 0.0

    def _check_htf_counter_trend(self, htf_state: HTFState, direction: str) -> float:
        """HTF alignment opposes entry direction = negative."""
        if not htf_state or not hasattr(htf_state, "alignment_score"):
            return 0.0
        alignment = htf_state.alignment_score
        if direction == "long" and alignment < -0.3:
            return HTF_COUNTER_TREND_NC
        if direction == "short" and alignment > 0.3:
            return HTF_COUNTER_TREND_NC
        return 0.0

    def _check_late_kill_zone(self, session_state: SessionState) -> float:
        """UK session near close (last ~45 min) = negative for new entries."""
        if not session_state or session_state.session_name != "UK":
            return 0.0
        if session_state.phase_score < 0.3:
            return LATE_KILL_ZONE_NC
        return 0.0

    def _check_volume_divergence(self, bars: list[Bar], direction: str) -> float:
        """Price direction and volume direction diverge = negative."""
        if len(bars) < 10:
            return 0.0
        recent = bars[-10:]
        avg_volume = sum(b.volume for b in recent) / len(recent)
        last_volume = recent[-1].volume
        if last_volume < avg_volume * 0.7:
            price_change_pct = abs(recent[-1].close - recent[0].close) / recent[0].close
            if price_change_pct > 0.002:
                return VOLUME_DIVERGENCE_NC
        return 0.0

    def _check_bb_squeeze(self, bars: list[Bar], direction: str) -> float:
        """Bollinger bandwidth near recent low = squeeze = breakout risk = negative."""
        if len(bars) < 20:
            return 0.0
        recent = bars[-20:]
        closes = [b.close for b in recent]
        mean = sum(closes) / len(closes)
        variance = sum((x - mean) ** 2 for x in closes) / len(closes)
        bandwidth = variance**0.5
        if len(closes) < 10:
            return 0.0
        older = closes[:-5]
        older_mean = sum(older) / len(older)
        older_var = sum((x - older_mean) ** 2 for x in older) / len(older)
        older_bandwidth = older_var**0.5
        if older_bandwidth > 0 and bandwidth < older_bandwidth * 0.5:
            return BB_SQUEEZE_NC
        return 0.0

    def _check_volatile_session(self, bars: list[Bar]) -> float:
        """ "ATR near 90th percentile of last 100 bars = high volatility = negative."""
        if len(bars) < 100:
            return 0.0
        recent = bars[-100:]
        trs = []
        for i in range(1, len(recent)):
            high = recent[i].high
            low = recent[i].low
            prev_close = recent[i - 1].close
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if not trs:
            return 0.0
        current_atr = sum(trs[-14:]) / min(14, len(trs))
        atr_values = trs[:-1]
        if not atr_values:
            return 0.0
        threshold = sorted(atr_values)[int(len(atr_values) * 0.90)]
        if current_atr >= threshold:
            return VOLATILE_SESSION_NC
        return 0.0

    def _check_adx_exhaustion(self, bars: list[Bar], direction: str) -> float:
        """ADX > 40 but price stalling = exhaustion = negative for continuation."""
        if len(bars) < 15:
            return 0.0
        trs = []
        for i in range(1, len(bars)):
            high = bars[i].high
            low = bars[i].low
            prev_close = bars[i - 1].close
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if len(trs) < 10:
            return 0.0
        recent_trend = sum(trs[-5:]) / 5
        older_trend = sum(trs[-10:-5]) / 5
        if recent_trend < older_trend * 0.8 and recent_trend > 0.0001:
            return ADX_EXHAUSTION_NC
        return 0.0

    def _check_mfi_negative(self, bars: list[Bar], direction: str) -> float:
        """MFI overbought (>80) for longs or oversold (<20) for shorts = negative."""
        if len(bars) < 15:
            return 0.0
        typical_prices = [(b.high + b.low + b.close) / 3 * b.volume for b in bars[-15:]]
        if not typical_prices or sum(typical_prices) == 0:
            return 0.0
        mfi = typical_prices[-1] / (sum(typical_prices) + 0.00001) * 100
        mfi = max(0.0, min(100.0, mfi))
        if direction == "long" and mfi > 80:
            return MFI_OVERBOUGHT_NC
        if direction == "short" and mfi < 20:
            return MFI_OVERSOLD_NC
        return 0.0

    def _check_asia_range_quality(self, asia_result, latest: Bar) -> float:
        """Asia range > 2% of price = low quality range = negative."""
        if not asia_result:
            return 0.0
        if asia_result.ilod is None or asia_result.ilod == 0:
            return 0.0
        range_pct = abs(asia_result.ilod - latest.close) / latest.close
        if range_pct > 0.020:
            return ASIA_RANGE_WIDE_NC
        return 0.0

    @staticmethod
    def _resample_to_h4(bars: list[Bar]) -> list[Bar]:
        """Resample M15 bars to H4 by taking every 16th bar's close as OHLC proxy."""
        # H4 = 16 M15 bars
        result = []
        for i in range(0, len(bars), 16):
            chunk = bars[i : i + 16]
            if not chunk:
                continue
            result.append(
                Bar(
                    time=chunk[0].time,
                    open=chunk[0].open,
                    high=max(b.high for b in chunk),
                    low=min(b.low for b in chunk),
                    close=chunk[-1].close,
                    volume=sum(b.volume for b in chunk),
                )
            )
        return result

    def _classify_pattern_type(self, pattern, bars: list[Bar], latest: Bar) -> str:
        """Classify the pattern type for base confidence lookup."""
        ptype = getattr(pattern, "pattern_type", "")
        if ptype in ("M", "W"):
            return "mw_rejection"
        # Check for OB/FV + S&D or BOS confluence
        has_ob = "order_block" in str(getattr(pattern, "key_levels", {})).lower()
        has_bos = "bos" in str(getattr(pattern, "pattern_type", "")).lower()
        has_snd = (
            "supply" in str(getattr(pattern, "pattern_type", "")).lower()
            or "demand" in str(getattr(pattern, "pattern_type", "")).lower()
        )
        if has_ob and has_snd:
            return "fv_ob_snd"
        if has_ob and has_bos:
            return "fv_ob_bos"
        if "kill" in ptype.lower() or "kz" in ptype.lower():
            return "kill_zone_pattern"
        return "default"

    # ── Technical indicator helpers ──────────────────────────────────

    @staticmethod
    def _compute_mfi(typical: np.ndarray, volumes: np.ndarray, period: int) -> float:
        """Compute Money Flow Index(14)."""
        if len(typical) < period + 1:
            return float("nan")
        raw_mf = typical * volumes
        pos_mf = np.where(typical[1:] > typical[:-1], raw_mf[1:], 0.0)
        neg_mf = np.where(typical[1:] < typical[:-1], raw_mf[1:], 0.0)
        # Sum over last `period` values
        pos_sum = np.sum(pos_mf[-period:])
        neg_sum = np.sum(neg_mf[-period:])
        if neg_sum == 0:
            return 100.0
        mfr = pos_sum / neg_sum
        return 100.0 - (100.0 / (1.0 + mfr))

    @staticmethod
    def _bollinger_bands(data: np.ndarray, period: int, std_mult: float) -> tuple:
        """Compute Bollinger Bands. Returns (upper, middle, lower)."""
        if len(data) < period:
            return float("nan"), float("nan"), float("nan")
        mid = float(np.mean(data[-period:]))
        std = float(np.std(data[-period:], ddof=0))
        return mid + std_mult * std, mid, mid - std_mult * std

    @staticmethod
    def _compute_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> tuple:
        """Compute ADX, +DI, -DI."""
        if len(closes) < period * 2:
            return float("nan"), float("nan"), float("nan")
        tr = np.zeros(len(closes))
        plus_dm = np.zeros(len(closes))
        minus_dm = np.zeros(len(closes))
        for i in range(1, len(closes)):
            h_diff = highs[i] - highs[i - 1]
            l_diff = lows[i - 1] - lows[i]
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            plus_dm[i] = max(h_diff, 0.0) if h_diff > l_diff and h_diff > 0 else 0.0
            minus_dm[i] = max(l_diff, 0.0) if l_diff > h_diff and l_diff > 0 else 0.0
        # Smooth with Wilder's method
        atr = np.mean(tr[1 : period + 1])
        smooth_plus = np.mean(plus_dm[1 : period + 1])
        smooth_minus = np.mean(minus_dm[1 : period + 1])
        for i in range(period + 1, len(closes)):
            atr = atr - atr / period + tr[i]
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]
        if atr == 0:
            return float("nan"), float("nan"), float("nan")
        plus_di = 100.0 * smooth_plus / atr
        minus_di = 100.0 * smooth_minus / atr
        dx = 100.0 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0.0
        # ADX is smoothed DX — single value approximation
        adx = dx  # simplified; full Wilder smoothing needs more bars
        return adx, plus_di, minus_di

    @staticmethod
    def _check_vwap_rejection(bars: list[Bar], latest: Bar, direction: str) -> bool:
        """Check if current bar rejected from VWAP(15) in trade direction.

        Long: bar's low crossed below VWAP but close is above → bullish rejection.
        Short: bar's high crossed above VWAP but close is below → bearish rejection.
        """
        lookback = min(20, len(bars))
        if lookback < 15:
            return False

        recent = bars[-lookback:]
        typical_prices = np.array([(b.high + b.low + b.close) / 3.0 for b in recent])
        volumes = np.array([b.volume for b in recent])

        # VWAP(15) using last 15 bars
        vp = typical_prices[-15:]
        vv = volumes[-15:]
        total_vol = vv.sum()
        if total_vol <= 0:
            return False
        vwap = float(np.sum(vp * vv) / total_vol)

        if direction == "long":
            return latest.low < vwap < latest.close
        else:
            return latest.high > vwap > latest.close

    def _get_session_state(self, ts: datetime) -> SessionState:
        session_name = self._session_analyzer.get_current_session(ts)
        _ = self._session_analyzer.is_kill_zone(ts)
        session_result = self._session_analyzer.score_session_phase_with_time(session_name, ts)
        return SessionState(
            session_name=session_name,
            kill_zone_active=session_result["kill_zone_active"],
            phase_score=session_result["phase_score"],
            directional_bias=session_result.get("directional_bias"),
        )

    def _compute_htf_state(self, bars: list[Bar]) -> HTFState:
        """Compute pseudo-HTF state from M15 bars.

        Uses the last 672 bars (~7 days of M15) as D1-equivalent
        for trend detection and phase classification.
        """
        from signal_engine.data_types import HTFPhase

        lookback = min(672, len(bars))
        if lookback < 20:
            return HTFState(HTFPhase.NEUTRAL, 0.0, 0.0, 0.0)

        recent = bars[-lookback:]
        closes = np.array([b.close for b in recent])
        highs = np.array([b.high for b in recent])
        lows = np.array([b.low for b in recent])

        ema_50 = self._ema(closes, min(50, len(closes)))

        # For M15 data: 80 bars = 20 hours for range
        range_bars = min(80, len(highs))
        range_high = float(np.max(highs[-range_bars:]))
        range_low = float(np.min(lows[-range_bars:]))
        range_size = (range_high - range_low) / range_low if range_low > 0 else 0.0

        # EMA slope over 80 bars
        ema_slope = 0.0
        if len(ema_50) >= 80:
            valid = ema_50[-80:]
            valid = valid[~np.isnan(valid)]
            if len(valid) >= 2:
                slope_raw = valid[-1] - valid[0]
                ema_slope = slope_raw / (len(valid) * valid[-1]) if valid[-1] > 0 else 0.0

        # Phase: use M15-appropriate thresholds
        # Boardroom: range < 0.3% over 20h is very tight
        # Trend: ema_slope > 0.00003 per bar is meaningful on M15
        phase = self._classify_htf_phase(range_size, ema_slope, highs, lows, closes)

        # Alignment score — include M15 direction alongside D1 for scoring
        htf_dir = "bullish" if ema_slope > 0.00003 else ("bearish" if ema_slope < -0.00003 else None)
        mtf_data = {}
        if htf_dir:
            mtf_data["D1"] = htf_dir
            mtf_data["M15"] = htf_dir
        alignment = self._htf_analyzer.analyze_htf_alignment(mtf_data)

        return HTFState(phase, alignment, ema_slope, range_size)

    @staticmethod
    def _classify_htf_phase(range_size, ema_slope, highs, lows, closes):
        """Classify HTF phase with M15-appropriate thresholds."""
        from signal_engine.data_types import HTFPhase

        # Consolidating: tight range + flat EMA
        if range_size <= 0.003 and abs(ema_slope) < 0.00003:
            return HTFPhase.CONSOLIDATING

        # Exhaustion: price near period extremes
        if len(highs) >= 200:
            period_high = float(np.max(highs))
            period_low = float(np.min(lows))
            current_price = float(closes[-1])
            total_range = period_high - period_low
            if total_range > 0:
                near_high = (period_high - current_price) / total_range < 0.05
                near_low = (current_price - period_low) / total_range < 0.05
                if near_high or near_low:
                    return HTFPhase.EXHAUSTION

        # Aligned: trending with EMA slope
        if abs(ema_slope) >= 0.00003:
            return HTFPhase.ALIGNED

        return HTFPhase.NEUTRAL

    @staticmethod
    def _get_asia_range_thresholds(symbol: str) -> float:
        """Pair-specific Asia range thresholds (Bug 3 fix)."""
        volatiles = {"XAUUSD", "XAGEUR", "XAGUSD"}
        return 2.0 if symbol.upper() in volatiles else 0.5

    def _get_rsi_divergence_boost(self, pattern, bars: list[Bar]) -> float:
        """Check RSI divergence for M/W patterns — returns confidence boost.

        Returns 0.15 if divergence confirmed, 0.0 otherwise.
        Never rejects — just rewards confirmed divergence.
        """
        """Check RSI divergence for M/W patterns.

        M (short): require RSI lower high while price makes higher high (bearish div).
        W (long):  require RSI higher low while price makes lower low (bullish div).
        """
        lookback = min(100, len(bars))
        closes = np.array([b.close for b in bars[-lookback:]])
        if len(closes) < 30:
            return 0.0  # not enough data, don't filter

        rsi = self._rsi(closes, 14)
        kl = getattr(pattern, "key_levels", {})

        if len(rsi) < 15:
            return 0.0

        if pattern.pattern_type == "M":
            sh1 = kl.get("SH1")
            sh2 = kl.get("SH2")
            if sh1 is None or sh2 is None:
                return 0.0
            rsi_at_peaks = []
            for peak_price in (sh1, sh2):
                diffs = np.abs(closes - peak_price)
                idx = int(np.argmin(diffs))
                rsi_val = rsi[idx]
                if not np.isnan(rsi_val):
                    rsi_at_peaks.append(rsi_val)
            if len(rsi_at_peaks) == 2 and rsi_at_peaks[1] < rsi_at_peaks[0]:
                return 0.15
            return 0.0

        elif pattern.pattern_type == "W":
            sl1 = kl.get("SL1")
            sl2 = kl.get("SL2")
            if sl1 is None or sl2 is None:
                return 0.0
            rsi_at_troughs = []
            for trough_price in (sl1, sl2):
                diffs = np.abs(closes - trough_price)
                idx = int(np.argmin(diffs))
                rsi_val = rsi[idx]
                if not np.isnan(rsi_val):
                    rsi_at_troughs.append(rsi_val)
            if len(rsi_at_troughs) == 2 and rsi_at_troughs[1] > rsi_at_troughs[0]:
                return 0.15
            return 0.0

        return 0.0

    @staticmethod
    def _rsi(data: np.ndarray, period: int = 14) -> np.ndarray:
        """Compute RSI."""
        rsi = np.full_like(data, np.nan, dtype=float)
        if len(data) < period + 1:
            return rsi
        delta = np.diff(data)
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        avg_gain = np.mean(gain[:period])
        avg_loss = np.mean(loss[:period])
        if avg_loss == 0:
            rsi[period] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[period] = 100.0 - (100.0 / (1.0 + rs))
        for i in range(period, len(delta)):
            avg_gain = (avg_gain * (period - 1) + gain[i]) / period
            avg_loss = (avg_loss * (period - 1) + loss[i]) / period
            if avg_loss == 0:
                rsi[i + 1] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    def _compute_session_bias(self, bars: list[Bar], latest: Bar) -> Optional[str]:
        """Compute session directional bias from recent price action.

        Compares the open of the current session to the current price.
        Returns 'bullish', 'bearish', or None.
        """
        if len(bars) < 5:
            return None

        # Look at last 16 bars (~4 hours) for session direction
        lookback = min(16, len(bars))
        session_open = bars[-lookback].open
        current = latest.close
        move_pct = (current - session_open) / session_open

        if move_pct > 0.001:  # > 10 pips up
            return "bullish"
        elif move_pct < -0.001:  # > 10 pips down
            return "bearish"
        return None

    @staticmethod
    def _ema(data: np.ndarray, period: int) -> np.ndarray:
        """Compute EMA."""
        if len(data) < period:
            # Not enough data — return NaN-filled array
            return np.full_like(data, np.nan)
        alpha = 2.0 / (period + 1)
        ema = np.copy(data)
        ema[:period] = np.nan
        ema[period] = np.mean(data[: period + 1])
        for i in range(period + 1, len(data)):
            ema[i] = alpha * data[i] + (1 - alpha) * ema[i - 1]
        return ema

    def _build_candidate(self, pattern, latest: Bar, bars: list[Bar]) -> dict:
        return {
            "symbol": self.symbol,
            "direction": pattern.direction,
            "entry_price": latest.close,
            "confidence": pattern.confidence,
            "pattern_type": pattern.pattern_type,
            "checklist_score": getattr(pattern, "checklist_score", 0.0),
            "key_levels": getattr(pattern, "key_levels", {}),
            "timestamp": latest.time,
            "timeframe": "M15",
        }
