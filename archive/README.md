# Archived Code — Ayumi

Archived 2026-05-01 during Sprint 2 (Ayumi remediation roadmap).

## fix-migration/
FIX 4.4 protocol code superseded by cTrader Open API migration.
- `api_client.py` — FIX client, superseded by `open_api_client.py`
- `market_data_feed.py` — FIX tick streaming, superseded by `open_api_spot_feed.py`
- `symbol_discovery.py` — FIX-dependent symbol discovery
- `session_range_gbpusd.py` — FIX-only live trading session
- `run_live_paper.py` — FIX-only live paper trading script

## order_manager.py (NOT archived)
Kept in `src/forex_bot/adapters/ctrader/order_manager.py`. While it contains dead FIX live-execution paths (now unreachable since `api_client.py` is archived), it also provides active position-sizing and paper-order logic used by `forward_test_engine.py` and `paper_trader.py`. Sprint 2 cleaned the dead `TYPE_CHECKING` import and type hints referencing the archived `cTraderAPIClient`.

## obsolete-scripts/
Test and utility scripts for the FIX protocol, Dukascopy data, and onboarding artifacts.

## unused-strategies/
Strategy files not referenced by any active launch script.
Kept for reference — may be useful for future blend expansion.

## obsolete-docs/
FIX protocol research, original spec, and sprint 1-3 artifacts.
