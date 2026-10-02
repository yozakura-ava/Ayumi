"""Canonical FTMO 1-Step Standard parameters — SINGLE SOURCE OF TRUTH.

All FTMO guard/enforcement modules MUST import from here.  Hard-coding
FTMO parameters in individual modules caused a P0 divergence bug where
the live ``RiskGuard`` used 5% daily drawdown while the locked spec is
3% (see card 26eac23a, Tomoe arch review §3.2).

FTMO 1-Step Standard Challenge (verified 2026-07-13):
    - Daily Drawdown:  3% of starting balance
    - Total Drawdown: 10% of starting balance
    - Reference account size: $10,000

These values are intentionally simple constants — not configurable via
constructor arguments or config files.  FTMO rules are fixed by the
prop-firm contract and must not be overridden at runtime.
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# LOCKED FTMO 1-Step Standard — DO NOT OVERRIDE
# ─────────────────────────────────────────────────────────────────────────────

#: Daily drawdown limit as a fraction of starting balance (0.03 = 3%).
FTMO_DAILY_DD_LIMIT_PCT: float = 0.03

#: Total drawdown limit as a fraction of starting balance (0.10 = 10%).
FTMO_TOTAL_DD_LIMIT_PCT: float = 0.10

#: Reference starting balance for FTMO 1-Step Standard ($10,000).
FTMO_REFERENCE_ACCOUNT_SIZE: float = 10_000.0

# ─────────────────────────────────────────────────────────────────────────────
# Standard profile parameters (aligned with FTMO 1-Step defaults)
# ─────────────────────────────────────────────────────────────────────────────

#: Risk per trade as a fraction of balance (0.5%).
FTMO_RISK_PER_TRADE_PCT: float = 0.005

#: Maximum concurrent positions.
FTMO_MAX_CONCURRENT_POSITIONS: int = 3

#: Maximum trades per day.
FTMO_MAX_TRADES_PER_DAY: int = 10

#: Minimum risk:reward ratio.
FTMO_MIN_RISK_REWARD: float = 1.5

#: Best-day rule: best day's profit must not exceed 50% of total
#: positive-days profit (FTMO hard cap).
FTMO_BEST_DAY_CAP_PCT: float = 0.50

#: Best-day enforcement threshold — halt trading at 40% to provide a
#: 10% safety buffer below the 50% FTMO hard cap.
FTMO_BEST_DAY_ENFORCE_PCT: float = 0.40

# ─────────────────────────────────────────────────────────────────────────────
# Derived dollar amounts (convenience for the reference $10K account)
# ─────────────────────────────────────────────────────────────────────────────

#: Daily DD limit in USD for the reference $10K account ($300).
FTMO_DAILY_DD_LIMIT_USD: float = FTMO_REFERENCE_ACCOUNT_SIZE * FTMO_DAILY_DD_LIMIT_PCT

#: Total DD limit in USD for the reference $10K account ($1,000).
FTMO_TOTAL_DD_LIMIT_USD: float = FTMO_REFERENCE_ACCOUNT_SIZE * FTMO_TOTAL_DD_LIMIT_PCT


__all__ = [
    "FTMO_DAILY_DD_LIMIT_PCT",
    "FTMO_TOTAL_DD_LIMIT_PCT",
    "FTMO_REFERENCE_ACCOUNT_SIZE",
    "FTMO_RISK_PER_TRADE_PCT",
    "FTMO_MAX_CONCURRENT_POSITIONS",
    "FTMO_MAX_TRADES_PER_DAY",
    "FTMO_MIN_RISK_REWARD",
    "FTMO_BEST_DAY_CAP_PCT",
    "FTMO_BEST_DAY_ENFORCE_PCT",
    "FTMO_DAILY_DD_LIMIT_USD",
    "FTMO_TOTAL_DD_LIMIT_USD",
]
