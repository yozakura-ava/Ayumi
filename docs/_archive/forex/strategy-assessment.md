# Strategy & Testing Assessment — AYU-40

**Issue:** [AYU-40](/AYU/issues/AYU-40)  
**Author:** Sage (QA + Research)  
**Date:** 2026-04-16  
**Status:** Done  
**Parent:** [AYU-38](/AYU/issues/AYU-38) Phase 1 Assessment

---

## 1. Strategy Inventory

### 1.1 Active Strategies (in `src/forex_bot/strategies/`)

| Strategy | File | Status | Notes |
|----------|------|--------|-------|
| Session Range Mean Reversion (SRM) | `session_range_mean_reversion.py` | **PASS** | Only consistent performer across walk-forward |
| Killzone Momentum | `killzone_momentum.py` | **PASS** | TTC signal engine in live paper |
| Momentum | `momentum.py` | **PASS** | TTC signal engine in live paper |
| BB Reversion GBPUSD | `gbpusd_bb_reversion.py` | **PASS** | Lower priority |
| Volatility Squeeze | `volatility_squeeze.py` | **PASS** | Lower priority |
| Multi-Session M/W | (embedded in `session_range_mean_reversion.py`) | **PASS** | Q7: 68% WR multi vs 54% single |
| USDJPY D1 Trend | `usdjpy_d1_trend.py` | **EXPERIMENTAL** | Not in live paper blend |
| Multi-Timeframe Filtered Momentum | `mtf_filtered_momentum.py` | **EXPERIMENTAL** | Not in live paper blend |
| Session Range MR ICT Filtered | `session_range_mr_ict_filtered.py` | **EXPERIMENTAL** | Not in live paper blend |
| Grid Strategy | `grid/` | **ABANDONED** | No recent activity, tests exist but strategy flagged as legacy |

### 1.2 Walk-Forward Results Summary

**Hybrid ICT/SMC + Quantitative Overlay** ([AYUAA-221](/AYUAA/issues/AYUAA-221)):
- **EURUSD:** 1/5 windows passed — Mean WR 42.4%, Sharpe -1.05
- **GBPUSD:** 1/5 windows passed — Mean WR 42.4%, Sharpe -1.70
- **Result: NO-GO** — Does not meet FTMO criteria (55% WR, >0.5 Sharpe, 3+ profitable windows)

**QA Gate Evaluation** (April 7, 2026):

| Strategy | Pair | Windows Passed | Mean WR | Result |
|----------|------|----------------|---------|--------|
| Regime Router | EURUSD | 1/5 | 43.0% | NO-GO |
| Regime Router | GBPUSD | 4/5 | 76.2% | GO (data concerns) |
| Keltner Channel | EURUSD | 1/5 | 38.3% | NO-GO |
| Keltner Channel | GBPUSD | 0/5 | 37.4% | NO-GO |
| Session Range MR | EURUSD | 2/5 | 54.0% | GO (borderline) |
| Session Range MR | GBPUSD | 5/5 | 59.1% | GO (strong) |
| Supertrend RSI | EURUSD | 0/5 | 20.0% | NO-GO |
| Supertrend RSI | GBPUSD | 2/5 | 40.0% | GO (suspect data) |

### 1.3 Q1-Q7 Backtest Synthesis Findings

From `docs/forex/tbd_backtest_synthesis.md`:

| Test | Finding | Decision |
|------|---------|----------|
| Q1: M/W 3:1 R:R | 1.04% hit rate | **REJECT** |
| Q2: LOD/HOD stop rate | 71% intrabar spikes | **SYSTEMIC** — 8-pip buffer required |
| Q3: Wednesday reversal | 22% reversal rate | **REJECT** |
| Q4: Friday gap | 12% freq, 91% level test | **DEFER** |
| Q5: Consolidation >=4hr | 94% breakout rate | **IMPLEMENT** |
| Q6: DXY leading/lagging | 51% lead, no edge | **REJECT** |
| Q7: Multi-session M/W | 68% WR vs 54% single | **IMPLEMENT** |

---

## 2. Testing Methodology Assessment

### 2.1 Backtest Infrastructure — STRENGTHS

- **Walk-forward validator** (`quant/walk_forward.py`): 5-window split with train/val/test, proper OOS evaluation
- **Multi-strategy engine** (`backtest/multi_strategy_engine.py`): Supports blending multiple strategies
- **Data loader** (`backtest/data_loader.py`): CSV loading with Eastern timezone handling
- **1969 tests passing** in main test suite — extensive coverage
- **Parameter sweep** infrastructure (`backtest/parameter_sweep/`): Optuna integration for optimization

### 2.2 Backtest Infrastructure — GAPS & RELIABILITY CONCERNS

1. **"Infinity" Profit Factor Bug** — Multiple strategies show "Infinity" profit factors, indicating division-by-zero when zero losing trades occur. Compromises validity of some GO decisions. Source: `qa-gate-evaluation-analysis-april2026.md`

2. **Look-Ahead Bias Risk** — Walk-forward runner uses `strategy.train(train_bars)` but need to verify the strategy doesn't peek at OOS data during training. Not fully audited.

3. **Survivorship Bias** — Not clear if historical data includes delisted symbols or only currently-tradeable instruments. Affects long-term backtest validity.

4. **94% Signal Rejection Rate** — Hybrid strategy quantitative filters reject 94% of signals (1,473/1,612 EURUSD, 1,447/1,554 GBPUSD). This is extremely conservative and may indicate overfitting to training data.

5. **Low Trade Counts** — Regime Router averages 6.0-6.2 trades/window, Supertrend RSI 0.8-3.0. Statistical significance compromised. No minimum trade threshold guardrail exists.

6. **Test Discovery Issue** — The `tests/` subdirectory uses `forex_trading` module imports that are not resolvable from the project root. The actual test suite that runs is `tests/test_*.py` at project root level (1969 passing). The nested `tests/integration/` and `tests/unit/` are non-functional.

### 2.3 Testing Path to FTMO Readiness

Current state vs FTMO requirements:

| FTMO Criterion | Current Status | Gap |
|---------------|----------------|-----|
| >55% Win Rate | SRM GBPUSD: 59.1% ✅ | Near threshold |
| >1.5 Profit Factor | Mixed — SRM GBPUSD: 2.4 ✅ | SRM EURUSD borderline at 0.99 |
| <5% Max Drawdown | Generally met ✅ | |
| >0.5 Sharpe Ratio | Negative in most windows ❌ | Major gap |
| >100 OOS Trades | Generally met ✅ | |
| 3+ Profitable Windows | Only SRM GBPUSD (5/5) ✅ | Most strategies fail this |

**Key Finding:** Sharpe ratio is the most critical gap. Most strategies produce negative Sharpe, meaning returns don't compensate for volatility. This is a fundamental strategy viability concern, not just a parameter tuning issue.

---

## 3. cTrader Integration Readiness Assessment

### 3.1 FIX Protocol (`ctrader_fix/connection.py`)

- Implements FIX 4.4 protocol
- Requires env vars: `CTRADER_HOST`, `CTRADER_SSL_PORT`, `CTRADER_ACCOUNT`, `CTRADER_PASSWORD`, `CTRADER_SENDER_COMP_ID`, `CTRADER_TARGET_COMP_ID`, `CTRADER_SENDER_SUB_ID`
- Has credential validation and SSL support
- **Status:** Functional but requires live cTrader credentials

### 3.2 Adapter Layer (`adapters/ctrader/`)

| Component | File | Status |
|-----------|------|--------|
| API Client | `api_client.py` | 36KB — extensive |
| Market Data Feed | `market_data_feed.py` | Functional, WebSocket-based |
| Paper Trader | `paper_trader.py` | Functional |
| Risk Guard (FTMO) | `risk_guard.py` | 14KB — FTMO-specific rules |
| Order Manager | `order_manager.py` | 18KB — full order lifecycle |
| Signal Adapter | `signal_adapter.py` | Connects strategies to execution |
| Trade Logger | `trade_logger.py` | Logging layer |

### 3.3 Live Paper Trading (`run_live_paper.py`)

- 5-strategy blend running: SRM XAUUSD, SRM USDJPY, SRM GBPUSD, TTC XAUUSD, TTC EURUSD
- Connects to cTrader demo account
- FTMO risk guard active
- **Status:** Operational

### 3.4 cTrader Integration — Open Items

1. **No real-money execution** — Only demo account confirmed working
2. **FIX credentials not in repo** — Requires `.env` setup (correctly guarded)
3. **cTrader API access** — Per `docs/forex/ctrader-fix-protocol-research.md`, Open API v2/v3 is the target; actual API key provisioning is a board/administrative step, not a code issue

---

## 4. FTMO Readiness Gap Analysis

| Gap | Severity | Description |
|-----|----------|-------------|
| Sharpe Ratio negative | **CRITICAL** | Most strategies fail this — returns don't compensate for risk |
| Win Rate <55% | **HIGH** | Most strategies at 40-54%, below 55% threshold |
| 3+ Profitable Windows | **HIGH** | Only SRM GBPUSD meets this; most strategies at 0-2/5 |
| Data quality bugs | **HIGH** | Infinity profit factors compromise some GO decisions |
| Low trade counts | **MEDIUM** | 6-15 trades/window insufficient for statistical significance |
| Hybrid strategy rejection rate | **MEDIUM** | 94% signal rejection too conservative |

---

## 5. Recommendations

### 5.1 What Carries Forward

| Strategy | Rationale |
|---------|-----------|
| **Session Range Mean Reversion (SRM)** | Only strategy passing walk-forward consistently (GBPUSD 5/5, EURUSD 2/5) |
| **Multi-session M/W detector** | Q7: 68% WR vs 54% single-session — proven edge |
| **Consolidation >=4hr filter** | Q5: 94% breakout rate — implement as entry filter |
| **8-pip LOD/HOD stop buffer** | Q2: 71% intrabar spike — required for all strategies |
| **cTrader adapter infrastructure** | Full adapter layer working, paper trading operational |
| **Backtest + walk-forward framework** | Solid infrastructure, 1969 tests passing |

### 5.2 What Gets Rebuilt / Deprecated

| Strategy | Rationale |
|---------|-----------|
| **Hybrid ICT/SMC + Quant Overlay** | Fails all FTMO criteria; 94% rejection too conservative |
| **Regime Router EURUSD** | 1/5 windows, negative Sharpe |
| **Keltner Channel** | Both pairs 0-1/5 windows, WR <40% |
| **Supertrend RSI** | EURUSD 0/5, WR 20% — critical failure |
| **Grid Strategy** | No recent activity, marked legacy |
| **3:1 R:R target** | Q1: Only 1.04% hit rate; re-test at 2:1 or 1.5:1 |

### 5.3 Immediate Actions Required

1. **Fix data quality bugs** — Infinity profit factor in walk-forward runner (division by zero when zero losing trades)
2. **Add minimum trade threshold** — Require ≥15 trades/window for statistical significance
3. **Audit look-ahead bias** — Verify strategies don't peek at OOS data during train()
4. **Re-test Sharpe improvements** — Focus on strategies with positive Sharpe potential (SRM GBPUSD has 2.4 PF, 59% WR, 5/5 windows)

### 5.4 Research Next Steps

1. **Explore SRM parameter expansion** — SRM is the only consistent performer; extend parameter ranges
2. **Explore multi-session 3:1 R:R** — Q1 failed globally but Q7 multi-session preference suggests multi-session sub-population may support 3:1
3. **Regime Router EURUSD root cause** — Only 20% pass rate with 6.2 avg trades; investigate regime detection misclassification

---

## 6. File Locations Referenced

- Strategies: `src/forex_bot/strategies/`
- Backtest engine: `src/forex_bot/backtest/engine.py`, `multi_strategy_engine.py`
- Walk-forward: `src/forex_bot/quant/walk_forward.py`, `backtest/walk_forward_runner.py`
- cTrader adapter: `src/forex_bot/adapters/ctrader/`
- FIX protocol: `src/forex_bot/ctrader_fix/connection.py`
- Live paper: `src/forex_bot/run_live_paper.py`
- Walk-forward eval: `docs/forex/hybrid-strategy-walk-forward-evaluation.md`
- QA gate analysis: `docs/forex/qa-gate-evaluation-analysis-april2026.md`
- Q1-Q7 synthesis: `docs/forex/tbd_backtest_synthesis.md`
