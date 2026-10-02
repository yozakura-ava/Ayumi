"""§10 — Structured JSON signal output for the TTC Signal Engine."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .data_types import Signal


def format_signal_json(signal: Signal, gate_results: Optional[dict] = None) -> str:
    """Serialize a Signal dataclass to a structured JSON string.

    Args:
        signal: The Signal instance to serialize.
        gate_results: Optional gate validation details dict.

    Returns:
        JSON string conforming to the §10 schema.
    """
    if gate_results is None:
        gate_results = {"_status": "not_evaluated"}

    output = {
        "signal_id": str(uuid.uuid4()),
        "timestamp": (signal.timestamp or datetime.now(timezone.utc)).isoformat(),
        "symbol": signal.symbol,
        "direction": signal.direction,
        "entry_price": signal.entry_price,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "confidence": signal.confidence,
        "gate_results": gate_results,
        "confluence_boosters": signal.boosters_active,
        "pattern_type": signal.pattern_type,
        "session": signal.setup_type or "unknown",
        "timeframe": signal.timeframe,
        "quality_score": signal.quality_score,
        "reversal_score": signal.reversal_score,
        "gates_passed": signal.gates_passed,
    }

    return json.dumps(output, default=str)


def parse_signal_json(json_str: str) -> dict[str, Any]:
    """Parse a signal JSON string back into a plain dict."""
    return json.loads(json_str)


def create_signal(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    confidence: float,
    pattern_type: str = "",
    session: str = "",
    timeframe: str = "H1",
    gates_passed: Optional[list[str]] = None,
    boosters_active: Optional[list[str]] = None,
) -> Signal:
    """Factory function to create a Signal.

    Warning: pattern_type and session default to empty string. Callers should
    provide these values to avoid corrupting ML training data and analytics.
    """
    if not pattern_type or not session:
        import logging

        logging.getLogger(__name__).warning("create_signal called without pattern_type or session")
    return Signal(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        confidence=confidence,
        pattern_type=pattern_type,
        timeframe=timeframe,
        timestamp=datetime.now(timezone.utc),
        gates_passed=gates_passed or [],
        boosters_active=boosters_active or [],
        setup_type=session,
    )
