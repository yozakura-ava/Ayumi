"""Strategy adapter — converts raw strategy outputs into OrchestratorTradeSignal objects."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from orchestrator.signal_orchestrator import OrchestratorTradeSignal

logger = logging.getLogger("ayumi.orchestrator")


class StrategyAdapter:
    """Converts strategy-specific output formats into OrchestratorTradeSignal objects."""

    def adapt_signal(self, strategy_id: str, strategy_output: dict) -> OrchestratorTradeSignal:
        """Convert a strategy's raw output dict to OrchestratorTradeSignal.

        Expected keys in strategy_output:
        - symbol (required)
        - direction (required, "LONG"/"SHORT")
        - entry_price (required)
        - stop_loss (required)
        - take_profit (optional, defaults to 0.0)
        - confidence (optional, defaults to 0.5)
        - confluence_strategies (optional)
        - confluence_timeframes (optional)
        - spread (optional)
        - atr (optional)
        """
        # Required fields
        missing = []
        for key in ("symbol", "direction", "entry_price", "stop_loss"):
            if key not in strategy_output or strategy_output[key] is None:
                missing.append(key)
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")

        symbol = strategy_output["symbol"]
        direction = strategy_output["direction"]
        entry_price = strategy_output["entry_price"]
        stop_loss = strategy_output["stop_loss"]
        take_profit = strategy_output.get("take_profit", 0.0)
        confidence = strategy_output.get("confidence", 0.5)

        # Normalize direction
        direction = direction.upper()
        if direction not in ("LONG", "SHORT"):
            logger.warning("Unknown direction %r for %s, defaulting to LONG", direction, symbol)
            direction = "LONG"

        # Build metadata from optional fields
        metadata = {}
        if "confluence_strategies" in strategy_output:
            metadata["confluence_strategies"] = strategy_output["confluence_strategies"]
        if "confluence_timeframes" in strategy_output:
            metadata["confluence_timeframes"] = strategy_output["confluence_timeframes"]
        if "spread" in strategy_output:
            metadata["spread"] = strategy_output["spread"]
        if "atr" in strategy_output:
            metadata["atr"] = strategy_output["atr"]

        signal = OrchestratorTradeSignal(
            strategy_id=strategy_id,
            symbol=symbol,
            direction=direction,
            entry_price=float(entry_price),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit),
            confidence=float(confidence),
            timestamp=strategy_output.get("timestamp") or datetime.now(timezone.utc),
            metadata=metadata,
        )

        sl_dist = abs(float(entry_price) - float(stop_loss))
        logger.info(
            "Adapted signal: strategy=%s symbol=%s dir=%s conf=%.2f entry=%.5f sl=%.5f sl_dist=%.6f",
            strategy_id,
            symbol,
            direction,
            confidence,
            float(entry_price),
            float(stop_loss),
            sl_dist,
        )
        return signal
