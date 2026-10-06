# Strategy Factory — Continuous Pipeline

> **Principle:** One item at a time. Record results. Queue the next. Forever evolving.
> Each step produces a documented result that persists between sessions.
>
> **Execution model:** Continuous queue (not timeboxed sprints). One item in flight,
> results recorded, next item queued. Craig can reorder at any time.
>
> **Origin:** Merges Sprint v2 council feedback (Kaito/Rei/Mika/Liora Jul 22) with
> continuous pipeline model. Council mandates preserved as ground rules.

---

## Status (2026-10-05 23:45 — Wave 0 + Wave 2 merged)

- **Sprint A — Factory Phase 1 wiring: DONE & MERGED** (SFA-1/2/3, 2026-10-04/05).
- **Sprint C — Crypto-First Factory: COMPLETE (all 6 wave cards merged 2026-10-06).** Plan: `docs/plans/sprint-2026-10-05-crypto-first-factory.md`. Parent card `3ab0f857` (hard-linked children gated by design; live work flows through soft-linked twins).
  - ✅ **0.1 Tournament root fix** — merged `6499e0f2`: 17/17 strategies run, 11/17 score (6 zero-signal → debt card `7c8ffd17`)
  - ✅ **1a.1 Adaptive funding model** — merged `eadbb741`: 8h/1h Binance cadence, perp P&L, FX path untouched
  - ✅ **1b.1 Real-trial PBO** — merged `cf8a7a64`: [T,N_trials] CSCV matrix, cost_sensitivity renamed, schema v2
  - ✅ **1b.2 CPCV runner** — merged `206da069`: purge+embargo, φ=C(N,k) paths, DSR deflation, TrialReturnStore bridge
  - ✅ **1a.2 Mark-price liquidation** — merged `afb0be36`: tiered MM brackets, partial/full closed-form solver, liq fees, mark-not-last (Rin APPROVE, 180/180)
  - ✅ **1a.3 Venue/tier cost model** — merged `d76ccfae`: maker/taker branching, Binance VIP tiers, fail-loud config, 3-way crypto overlay (Rin APPROVE, 303/303)
  - ✅ **1a.4 Fail-loud integrity gate** — merged `de132dd5`: gaps/delistings/anomalies, no-repair contract, opt-in on all overlays (Rin APPROVE, 389/389)
  - ✅ **1a.5 Point-in-time universe** — merged `c93f415f`: as-of resolution w/ delisting records, CRSP convention, integrity-gate wiring (Rin APPROVE, 494/494)
  - ✅ **1b.3 Risk-adjusted ranking** — merged `4a8a64c2`: BH-FDR step-up on per-trial returns, raw-return sort killed (Rin APPROVE_WITH_NOTES, 220 passed)
  - ✅ **1b.4 Provenance columns** — merged `cf723589`: git_commit + data_hash in factory_verdicts, srf hash reused (Rin APPROVE_WITH_NOTES, 41/41)
  - **Hard gate LIFTED (2026-10-06): 1a.1–1a.4 landed — Tier A/B crypto verdicts unblocked.**
- **Sprint B — R&D hopper: DESIGNED, deferred to Sprint D.** Design tracked in git (`rd-hopper-engine-design.md`). Card `ea42e6be` open.
- **Sprint D candidates (deferred, not dropped):** ML meta-labeling + calibration, vol-target/DD-ladder overlays, CUSUM decay monitor, research dashboard UI, LightGBM challenger, Sprint B hopper build-out. RL declined this cycle.
- **Debt cards open:** `7c8ffd17` (zero-signal strategies → 17/17), `706eab50` (AC2 base-pin re-pin + tick_stall refresh).
- **Ops:** origin push fixed via gh token (yozakura-va write); forward test healthy, untouched per grilling decision; audit 2026-10-05 YELLOW → remediation on track.

---

## Ground Rules (Council-Mandated, Non-Negotiable)

1. **OOS isolation (Kaito):** Last 6 months (Jan 2026-Jul 2026) held out. No optimizer touches it. Final validation only.
2. **Regime labels locked (Kaito):** RegimeDetector config FROZEN for entire pipeline run. No tuning detector params mid-stream.
3. **ML confidence frozen (Kaito):** RandomForest learner stays frozen during optimization. Rebuild only on final locked blend.
4. **Spread costs mandatory (Liora):** XAUUSD 3.0 pips, EURUSD/GBPUSD 1.5 pips, USDJPY 1.2 pips. Commission $3.5/lot. Slippage 0.2 pips. PF must survive real costs.
5. **No bar subsampling (Liora):** Use full bar resolution for all profiling. If compute is tight, reduce date range — never skip bars.
6. **ATR lookback test (Liora):** Current 50-bar is baseline. Test 100 and 200-bar lookbacks for regime stability before trusting profiles.
7. **PBO required (Liora):** Any Optuna-derived parameters must have Probability of Backtest Overfitting computed before adoption.
8. **Minimum significance (Liora):** Any regime bucket with <10 trades = "insufficient data" — not a signal.
9. **CPU/memory caps (Craig):** nice -n 19, cpulimit -l 20, DuckDB threads=1 memory_limit=512MB.
10. **Kill criteria (Mika):** If a queued item hasn't produced measurable progress in its time budget, kill it, record why, move to next.
11. **Sequential dependency (Kaito):** Items that depend on other items' results wait. No fake parallelism.
12. **Forward test isolation (Rei):** Running blend stays untouched until a new validated config passes all gates. Deploy only on PASS.

---

## Kill Criteria (per Mika)

| Item Type | Kill Condition | Action |
|-----------|---------------|--------|
| Gate tuning | Loosened gate PF < 1.2 (with spread) | Revert to original gate |
| New strategy | <20 trades on full XAUUSD M15 data | Abandon, queue next |
| New strategy | PF < 0.8 in ALL regimes | Abandon |
| FX retuning | PF < 0.8 in ALL regimes for a pair | Skip that pair |
| Optuna sweep | Best params don't beat default by >10% PF | Keep defaults |
| Profiling | All regime buckets <10 trades | Mark "insufficient data" |

---

## Dependency Chain (per Kaito)

```
Gate loosening (1-3) → Re-evaluate blend (4) → Lock strategy set
                                                    ↓
                                      Optuna sweeps (10-13) → Blend weights (14)
                                                    ↓
                                      Final validation (20)

New strategies (5-6) → Profile → Add to blend set

USDJPY harvest → Aggregate (7) → Profile (8) → Retune (9) → Add to blend set

Confidence layers (15-16) → After blend set locked
Risk infra (18) → Parallel, non-blocking
ATR lookback test (19) → Before trusting any profile conclusions
```

Items in different branches can overlap IF compute allows. Same-branch items are strictly sequential.

---

## Current Queue (ordered by leverage)

> Items are worked ONE AT A TIME, top to bottom. Each item has a type, expected
> gain, and time budget. When complete, results are recorded and the next item
> starts. Craig can reorder at any time.

| # | Item | Type | Expected Gain | Time | Status | Depends On |
|---|------|------|---------------|------|--------|------------|
| 1 | Loosen KZ gate: ADX[15,28] + NY_AM session | Gate tuning | +20-30 trades/yr | 2h | **next** | — |
| 2 | Loosen SRMR+ gate: CHOPPY + London | Gate tuning | +15-25 trades/yr | 2h | queued | — |
| 3 | Loosen Donchian gate: ADX<35 + ATR_pct<60 | Gate tuning | +10-20 trades/yr | 2h | queued | — |
| 4 | Re-evaluate gated blend with loosened gates + spread costs | Validation | quality check | 2h | queued | 1-3 |
| 5 | B.3 London Breakout Retest (XAUUSD) | New strategy | +30-50 trades/yr | 4h | queued | — |
| 6 | Mean-reversion strategy for CHOPPY regime | New strategy | +30-50 trades/yr | 4h | queued | — |
| 7 | USDJPY: aggregate ticks → bars | Data | unblocks #8 | 2h | blocked | harvest |
| 8 | USDJPY + FX profiling (full bars, no subsampling) | Profiling | +50-100 trades/yr | 3h | queued | 7 |
| 9 | FX param retuning (if profiling shows edge) | Retuning | +50-100 trades/yr | 3h | queued | 8 |
| 10 | Optuna sweep: KZ XAUUSD M15 (20 trials, OOS excluded) | Optimization | +20-30 trades/yr | 2h | queued | 4 |
| 11 | Optuna sweep: SRMR+ XAUUSD M15 | Optimization | +20-30 trades/yr | 2h | queued | 4 |
| 12 | Optuna sweep: Donchian XAUUSD H1 | Optimization | +20-30 trades/yr | 2h | queued | 4 |
| 13 | Optuna sweep: DualTF XAUUSD M15 | Optimization | +20-30 trades/yr | 2h | queued | 4 |
| 14 | Blend weight optimization (ml/blend_optimizer) | Optimization | quality gain | 2h | queued | 10-13 |
| 15 | Wire spread/volatility gates to confidence engine | Confidence | quality gain | 3h | queued | — |
| 16 | Wire ML confidence learner (rebuild on final blend) | Confidence | quality gain | 3h | queued | 14 |
| 17 | Profile Tier 3 strategies (momentum, session_breakout...) | Discovery | unknown | 2h each | queued | — |
| 18 | FTMO trailing DD guard + daily loss budget | Risk | safety | 2h | queued | — |
| 19 | ATR lookback stability test (50 vs 100 vs 200 bar) | Validation | robustness | 1h | queued | — |
| 20 | Full blend validation: spread + OOS walk-forward + MC + PBO | Validation | FINAL GATE | 3h | queued | all above |

---

## Progress Log

| Date | Item | Result | File |
|------|------|--------|------|
| Jul 22 | Fix VS + VRB zero-trade bugs | ✅ Both fixed, on main | commit 788c82b, 1874099 |
| Jul 22 | Build B.1 Donchian ATR v2 | ✅ 22 tests, 144 trades smoke | commit 0a89e37 |
| Jul 22 | Build B.2 Dual-TF Squeeze Pro | ✅ 16 tests, thin signal volume | commit b514b0a |
| Jul 22 | ttc_xauusd verification | ✅ OVERFIT verdict | ttc_xauusd_verification_2026-07-22.md |
| Jul 22 | XAUUSD regime profiles | ✅ Sweet spots found | regime_profiles_raw_2026-07-22.md |
| Jul 22 | Gated blend evaluation (no spread) | ✅ PF=1.74, DD=5.67% | gated_blend_results_2026-07-22.md |
| Jul 22 | Walk-forward validation | ✅ 3/5 pass, MC 99.9% | walk_forward_gated_blend_2026-07-22.md |
| Jul 22 | FX regime profiles (EURUSD/GBPUSD) | ❌ No edge with current params | fx_regime_profiles_2026-07-22.md |
| Jul 22 | Wire gated blend to forward test | ✅ Live, PID 2779285 | commit 282c1c1 |
| Jul 22 | Council review of sprint plan | ✅ 4/4 REVISE → pipeline model | sprint-volume-expansion-v2.md (archive) |
| Oct 5 | Sprint A closed (SFA-1/2/3 merged) | ✅ all Rin-approved | git 2279b5e0/222b8809/5e11645e |
| Oct 5 | Audit 2026-10-05 (YELLOW) + 5-lane research swarm + Craig grilling | ✅ verdict + sprint C approved | docs/audits/ayumi-audit-2026-10-05.md |
| Oct 5 | Wave 0.1 tournament root fix | ✅ 17/17 run, 11/17 score | merged 6499e0f2 |
| Oct 5 | 1b.1 real-trial PBO (retire synthetic matrix) | ✅ 177/177 | merged cf8a7a64 |
| Oct 5 | 1a.1 adaptive funding model (8h/1h) | ✅ 65/65 | merged eadbb741 |
| Oct 5 | 1b.2 CPCV runner (purge+embargo, φ=C(N,k)) | ✅ 75/75 + Rin 51/51 | merged 206da069 |
| Oct 5 | Debt carded: zero-signal strategies, AC2 re-pin | ✅ queued | cards 7c8ffd17, 706eab50 |

---

## Note on PF Accuracy (per Liora)

The gated blend PF=1.74 was computed WITHOUT spread costs. With realistic spread
(3-pip XAUUSD ~$3/trade), the net on 170 trades drops from +$2,820 to ~$2,610.
Still positive (PF ~1.6) but this means:

- All future backtests MUST include spread costs
- Gate loosening (#1-3) must maintain PF>1.2 WITH spread, not without
- The "real" PF target is >1.2 post-spread for a strategy to be blend-viable
