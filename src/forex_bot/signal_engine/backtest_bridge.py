"""Wire the signal engine into the existing backtest engine."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from .data_types import (
    Level,
    LevelType,
    Signal,
    Swing,
)
from .htf_analyzer import HTFAnalyzer
from .level_counter import LevelCounter
from .session_logic import SessionAnalyzer
from .swing_detector import SwingDetector


class SignalEngineBridge:
    """Bridge between signal engine components and the backtest engine.

    The existing backtest engine (backtest/engine.py) expects a strategy to
    produce StrategySignal objects with direction, confidence, entry/SL/TP prices.

    This bridge orchestrates the signal engine pipeline and exposes:
    - run(df) → List[Signal] for batch processing
    - get_signals_for_bar(df, bar_idx) → Optional[Signal] for per-bar queries
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.lookback = config.get("lookback", 5)
        self.min_confidence = config.get("min_confidence", 0.40)
        self.symbol = config.get("symbol", "EURUSD")

        self.swing_detector = SwingDetector(lookback=5)  # fixed small lookback for swing detection
        self.level_counter = LevelCounter()
        self.htf_analyzer = HTFAnalyzer()
        self.session_analyzer = SessionAnalyzer()

        # Cached state from the last run
        self._swing_highs: list[Swing] = []
        self._swing_lows: list[Swing] = []
        self._levels: list[Level] = []

    def run(self, df: pd.DataFrame) -> list[Signal]:
        """Process an OHLCV DataFrame and return signals for qualifying setups.

        Args:
            df: DataFrame with columns [open, high, low, close, volume, time]
                time column should be datetime.

        Returns:
            List of Signal objects for qualifying setups.
        """
        if len(df) < 50:
            return []

        # Step 1: Detect swings — build full history up to current bar
        lookback_end = len(df) - self.lookback  # index where lookback window starts
        eval_df = df.iloc[lookback_end:].copy()
        self._swing_highs, self._swing_lows = self.swing_detector.detect_swings(
            eval_df["high"].values, eval_df["low"].values
        )

        # Step 2: Count levels
        self._levels = self.level_counter.detect_levels_with_tracking(self._swing_highs, self._swing_lows)

        # Step 3: For each bar in the eval window, check for signals
        signals: list[Signal] = []
        for i in range(len(eval_df)):
            sig = self._evaluate_bar(eval_df, i)
            if sig is not None and sig.confidence >= self.min_confidence:
                signals.append(sig)

        return signals

    def get_signals_for_bar(self, df: pd.DataFrame, bar_idx: int) -> Optional[Signal]:
        """Evaluate a single bar for signal generation.

        Note: run() should be called first to populate swing/level state.
        If called standalone, will detect swings up to bar_idx.
        """
        if bar_idx < self.lookback:
            return None
        if not self._swing_highs:
            self._swing_highs, self._swing_lows = self.swing_detector.detect_swings(
                df["high"].values[: bar_idx + 1],
                df["low"].values[: bar_idx + 1],
            )
            self._levels = self.level_counter.detect_levels_with_tracking(self._swing_highs, self._swing_lows)

        return self._evaluate_bar(df, bar_idx)

    def _evaluate_bar(self, df: pd.DataFrame, bar_idx: int) -> Optional[Signal]:
        """Evaluate a single bar for signal conditions.

        Phase 1 implementation: produces signals when price is near a
        counted level with swing-based pattern context.
        """
        bar = df.iloc[bar_idx]
        close = float(bar["close"])

        if not self._levels:
            return None

        # Find the nearest completed level
        nearest_level = self._find_nearest_level(close)

        if nearest_level is None:
            return None

        # Check if price is near the level (within 0.5%)
        distance_pct = abs(close - nearest_level.price) / nearest_level.price
        if distance_pct > 0.005:
            return None

        # Determine direction based on level type and price position
        direction = self._determine_direction(nearest_level, close)

        if direction is None:
            return None

        # Calculate entry, SL, TP
        entry = close
        stop_dist = self._estimate_stop_distance(df, bar_idx, direction)
        if stop_dist <= 0:
            return None

        stop = entry - stop_dist if direction == "long" else entry + stop_dist
        tp_dist = stop_dist * 3.0  # Minimum 3:1 R:R
        tp = entry + tp_dist if direction == "long" else entry - tp_dist

        # Calculate confidence based on available confluence
        confidence = self._calculate_confidence(nearest_level, distance_pct, bar_idx)

        # Session context
        timestamp = self._get_timestamp(df, bar_idx)
        if timestamp:
            _ = self.session_analyzer.get_current_session(timestamp)

        return Signal(
            symbol=self.symbol,
            direction=direction,
            entry_price=entry,
            stop_loss=stop,
            take_profit=tp,
            confidence=confidence,
            gates_passed=["pattern_at_level", "rr_minimum"],
            boosters_active=[],
            pattern_type=nearest_level.level_type.value,
            timeframe="H1",
            timestamp=timestamp,
            setup_type=f"level_reaction_{nearest_level.level_type.value}",
        )

    def _find_nearest_level(self, price: float) -> Optional[Level]:
        """Find the nearest completed level to the given price."""
        completed = [lv for lv in self._levels if lv.completed]
        if not completed:
            return None
        return min(completed, key=lambda lv: abs(lv.price - price))

    def _determine_direction(self, level: Level, current_price: float) -> Optional[str]:
        """Determine trade direction based on level type and price position.

        Rise levels (R1-R3) near price → expect continuation up (long)
        Drop levels (D1-D3) near price → expect bounce up (long)
        R3/D3 → reversal expected (opposite direction)
        """
        lt = level.level_type

        if lt in (LevelType.R1, LevelType.R2):
            # Continuation — pullback in rise, buy
            if current_price < level.price:
                return "long"
        elif lt in (LevelType.D1, LevelType.D2):
            # Continuation — bounce in drop, sell
            if current_price > level.price:
                return "short"
        elif lt == LevelType.R3:
            # Exhaustion — reversal expected, short
            if current_price < level.price:
                return "short"
        elif lt == LevelType.D3:
            # Exhaustion — reversal expected, long
            if current_price > level.price:
                return "long"

        return None

    def _estimate_stop_distance(self, df: pd.DataFrame, bar_idx: int, direction: str) -> float:
        """Estimate stop distance using recent swing or ATR."""
        # Simple ATR-based estimate (14-bar)
        lookback = min(14, bar_idx)
        if lookback < 2:
            return 0.0

        tr_sum = 0.0
        for i in range(bar_idx - lookback + 1, bar_idx + 1):
            h = float(df.iloc[i]["high"])
            low_val = float(df.iloc[i]["low"])
            c_prev = float(df.iloc[i - 1]["close"])
            tr = max(h - low_val, abs(h - c_prev), abs(low_val - c_prev))
            tr_sum += tr

        atr = tr_sum / lookback
        return atr * 1.5

    def _calculate_confidence(self, level: Level, distance_pct: float, bar_idx: int) -> float:
        """Calculate a base confidence score.

        Phase 1: simplified confluence. Full scoring added in Phase 3+.
        """
        score = 0.40  # Base for being at a counted level

        # Closer to level = higher confidence
        if distance_pct <= 0.002:
            score += 0.15
        elif distance_pct <= 0.005:
            score += 0.10

        # R3/D3 levels (exhaustion) get bonus
        if level.level_type in (LevelType.R3, LevelType.D3):
            score += 0.15

        # R2/D2 levels get moderate bonus
        if level.level_type in (LevelType.R2, LevelType.D2):
            score += 0.05

        return min(score, 1.0)

    @staticmethod
    def _get_timestamp(df: pd.DataFrame, bar_idx: int) -> Optional[datetime]:
        """Extract timestamp from DataFrame."""
        if "time" in df.columns:
            ts = df.iloc[bar_idx]["time"]
            if isinstance(ts, datetime):
                return ts
            try:
                return pd.to_datetime(ts).to_pydatetime()
            except Exception:
                raise ValueError(f"Cannot parse timestamp at bar {bar_idx}: {ts!r}")  # noqa: B904
        elif isinstance(df.index, pd.DatetimeIndex):
            return df.index[bar_idx].to_pydatetime()
        raise ValueError(f"No timestamp source available for bar {bar_idx}")
