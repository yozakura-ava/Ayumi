# Multi-Strategy Forward Test Plan

**Date:** 2026-04-26  
**Author:** Ava (Planner)  
**Status:** Revised (replace, not add)  
**SP Estimate:** 5 SP

---

## 1. Architecture

### Current State
The existing launcher (`scripts/launch_forward_test_preloaded.py`) runs a **single strategy** (SRMR+) through `ForwardTestEngine`, consuming 234MB, one FIX session, and one tick feed. This will be **replaced** by the multi-strategy launcher — not run alongside it.
- Tick streaming via FIX → bar building
- Strategy evaluation on bar close
- Paper trading via `PaperTrader` + `FTMOConfig`
- Trade logging

The `BlendForwardTestRunner` exists as a standalone pipeline (confidence → routing → sizing) but is **not wired** into the engine's tick loop. It expects explicit `on_signal()` calls.

### Target State
Replace single-strategy engine with a **blend runner** that:

```
FIX Tick Stream
    │
    ▼
LiveMarketDataFeed (tick → bar aggregation, multi-symbol)
    │
    ▼
ForwardTestEngine (modified: multi-symbol, multi-strategy)
    │
    ├─► SRMRPlusStrategy ──────┐
    ├─► KillzoneMomentum ──────┤
    ├─► Momentum ──────────────┤
    │                          ▼
    │               cTraderLiveAdapter.evaluate_all_strategies()
    │                          │
    │                          ▼
    │               StrategyAdapter.adapt_signal() per signal
    │                          │
    │                          ▼
    │               BlendForwardTestRunner.on_signal()
    │                    │           │
    │              ConfidenceEngine  ProfileRouter
    │                    │           │
    │                    ▼           ▼
    │               SLPositionSizer (risk sizing, daily cap)
    │                    │
    │              ┌─────┴──────┐
    │              ▼            ▼
    │         ACCEPTED      REJECTED
    │              │            │
    │              ▼            ▼
    │        PaperTrader    SignalLog
    │              │
    │              ▼
    │        TradeLogger
    │
    ▼
  Health Monitor / Stats
```

### Key Design Decision: Integration Approach

**Option A (Recommended): Extend ForwardTestEngine** — Add blend runner as the signal processing backend. Engine handles tick→bar + multi-symbol. On evaluation, signals go through blend pipeline instead of directly to PaperTrader.

**Option B: Separate launcher** — New script that runs blend runner with its own tick loop, bypassing ForwardTestEngine entirely.

**Decision: Option A.** ForwardTestEngine already handles reconnection, health monitoring, bar building, and shutdown. Duplicating that is wasteful. The change is surgical: swap out `cTraderLiveAdapter.evaluate_all_strategies()` → blend runner for signal processing.

### Multi-Symbol Strategy Matrix

| Strategy | GBPUSD | EURUSD | Notes |
|----------|--------|--------|-------|
| SRMR+ | ✅ Primary | ✅ | Proven 5/5 GBPUSD |
| Killzone Momentum | ✅ | ✅ | PASS, multi-symbol in registry |
| Momentum | ✅ | ✅ (native) | PASS, native EURUSD |

### FTMO Risk Profile
- Account: $100K paper
- Risk per trade: 0.5% ($500)
- Daily loss limit: 5% ($5,000)
- Profile routing: sniper (≥0.70 confidence) vs swarm (≥0.40)
- Max concurrent: 3 sniper, 5 swarm

---

## 2. Files to Modify/Create

### Create
| File | Purpose |
|------|---------|
| `scripts/launch_blend_forward_test.py` | New launcher — instantiates blend runner + engine, wires multi-strategy |

### Modify
| File | Change |
|------|--------|
| `src/forex_bot/adapters/ctrader/forward_test_engine.py` | Add `blend_runner` optional parameter; when set, route signals through blend pipeline instead of direct PaperTrader execution. Add multi-symbol support (`symbols: list[str]` in config). |
| `src/forex_bot/adapters/ctrader/signal_adapter.py` | Ensure `evaluate_all_strategies` returns signals with strategy_id attribution compatible with `StrategyAdapter.adapt_signal()`. |

### No Changes Needed (already work)
| File | Why |
|------|-----|
| `forward_test/blend_runner.py` | Already strategy-agnostic, accepts any strategy_id + signal_data |
| `strategies/registry.py` | Already has SRMR+, killzone_momentum, momentum registered |
| `confidence/engine.py` | Already gates + confluence scoring |
| `risk/sl_position_sizer.py` | Already handles FTMO-style sizing |
| `risk/profile_router.py` | Already routes sniper/swarm |

---

## 3. Detailed Changes

### 3.1 ForwardTestEngine Modifications

1. **ForwardTestConfig**: Add `symbols: list[str] = ["GBPUSD"]` (replaces single `symbol` field, keep for backward compat). Add `blend_runner: Optional[BlendForwardTestRunner] = None`.

2. **`_evaluate_strategies()`**: After getting signals from `cTraderLiveAdapter`, if blend_runner is set:
   - For each signal, adapt via `StrategyAdapter` and pass to `blend_runner.on_signal()`
   - Log accepted/rejected with strategy attribution
   - Track `signals_rejected` in health stats

3. **Multi-symbol bar tracking**: `_bars` and `_current_bar` already keyed by symbol. Main changes:
   - `_on_tick()` filters by `config.symbols` list instead of single symbol
   - `_evaluate_strategies()` runs per symbol in the symbols list
   - Market feed subscribes to all symbols

4. **Bar preloading**: Launcher preloads bars per symbol (same pattern as current, extended to list).

### 3.2 New Launcher (`scripts/launch_blend_forward_test.py`)

```
1. Load .env
2. Fetch H1 bars via OpenAPI for GBPUSD (and EURUSD if symbol_id available)
3. Instantiate strategies: SRMRPlusStrategy, KillzoneMomentumStrategy, MomentumStrategy
4. Instantiate BlendForwardTestRunner with FTMO config
5. Instantiate ForwardTestEngine with blend_runner + multi-symbol config
6. Preload bars into engine._bars per symbol
7. Start engine, block on main loop
```

### 3.3 Signal Adapter Compatibility

Verify `cTraderLiveAdapter.evaluate_all_strategies()` returns signals with fields compatible with `StrategyAdapter.adapt_signal()`. The blend runner expects:
- `strategy_id`, `symbol`, `direction`, `entry_price`, `stop_loss`, `take_profit`, `confidence`, `timestamp`

The `StrategyAdapter` likely maps from `StrategySignal` (backtest) to `TradeSignal` (orchestrator). May need a thin adapter if field names differ.

---

## 4. Acceptance Criteria

| # | Criterion | Verification |
|---|-----------|-------------|
| AC1 | Launcher starts without errors on weekend (no live ticks needed) | `python scripts/launch_blend_forward_test.py` — clean startup log |
| AC2 | All 3 strategies instantiated and registered | Log shows 3 strategy names |
| AC3 | Blend runner receives signals in simulation mode | Unit test: mock tick → verify on_signal called with correct strategy_id |
| AC4 | Signals below confidence threshold rejected with logged reason | Log shows "Order rejected" with rejection_reason |
| AC5 | FTMO risk profile enforced (0.5% per trade, 5% daily cap) | Config inspection + sizer unit test |
| AC6 | GBPUSD bars preloaded from OpenAPI | Log shows bar count for GBPUSD |
| AC7 | EURUSD bars preloaded if symbol available (graceful skip if not) | Log shows "EURUSD: N bars" or "EURUSD: skipped" |
| AC8 | Existing single-strategy launcher still works | No breaking changes to ForwardTestEngine defaults |
| AC9 | Signal log includes strategy attribution for all signals | Log format: `strategy_id | symbol | direction | confidence | accepted/rejected` |
| AC10 | Clean shutdown persists state via StatePersistence | `blend_runner.stop()` called on SIGINT/SIGTERM |

---

## 5. SP Estimate: 5 SP

| Task | SP | Notes |
|------|----|-------|
| ForwardTestEngine multi-symbol + blend runner integration | 2 | Core plumbing — config change, evaluation loop, signal routing |
| New launcher script | 1 | Mostly wiring existing components |
| Signal adapter compatibility / thin mapping | 0.5 | May be trivial, may need adapter layer |
| Multi-symbol bar preloading | 0.5 | Extend existing fetch pattern |
| Testing (AC1-AC10) | 1 | Weekend verification + unit tests |

---

## 6. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Signal format mismatch** between `cTraderLiveAdapter` output and `BlendForwardTestRunner.on_signal()` input | Medium | Inspect `StrategyAdapter.adapt_signal()` early; write a thin mapper if needed |
| **Multi-symbol tick subscription** — OpenAPI symbol IDs for EURUSD may differ from expected | Low | Launcher queries feed's symbol map on startup; graceful fallback if symbol unavailable |
| **Evaluation thread safety** — multiple symbols evaluating simultaneously could race on blend runner state | Medium | Blend runner already single-threaded per signal; engine's `_eval_semaphore` (binary) prevents concurrent evals. For multi-symbol, may need per-symbol semaphore or queue-based serial processing |
| **Existing forward test breakage** | Low | All changes are additive (new optional params). Default behavior unchanged. Verify AC8. |
| **Weekend testing limitation** — can't verify live tick processing until Sunday 5pm | Medium | Structure work so startup + bar preloading + config are verifiable now; tick processing verified on market open |
| **Killzone/Momentum strategy configs** — may need symbol-specific tuning for GBPUSD (Momentum was native EURUSD) | Low | Use default configs initially; tuning is a separate task |
| **State persistence conflict** — blend runner writes to `data/risk_state.json`, existing forward test may use different state | Low | Use distinct state_path per launcher |

---

## 7. Execution Order

1. **Read** `StrategyAdapter.adapt_signal()` and `cTraderLiveAdapter.evaluate_all_strategies()` to confirm signal format compatibility
2. **Modify** `ForwardTestEngine` — add multi-symbol + blend runner support
3. **Create** `scripts/launch_blend_forward_test.py`
4. **Test** startup on weekend (AC1, AC2, AC6-AC8)
5. **Verify** signal routing with mock ticks (AC3, AC4, AC9)
6. **Wait** for market open, verify live tick processing (AC3, AC4, AC9, AC10)
7. **Monitor** for 24-48h before considering stable
