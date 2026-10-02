# Phase 1A-3: Strategy Qualification Sub-Gate Audit

**Date:** 2026-07-08  
**Auditor:** Builder agent (subagent depth 1/1)  
**Scope:** Walk-forward evaluation of all strategies in `src/forex_bot/config/strategies.yaml` against FTMO 1-Step numeric criteria.  
**Mode:** Read-only audit — no source files modified.

---

## 1. Numeric Pass Criteria (FTMO 1-Step)

| Criterion              | Threshold     |
| ---------------------- | ------------- |
| Sharpe Ratio           | ≥ 0.5         |
| Win Rate               | ≥ 55%         |
| Walk-Forward Windows   | ≥ 3/5 positive |
| Max Drawdown           | < 5%          |

A strategy **PASS**es only if ALL four criteria are met. Otherwise **FAIL**.  
**NEEDS-RESEARCH** = insufficient data or config ambiguity prevents a verdict.

---

## 2. Strategies Inventory

**Source:** `src/forex_bot/config/strategies.yaml` (commit 1c0790a, 2026-07-08)

| # | Strategy ID         | Type      | Symbol  | TF  | Enabled | Config Params (key)                                      |
|---|---------------------|-----------|---------|-----|---------|----------------------------------------------------------|
| 1 | srmr_gbpusd_h1      | srmr_plus | GBPUSD  | H1  | yes     | sr_min=25, entry=15, sl=25, tp1_rr=1.5                  |
| 2 | srmr_eurusd_h1      | srmr_plus | EURUSD  | H1  | yes     | sr_min=15, entry=12, sl=20, tp1_rr=1.5                  |
| 3 | srmr_xauusd_h1      | srmr_plus | XAUUSD  | H1  | yes     | sr_min=200, entry=150, sl=300, tp1_rr=1.5, pip=0.01     |
| 4 | srmr_usdjpy_h1      | srmr_plus | USDJPY  | H1  | yes     | sr_min=15, entry=12, sl=20, tp1_rr=1.5, pip=0.01        |
| 5 | srmr_audusd_h1      | srmr_plus | AUDUSD  | H1  | **no**  | Phase 0: disabled — no historical data                   |
| 6 | srmr_usdchf_h1      | srmr_plus | USDCHF  | H1  | **no**  | Phase 0: disabled — no historical data                   |
| 7 | srmr_usdcad_h1      | srmr_plus | USDCAD  | H1  | **no**  | Phase 0: disabled — no historical data                   |
| 8 | ttc_xauusd_h1       | ttc       | XAUUSD  | H1  | yes     | rr_ratio=3.0, min_conf=0.40                              |

**Totals:** 8 strategies (5 enabled, 3 disabled).  
*Note: Task context said "10 strategies, 9 active" — actual count is 8 total, 5 active. The 3 disabled strategies were disabled in Phase 0 (commit 1c0790a).*

---

## 3. Existing Walk-Forward Revalidation Report (2026-07-03)

**Source:** `docs/forex/wf-revalidation-2026-07/REPORT.md` + `summary.json`

The 2026-07-03 report ran SRMR+ WF across 4 pairs using:
- `n_windows=5`, `train_ratio=0.7`, `val_ratio=0.15`, `overlap_ratio=0.2`
- FX majors: parquet H1 data (17K bars, 2023-06 → 2026-04)
- XAUUSD: CSV M15 data (74K bars, 2023-01 → 2026-04)

### 3.1 H1 Results (from existing report)

| Pair    | Windows Passed | PF mean | WR mean | Sharpe  | Max DD  | Trades/Window | Mean PnL  |
|---------|---------------|---------|---------|---------|---------|---------------|-----------|
| GBPUSD  | 0 / 5         | 0.81    | 37.7%   | −2.40   | 2.40%   | 14.4          | −$126.63  |
| EURUSD  | 1 / 5         | 0.94    | 36.0%   | −3.56   | 3.09%   | 13.0          | −$144.72  |
| USDJPY  | 1 / 5         | 1.27    | 42.8%   | −0.01   | 2.19%   | 13.6          | −$31.67   |
| XAUUSD  | 5 / 5         | 8.02    | 86.3%   | 13.68   | 0.60%   | 48.4          | +$2,020.31 |

### 3.2 M15 Data Availability Check

| Pair    | M15 CSV Exists | Bars   | Range                    |
|---------|---------------|--------|--------------------------|
| GBPUSD  | yes           | 78,212 | 2023-01-01 → 2026-04-10  |
| EURUSD  | yes           | 78,274 | 2023-01-01 → 2026-04-10  |
| USDJPY  | yes           | 78,179 | 2023-01-01 → 2026-04-10  |
| XAUUSD  | yes           | 74,325 | 2023-01-02 → 2026-04-10  |
| GBPJPY  | yes           | 78,132 | 2023-01-01 → 2026-04-10  |
| AUDUSD  | no            | —      | —                        |
| USDCHF  | no (M5 only)  | —      | —                        |
| USDCAD  | no (M5 only)  | —      | —                        |

**Finding:** M15 data exists for all 4 enabled SRMR+ pairs + GBPJPY. The parquet `reset_index` bug fix (commit 1c0790a) is confirmed working — `CsvDataLoader.load_parquet()` now successfully loads parquet files.

---

## 4. Fresh M15 Walk-Forward Runs (2026-07-08)

Ran SRMR+ WF on M15 data for all 4 enabled pairs using current codebase (HEAD at 0304037).

**Parameters:** `n_windows=5`, `train_ratio=0.7`, `val_ratio=0.15`, `overlap_ratio=0.2`, `min_confidence=0.40`, `initial_balance=10000`

### 4.1 XAUUSD M15

| Window | Win Rate | PF    | Max DD | Sharpe | Trades | PnL       | Passed |
|--------|----------|-------|--------|--------|--------|-----------|--------|
| 0      | 46.4%    | 1.63  | 1.9%   | 3.40   | 28     | +$329.16  | FAIL*  |
| 1      | 25.0%    | 0.49  | 4.1%   | −5.49  | 20     | −$384.01  | FAIL   |
| 2      | 42.5%    | 1.37  | 2.0%   | 1.89   | 40     | +$216.47  | FAIL   |
| 3      | 35.0%    | 0.80  | 2.8%   | −1.74  | 20     | −$134.01  | FAIL   |
| 4      | 53.5%    | 1.74  | 1.5%   | 3.53   | 43     | +$428.01  | FAIL   |
| **Mean** | **40.5%** | **1.21** | **2.5%** | **0.32** | **30** | **+$91.12** | **0/5** |

*Window 0 passed the codebase go_nogo (win_rate>55%, PF>1.0, PnL>0, DD<10%) but fails the task's stricter Sharpe≥0.5 threshold.

**Spread used:** 2.5 pips (matching original report). Also tested with FTMO spread (40.0 pips) → 0/5 pass, WR 39.7%, Sharpe −0.65.

### 4.2 USDJPY M15

| Window | Win Rate | PF    | Max DD | Sharpe | Trades | PnL       | Passed |
|--------|----------|-------|--------|--------|--------|-----------|--------|
| 0      | 40.0%    | 0.79  | 4.3%   | −1.83  | 20     | −$145.30  | FAIL   |
| 1      | 22.2%    | 0.18  | 13.0%  | −6.07  | 18     | −$1,239.09| FAIL   |
| 2      | 45.5%    | 1.22  | 3.7%   | 1.24   | 33     | +$128.42  | FAIL   |
| 3      | 38.7%    | 1.02  | 2.7%   | 0.11   | 31     | +$11.23   | FAIL   |
| 4      | 40.0%    | 0.82  | 3.0%   | −1.57  | 20     | −$124.69  | FAIL   |
| **Mean** | **37.3%** | **0.80** | **5.3%** | **−1.62** | **24** | **−$273.89** | **0/5** |

**Spread used:** 2.0 pips (strategies.yaml value)

### 4.3 GBPUSD M15

| Window | Win Rate | PF    | Max DD | Sharpe | Trades | PnL       | Passed |
|--------|----------|-------|--------|--------|--------|-----------|--------|
| 0      | 31.6%    | 0.58  | 4.1%   | −4.25  | 19     | −$302.38  | FAIL   |
| 1      | 16.7%    | 0.23  | 4.6%   | −12.06 | 12     | −$442.81  | FAIL   |
| 2      | 27.8%    | 1.24  | 5.3%   | 0.95   | 18     | +$179.67  | FAIL   |
| 3      | 47.8%    | 1.00  | 2.3%   | 0.01   | 23     | +$0.99    | FAIL   |
| 4      | 53.3%    | 1.38  | 1.1%   | 2.45   | 15     | +$149.49  | FAIL   |
| **Mean** | **35.4%** | **0.89** | **3.5%** | **−2.58** | **17** | **−$83.01** | **0/5** |

### 4.4 EURUSD M15

| Window | Win Rate | PF    | Max DD | Sharpe | Trades | PnL       | Passed |
|--------|----------|-------|--------|--------|--------|-----------|--------|
| 0      | 40.0%    | 0.68  | 4.3%   | −2.99  | 20     | −$235.84  | FAIL   |
| 1      | 46.7%    | 0.90  | 1.7%   | −0.78  | 15     | −$47.00   | FAIL   |
| 2      | 40.7%    | 1.07  | 1.8%   | 0.45   | 27     | +$42.97   | FAIL   |
| 3      | 57.7%    | 1.20  | 2.3%   | 1.26   | 26     | +$115.16  | PASS   |
| 4      | 27.8%    | 0.89  | 5.5%   | −0.60  | 18     | −$79.52   | FAIL   |
| **Mean** | **42.6%** | **0.95** | **3.1%** | **−0.53** | **21** | **−$40.85** | **1/5** |

### 4.5 TTC XAUUSD (from existing TTS WF report)

**Source:** `reports/tts_walkforward/tts_M15_20260412_0017.json`  
Only EURUSD data available with go_nogo results. No XAUUSD TTS WF report exists.

TTC EURUSD M15 (for reference): WR 61.2%, 8 trades/window, 1 pair only, pass=false.

---

## 5. ⚠️ CRITICAL FINDING: Original Report Non-Reproducible

The 2026-07-03 WF report showed XAUUSD M15 with **5/5 windows PASS, WR 86.3%, Sharpe 13.68, 48.4 trades/window**. 

Running the same WF on the same data with the same parameters today produces **0/5 windows PASS, WR 40.5%, Sharpe 0.32, 30 trades/window**.

**Root cause:** Code changes between 2026-07-03 and 2026-07-08:
1. `509be00` (2026-07-01): PF sanitization + trade count guardrail (should not affect trade count)
2. `0c8e9ae` (2026-07-05): DXY overlay added (default False — should not affect results)
3. `5b0b268` (pre-report): `tp1_rr` changed 1.0→1.5 (fewer TP hits → fewer wins)
4. `adx_max_threshold` changed 20.0→25.0 (more signals allowed, but not enough to offset TP change)

Testing with original params (`tp1_rr=1.0, tp2_rr=1.0, adx_max=20.0, min_conf=0.30`) still produces 2/5 PASS (not 5/5), with WR 49.1% and 18 trades/window — still not matching the original 86.3% WR and 48.4 trades.

**The trade count discrepancy (18 vs 48) suggests a deeper code change in the signal generation or backtest engine path that we have not yet identified.** This may be in `MultiStrategyBacktestEngine`, `ConfidencePositionSizer`, or the signal evaluation path.

**Impact:** The original WF revalidation report should be considered **stale and unreliable**. All qualification decisions must use the fresh 2026-07-08 WF results from this audit.

---

## 6. Per-Strategy Verdicts

Using the **fresh M15 WF results** (Section 4) as primary evidence, cross-referenced with H1 results from the existing report (Section 3.1):

| # | Strategy ID         | Symbol  | TF  | Sharpe  | WR     | WF Pass | Max DD | Verdict         |
|---|---------------------|---------|-----|---------|--------|---------|--------|-----------------|
| 1 | srmr_gbpusd_h1      | GBPUSD  | H1  | −2.40   | 37.7%  | 0/5     | 2.4%   | **FAIL**        |
|   | (M15 re-test)       | GBPUSD  | M15 | −2.58   | 35.4%  | 0/5     | 3.5%   | **FAIL**        |
| 2 | srmr_eurusd_h1      | EURUSD  | H1  | −3.56   | 36.0%  | 1/5     | 3.1%   | **FAIL**        |
|   | (M15 re-test)       | EURUSD  | M15 | −0.53   | 42.6%  | 1/5     | 3.1%   | **FAIL**        |
| 3 | srmr_xauusd_h1      | XAUUSD  | M15 | 0.32    | 40.5%  | 0/5     | 2.5%   | **FAIL** ⚠️     |
| 4 | srmr_usdjpy_h1      | USDJPY  | H1  | −0.01   | 42.8%  | 1/5     | 2.2%   | **FAIL**        |
|   | (M15 re-test)       | USDJPY  | M15 | −1.62   | 37.3%  | 0/5     | 5.3%   | **FAIL**        |
| 5 | srmr_audusd_h1      | AUDUSD  | H1  | —       | —      | —       | —      | **NEEDS-RESEARCH** (disabled, no data) |
| 6 | srmr_usdchf_h1      | USDCHF  | H1  | —       | —      | —       | —      | **NEEDS-RESEARCH** (disabled, no data) |
| 7 | srmr_usdcad_h1      | USDCAD  | H1  | —       | —      | —       | —      | **NEEDS-RESEARCH** (disabled, no data) |
| 8 | ttc_xauusd_h1       | XAUUSD  | H1  | —       | —      | —       | —      | **NEEDS-RESEARCH** (no WF data for XAUUSD) |

### Verdict Summary

| Verdict         | Count | Strategies                                    |
|-----------------|-------|-----------------------------------------------|
| **PASS**        | 0     | —                                             |
| **FAIL**        | 4     | srmr_gbpusd_h1, srmr_eurusd_h1, srmr_xauusd_h1, srmr_usdjpy_h1 |
| **NEEDS-RESEARCH** | 4 | srmr_audusd_h1, srmr_usdchf_h1, srmr_usdcad_h1, ttc_xauusd_h1 |

**No strategy passes the FTMO 1-Step qualification criteria.**

---

## 7. Criteria Breakdown

### 7.1 Sharpe ≥ 0.5

| Strategy         | H1 Sharpe | M15 Sharpe | Pass? |
|------------------|-----------|------------|-------|
| srmr_gbpusd_h1   | −2.40     | −2.58      | NO    |
| srmr_eurusd_h1   | −3.56     | −0.53      | NO    |
| srmr_xauusd_h1   | —         | 0.32       | NO    |
| srmr_usdjpy_h1   | −0.01     | −1.62      | NO    |

### 7.2 Win Rate ≥ 55%

| Strategy         | H1 WR  | M15 WR | Pass? |
|------------------|--------|--------|-------|
| srmr_gbpusd_h1   | 37.7%  | 35.4%  | NO    |
| srmr_eurusd_h1   | 36.0%  | 42.6%  | NO    |
| srmr_xauusd_h1   | —      | 40.5%  | NO    |
| srmr_usdjpy_h1   | 42.8%  | 37.3%  | NO    |

### 7.3 ≥ 3/5 WF Windows Positive

| Strategy         | H1 Pass  | M15 Pass | Pass? |
|------------------|----------|----------|-------|
| srmr_gbpusd_h1   | 0/5      | 0/5      | NO    |
| srmr_eurusd_h1   | 1/5      | 1/5      | NO    |
| srmr_xauusd_h1   | —        | 0/5      | NO    |
| srmr_usdjpy_h1   | 1/5      | 0/5      | NO    |

### 7.4 Max Drawdown < 5%

| Strategy         | H1 Max DD | M15 Max DD | Pass? |
|------------------|-----------|------------|-------|
| srmr_gbpusd_h1   | 2.4%      | 3.5%       | YES   |
| srmr_eurusd_h1   | 3.1%      | 3.1%       | YES   |
| srmr_xauusd_h1   | —         | 2.5%       | YES   |
| srmr_usdjpy_h1   | 2.2%      | 5.3%       | NO (M15) |

---

## 8. Findings & Recommendations

### Finding 1: Original WF Report Non-Reproducible (CRITICAL)
The 2026-07-03 WF revalidation report showing XAUUSD 5/5 PASS is not reproducible with the current codebase. The original report should be considered stale. **All downstream decisions based on the original report should be re-evaluated.**

### Finding 2: No Strategy Passes FTMO 1-Step Criteria
All 4 enabled SRMR+ strategies fail on at least 3 of 4 criteria. The primary failure mode is low win rate (35-43% vs 55% threshold) and negative Sharpe.

### Finding 3: M15 Does Not Rescue FX Majors
Moving from H1 to M15 (4.5× more bars) does not meaningfully improve SRMR+ performance on GBPUSD, EURUSD, or USDJPY. Trade counts increase (13→17-24) but win rates and Sharpe remain well below thresholds.

### Finding 4: XAUUSD Regressed Sharply
XAUUSD was the standout in the original report (86% WR, Sharpe 13.68) but now shows 40.5% WR and Sharpe 0.32. The `tp1_rr` change (1.0→1.5) is a partial explanation but does not fully account for the trade count drop (48→30). Further investigation needed.

### Finding 5: TTC Strategy Has No WF Coverage
The `ttc_xauusd_h1` strategy has no walk-forward validation data for XAUUSD. Existing TTS WF reports only cover EURUSD and show marginal results (WR 61%, but only 8 trades/window).

### Finding 6: Disabled Strategies Lack Data
AUDUSD, USDCHF, USDCAD remain disabled with no M15 data (only M5 for USDCHF/USDCAD). These cannot be evaluated without data acquisition.

### Recommendations

1. **Investigate XAUUSD regression** — The original report's 86% WR was the basis for the "scope down to XAUUSD-only" recommendation. If that result was a code bug, the entire forward-test strategy needs rethinking.
2. **Run TTC WF for XAUUSD** — The TTC strategy with Optuna-optimized XAUUSD M15 params has never been walk-forward validated. This is the next candidate to test.
3. **Consider strategy parameter retuning** — The `tp1_rr=1.5` change (from 1.0) may be too aggressive for the current signal quality. Test with `tp1_rr=1.0` as a controlled experiment.
4. **Acquire M15 data for disabled pairs** — AUDUSD, USDCHF, USDCAD may perform differently. Data acquisition is a prerequisite for evaluation.
5. **Do not proceed to Phase 1B** — No strategy qualifies. The qualification gate is blocked.

---

## 9. Audit Artifacts

| Artifact | Location |
|----------|----------|
| Existing WF report | `docs/forex/wf-revalidation-2026-07/REPORT.md` |
| Existing WF summary JSON | `docs/forex/wf-revalidation-2026-07/summary.json` |
| Per-pair WF JSON (existing) | `docs/forex/wf-revalidation-2026-07/srmrplus_wf_*.json` |
| Fresh M15 USDJPY WF | `/tmp/wf_usdjpy_m15.json` |
| Fresh M15 XAUUSD WF | `/tmp/wf_xauusd_m15_s2.5.json` |
| Fresh M15 GBPUSD + EURUSD WF | `/tmp/wf_fx_m15.json` |
| Strategies config | `src/forex_bot/config/strategies.yaml` |
| WF runner | `src/forex_bot/backtest/walk_forward_runner.py` |
| Data loader (fixed) | `src/forex_bot/backtest/data_loader.py` (commit 1c0790a) |

---

**Audit Status:** COMPLETE  
**Overall Verdict:** **BLOCKED** — No strategy passes qualification. Phase 1B cannot proceed.
