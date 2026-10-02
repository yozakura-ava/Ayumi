# Phase 1A — Engine Inventory & Consolidation Plan

**Audit Date:** 2026-07-08  
**Auditor:** Builder agent (Phase 1A-5)  
**Scope:** All forward-test and backtest engine implementations in Ayumi  
**Method:** Read-only code inspection — no source files modified

---

## 1. Forward-Test Engines (5 implementations)

### 1.1 ForwardTestEngine — **CANONICAL**

| Field | Value |
|-------|-------|
| **File** | `src/forex_bot/adapters/ctrader/forward_test_engine.py` |
| **Lines** | 2,734 |
| **Classes** | `ForwardTestConfig`, `ForwardTestHealth`, `LiveExecutionStatus`, `LiveExecutionOutcome`, `ForwardTestEngine` |
| **Functions** | 53 methods |
| **Status** | **PRODUCTION** — canonical engine |

**Purpose:** Wires cTrader live market data (tick streaming) into `PaperTrader` (signal processing / risk / P&L tracking) and `cTraderLiveAdapter` (strategy evaluation). Full end-to-end forward test on real market data. Includes confidence engine gating, FTMO risk guard, kill switch, health monitoring, trade journaling, and bar-close invariant.

**Launchers using it:**
- `scripts/launch_forward_test.py` — single-strategy SRMR+ on GBPUSD (paper mode)
- `scripts/launch_forward_test_preloaded.py` — same but with 100 H1 bars preloaded via OpenAPI
- `scripts/run_live_session_range_gbpusd.py` — thin wrapper, Session Range MR on GBPUSD
- `scripts/launch_blend_forward_test.py` — **PRODUCTION systemd service** (`ayumi-forward-test.service`). Subclasses `ForwardTestEngine` as `BlendForwardTestEngine` and adds `BlendForwardTestRunner` for multi-strategy blend pipeline. Runs live on GBPUSD, EURUSD, USDJPY, XAUUSD, AUDUSD, USDCHF, USDCAD.

**Consolidation recommendation:** **KEEP AS CANONICAL.** This is the production engine — 2,734 lines of battle-tested code with bar-close verification, FTMO risk guard, confidence gating, kill switch, and health monitoring. The systemd service runs this engine 24/7. All consolidation should converge *toward* this engine, not away from it.

---

### 1.2 MultiStrategyOrchestrator — **DEPRECATED**

| Field | Value |
|-------|-------|
| **File** | `src/forex_bot/engine/orchestrator.py` |
| **Lines** | 464 |
| **Classes** | `OrchestratorStatus`, `MultiStrategyOrchestrator` |
| **Functions** | 21 methods |
| **Status** | **DEPRECATED** — superseded by ForwardTestEngine + blend pipeline |

**Purpose:** Multi-strategy orchestrator that manages strategy slots, routes ticks to strategies, monitors health. Config-driven via `config/strategies.yaml`. Was an earlier attempt at unified multi-strategy orchestration.

**Launchers using it:**
- `src/forex_bot/run_srmr_plus_forward.py` — standalone forward test runner module

**Note:** The api_client.py docstring explicitly mentions "the dead-code `MultiStrategyOrchestrator.__init__`", confirming this engine is recognized as dead code internally.

**Consolidation recommendation:** **DELETE.** All production traffic runs through `ForwardTestEngine` (via `launch_blend_forward_test.py`). The `MultiStrategyOrchestrator` is explicitly referenced as dead code. Its useful concepts (strategy slots, health monitoring) are already implemented in `ForwardTestEngine` and the blend pipeline. The `run_srmr_plus_forward.py` launcher should be removed or migrated to use `ForwardTestEngine`.

---

### 1.3 TradingOrchestrator — **EXPERIMENTAL / UNFINISHED**

| Field | Value |
|-------|-------|
| **File** | `src/forex_bot/engine/trading_orchestrator.py` |
| **Lines** | 1,220 |
| **Classes** | `StrategySlotConfig`, `RiskConfig`, `OrchestratorConfig`, `_RegisteredStrategy`, `TradingOrchestrator` |
| **Functions** | 44 methods |
| **Status** | **EXPERIMENTAL** — unfinished rewrite, not in production |

**Purpose:** Intended to "replace the three separate execution paths" (per its own docstring) with a single config-driven orchestrator for both paper trading and forward testing. Supports strategy registry, signal pipeline, risk integration, and order management. Designed for bar-close and tick-driven execution.

**Launchers using it:**
- `scripts/test_mvp_full.py` — test script only
- `scripts/test_mvp_mainloop.py` — test script only

**Note:** The docstring says "Replaces the three separate execution paths (forward_test_engine, run_live_paper, Kai's orchestrator)" — but this replacement never landed in production. Only test scripts import it. 1,220 lines of unfinished refactoring.

**Consolidation recommendation:** **DELETE.** This is a 1,220-line unfinished rewrite that never reached production. Its useful design concepts (strategy registry, config-driven slots) are already present in `ForwardTestEngine`'s blend pipeline. The test scripts (`test_mvp_full.py`, `test_mvp_mainloop.py`) should be removed or updated to test the canonical engine.

---

### 1.4 SignalOrchestrator — **KEEP AS MIXIN/COMPONENT**

| Field | Value |
|-------|-------|
| **File** | `src/forex_bot/orchestrator/signal_orchestrator.py` |
| **Lines** | 150 |
| **Classes** | `OrchestratorTradeSignal`, `OrchestratedOrder`, `SignalOrchestrator` |
| **Functions** | 3 methods |
| **Status** | **PRODUCTION COMPONENT** — not a standalone engine |

**Purpose:** Wires together the signal pipeline: confidence engine → profile router → position sizer → execution. Not a full engine — it's a component that processes signals and produces sized orders. Used by `BlendForwardTestRunner` which wraps it.

**Consumers:**
- `src/forex_bot/forward_test/blend_runner.py` — instantiates `SignalOrchestrator` internally
- `src/forex_bot/orchestrator/strategy_adapter.py` — uses `OrchestratorTradeSignal`
- `src/forex_bot/backtest/blend_backtest.py` — uses `OrchestratorTradeSignal` for backtest blend
- `src/forex_bot/analysis/missed_bid_detector.py` — uses `OrchestratorTradeSignal`

**Consolidation recommendation:** **KEEP AS-IS.** This is not a competing engine — it's a component within the canonical architecture. `BlendForwardTestRunner` (which is used by the production `ForwardTestEngine` subclass) depends on it. Well-scoped at 150 lines.

---

### 1.5 BlendForwardTestRunner — **KEEP AS PRODUCTION COMPONENT**

| Field | Value |
|-------|-------|
| **File** | `src/forex_bot/forward_test/blend_runner.py` |
| **Lines** | 350 |
| **Classes** | `BlendForwardTestRunner` |
| **Functions** | 14 methods |
| **Status** | **PRODUCTION COMPONENT** — used by production launcher |

**Purpose:** Production forward test runner wiring the full Ayumi signal pipeline: confidence engine → profile router → position sizer → orchestrator. Handles state persistence across restarts. Registers `ISignalStrategy` instances evaluated on each bar.

**Consumers:**
- `scripts/launch_blend_forward_test.py` — **PRODUCTION systemd service** — creates `BlendForwardTestRunner` and injects it into `BlendForwardTestEngine(ForwardTestEngine)`
- `scripts/launch_forward_test_v2.py` — v2 launcher (experimental refactor) — creates `BlendForwardTestRunner` standalone

**Consolidation recommendation:** **KEEP AS-IS.** This is the production blend pipeline component, not a competing engine. It composes with `ForwardTestEngine` (the canonical engine) rather than competing with it. The v2 launcher's standalone use should be evaluated separately.

---

## 2. Forward-Test Engine Summary

| # | Engine | Lines | Status | Action |
|---|--------|-------|--------|--------|
| 1 | `ForwardTestEngine` | 2,734 | **PRODUCTION** | **CANONICAL** — keep, all consolidation targets this |
| 2 | `MultiStrategyOrchestrator` | 464 | DEPRECATED (dead code) | **DELETE** — explicitly referenced as dead code |
| 3 | `TradingOrchestrator` | 1,220 | EXPERIMENTAL (unfinished) | **DELETE** — never reached production, only test scripts |
| 4 | `SignalOrchestrator` | 150 | PRODUCTION COMPONENT | **KEEP** — not a competing engine, component of canonical arch |
| 5 | `BlendForwardTestRunner` | 350 | PRODUCTION COMPONENT | **KEEP** — not a competing engine, component of canonical arch |

**True competing engines: 3** (ForwardTestEngine, MultiStrategyOrchestrator, TradingOrchestrator)  
**Of those, production: 1** (ForwardTestEngine)  
**Lines to delete: 1,684** (MultiStrategyOrchestrator 464 + TradingOrchestrator 1,220)

---

## 3. Launcher Scripts Inventory

| Script | Engine Used | Production? | Notes |
|--------|------------|-------------|-------|
| `scripts/launch_blend_forward_test.py` | `ForwardTestEngine` (subclassed as `BlendForwardTestEngine`) + `BlendForwardTestRunner` | **YES** — systemd service | 55,207 lines! Multi-strategy blend on 7 symbols, live mode |
| `scripts/launch_forward_test.py` | `ForwardTestEngine` | No | Single-strategy SRMR+ on GBPUSD, paper mode |
| `scripts/launch_forward_test_preloaded.py` | `ForwardTestEngine` | No | Same as above but with 100 H1 bars preloaded via OpenAPI |
| `scripts/launch_forward_test_v2.py` | `BlendForwardTestRunner` (standalone, no `ForwardTestEngine`) | No | Experimental v2 refactor using new infrastructure modules |
| `scripts/run_live_session_range_gbpusd.py` | `ForwardTestEngine` | No | Thin wrapper, Session Range MR on GBPUSD |
| `src/forex_bot/run_srmr_plus_forward.py` | `MultiStrategyOrchestrator` | No | **Uses deprecated engine** |

**Note on `launch_blend_forward_test.py` size:** At 55,207 lines this is unusually large — likely contains embedded data or generated code. Worth investigating in a separate audit.

---

## 4. Backtest Engines (11 classes across 2 codebases)

### 4.1 `src/forex_bot/backtest/` — Primary Backtest Codebase

| # | Class | File | Lines | Status | Used By |
|---|-------|------|-------|--------|---------|
| 1 | `BacktestEngine` (alias) | `backtest/engine.py` | 41 | **PRODUCTION** | Re-exports `SimpleBacktestEngine`. Most scripts import `from backtest.engine import BacktestEngine` |
| 2 | `SimpleBacktestEngine` | `backtest/simple_engine.py` | 550 | **PRODUCTION** | The actual implementation behind `BacktestEngine` alias. Used by `backtest/types.py` and `backtest/engine.py` |
| 3 | `BacktestEngine` (types.py) | `backtest/types.py` | 631 | **DUPLICATE** | A *second* `BacktestEngine` class defined directly in `types.py` (line 276). Shadows the alias. Legacy. |
| 4 | `EnhancedBacktestEngine` | `backtest/enhanced_engine.py` | 592 | **ACTIVE** | Used by `scripts/test_regime_router.py`, `scripts/test_regime_router_sweep.py` |
| 5 | `MultiStrategyBacktestEngine` | `backtest/multi_strategy_engine.py` | 661 | **ACTIVE** | Multi-strategy backtest support |
| 6 | `VAPSBacktestEngine` | `backtest/vaps_engine.py` | 64 | **ACTIVE** | Extends `MultiStrategyBacktestEngine`. Used by `scripts/run_vaps_ab_walkforward.py` |
| 7 | `AmalgamationEngine` | `backtest/amalgamation.py` | 735 | **ACTIVE** | Signal amalgamation. Used by `AmalgamatedBacktestEngine` |
| 8 | `AmalgamatedBacktestEngine` | `backtest/amalgamation.py` | (same file) | **ACTIVE** | Used by `backtest/runner.py` |
| 9 | `SignalConfluenceEngine` | `backtest/ict_smc/confluence_engine.py` | 470 | **EXPERIMENTAL** | ICT/SMC confluence signals. No external consumers found. |
| 10 | `SweepRunner` | `backtest/parameter_sweep/sweep_runner.py` | 134 | **ACTIVE** | Parameter sweep runner. Used by 4+ sweep scripts |

### 4.2 `src/forex_bot/engine/` — New Engine Architecture (forward-test infrastructure)

| # | Class | File | Lines | Status | Notes |
|---|-------|------|-------|--------|-------|
| 11 | `EngineCore` | `engine/base.py` | 324 | **PRODUCTION** | Base class for `BacktestEngine` (mixin architecture) |
| 12 | `BacktestEngine` (mixin) | `engine/engine.py` | 178 | **PRODUCTION** | Composed: `EngineCore + ProgressiveSLMixin + TradeManagementMixin + CombinedSignalMixin` |

### 4.3 `src/forex_trading/services/backtest/` — Domain Layer Backtest (v2 architecture)

| # | Class | File | Lines | Status | Notes |
|---|-------|------|-------|--------|-------|
| 13 | `EngineCore` (v2) | `engine_core/base.py` | 328 | **UNUSED** | Separate `EngineCore` in domain layer. Not imported by any scripts. |
| 14 | `BacktestEngine` (v2) | `engine_v2.py` | 382 | **UNUSED** | Composed backtest engine in domain layer. Not imported by any scripts. |
| 15 | `PropFirmRuleEngine` | `prop_firm_rules.py` | 218 | **UNKNOWN** | Prop firm rule validation. No script imports found. |

### 4.4 Other Engine-like Classes (not backtest runners)

| Class | File | Lines | Purpose |
|-------|------|-------|---------|
| `TradeRulesEngine` | `hybrid/trade_rules.py` | — | Trade rules engine for hybrid mode |
| `ConfluenceEngine` | `strategies/ict/confluence.py` | — | ICT confluence strategy engine |
| `ICTEngine` | `strategies/ict/engine.py` | — | ICT strategy engine |
| `FeatureEngineer` | `quant/correlation_regime_hmm.py` | — | Feature engineering for HMM |
| `CointegrationEngine` | `quant/cointegration.py` | — | Cointegration testing |

---

## 5. Backtest Consolidation Observations

1. **Three separate `BacktestEngine` definitions exist:**
   - `backtest/engine.py` → alias for `SimpleBacktestEngine` (production path)
   - `backtest/types.py` line 276 → standalone class (legacy, shadows the alias)
   - `engine/engine.py` → mixin-composed `BacktestEngine` (new architecture)
   - `forex_trading/services/backtest/engine_v2.py` → another mixin-composed version (unused)

2. **The `forex_trading/services/backtest/` codebase (928 lines across 3 files) appears entirely unused** — no scripts or src/ modules import from it. This is likely an abandoned domain-layer migration.

3. **`SignalConfluenceEngine` in `backtest/ict_smc/` has no external consumers** — likely experimental ICT/SMC work.

4. **Two parallel architectures:** `backtest/` (legacy, functional) and `engine/` (new mixin-based, partially wired). The `engine/engine.py` `BacktestEngine` is composed via mixins but the production scripts still import from `backtest/engine.py` which aliases `SimpleBacktestEngine`.

---

## 6. Consolidation Recommendations

### Forward-Test Engines
1. **Declare `ForwardTestEngine` as canonical** — it's the only production engine, verified by systemd service configuration
2. **Delete `MultiStrategyOrchestrator`** (464 lines) — explicitly dead code per internal docstrings
3. **Delete `TradingOrchestrator`** (1,220 lines) — unfinished rewrite, only test scripts use it
4. **Keep `SignalOrchestrator` and `BlendForwardTestRunner`** — these are components within the canonical architecture, not competing engines
5. **Evaluate `launch_forward_test_v2.py`** — uses `BlendForwardTestRunner` standalone without `ForwardTestEngine`. Either complete the v2 architecture or remove the v2 launcher

### Backtest Engines
1. **Standardize on `SimpleBacktestEngine`** (via `backtest.engine.BacktestEngine` alias) as canonical
2. **Remove the duplicate `BacktestEngine` class in `backtest/types.py`** — it shadows the alias and causes confusion
3. **Delete `forex_trading/services/backtest/`** (928 lines) — entirely unused domain-layer migration
4. **Investigate `engine/` mixin architecture** — `EngineCore` + `BacktestEngine(EngineCore, mixins)` is the intended future architecture but isn't wired into production scripts yet
5. **Mark `SignalConfluenceEngine` as experimental** — no external consumers

### Estimated Cleanup
- **Forward-test deletions:** 1,684 lines (2 dead engines + 1 deprecated launcher)
- **Backtest deletions:** 928+ lines (unused domain layer + duplicate class)
- **Total:** ~2,600+ lines of dead/duplicate code removable

---

## 7. Canonical Architecture (Target State)

```
Forward Test:
  ForwardTestEngine (canonical)
    ├── BlendForwardTestRunner (signal pipeline component)
    │   └── SignalOrchestrator (confidence → routing → sizing)
    ├── PaperTrader (risk / P&L)
    ├── cTraderLiveAdapter (strategy evaluation)
    └── FTMOConfig + KillSwitch + HealthMonitor (safety)

Backtest:
  SimpleBacktestEngine (canonical, aliased as BacktestEngine)
    ├── MultiStrategyBacktestEngine (multi-strategy extension)
    │   └── VAPSBacktestEngine (VAPS regime extension)
    ├── EnhancedBacktestEngine (regime router extension)
    ├── AmalgamatedBacktestEngine (signal amalgamation)
    └── SweepRunner (parameter sweeps)
```

---

**Audit complete.** No source files were modified. All data gathered via `grep`, `wc -l`, `head`, and direct file reads on 2026-07-08.
