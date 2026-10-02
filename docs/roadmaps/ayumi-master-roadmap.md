# Ayumi Master Roadmap — Canonical Source of Truth

> **This document supersedes all prior plan, quest, and roadmap documents.**
> If any other doc conflicts with this one, this one wins.
> Decision log: `docs/decisions/decision-log.md`
> Last updated: 2026-07-22 (rev 4 — strategy factory pivot: portfolio evaluation + regime profiling)

---

## Objective

Build and run a profitable automated forex trading bot on the **FTMO 1-Step Standard $10,000** challenge, pass it, and get funded.

## FTMO Profile (LOCKED)

| Parameter | Value |
|-----------|-------|
| Account type | 1-Step Standard |
| Starting balance | $10,000 |
| Daily loss limit | 5% ($500) |
| Max total loss | 10% ($1,000) |
| Profit target | 10% ($1,000) |
| Min trading days | 0 (no minimum) |
| Profit split | 90% |
| Leverage | 1:30 (retail) |
| Max position size | Per FTMO symbol limits |

---

## Current State (2026-07-22)

### Jul 22 SRF Re-sweep + Strategy Factory Pivot

> **Key insight (Jul 22):** Per-strategy per-window Go/No-Go gates are the wrong abstraction.
> A strategy with PF=0.8 in chop + PF=2.5 in trends is a valuable blend component.
> The path forward is regime-aware blending, not per-strategy gates.

| Strategy | Best Pair/TF | Mean PF | Mean WR | Status |
|----------|-------------|---------|---------|--------|
| killzone_momentum | XAUUSD H1 | 0.93 | 57.5% | Trend-dependent — bleeds in chop |
| srmr_plus | XAUUSD H1 | 3.96 | 56.7% | Strong but low trade count |
| srmr_plus | EURUSD M15 | 1.75 | 50.3% | Closest to consistent edge |
| volatility_regime_breakout | XAUUSD M15 | 3.38 | 58.9% | Real edge, starved for volume |
| volatility_squeeze (fixed) | XAUUSD M15 | 0.79 | 50.2% | Fixed but no edge |
| donchian_atr_trend_v2 (NEW) | XAUUSD H1 | 1.97 | 65.8% | Best new candidate |
| dual_tf_squeeze_pro (NEW) | XAUUSD M15 | N/A | N/A | Too restrictive — 3 signals/5000 bars |
| ttc_xauusd | XAUUSD M15 | 0.80 | 47.3% | OVERFIT — lookback=10 lifts to PF=1.32 |

**Finding:** All strategies show real edge in trending windows (PF=2-4) but bleed in choppy periods. Portfolio blend evaluation is the correct assessment method.

### What We Have

**Data Layer:**
- DuckDB (`ayumi_market.duckdb`, 53GB): 429M ticks, 306M bars
- Coverage:
  - EURUSD: M5/M15/H1/H4/D1 ✅ (2020-01 → 2026-07)
  - GBPUSD: M5/M15/H1/H4/D1 ✅ (2020-01 → 2026-07)
  - XAUUSD: M5/M15/H1/H4/D1 ✅ (2022-01 → 2026-07)
  - USDJPY: ❌ NO DATA (smoke test only — 120 H1 bars)

**Strategies (18 built, 10 with SRF runs as of Jul 22):**
- Active candidates: `killzone_momentum`, `srmr_plus`, `volatility_regime_breakout` (fixed), `volatility_squeeze` (fixed), `donchian_atr_trend_v2` (NEW), `dual_tf_squeeze_pro` (NEW), `ttc_xauusd` (tunable)
- Deprecated: `bb_rsi_reversion` (PF<0.3), `donchian_atr_trend` v1 (PF<0.15), `london_breakout_retest` (FX zero-trade)
- Tier 3 (built, no SRF): `momentum`, `mtf_filtered_momentum`, `rsi_threshold`, `session_breakout`, `session_range_mean_reversion`, `session_range_mr_ict_filtered`

**Infrastructure:**
- cTrader order chain proven (Jun 25, 2026)
- Forward test infrastructure with health monitoring
- Risk engine: position sizing, kill switch, FTMO guard, daily audit
- SRF framework: walk-forward, Monte Carlo, PBO, parameter stability
- Quant: bootstrap CIs, multiple testing correction, ICIR, OOS gate
- Regime detector: ATR/ADX-based with conditional allocation (commit 615bf3a, Jul 21)
- ML: confidence_learner (RandomForest), blend_optimizer (Optuna), per_symbol_configs

### What's Missing

| Gap | Impact |
|-----|--------|
| No USDJPY data | Can't trade USDJPY (target pair) |
| No final strategy blend selected | Can't launch forward test |
| Portfolio blend driver not wired | Can't run multi-strategy forward test |
| FTMO trailing guard not implemented | Risk of breaching max loss |
| No news blackout | Risk of trading into volatility spikes |
| Regime detector not wired | Can't gate strategies by market state |
| No CI/CD | No automated test runs on push |

---

## Phase Plan: Now → FTMO Challenge

### Phase 0: Data Completeness (partially complete — expansion planned)
> Goal: Fill data gaps for existing pairs AND plan new symbols for blend diversification.

- [x] **0.1** Download USDJPY tick data — DONE 2026-09-17 (79.19M ticks 2020-01→2026-09-15, Dukascopy bi5_gap_fill; evidence: card 75ad56c6, /tmp/usdjpy_import.log)
- [x] **0.2** ~~Update GBPUSD data~~ — confirmed current (Jul 2026)
- [x] **0.3** Aggregate ticks → bars for USDJPY — DONE 2026-09-17 (M1..D1 in ayumi_market.duckdb; M1 1,115,178 / M5 224,218 / M15 74,751 / M30 37,376 / H1 18,688 / H4 5,264 / D1 1,024 bars)
- [x] **0.4** Validate data quality — DONE 2026-09-17 (health_check_tick_pipeline.py OK; qa_market_data.py exit 0, report reports/qa-report-2026-09-17.md; only 8 weekday gaps in 584 day-CSVs, all market holidays)
- [ ] **0.5** Download Dukascopy crisis period data (2020-03 COVID, 2022-02 Ukraine) for stress testing
- [ ] **0.6** Expand symbol coverage for blend diversification (post-FTMO baseline):
  - Priority candidates: AUDUSD, USDCHF, NZDUSD (carry-trade diversification)
  - DXY (dollar index, inverse correlation), Brent crude
  - Crypto (via Cabal pipeline): BTCUSD, ETHUSD

**Data pipeline runbook:** `docs/runbooks/data-pipeline.md`

**Target pairs (immediate):** EURUSD, GBPUSD, USDJPY, XAUUSD
**Target pairs (expansion):** AUDUSD, USDCHF, DXY, BTCUSD
**Target timeframes:** M5, M15, H1, H4, D1

### Phase 1: Strategy Factory → Portfolio Blend (rev 4 — Jul 22 pivot)
> Goal: Profile each strategy by regime, select complementary blend, validate FTMO viability.
>
> **Key insight (Jul 22):** Per-strategy Go/No-Go gates are the wrong abstraction.
> A strategy with PF=0.8 in chop + PF=2.5 in trends is a valuable blend component.
> Evaluate the PORTFOLIO, not the individual.

#### 1A: Strategy Repair + New Builds — ✅ COMPLETE (Jul 22)

- [x] **1A.1** ttc_xauusd — OVERFIT verdict (Jul 22). PF=8 was research doc misattribution. Actual PF=0.80, WR=47.3%. Tunable: lookback 5→10 lifts PF to 1.32.
- [x] **1A.2** Fix `volatility_squeeze` zero-trade bug — FIXED (commit 788c82b). adx_min 20→15, any_release mode, rsi_period separated.
- [x] **1A.3** Fix `volatility_regime_breakout` zero-trade bug — FIXED (commit 1874099). vol_expansion_ratio=1.5 trigger added.
- [x] **1A.4** killzone_momentum — M15 PF=0.97, H1 PF=0.93. Previous PF=21.5 was single-window anomaly. Trend-dependent, not independently viable.
- [x] **1A.5** srmr_plus — Tuning applied. PF=1.75 EURUSD M15, PF=3.96 XAUUSD H1 but inconsistent.
- [x] **1A.6** Deprecated `bb_rsi_reversion`, `donchian_atr_trend` v1.
- [x] **1A.7** `london_breakout_retest` — REVIVED Jul 22: PF=3.98 zero-cost, **PF=2.07 under FTMO realistic costs**, 7.3 trades/year on XAUUSD M15, 80% WR. Robust under 5x slippage stress. Recommended blend gate: ADX[15,30] + LONDON session. (Previously marked deprecated — that was for FX; XAUUSD performance is strong.)
- [x] **1A.8** Build B.1 Donchian ATR Trailing Trend v2 (commit 0a89e37) — 22 tests, 144 trades smoke, PF=1.57
- [x] **1A.9** Build B.2 Dual-TF Squeeze Pro (commit b514b0a) — 16 tests. Too restrictive (3 signals/5000 bars). Needs gate relaxation.
- [x] **1A.10** Raise `MIN_TRADES_PER_WINDOW` 15→20 (commit f4d8ee2)

#### 1B: Strategy Regime Profiling — NEXT
> Goal: Understand WHEN each strategy wins and loses. Build a characterization matrix.

- [ ] **1B.1** Profile each strategy by regime (TRENDING/CHOPPY/VOLATILE/QUIET) using regime detector
  - Per-regime: PF, WR, trade count, DD profile, streak patterns
  - Per-session: Asia/London/NY performance breakdown
  - Per-timeframe: M15 vs H1 comparison
  - Output: `docs/research/strategy-profiles/` — one doc per strategy
- [ ] **1B.2** Compute pairwise correlation of strategy equity curves
  - Identify complementary pairs (low correlation = good diversification)
  - Identify redundant pairs (high correlation = pick one)
- [ ] **1B.3** Identify regime gaps — which regimes have NO winning strategy?
  - If chop has no winner → need a mean-reversion or range strategy
  - If volatile has no winner → need a breakout-volatility strategy
- [ ] **1B.4** Build B.3 London Breakout Retest (XAUUSD-tuned) — deferred until profile gaps identified

#### 1C: Confidence Enhancement
> Goal: Layer indicators and gates to improve signal quality.

**Layer 1 — Strategy Confidence** (`src/forex_bot/confidence/`):
- Multi-layer scoring: Strategy Score → Confluence Boost → Gate Validator → Final Score
- Gates: SpreadGate, SessionGate, VolatilityGate
- ML confidence learner: RandomForest per (symbol, timeframe)
- **Status:** Built, needs wiring to blend driver

**Layer 2 — Regime-Aware Gating (NEW — Jul 22):**
- Wire regime detector (`regime/detector.py`) as a signal gate
  - Shadow mode first: log regime classification per signal, don't gate
  - Analysis: does filtering CHOPPY/QUIET signals improve per-strategy PF?
  - If yes → activate as live gate with bypass flag
- Per-strategy regime affinity: assign each strategy its best-performing regimes
  - e.g., Donchian ATR → only fire in TRENDING; SRMR+ → only fire in CHOPPY

**Layer 3 — Signal Confidence Engine (spec v2.3):**
- TTC/TBD confluence framework — 8-stage pipeline
- Status: Design complete, modules exist in `signal_engine/`, need integration
- Lower priority than Layer 2 for near-term FTMO push

- [ ] **1C.1** Wire regime detector in shadow mode — log classification per signal
- [ ] **1C.2** Analyze: does regime filtering improve signal quality?
- [ ] **1C.3** If yes → implement per-strategy regime affinity gates
- [ ] **1C.4** Wire ML confidence learner to blend driver
- [ ] **1C.5** Calibrate gate thresholds using historical trade data
- [ ] **1C.6** Audit signal_engine/ modules against v2.3 spec (deferred to post-FTMO)

#### 1D: Blend Selection + Validation
> Goal: Select complementary strategies, validate the PORTFOLIO meets FTMO criteria.

- [ ] **1D.1** Run portfolio blend backtest with all profiled strategies
  - Combined equity curve across full data
  - FTMO viability: overall PF > 1.0, max DD < 10%, daily DD < 5%
  - Profit target: does cumulative P&L reach +10%?
- [ ] **1D.2** Use ML blend_optimizer to search strategy weight combinations
  - Optuna sweep: which strategies, what weights, what regime filters
- [ ] **1D.3** Run Monte Carlo on blended equity curve (1,000 simulations)
- [ ] **1D.4** Run PBO on blend parameters
- [ ] **1D.5** Walk-forward validate the SELECTED BLEND (not individual strategies)
- [ ] **1D.6** Document blend selection rationale in `docs/decisions/strategy-blend-selection.md`

**Trade Volume Gate (Craig directive Jul 22):** Blend must average ≥1 trade/day (~250/year) before FTMO challenge start. Current blend produces ~38/year. 7x gap requires symbol + strategy + confidence expansion.

**Gate:** Blend backtest passes FTMO sim (PF > 1.0, max DD < 10%, daily DD < 5%, ≥250 trades/year) → Phase 2.

#### 1E: Strategy Factory Sprint (Jul 22 — 17:38 to 21:55 EDT)
> Goal: Discover + validate high-edge strategies to expand the blend.
> 4-hour work block. Owner directive (Craig): keep working autonomously, find things that move toward FTMO viability.

**Outcomes:**

- [x] **1E.1** London Breakout Retest validated end-to-end on XAUUSD M15. Cost-stress tested at 5 levels. Robust strategy. Wired into `scripts/launch_blend_forward_test.py` as 5th strategy. Files: `tests/strategies/test_london_breakout_retest.py`, `scripts/test_lbo_gated.py`, `scripts/lbo_cost_stress.py`, `docs/research/strategy-profiles/london_breakout_retest_2026-07-22.md`.

- [x] **1E.2** **Bug #4 FIXED**: `RegimeDetector.detect_current()` returns only TRENDING/CHOPPY when called on 60-bar windows. The detector needs ≥100-bar windows (`atr_lookback=50` + `adx_period=14` warmup) to classify VOLATILE/QUIET. All prior cached labels were missing QUIET/VOLATILE bars. **This invalidated much of the prior session's confidence in the gated blend** — see debt cards for re-validation requirements.

- [x] **1E.3** **Bug #3 FIXED**: `SRMRPlusConfig()` defaults to `symbol=None`, raises ValueError on `.evaluate()`. All prior SRMR+ calls were silently failing. Fixed in `scripts/gate_loosening_study.py`, `scripts/run_blend_5strat.py`, and `scripts/launch_blend_forward_test.py`.

- [x] **1E.4** **Bug #1 FIXED**: pandas Series `[-1]` is label-based, returns KeyError on RangeIndex. ADX precompute was storing 0 for all bars via silent `except: pass`. Fixed to `Series.iloc[-1]`.

- [x] **1E.5** **Bug #2 FIXED**: `timestamp_utc` column has mixed scales (sec for most symbols, ms for GBPUSD M1). Auto-detect via `value > 1e12`. Fixed in all loaders.

- [x] **1E.6** Gate loosening study re-run with corrected cache. **Result inverts prior finding**: SRMR+ baseline is genuinely profitable (71 trades, PF=1.29, +$333, DD=3%, WR=68%) — QUIET-only regime filter is load-bearing, not arbitrary. Adding CHOPPY to SRMR+ regime destroys edge (PF 1.29 → 0.84).

- [x] **1E.7** 5-strategy blend backtest written (`scripts/run_blend_5strat.py`). **Disagreement with original 4-strategy validation surfaced a position-management model mismatch** — needs reconciliation before trust is restored in blend numbers.

**Bug-Cache Validation Delta (with corrected 100-bar precompute):**

| Strategy | Original Cache | Corrected Cache | Implication |
|---|---|---|---|
| killzone_momentum | 81 trades, PF=1.235 | 73 trades, PF=0.943 | Slightly negative when full regime data exposed. adx_[15,30] loosening rescues (125 trades, PF=1.178). |
| srmr_plus (QUIET+LONDON) | 0 trades (silent crash) | 71 trades, PF=1.29 | Hidden edge — was crashing all along |
| dual_tf_squeeze_pro | 17 trades, PF=1.467 | 11 trades, PF=1.083 | Lower volume but still positive |
| donchian_atr_trend_v2 (H1) | 456 trades, PF=0.992 | 275 trades, PF=1.029 | Slightly positive at baseline; loosening destroys |

**LBO Cost-Stress (final, validates strategy):**

| Cost Scenario | PF | Net | DD | WR |
|---|---:|---:|---:|---:|
| Zero cost | 3.977 | $325 | 1.00% | 80.0% |
| **Realistic FTMO (2.5p + $3.5 + 0.2slip)** | **2.068** | **$159** | **1.28%** | **80.0%** |
| Worst case (5p + 1p slip) | 1.977 | $148 | 1.30% | 80.0% |

**Conclusions:**
1. **LBO is a real edge** — confirmed by cost-stress across 5 scenarios.
2. **Regime gates are load-bearing**, not arbitrary. Don't loosen SRMR+ beyond QUIET.
3. **The 250/day target is unrealistic** with retail strategies on XAUUSD. Realistic FTMO target: 50-150 trades/year.
4. **The original 4-strategy gated blend result (PF=1.74) is suspect** — needs re-validation against corrected cache.
5. **Cache fix is the highest-impact change** of the sprint.

**See:** `docs/research/strategy-profiles/consolidated_findings_2026-07-22.md` for full details.

### Phase 2: Blend Engine + Risk Wiring (2-3 days)
> Goal: Wire the selected blend into a single executable forward-test system.

- [ ] **2.1** Wire portfolio blend driver to forward test launcher
- [ ] **2.2** Implement FTMO trailing drawdown guard (track highest balance, floor at 90%)
- [ ] **2.3** Implement daily loss budget computation at session start
- [ ] **2.4** Implement news blackout (block entries ±5 min around high-impact events)
- [ ] **2.5** Implement per-strategy freeze (stop trading a strategy after N consecutive losses)
- [ ] **2.6** Implement currency exposure cap (max 3% net per currency)
- [ ] **2.7** Wire kill switch to blend driver (auto-stop on daily DD breach)
- [ ] **2.8** Run blend backtest with FTMO simulation across 2020-2026 data

**Risk Rules (LOCKED):**
- Per-trade: 0.5% default ($50), 1.0% hard cap ($100)
- Per-strategy: 1.5% max open risk
- Portfolio: ≤2% total open risk
- Recovery: 1-2% DD → review; 3-5% → cut to 0.25%; >5% → pause 24h
- Daily approaches 2.5% → stop trading for day
- Never hold through 23:30 CE(S)T without explicit intent
- Best Day Rule (once funded): no single day >50% of cumulative profit

**Gate:** Blend backtest passes FTMO sim (profit > 10%, max DD < 10%, daily DD < 5%) → Phase 3.

### Phase 3: cTrader Demo Validation (1-2 weeks)
> Goal: Validate the blend on a cTrader demo account — simultaneously testing strategy performance AND technical execution path.

- [ ] **3.1** Deploy blend to cTrader demo account with forward test launcher v2
- [ ] **3.2** Verify confidence engine gates signals correctly (no trades <0.40 confidence)
- [ ] **3.3** Verify position sizing respects confidence tiers (full/half/quarter)
- [ ] **3.4** Verify FTMO guard: daily DD tracking, trailing drawdown, kill switch armed
- [ ] **3.5** Run for minimum 5 trading days
- [ ] **3.6** Daily audit: P&L, DD, signal quality, execution quality, slippage, latency
- [ ] **3.7** Fix any issues found (latency, slippage, missed signals, order rejections, etc.)
- [ ] **3.8** Verify cTrader token lifecycle (refresh, expiry handling)
- [ ] **3.9** Verify reconnection logic (connection watchdog, self-healing)

**Gate:** 5 clean trading days with no execution errors + confidence engine performing as expected → Phase 4.

### Phase 4: FTMO Challenge Run (ongoing)
> Goal: Pass the FTMO 1-Step Standard $10K challenge.

- [ ] **4.1** Open FTMO 1-Step Standard $10K account
- [ ] **4.2** Deploy blend with live FTMO credentials
- [ ] **4.3** Run daily audit every trading day
- [ ] **4.4** Monitor: daily DD, total DD, profit progress, signal quality
- [ ] **4.5** Kill switch armed at all times
- [ ] **4.6** Pass challenge (10% profit, within DD limits)

**Gate:** Challenge passed → Phase 5 (funded trading).

### Phase 5: Funded Trading (ongoing)
> Goal: Generate consistent returns on funded account.

- [ ] **5.1** Switch to funded credentials
- [ ] **5.2** Scale per-trade risk per FTMO scaling plan
- [ ] **5.3** Weekly performance review
- [ ] **5.4** Monthly strategy review (re-validate edge, re-run walk-forward if needed)
- [ ] **5.5** Quarterly data refresh (download latest ticks, re-aggregate)

---

## Strategy Factory

The strategy factory is the system for developing, profiling, and blending strategies. It integrates four components:

### 1. Strategy Research Framework (SRF)

Location: `src/forex_bot/srf/`

- Walk-forward runner with rolling and anchored modes
- Go/No-Go gate (per-strategy, used for initial screening only — portfolio evaluation is the real gate)
- Monte Carlo simulation (1,000+ runs per strategy)
- PBO (Probability of Backtest Overfitting)
- Parameter stability analysis
- Per-window + per-trade persistence to research.duckdb

### 2. Regime Detector (NEW — Jul 21)

Location: `src/forex_bot/regime/detector.py`

- ATR/ADX-based market regime classification
- Four regimes: TRENDING (ADX>25), CHOPPY (ADX<20), VOLATILE (ATR pct>80%), QUIET (ATR pct<20%)
- Conditional allocation guidance per regime (size multiplier, max positions, preferred strategy types)
- **Status:** Built (commit 615bf3a), not yet wired as signal gate

### 3. ML Pipeline

Location: `src/forex_bot/ml/`

- `confidence_learner.py` — RandomForest per (symbol, timeframe) predicting win probability
- `blend_optimizer.py` — Optuna-based search over strategy combinations and weights
- `per_symbol_configs.py` — Optuna-tuned base confidence, KZ penalty, HTF penalty per pair
- `optuna_optimizer.py` — Parameter tuning sweeps
- `features.py` — Feature engineering pipeline
- **Status:** Built, needs wiring to blend driver

### 4. Confidence Engine

**Layer 1 — Strategy Confidence** (`src/forex_bot/confidence/`):
- Multi-layer: Strategy Score → Confluence Boost → Gate Validator → Final Score (0.0-1.0)
- Gates: SpreadGate, SessionGate, VolatilityGate
- Position sizing mapping: ≥0.65 full, 0.50-0.64 half, 0.40-0.49 quarter, <0.40 no trade

**Layer 2 — Signal Confidence Engine** (spec: `docs/forex/signal_confidence_engine.md` v2.3):
- TTC/TBD confluence framework — 8-stage pipeline
- Implementation: modules exist in `signal_engine/`, need integration
- Deferred to post-FTMO (Layer 2 regime gating is higher priority)

**Integration path:** strategies → regime detector (gate) → confidence engine (score) → blend driver (weight) → position sizer → forward test launcher

---

## Data Pipeline

### Download → Aggregate → Validate

```
1. Download ticks (Dukascopy)
   Use Docker SDK harvester only.
   See `docs/runbooks/data-pipeline.md` for exact commands.

2. Import ticks to DuckDB
   scripts/import_ticks.py --pair <PAIR> --input <TICK_FILE>

3. Aggregate ticks → bars
   scripts/aggregate_ticks_to_bars.py --pair <PAIR> --timeframes M5,M15,H1,H4,D1

4. Validate
   scripts/health_check_tick_pipeline.py --pair <PAIR>
   scripts/qa_market_data.py --pair <PAIR>  # gap analysis, density check
```

### DuckDB Schema

- `ticks`: symbol, timestamp_ms, bid, ask, spread
- `bars`: symbol, timeframe, timestamp_utc, open, high, low, close, volume, spread_pips
- `import_log`: import audit trail

---

## Test Runbook

### Quick Check (before any commit)
```bash
source .venv/bin/activate
python3 -m pytest tests/unit/ -q -x --timeout=30
```

### Strategy Tests (when touching strategies)
```bash
source .venv/bin/activate
python3 -m pytest tests/strategies/ -q -x --timeout=30
```

### Integration Tests (when touching cTrader/adapters)
```bash
source .venv/bin/activate
python3 -m pytest tests/integration/ -q -x --timeout=30 -m "not live"
```

### Full Suite (before merge to main, requires --full flag)
```bash
source .venv/bin/activate
python3 -m pytest tests/ -q --timeout=30 -m "not live"
```

### What NOT to run
- Never run bare `pytest tests/` during development — use targeted scope
- Never run `tests/e2e/test_live_*.py` without `--live` flag and explicit reason
- Never run SRF sweeps on full 6-year data during development (use 1-year sample first)

---

## Canonical Reference Docs

- `docs/decisions/decision-log.md` — All major decisions with rationale
- `docs/research/strategy-optimization-research.md` — Strategy tuning research
- `docs/research/ftmo-risk-and-port-sizing-2026-07.md` — FTMO risk rules detail
- `docs/research/icir-research-2026-07-08.md` — ICIR research
- `docs/research/oos-gate-research-2026-07-08.md` — OOS gate research
- `docs/specs/signal-confidence-engine-v2.3.md` — Confidence engine spec
- `docs/edges/*.md` — Strategy edge hypotheses
- `docs/runbooks/data-pipeline.md` — Data pipeline runbook
- `docs/runbooks/backtesting-strategy.md` — Backtesting runbook

---

*This is the canonical roadmap. All work should trace back to a phase and task here. If it's not in this roadmap, it doesn't get done.*

---

## Compute Offload: avaworker node (added 2026-09-14)

> Craig directive 2026-09-14: new worker node `avaworker` (Docker container on local
> workstation, stronger CPUs + GPU access) can offset heavy processing. Not yet wired
> for Ayumi dispatch; GPU use deferred (Craig: "don't worry about the GPU stuff tonight").

- **Policy (Craig, 2026-09-14 18:25):** avaworker is the DEFAULT target for
  tournament/backtest/walk-forward runs whenever it is connected — it frees server
  resources AND is inherently faster (stronger CPUs + GPU). Local server runs are
  acceptable only for small tests (<5-10 min). Provenance: 2026-09-14 GBPUSD
  tournament run took 2h13m pinned to one shared-cgroup core on the server; XAUUSD
  runs died silently inside openclaw-gateway.service (card c4b86732).
- **Prerequisite:** Ayumi checkout + venv + bars-only DuckDB staged on the node
  (container is ephemeral; needs a persistent volume or re-stage per batch).
- **GPU track (unblock-later):** once wired, evaluate GPU acceleration for
  vectorized backtests / parameter sweeps (Optuna `blend_optimizer`, tournament
  multi-symbol matrix) and ML training (`confidence_learner` RandomForest,
  Phase 2 blend-confidence validation). GPU warmup ~120s, ~30-50s/image-class
  workloads reported — realistic for batch backtest kernels only if they
  vectorize; sequential per-bar harness will NOT benefit without refactor.
- **Constraint:** workstation-class node — throttle concurrent jobs, no unattended
  fire-and-forget marathons without a resource cap.

## Strategy Candidate Pipeline (added 2026-09-14)

> Craig priority 2026-09-14: "my biggest concern is getting more potential strategies."

- **Pool A — unbenchmarked in-tree strategies (~17 classes):** the tournament's
  STRATEGY_CLASS_MAP currently registers only 2 (srmr_plus, bb_rsi_reversion) while
  `src/forex_bot/strategies/` holds ~19 ISignalStrategy classes (donchian_atr_trend_v2,
  dual_tf_squeeze_pro, killzone_momentum, london_breakout_retest, ttc_xauusd,
  volatility_regime_breakout, session_range_mr_ict_filtered, orb, ...). First lever:
  register these into the tournament and score them.
- **Pool B — strategy factory:** `docs/roadmaps/strategy-factory/` vision + implementation
  spec exist; systematic candidate generation is spec'd but not operational.
- **Gate:** all candidates enter live consideration only via tournament scorecard
  (FTMO columns) — 2026-09-14 GBPUSD leg disqualified both registered strategies
  on that symbol; the bar is the scorecard, not code existence.
