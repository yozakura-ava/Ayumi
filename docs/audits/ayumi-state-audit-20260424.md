# Ayumi Codebase State Audit — 2026-04-24

## Summary

The codebase is in **reasonable shape** but has a critical split between two forward-test architectures, a 90-day backtest that's net negative, and the forward test hasn't been verified working since the position spam fix on Apr 21.

---

## 1. Codebase Structure

```
src/forex_bot/
├── adapters/ctrader/     # cTrader API client, paper trader, risk guard, forward test engine
├── backtest/             # Walk-forward engine, multi-strategy, parameter sweep, trade management
├── cbot/                 # cBot (C#) scripts — legacy
├── config/               # Configuration
├── core/                 # pip/protocol/spread types
├── ctrader_fix/          # FIX protocol connection (low-level)
├── data/                 # Data files
├── engine/               # Engine module
├── hybrid/               # HybridEngine v2 — signal types, risk manager, session filtering, paper trader
├── indicators/           # Empty (just __init__.py)
├── logs/                 # Runtime logs
├── ml/                   # Feature engineering, Optuna optimizer, per-symbol configs, confidence learner
├── quant/                # GO/NO-GO criteria, walk-forward, regime detection, portfolio, VAPS
├── signal_engine/        # TTC signal engine (swing detector, level counter, HTF analyzer, session logic)
├── storage/              # Storage layer
├── strategies/           # 12 strategy implementations (SRM+, volatility squeeze, momentum, etc.)
├── run_live_paper.py     # Live paper trading — Blend One (5 strategies on M15)
├── run_srmr_plus_forward.py  # SRMR+ H1 forward test runner
└── signal_validator.py   # Signal validation
```

**2615 tests pass, 1 fails, 65 skipped** (109s runtime). The single failure is `test_reset_daily_clears_rules_engine_daily_bucket` in the hybrid paper trader — minor.

---

## 2. Git Status

- **Latest commit:** `91c78bf` — fix(tests): move non-London test timestamps outside London session hours
- **Active branch:** `main` (+ `forex-manager/AYUAA-774-syntax-fix` checked out in worktree)
- **Recent work (last 20 commits):** Hybrid engine framework (AYU-111, AYU-126, AYU-129), London risk restrictions, GO/NO-GO migration, parquet spread modeling, portfolio backtest
- **No commits from Kai tagged AYUAA-805 or AYUAA-806 exist.** These tickets may have been assigned on Paperclip but never completed or tracked under different IDs.

---

## 3. TTC Signal Engine Status

**Location:** `src/forex_bot/signal_engine/`

**Implemented (Phase 1):**
- SwingDetector — N-bar swing detection with equal swing merging
- LevelCounter — Rise/drop counting with R3/D3 magnitude validation
- HTFAnalyzer — Phase classification + MTF alignment scoring
- SessionAnalyzer — Session timing, kill zones, NY manipulation detection, Asia control
- BacktestBridge — Wire into backtest engine
- RiskSizer — Confidence-based position sizing

**Stubs (Phase 2+):**
- PatternDetector (M/W 11-point, SVC, traps, Asia liquidity grab)
- GateValidator
- ConfluenceScorer
- StopTargetCalculator

**Backtest performance (Optuna TTC walk-forward, Apr 12):**
| Pair | Win Rate | Profit Factor | Max DD | Trades | FTMO Pass |
|------|----------|---------------|--------|--------|-----------|
| EURUSD | 53.1% | 1.30 | 1.0% | 31 | ✅ |
| USDJPY | 71.0% | 3.92 | 0.7% | 29 | ✅ |
| XAUUSD | 54.5% | 2.06 | 0.7% | 32 | ✅ |
| GBPJPY | 52.8% | 1.35 | 1.6% | 36 | ✅ |

**Wired into forward test?** Yes — `run_live_paper.py` includes TTC_XAUUSD and TTC_EURUSD. `forward_test_engine.py` also supports it. The TTC signal engine is instantiated via `ttc_strategy_factory` from `backtest/parameter_sweep/ttc_optimizer.py`.

---

## 4. Forward Test Status

**Two forward test architectures exist:**

### A. `run_live_paper.py` — "Blend One" (the main one)
- 5 strategies on M15: SRM XAUUSD, SRM USDJPY, SRM GBPUSD, TTC XAUUSD, TTC EURUSD
- Uses `adapters/ctrader/paper_trader.py` + `adapters/ctrader/market_data_feed.py`
- Has FTMO risk guard, position tracking, dedup
- **Last run status unknown** — no recent logs visible

### B. `run_srmr_plus_forward.py` — SRMR+ H1 only
- SRMR+ on GBPUSD, EURUSD, XAUUSD
- Uses `adapters/ctrader/forward_test_engine.py`
- More mature, has dedicated signal adapter

### Position Spam Fix (Apr 21, commit `4bc3216` by Kai)
Kai's fix addressed:
- ✅ Pre-populate bar buffer from historical CSV (fixes zero-signal cold start)
- ✅ Symbol-level dedup in PaperTrader (max 1 open position per symbol)
- ✅ Per-symbol 60s cooldown after opening
- ✅ Orphaned position detection on startup
- ✅ Weekend market hours bug
- ✅ Tick counter inflation on shared feed

**Blocker:** The fix was committed but **no evidence it's been tested end-to-end since**. The forward test needs to be run to confirm the spam fix works.

---

## 5. 90-Day Backtest Results (AYU-135)

| Metric | Value |
|--------|-------|
| Total Trades | 607 |
| Win Rate | 30.1% |
| Profit Factor | 0.815 |
| Total PnL | -$819.63 (-4.1%) |
| Max Drawdown | 9.59% |
| Sharpe | -1.30 |
| FTMO Compliant | ✅ (DD < 10%) |
| GO/NO-GO | **NO-GO** (win rate + PF fail, p=0.98) |

**This is a blended 90-day backtest that is net negative.** FTMO compliance passes on drawdown alone, but the GO/NO-GO framework says NO-GO because win rate and profit factor don't meet thresholds and the results aren't statistically significant.

---

## 6. ML Pipeline Status

`src/forex_bot/ml/` has 16 files:
- Feature engineering (`features.py`, `confluence_features.py`)
- Optuna optimizer with per-symbol configs
- Confidence learner
- Signal simulator
- Per-symbol optimizer + tier optimizer
- Training pipeline (`run_pipeline.py`, `train_model.py`)

Optuna study results exist in `reports/optuna_ttc/` (23 files) and `reports/optuna/`. The ML pipeline is active and producing results that feed into strategy parameter selection.

---

## 7. Kai's Deliverables

**AYUAA-805/806:** No commits found with these ticket IDs. Either:
- They were tracked on Paperclip but never completed
- They were completed under different commit messages
- They were superseded by AYUAA-807/808

**AYUAA-807/808 (completed by Kai, Apr 21):**
- AYUAA-807: Forward test fixes (cold start, position spam, zombie heartbeat)
- AYUAA-808: Wire TTC XAUUSD into forward test
- Commit `4bc3216` — substantial fix, looks solid

**Other Kai work visible:**
- `ef7c3e1` — Multi-strategy orchestrator + strategy executor + trade journal [KAI-WIP-UNREVIEWED]
- Multiple review fixes (ATR stops, session filter, CI format)

---

## 8. Config & Connectivity

- `.env` exists (772 bytes) — credentials present (not inspected)
- `.env.example` has slots for: FTMO credentials, cTrader FIX API (host, ports, account, password, sender/target comp IDs)
- cTrader adapter connects via FIX 4.4 protocol to `live-uk-eqx-01.p.c-trader.com`
- `ctrader_fix/connection.py` — low-level FIX connection with SSL support

---

## 9. Hybrid Engine v2 (New Architecture)

`src/forex_bot/hybrid/` is a **new forward-test framework** being built alongside the old one:
- Signal types, RiskManager, HybridEngine, PaperTrader, TradeRules
- Session filtering with London risk restrictions (AYU-129)
- CLI signal command (AYU-120)
- Framework bugfixes + max lot cap (AYU-126)

This is **not yet the default** — `run_live_paper.py` still uses the old `adapters/ctrader/paper_trader.py`. The hybrid engine appears to be the planned migration target.

---

## 10. Path to Working Forward Test

### Immediate Blockers
1. **No verified end-to-end test since Apr 21 fix** — need to run forward test and confirm signals are generated, positions aren't spamming, and risk guard works
2. **90-day backtest is NO-GO** — the current strategy blend is net negative. Running a forward test against it is testing a losing system
3. **Two competing architectures** — old (`adapters/ctrader/`) vs new (`hybrid/`). Work is split between them

### Recommended Path

**Option A: Verify & Run Existing System (1-2 days)**
1. Run `run_live_paper.py` with `--paper-only` for 24-48 hours
2. Confirm: signals firing, no position spam, risk guard working, logs clean
3. Accept that the 90-day backtest is NO-GO and use forward test as ground truth
4. If forward test is also negative → strategy work needed before scaling

**Option B: Migrate to Hybrid Engine (3-5 days)**
1. Finish hybrid engine migration (it's 80% there)
2. Wire TTC signal engine into hybrid engine's paper trader
3. Run GO/NO-GO on the hybrid engine's backtest results
4. Then go live with hybrid engine

**Option C: Fix Strategy First (5-10 days)**
1. Address the 30% win rate — that's the core problem
2. TTC walk-forward results look good individually but the blend is bad
3. Run per-strategy analysis to find which strategies are dragging the portfolio down
4. Either drop losers or rebalance weights

### Estimated Effort
- **Minimum viable forward test verification:** 4-8 hours (run, monitor, diagnose)
- **Full clean forward test with confidence:** 2-3 days (includes strategy triage)
- **Production-ready forward test with updated strategy:** 1-2 weeks

---

## Key Files to Read Next
- `src/forex_bot/run_live_paper.py` — full config and strategy wiring
- `src/forex_bot/adapters/ctrader/forward_test_engine.py` — the SRMR+ forward test path
- `reports/AYU-135_90day_results.json` — full 90-day trade log
- `src/forex_bot/hybrid/engine.py` — new architecture target
- `logs/` — any recent forward test logs
