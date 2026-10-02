"""TestCanaryStrategy — execution-path validation strategy.

Fires on every new 15m bar close to guarantee the full signal-to-fill
pipeline is exercised. NOT for profit — purely diagnostic.

Logic:
    - On each bar close, emit a LONG signal.
    - Entry = bar close price.
    - SL = entry * (1 - tp_sl_pct)   [-5% by default]
    - TP = entry * (1 + tp_sl_pct)   [+5% by default]
    - Confidence = 0.60 (above the 0.50 min_confidence gate)
    - Alternates direction each bar to avoid building up net exposure

If this strategy's signals don't result in live fills, the problem is
in the order chain, not the signal layer.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from backtest.strategies.isignal_strategy import ISignalStrategy
from core.types import Bar, MarketState, StrategySignal, TradeDirection

logger = logging.getLogger("ayumi.test_canary")


class TestCanaryStrategy(ISignalStrategy):
    """Fire a signal on every bar close to validate the execution pipeline.

    Constructor defaults to tp_sl_pct=0.0 (disabled) for safety.
    ``from_env()`` mirrors that safety posture: env-var-unset means
    disabled. To explicitly enable, set AYUMI_ENABLE_CANARY=1.

    Card cef77185-a49a-4535-a206-173a0850d8e9 (sprint 2026-09-18-ayumi-
    prodbug-24, final): the previous default ("1") meant any process
    that imported ``TestCanaryStrategy.from_env`` without setting the
    env var would silently emit diagnostic signals on every bar — a
    production-canary failure mode. The contract (test_env_var_not_set
    _means_disabled, xfail→xpinned at DEBT 6ea40384) requires the
    opposite: env-var-unset must produce a disabled instance. The
    direct constructor path (``TestCanaryStrategy(tp_sl_pct=0.0)``)
    was already correct; only the env-driven factory needed inversion.
    """

    def __init__(self, tp_sl_pct: float = 0.0):  # 0.0 = disabled (no signals)
        self.tp_sl_pct = tp_sl_pct
        self._bar_count = 0

    @classmethod
    def from_env(cls, default_tp_sl_pct: float = 0.005) -> "TestCanaryStrategy":
        """Construct from environment gating.

        Env-var-unset means DISABLED — opt-in only via
        ``AYUMI_ENABLE_CANARY=1``. This mirrors the constructor default
        (``tp_sl_pct=0.0``) so ``TestCanaryStrategy()`` and
        ``TestCanaryStrategy.from_env()`` agree on the safety posture
        when no env var is present. Card cef77185 (DEBT 6ea40384).
        """
        enabled = os.getenv("AYUMI_ENABLE_CANARY", "0").strip() == "1"
        if enabled:
            return cls(tp_sl_pct=default_tp_sl_pct)
        return cls(tp_sl_pct=0.0)

    @property
    def name(self) -> str:
        return "Test Canary"

    @property
    def enabled(self) -> bool:
        return self.tp_sl_pct > 0

    def evaluate(self, state: MarketState) -> Optional[StrategySignal]:
        if not self.enabled:
            return None
        if not state.bars:
            return None

        if self.enabled:
            logger.warning(
                "Test Canary is ENABLED (tp_sl_pct=%.4f) — emitting diagnostic signals",
                self.tp_sl_pct,
            )

        bar: Bar = state.bars[-1]
        entry = bar.close
        if entry <= 0:
            return None

        self._bar_count += 1

        # Alternate LONG/SHORT each bar to avoid directional bias
        direction = TradeDirection.LONG if self._bar_count % 2 == 1 else TradeDirection.SHORT

        sl_distance = entry * self.tp_sl_pct
        if direction == TradeDirection.LONG:
            stop_loss = entry - sl_distance
            take_profit = entry + sl_distance
        else:
            stop_loss = entry + sl_distance
            take_profit = entry - sl_distance

        return StrategySignal(
            direction=direction,
            confidence=0.60,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit_1=take_profit,
            take_profit_2=take_profit,
            take_profit_3=take_profit,
            rationale=f"canary_{self._bar_count}",
            is_volatile=False,
        )
