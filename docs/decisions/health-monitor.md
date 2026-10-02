# Health Monitor — Design Decision

**Issue:** AYUAA-844  
**Date:** 2026-05-04  
**Author:** Kai  
**Status:** implemented

## Problem

Post-mortems from AYUAA-778 and AYUAA-807 showed data-stream failures going undetected for hours or days. Both `ForwardTestEngine` and `MultiStrategyOrchestrator` had ad-hoc health checks that were incomplete and duplicated.

## Decision

Extract a standalone, composable `HealthMonitor` class in `src/forex_bot/engine/health_monitor.py` that both engines can use.

## Design

### Component: `HealthMonitor`

- **Three detection checks:**
  1. **Data silence** — no ticks within `data_silence_threshold_sec` (default 30s)
  2. **Zero signals** — `zero_signal_tick_threshold` ticks with no signals (default 100)
  3. **Zero P&L variance** — `zero_pnl_variance_trade_threshold` trades with all identical P&L (default 10)

- **Composable API:** `record_tick()`, `record_signal()`, `record_trade_pnl()` — any engine feeds it data
- **Read-only:** never modifies trading state
- **Fail-safe:** all exceptions caught internally, never propagate
- **Configurable:** `HealthMonitorConfig` dataclass with all thresholds
- **Thread-safe:** all state protected by `threading.Lock`
- **Background loop:** optional `start()`/`stop()` for periodic checking, or call `check()` manually

### Orchestrator Integration

- `MultiStrategyOrchestrator` now creates a `HealthMonitor` instance
- Feeds it from `_on_tick`, signal generation, and closed position P&L
- Health status exposed via `OrchestratorStatus.health_healthy` and `health_alerts`
- Original basic staleness check preserved alongside the new monitor

### ForwardTestEngine

Not modified in this PR — it already has comprehensive health monitoring. Can adopt `HealthMonitor` in a follow-up to deduplicate.

## Alternatives Considered

1. **Extend ForwardTestEngine's health code** — too tightly coupled to that engine
2. **Separate process/service** — overkill for read-only monitoring; adds deployment complexity
3. **Only log warnings** — rejected; need programmatic `healthy` flag for downstream checks

## Trade-offs

- Monitor does **not** halt trading — only alerts. Decision to halt is left to the engine.
- `min_uptime_before_alerts_sec` (default 60s) prevents false positives during startup.
- P&L variance check requires ≥2 distinct trades to compute variance.
