# Codebase Assessment: src/forex_bot/

**Author:** Kai (Lead Engineer)
**Date:** 2026-04-17
**Issue:** [AYU-39](/AYU/issues/AYU-39)
**Scope:** Full read-only audit of `src/forex_bot/`

---

## 1. Executive Summary

The forex_bot codebase is a **large, functional but heavily duplicated** Python trading system (~14,000 lines across 100+ source files). It contains:

- A working backtest engine with walk-forward validation
- ICT/SMC strategy analysis (confluence engine, order blocks, FVGs, liquidity sweeps)
- TTC (trend trade confluence) signal system with pattern detection
- cTrader FIX adapter for live/paper trading
- FTMO-compliant risk management
- ML pipeline for confidence learning
- A parallel C# cBot implementation

**Critical findings:** Massive code duplication across 4+ engine variants, 48 test files are stubs (zero assertions), several dead modules, and numerous subtle bugs in ADX/RSI calculations and spread handling.

---

## 2. File/Directory Inventory

### 2.1 Top-Level Structure

```
src/forex_bot/
  __init__.py                  # Package marker
  run_live_paper.py            # Live paper trading entry point (941 lines)
  signal_validator.py          # Standalone validator (DEAD — duplicates gate_validator)
  requirements.txt             # numpy, pandas, scikit-learn, xgboost, optuna
  adapters/                    # cTrader live trading adapters
  backtest/                    # Backtest engines and strategies
  cbot/                        # C# cBot project (separate codebase, 35 files)
  config/                      # Session configuration
  ctrader_fix/                 # Legacy FIX connection (DEAD — replaced by adapters/ctrader)
  data/                        # Historical data downloader
  logs/                        # Live paper trade logs
  ml/                          # ML pipeline and optimization
  quant/                       # Quantitative analysis (walk-forward, regime, portfolio)
  signal_engine/               # TTC signal processing pipeline
  strategies/                  # Production strategies
```

### 2.2 Module Inventory

| Module | Files | Lines | Status | Purpose |
|--------|-------|-------|--------|---------|
| `backtest/` | 42 | ~7,800 | Active | Backtest engines, trade management, ICT/SMC, parameter sweep |
| `strategies/` | 14 | ~2,800 | Active | Production strategy implementations |
| `signal_engine/` | 14 | ~3,700 | Active | TTC pattern detection, confluence scoring, gate validation |
| `adapters/ctrader/` | 10 | ~3,400 | Active | cTrader FIX protocol, order management, risk guard |
| `quant/` | 13 | ~3,000 | Active | Walk-forward validation, regime detection, portfolio, cointegration |
| `ml/` | 16 | ~5,500 | Mixed | ML training pipeline, optimization scripts (many standalone) |
| `data/` | 2 | ~420 | Active | Historical data download from cTrader Open API |
| `config/` | 2 | ~55 | Active | Session time definitions |
| `ctrader_fix/` | 2 | ~255 | **DEAD** | Legacy FIX connection (replaced by adapters) |
| `cbot/` | 35 | ~N/A | Separate | C# cBot — not Python, separate project |

**Total Python source: ~26,900 lines across 115 files**

---

## 3. Working Components (What to Keep)

### 3.1 Core Backtest Infrastructure

- **`backtest/multi_strategy_engine.py`** — Primary backtest engine. Most-used in the codebase. Runs individual and combined strategy backtests.
- **`backtest/enhanced_engine.py`** — Enhanced engine with trade management (partial exits, trailing stops, session filters).
- **`backtest/walk_forward_runner.py`** — Walk-forward validation runner using quant.walk_forward.
- **`backtest/data_loader.py`** — CSV data loading into Bar objects.
- **`backtest/portfolio_blend.py`** — Portfolio blend framework with correlation analysis and walk-forward.
- **`backtest/runner.py`** — CLI orchestration for all backtest types (1250 lines, has duplication issues).

### 3.2 ICT/SMC Analysis

- **`backtest/ict_smc/confluence_engine.py`** — Central confluence engine combining structure, order blocks, FVGs, sweeps, premium/discount.
- **`backtest/ict_smc/market_structure.py`** — Swing point detection, BOS/CHoCH, trend bias.
- **`backtest/ict_smc/order_block.py`** — Impulsive candle / order block detection.
- **`backtest/ict_smc/fvg.py`** — Fair Value Gap detection and tracking.
- **`backtest/ict_smc/liquidity_sweep.py`** — Liquidity pool and sweep analysis.
- **`backtest/ict_smc/premium_discount.py`** — Premium/discount zone classification.

### 3.3 Signal Engine (TTC System)

- **`signal_engine/pattern_detector.py`** — M/W formations, SVCs, traps, FL patterns (1634 lines, largest file).
- **`signal_engine/gate_validator.py`** — Hard gate validation for signal quality.
- **`signal_engine/confluence_scorer.py`** — Weighted confluence scoring with 7 boost factors.
- **`signal_engine/session_logic.py`** — Session definitions, kill zone detection, phase scoring.
- **`signal_engine/htf_analyzer.py`** — Higher-timeframe phase analysis.
- **`signal_engine/level_counter.py`** — R1-R3 / D1-D3 counting.
- **`signal_engine/swing_detector.py`** — N-bar swing detection.
- **`signal_engine/stop_target.py`** — ATR-based SL/TP calculator.
- **`signal_engine/tp_manager.py`** — 3-level TP with progressive SL management.
- **`signal_engine/risk_sizer.py`** — Confidence-based position sizing (T1-T5 tiers).
- **`signal_engine/backtest_bridge.py`** — Bridge to ISignalStrategy interface.

### 3.4 Production Strategies

- **`strategies/session_range_mean_reversion.py`** — Core MR strategy with regime filter variant.
- **`strategies/session_range_mr_ict_filtered.py`** — MR + ICT confluence wrapper.
- **`strategies/volatility_squeeze.py`** — BB/KC squeeze breakout.
- **`strategies/killzone_momentum.py`** — Killzone-based momentum.
- **`strategies/usdjpy_d1_trend.py`** — D1 trend following (implements ISignalStrategy).
- **`strategies/mtf_filtered_momentum.py`** — MTF regime filter wrapper.
- **`strategies/grid/`** — Full grid trading system (manager, adapter, config, trend filter).

### 3.5 cTrader Live Trading

- **`adapters/ctrader/api_client.py`** — Full FIX 4.4 protocol implementation (947 lines).
- **`adapters/ctrader/market_data_feed.py`** — Live market data via FIX QUOTE.
- **`adapters/ctrader/order_manager.py`** — Order lifecycle management.
- **`adapters/ctrader/risk_guard.py`** — FTMO-compliant risk management (daily loss, drawdown, circuit breaker).
- **`adapters/ctrader/paper_trader.py`** — Paper trading engine.
- **`adapters/ctrader/signal_adapter.py`** — Strategy-to-cTrader bridge.
- **`run_live_paper.py`** — Main live paper trading system (941 lines).

### 3.6 Quantitative Analysis

- **`quant/walk_forward.py`** — Walk-forward validation with GO/NO-GO criteria (487 lines).
- **`quant/regime.py`** — Volatility/trend/session regime detection.
- **`quant/vaps.py`** — Volatility-adaptive position sizing.
- **`quant/portfolio.py`** — Multi-strategy portfolio management.
- **`quant/pipeline.py`** — Pre-trade checks and trade decision pipeline.
- **`quant/cointegration.py`** — Engle-Granger cointegration for pairs trading.
- **`quant/correlation.py`** — Rolling correlation and exposure tracking.

### 3.7 ML Pipeline

- **`ml/train_model.py`** — Core ML training (walk-forward, GB/RF/XGB).
- **`ml/features.py`** — Technical feature engineering.
- **`ml/signal_simulator.py`** — Signal generation and trade labeling.
- **`ml/confidence_learner.py`** — Per-symbol confidence classifier.
- **`ml/mean_reversion.py`** — ML-based MR strategy (implements ISignalStrategy).
- **`ml/per_symbol_configs.py`** — Auto-generated per-symbol TTC configs.

### 3.8 Trade Management

- **`backtest/trade_management/trade_manager.py`** — Central orchestrator.
- **`backtest/trade_management/partial_exit.py`** — Tiered TP system.
- **`backtest/trade_management/trailing_stop.py`** — 4 trailing stop methods (ATR, step, time, SAR).
- **`backtest/trade_management/session_filter.py`** — Session-based entry/hold filtering.
- **`backtest/trade_management/exit_refinement.py`** — Time stops, momentum reversal.

---

## 4. Dead/Deprecated Code (What to Cut)

### 4.1 Confirmed Dead Modules

| File | Lines | Reason |
|------|-------|--------|
| `ctrader_fix/connection.py` | 250 | Replaced by `adapters/ctrader/api_client.py`. Different FIX header format, simpler implementation. Zero unique functionality. |
| `quant/regime_detection.py` | 257 | Exact duplicate of `quant/regime.py` with hardcoded thresholds instead of configurable ones. Zero imports anywhere. |
| `ml/tier_optimizer.py` | 661 | Zero imports. Standalone script using synthesized/fabricated data, not grounded in real backtests. |
| `ml/data_source.py` | 174 | Zero imports. SQLite/CSV loaders never used by any module. |
| `ml/predict.py` | 109 | Zero imports. Signal filter using pickle-loaded models, never integrated. |
| `signal_validator.py` | 189 | Duplicates `signal_engine/gate_validator.py` with incompatible type system. Zero runtime imports. |
| `backtest/parameter_sweep/legacy_optimizer.py` | 65 | Only supports one strategy, named "legacy". Superseded by `ttc_optimizer.py`. |

### 4.2 Likely Dead/Experimental

| File | Lines | Reason |
|------|-------|--------|
| `ml/weight_optimizer.py` | 351 | Standalone script only. Monkey-patches strategy constants. |
| `ml/optuna_optimizer.py` | 576 | Standalone script only. Heavy monkey-patching. |
| `ml/per_symbol_optimizer.py` | 557 | Standalone script. Overwrites hand-curated configs with JSON. |
| `ml/backtest_with_ml.py` | 476 | Experimental. Data leakage risk (trains and tests on same data). |
| `backtest/ict_smc/volume_delta.py` | 111 | Not imported in `ict_smc/__init__.py` or used by `confluence_engine.py`. |
| `backtest/engine.py` BacktestEngine.run() | ~20 | Contains `pass` — no signal evaluation logic. `MultiStrategyBacktestEngine` is what's used. |

### 4.3 cBot (C# Project)

The `cbot/` directory is a **complete C# cBot project** with ICT/SMC backtesting. It's a separate codebase from the Python system and appears to be an earlier/exploratory implementation. 35 C# files including strategies, backtest engine, and tests. Decision needed: archive or reference only.

---

## 5. Dependency Graph

### 5.1 Module-Level Dependencies

```
run_live_paper.py
  ├── adapters/ctrader/api_client.py
  │     └── (stdlib only — socket, ssl, threading)
  ├── adapters/ctrader/market_data_feed.py
  │     └── adapters/ctrader/api_client.py
  ├── adapters/ctrader/order_manager.py
  │     └── adapters/ctrader/models.py
  ├── adapters/ctrader/paper_trader.py
  │     ├── adapters/ctrader/models.py
  │     ├── adapters/ctrader/order_manager.py
  │     └── adapters/ctrader/risk_guard.py
  ├── adapters/ctrader/signal_adapter.py
  │     ├── backtest/engine.py
  │     └── adapters/ctrader/paper_trader.py
  └── strategies/* (backtest engine types only)

backtest/runner.py
  ├── backtest/engine.py
  ├── backtest/enhanced_engine.py
  │     ├── backtest/engine.py
  │     ├── backtest/strategies/ (ISignalStrategy)
  │     └── backtest/trade_management/*
  ├── backtest/multi_strategy_engine.py
  │     ├── backtest/engine.py
  │     ├── backtest/strategies/ (ISignalStrategy)
  │     └── signal_engine/risk_sizer.py
  ├── backtest/amalgamation.py
  │     ├── backtest/engine.py
  │     └── signal_engine/risk_sizer.py
  ├── backtest/walk_forward_runner.py
  │     ├── quant/walk_forward.py
  │     ├── backtest/engine.py
  │     └── backtest/multi_strategy_engine.py
  ├── backtest/portfolio_blend.py
  │     ├── quant/walk_forward.py
  │     ├── backtest/engine.py
  │     └── backtest/multi_strategy_engine.py
  ├── backtest/strategies/tts_strategy.py
  │     ├── backtest/engine.py
  │     ├── signal_engine/* (most modules)
  │     └── ml/per_symbol_configs.py
  └── strategies/grid/adapter.py
        └── backtest/engine.py

strategies/
  ├── strategies/session_range_mr_ict_filtered.py
  │     ├── strategies/session_range_mean_reversion.py
  │     └── backtest/ict_smc/* (confluence_engine, models)
  └── strategies/mtf_filtered_momentum.py
        ├── quant/bar_resample.py
        └── quant/mtf_regime.py

quant/
  ├── quant/pipeline.py
  │     ├── quant/config.py
  │     ├── quant/correlation.py
  │     ├── quant/regime.py
  │     ├── quant/vaps.py
  │     └── quant/position_sizing.py
  └── quant/portfolio.py
        └── strategies/* (heavy runtime imports)

ml/
  ├── ml/train_model.py
  │     ├── ml/features.py
  │     └── ml/signal_simulator.py
  └── ml/mean_reversion.py
        ├── ml/features.py
        ├── ml/signal_simulator.py
        └── backtest/strategies/ (ISignalStrategy)

data/
  └── data/ctrader_client.py
        └── (external: ctrader_open_api, twisted)
```

### 5.2 Shared Foundation

All modules depend on `backtest/engine.py` for core types: `Bar`, `MarketState`, `StrategySignal`, `TradeDirection`, `SimulatedTrade`, `ExitReason`, `SessionType`, `BarPeriod`.

The `signal_engine/` package is largely self-contained — it defines its own data types in `data_types.py` and only depends on `numpy` and `pytz`. The `backtest_bridge.py` is the only file that connects signal_engine to the backtest engine.

---

## 6. Test Coverage Assessment

### 6.1 Overview

| Metric | Value |
|--------|-------|
| Total test files | 86 |
| Real tests (5+ assertions) | 36 |
| Stubs (0 assertions) | 48 |
| Low coverage (1-4 assertions) | 1 |
| Total assertions across all tests | 1,507 |

### 6.2 Well-Tested Modules (Real Tests)

| Module | Test File(s) | Assertions |
|--------|-------------|------------|
| Statistical study | test_statistical_study.py | 108 |
| cTrader live execution | test_ctrader_live_execution.py | 95 |
| ML pipeline | test_ml_pipeline.py | 91 |
| Portfolio | test_portfolio.py | 86 |
| Portfolio blend | test_portfolio_blend.py | 82 |
| Selective pairing | test_selective_pairing.py | 76 |
| Quant pipeline | test_quant_pipeline.py | 60 |
| Asia session analyzer | test_asia_session_analyzer.py | 55 |
| cTrader risk guard | test_ctrader_risk_guard.py | 41 |
| Session range GBPUSD | test_session_range_gbpusd.py | 40 |
| Stop/target | test_stop_target.py | 39 |
| Pattern detector | test_pattern_detector.py | 37 |
| Walk-forward runner | test_walk_forward_runner.py | ~30 |
| Trade management | test_trade_management.py | ~30 |
| ICT confluence | strategies/ict/test_confluence.py | 32 |

### 6.3 Stub Tests (0 Assertions — Files Exist But Don't Test Anything)

**48 files** are stubs. These have test file names but contain zero assertions. Key gaps:

- **Strategies:** volatility_squeeze, momentum, killzone_momentum, session_range_mean_reversion, usdjpy_d1_trend, gbpusd_bb_reversion, grid_strategy
- **Backtest engines:** enhanced_engine, walk_forward, hybrid_strategy, amalgamation, vaps_engine, parameter_sweep, sweep_runner
- **Quant modules:** walk_forward, regime, mtf_regime, position_sizing, correlation, cointegration
- **ML modules:** ml_confidence, ml_mean_reversion, optuna_optimizer, data_source
- **Signal engine:** adx_calculation, flatten, signal_validator
- **Trade management:** trade_management, transaction_costs

### 6.4 Completely Untested Modules (No Test File At All)

24 source modules have no corresponding test file:

| Module | Lines | Risk |
|--------|-------|------|
| `backtest/multi_strategy_engine.py` | 601 | HIGH — primary engine |
| `backtest/ict_smc/market_structure.py` | 197 | HIGH — core ICT component |
| `backtest/ict_smc/liquidity_sweep.py` | 159 | HIGH — core ICT component |
| `backtest/ict_smc/premium_discount.py` | 91 | MEDIUM |
| `backtest/ict_smc/strategy_adapter.py` | 37 | MEDIUM |
| `backtest/ict_smc/models.py` | 186 | MEDIUM |
| `backtest/strategies/scalper_strategy.py` | 317 | MEDIUM |
| `backtest/strategies/confluence_wrapped_strategy.py` | 196 | MEDIUM |
| `backtest/builtin_strategies.py` | 112 | LOW |
| `adapters/ctrader/signal_adapter.py` | 167 | MEDIUM |
| `adapters/ctrader/trade_logger.py` | 110 | LOW |
| `strategies/mtf_filtered_momentum.py` | 91 | MEDIUM |
| `ml/features.py` | 363 | HIGH — shared dependency |
| `ml/signal_simulator.py` | 353 | HIGH — shared dependency |
| `ml/confluence_features.py` | 93 | MEDIUM |
| `ml/backtest_with_ml.py` | 476 | LOW (experimental) |
| `ml/per_symbol_optimizer.py` | 557 | LOW (standalone) |
| `ml/weight_optimizer.py` | 351 | LOW (standalone) |
| `ml/run_pipeline.py` | 116 | LOW |
| `ml/predict.py` | 109 | LOW (dead) |
| `ml/tier_optimizer.py` | 661 | LOW (dead) |
| `ml/per_symbol_configs.py` | 144 | LOW (generated) |
| `quant/bar_resample.py` | 48 | MEDIUM |
| `quant/config.py` | 137 | LOW |

---

## 7. Technical Debt Register

### 7.1 Critical Debt

| ID | Issue | Location(s) | Impact |
|----|-------|-------------|--------|
| TD-01 | **Engine code duplication** — `_reset`, `_calculate_metrics`, `_calculate_sharpe_ratio`, `_get_pip_value`, `_update_daily_tracking` copied 4+ times across engine.py, enhanced_engine.py, multi_strategy_engine.py, amalgamation.py | 4 engine files | Maintenance nightmare. Bug fix in one engine doesn't propagate. Inconsistent Sharpe (sqrt(252) vs sqrt(6048)), inconsistent spread handling. |
| TD-02 | **Technical indicator duplication** — `_calculate_atr`, `_calculate_rsi`, `_calculate_adx`, `_calculate_ema`, `_calculate_sma`, `_calculate_std` duplicated across 7+ strategy files with subtle inconsistencies | volatility_squeeze, momentum, killzone_momentum, session_range_mean_reversion, usdjpy_d1_trend, gbpusd_bb_reversion, scalper_strategy | Different RSI implementations (Wilder vs simple), different ADX bugs in each file. |
| TD-03 | **48 stub test files** — Tests exist but contain zero assertions | tests/ | False confidence in coverage. Modules appear tested but aren't. |
| TD-04 | **`_get_pip_value` duplicated 5+ times** — Different implementations across engines, some missing JPY handling | engine.py, enhanced_engine.py, amalgamation.py, multi_strategy_engine.py, trailing_stop.py | Incorrect pip values for JPY pairs in some engines. |

### 7.2 High Debt

| ID | Issue | Location(s) | Impact |
|----|-------|-------------|--------|
| TD-05 | **ADX calculation bugs** — Different smoothing bugs in each implementation. Some initialize before smoothing, others use stale smoothed values. | usdjpy_d1_trend.py:197, killzone_momentum.py:174, session_range_mean_reversion.py, grid/trend_filter.py | Incorrect trend strength signals. |
| TD-06 | **Monkey-patching strategy constants** — 3 ML optimizer files `setattr()` on module-level constants to tune parameters | ml/optuna_optimizer.py, ml/per_symbol_optimizer.py, ml/weight_optimizer.py, backtest/parameter_sweep/ttc_optimizer.py | Not thread-safe. Breaks if constants are moved or renamed. Makes optimization fragile. |
| TD-07 | **Parabolic SAR implementation broken** — AF resets every bar; flip-point tracking never activates (hasattr always False) | backtest/trade_management/trailing_stop.py:164-205 | SAR trailing stop produces incorrect values. |
| TD-08 | **Confluence engine weights don't sum to 1.0** — Sum is 1.15 | backtest/ict_smc/confluence_engine.py | Confidence scores are inflated. |
| TD-09 | **Strategy adapter drops H4 context** — `strategy_adapter.py` doesn't pass h4_bars to confluence engine | backtest/ict_smc/strategy_adapter.py:24 | Adapter produces weaker signals than direct engine usage. |
| TD-10 | **`paper_trader._running` never set to True** — `is_running` property always returns False | adapters/ctrader/paper_trader.py | Live trading status check is broken. |

### 7.3 Medium Debt

| ID | Issue | Location(s) | Impact |
|----|-------|-------------|--------|
| TD-11 | **Dead code paths** — unreachable code after return, always-False conditions, unused variables | scalper_strategy.py:148-151, pattern_detector.py:485-502, confluence_wrapped_strategy.py:22, session_filter.py:105 | Code noise, potential confusion. |
| TD-12 | **`datetime.utcnow()` deprecated** — Used in 10+ files | models.py, order_manager.py, paper_trader.py, ttc_optimizer.py, data_source.py | Will break on Python 3.14+. |
| TD-13 | **Hardcoded paths** — Absolute paths to data files, fragile path traversal | runner.py:59, ttc_optimizer.py:25-27, session_range_gbpusd.py | Breaks on different machines or if project moves. |
| TD-14 | **`ict_smc/models.py` uses `datetime.now()` for trade timestamps** | models.py (FairValueGap, OrderBlock) | Wall clock time instead of bar time — incorrect for backtesting. |
| TD-15 | **`HybridStrategy` doesn't implement `ISignalStrategy`** — Different evaluate() signature | backtest/hybrid_strategy.py | Can't be used with standard backtest infrastructure. |
| TD-16 | **`signal_engine/level_counter.py` class-level mutable attributes** | level_counter.py | Bug if multiple instances used simultaneously. |
| TD-17 | **`ml/mean_reversion.py` rebuilds feature matrix on every `evaluate()` call** | ml/mean_reversion.py:720-771 | O(n) per bar — too expensive for live trading. |
| TD-18 | **`quant/correlation.py` recomputes full matrix on every tick** | quant/correlation.py:69-87 | O(n*p^2) per tick — performance concern for live trading. |

### 7.4 Low Debt

| ID | Issue | Location(s) | Impact |
|----|-------|-------------|--------|
| TD-19 | `backtest/__init__.py` missing `__all__` entries | backtest/__init__.py | Incomplete public API documentation. |
| TD-20 | `ml/__init__.py` is empty | ml/__init__.py | No public API surface. |
| TD-21 | `backtest/ict_smc/volume_delta.py` unused | ict_smc/ | Dead code in active package. |
| TD-22 | No file rotation for trade logs | adapters/ctrader/trade_logger.py | Files grow indefinitely. |
| TD-23 | `data/ctrader_client.py` async method called synchronously | data/ctrader_client.py | Will fail at runtime. |

---

## 8. Code Quality Issues

### 8.1 Architecture

- **No shared utility module** for technical indicators — each strategy file reimplements ATR, RSI, ADX, EMA, SMA independently with varying quality.
- **4 backtest engine variants** (engine, enhanced_engine, multi_strategy_engine, amalgamation) with massive code duplication instead of a shared base class with mixins/composition.
- **`runner.py` reimplements backtest logic inline** (~340 lines) instead of using existing engines.
- **ML optimizer scripts are standalone** — not importable, heavy monkey-patching, can't be used as library functions.

### 8.2 Consistency

- **Sharpe ratio annualization**: sqrt(252) in most places, sqrt(6048) in runner.py
- **Spread handling**: Some engines add at entry, some deduct at exit, some do round-trip — inconsistent PnL
- **Session detection**: Multiple implementations with overlapping but different kill zone definitions
- **Pip value calculation**: 5+ implementations, some missing JPY/commodity handling
- **`TradeDirection.NEUTRAL`**: Exists in adapter models but not in backtest engine — type mismatch risk

### 8.3 Patterns

- Good: Dataclass-based config pattern with presets (trade_management/config.py, strategies/grid/config.py, quant/config.py)
- Good: ISignalStrategy interface for strategy pluggability
- Good: Walk-forward validation infrastructure
- Bad: No dependency injection — modules import concrete implementations
- Bad: Global mutable state (STRATEGY_REGISTRY, class-level attributes in level_counter)
- Bad: No structured logging — mix of print() and logging

---

## 9. Recommendations (Priority Order)

### P0 — Immediate (Blocks Phase 2)

1. **Extract shared technical indicators** into `quant/indicators.py` — ATR, RSI, ADX, EMA, SMA, STD. Single correct implementation. Eliminate 7+ duplicates.
2. **Extract shared engine base** — Common methods (_reset, _calculate_metrics, _calculate_sharpe_ratio, _get_pip_value, _update_daily_tracking) into a base class or mixin. Fix Sharpe inconsistency and spread handling.
3. **Fix critical bugs**: ADX calculations, Parabolic SAR, confluence weight sum, paper_trader._running.

### P1 — Before New Development

4. **Delete dead code**: ctrader_fix/, quant/regime_detection.py, ml/tier_optimizer.py, ml/data_source.py, ml/predict.py, signal_validator.py, backtest/parameter_sweep/legacy_optimizer.py
5. **Convert 48 stub tests to real tests** — Prioritize: multi_strategy_engine, walk_forward, enhanced_engine, regime, volatility_squeeze, session_range_mean_reversion
6. **Fix strategy adapter** to pass H4 context
7. **Replace `datetime.utcnow()`** with `datetime.now(timezone.utc)`

### P2 — During Refactor

8. **Consolidate engine variants** — Consider single engine with pluggable trade management
9. **Eliminate monkey-patching** in ML optimizers — Use strategy config objects instead
10. **Add proper logging** throughout (replace print() calls)
11. **Make technical indicator calculations timeframe-aware** (ATR lookback, SL buffers, etc.)
12. **Archive cbot/ C# project** — Move to separate repo or archive directory

---

## 10. Statistics

| Metric | Value |
|--------|-------|
| Python source files | 115 |
| Python source lines | ~26,900 |
| C# source files | 35 (separate project) |
| Test files | 86 |
| Test files with real assertions | 36 (42%) |
| Test files that are stubs | 48 (56%) |
| Untested source modules | 24 |
| Dead/deprecated files | 7 |
| Standalone ML scripts | 5 |
| Technical debt items (critical) | 4 |
| Technical debt items (high) | 6 |
| Technical debt items (medium) | 8 |
| Technical debt items (low) | 5 |
| Duplicated code patterns | 5 major (engine duplication, indicator duplication, pip value, load_bars, session detection) |
