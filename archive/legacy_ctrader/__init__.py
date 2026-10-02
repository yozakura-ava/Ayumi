"""Legacy cTrader modules archived on 2026-06-16 (BQ-1043 Phase 5).

Replaced by the new infrastructure in src/forex_bot/adapters/ctrader/:
- session.py, credential_store.py, token_lifecycle.py
- market_data_feed.py, order_gateway.py, position_tracker.py
- execution_event_handler.py, connection_watchdog.py, reconnect_strategy.py
- error_classifier.py, kill_switch.py, connection_state.py

Files in this package should only be imported by compatibility shims.
"""
