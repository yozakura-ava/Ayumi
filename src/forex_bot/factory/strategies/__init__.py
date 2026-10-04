"""Factory-shipped concrete strategies (SFA-2).

Holds :class:`ISignalStrategy` implementations that the SFA-1 bridge ships
*via* ``forex_bot.factory.bridge``.  These are **minimal but real**
implementations — enough for the bridge to instantiate them through the
registry builders and for the validation runner to exercise them
end-to-end (WF + DSR + spread-cost gate).

Currently this subpackage contains:

* :class:`USDJPYD1TrendStrategy` — D1 timeframe trend-following strategy
  for USDJPY.  This is the concrete strategy class that SFA-1 deferred
  (the SFA-1 bridge used a placeholder builder for ``strategy_id ==
  "usdjpy_d1_trend"``); SFA-2 lands the actual implementation here.

The subpackage is deliberately tiny — the spec expects the per-archetype
templates to land in SFA-3 (momentum / mean_reversion / breakout /
trend_following / session_based).  This module only resolves the
remaining SFA-1 deferred entry.
"""

from __future__ import annotations

from forex_bot.factory.strategies.usdjpy_d1_trend import (
    USDJPYD1TrendConfig,
    USDJPYD1TrendStrategy,
)

__all__ = ["USDJPYD1TrendConfig", "USDJPYD1TrendStrategy"]