"""IBKR live (read-only) adapter for Portfolio Intelligence.

This adapter wraps the IBKR Flex Web Service to produce structured
account snapshots for Portfolio Intelligence. It is **read-only**:
no order placement, no position modification, no state-changing calls.

See `connector.py` for the security statement at the top of the module.
"""

from __future__ import annotations

from .connector import (
    AccountInfo,
    CashRow,
    FlexStatement,
    IBKRFlexAuthError,
    IBKRFlexConfigError,
    IBKRFlexConnector,
    IBKRFlexError,
    IBKRFlexQueryInvalidError,
    IBKRFlexStatementNotReady,
    Position,
)

__all__ = [
    "AccountInfo",
    "CashRow",
    "FlexStatement",
    "IBKRFlexAuthError",
    "IBKRFlexConfigError",
    "IBKRFlexConnector",
    "IBKRFlexError",
    "IBKRFlexQueryInvalidError",
    "IBKRFlexStatementNotReady",
    "Position",
]
