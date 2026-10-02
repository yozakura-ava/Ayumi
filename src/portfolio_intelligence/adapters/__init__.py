"""Portfolio Intelligence broker/exchange adapters.

Each adapter exposes a connector for a single source (IBKR, cTrader,
etc.). Adapters in this directory MUST be read-only at the connection
layer — any side-effecting operation belongs in the ctrader/ subpackage
of forex_bot, not here.
"""
