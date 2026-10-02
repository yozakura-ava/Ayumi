from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .engine import Bar, MarketState, StrategySignal, TradeDirection
from .strategies import ISignalStrategy


class StatArbStrategy(ISignalStrategy):
    """Statistical arbitrage strategy implementing ISignalStrategy.

    Trades the spread between two cointegrated pairs (e.g. EURUSD/GBPUSD).
    Entry on z-score extremes, exit on mean reversion, stop-loss on further divergence.
    """

    def __init__(
        self,
        lookback: int = 60,
        entry_threshold: float = 2.0,
        exit_threshold: float = 0.0,
        stop_loss_threshold: float = 3.0,
        atr_multiplier: float = 2.0,
        pair_b_bars: list[Bar] | None = None,
    ):
        from quant.cointegration import PairsSignalGenerator

        self.lookback = lookback
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.stop_loss_threshold = stop_loss_threshold
        self.atr_multiplier = atr_multiplier
        self._signal_generator = PairsSignalGenerator(
            entry_threshold=entry_threshold,
            exit_threshold=exit_threshold,
            stop_loss_threshold=stop_loss_threshold,
            lookback=lookback,
        )
        self._pair_b_bars: list[Bar] = pair_b_bars or []
        self._last_signal: str | None = None
        self._position_open: bool = False
        self._bar_count: int = 0
        self._recompute_interval: int = lookback

    @property
    def name(self) -> str:
        return "Statistical Arbitrage"

    def set_pair_b_bars(self, bars: list[Bar]):
        self._pair_b_bars = bars

    def reset(self):
        self._signal_generator.reset()
        self._last_signal = None
        self._position_open = False
        self._bar_count = 0

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.lookback + 1:
            return None

        if len(self._pair_b_bars) < len(state.bars):
            return None

        self._bar_count += 1
        prices_a = np.array([b.close for b in state.bars])
        prices_b = np.array([b.close for b in self._pair_b_bars[: len(state.bars)]])

        should_recompute = (
            self._bar_count == 1
            or self._bar_count % self._recompute_interval == 0
            or self._signal_generator.hedge_ratio is None
        )

        if should_recompute:
            if not self._signal_generator.update_cointegration(prices_a, prices_b):
                return None

        signal, reason = self._signal_generator.generate_signal(prices_a, prices_b)

        if signal is None:
            return None

        safe_reason = reason if reason else "unknown"

        if signal.startswith("entry"):
            self._last_signal = signal
            self._position_open = True
            return self._create_signal(state, signal, safe_reason)

        if signal == "exit":
            position_side = self._last_signal
            self._last_signal = None
            self._position_open = False
            return self._create_close_signal(state, position_side, safe_reason, is_stop=False)

        if signal == "stop_loss":
            position_side = self._last_signal
            self._last_signal = None
            self._position_open = False
            return self._create_close_signal(state, position_side, safe_reason, is_stop=True)

        if signal.startswith("hold"):
            if not self._position_open or self._last_signal is None:
                return None
            return self._create_signal(state, self._last_signal, safe_reason)

        return None

    def _create_signal(self, state: MarketState, direction_signal: str, reason: str) -> StrategySignal | None:
        latest = state.latest_bar
        atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)

        if atr < 1e-10:
            atr = 0.0001

        is_long = direction_signal in ("entry_long", "hold_long")
        direction = TradeDirection.LONG if is_long else TradeDirection.SHORT

        entry = latest.close
        if is_long:
            sl = entry - atr * self.atr_multiplier
            risk = abs(entry - sl)
            tp1 = entry + risk * 1.0
            tp2 = entry + risk * 2.0
            tp3 = entry + risk * 3.0
        else:
            sl = entry + atr * self.atr_multiplier
            risk = abs(entry - sl)
            tp1 = entry - risk * 1.0
            tp2 = entry - risk * 2.0
            tp3 = entry - risk * 3.0

        z_score = self._signal_generator.compute_z_score(
            np.array([b.close for b in state.bars]),
            np.array([b.close for b in self._pair_b_bars[: len(state.bars)]]),
        )
        confidence = min(0.85, 0.55 + abs(z_score) * 0.10) if z_score else 0.60
        side = "long" if is_long else "short"
        rationale = f"StatArb {side} spread: z={z_score:.2f}, {reason}"

        return StrategySignal(
            direction=direction,
            confidence=confidence,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rationale=rationale,
        )

    def _create_close_signal(
        self,
        state: MarketState,
        position_side: str | None,
        reason: str,
        is_stop: bool,
    ) -> StrategySignal:
        latest = state.latest_bar
        z_score = self._signal_generator.compute_z_score(
            np.array([b.close for b in state.bars]),
            np.array([b.close for b in self._pair_b_bars[: len(state.bars)]]),
        )

        is_long_position = position_side in ("entry_long", "hold_long")
        signal_label = "stop_loss" if is_stop else "exit"
        side_label = "long" if is_long_position else "short"
        rationale = f"StatArb {signal_label} {side_label}: z={z_score:.2f}, {reason}"

        close_direction = TradeDirection.SHORT if is_long_position else TradeDirection.LONG

        return StrategySignal(
            direction=close_direction,
            confidence=0.95,
            entry_price=latest.close,
            stop_loss=latest.close,
            take_profit_1=latest.close,
            take_profit_2=latest.close,
            take_profit_3=latest.close,
            rationale=rationale,
        )

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - 14, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14

    def get_current_z_score(self, state: MarketState) -> float | None:
        if len(state.bars) < self.lookback + 1:
            return None
        if len(self._pair_b_bars) < len(state.bars):
            return None

        prices_a = np.array([b.close for b in state.bars])
        prices_b = np.array([b.close for b in self._pair_b_bars[: len(state.bars)]])

        if self._signal_generator.hedge_ratio is None:
            if not self._signal_generator.update_cointegration(prices_a, prices_b):
                return None

        return self._signal_generator.compute_z_score(prices_a, prices_b)


@dataclass
class StatArbBacktestResult:
    """Container for statistical arbitrage backtest results."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    z_scores: list[float] | None = None
    signals: list[str] | None = None

    def __post_init__(self):
        if self.z_scores is None:
            self.z_scores = []
        if self.signals is None:
            self.signals = []

    def add_signal(self, signal_type: str, z_score: float | None):
        self.signals.append(signal_type)
        if z_score is not None:
            self.z_scores.append(z_score)
