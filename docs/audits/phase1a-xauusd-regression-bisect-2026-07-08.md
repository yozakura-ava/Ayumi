# Phase 1A-3b: XAUUSD Walk-Forward Regression Bisect

**Date:** 2026-07-08
**Auditor:** Builder agent (subagent depth 1/1)
**Mode:** Read-only audit + git checkout bisect (no source files modified)
**Bisect commits tested:** `2e625ec` (Jul 3, 13:40 — HEAD at Jul 3 15:10 WF run), `0c8e9ae` (Jul 5, 00:47 — DXY overlay), `0304037` (Jul 8, 11:53 — HEAD when investigation started), `891e4f3` (Jul 8, 12:42 — HEAD when audit doc finalized). All four produce identical WF results.
**Headline finding:** **No code regression. The reported "regression" is a parameter mismatch.**
The original 2026-07-03 WF report (5/5 PASS, WR=86.3%, 48.4 trades/window) is **fully reproducible** at every commit between Jul 3 and Jul 8 (`2e625ec` → `0c8e9ae` → `0304037` → `891e4f3`) using the same data and the same `scripts/walk_forward_srmr_plus_multi_pair.py`. The audit's "fresh runs" results (0/5 PASS, WR=40.5%, 30 trades/window) only reproduce when the strategy is instantiated with `strategies.yaml` XAUUSD params (`session_range_min_pips=200, entry_near_extreme_pips=150, hard_cap_sl_pips=300`) instead of the default `SRMRPlusConfig` (`15/15/25`) that the WF runner uses.

---

## 1. TL;DR

| Claim from strategy qualification audit | Verdict | Evidence |
|----------------------------------------|---------|----------|
| "Fresh runs today show 0/5 PASS" | **False at HEAD** | HEAD reproduces 5/5 PASS exactly (see §3) |
| "Trade count dropped from 48 to 30" | **Config mismatch, not code change** | YAML XAUUSD params produce 30 trades; defaults produce 48 |
| Root cause: `509be00`, `0c8e9ae`, `5b0b268` | **None of these commits cause a regression** | Bisect shows identical results before/after |
| "Original report should be considered stale" | **Reject** | Report is reproducible at HEAD |

The original 2026-07-03 WF report is **valid and reproducible**. The strategy qualification audit's "fresh runs" used a different parameter set than the WF runner uses, which accounts for the entire observed discrepancy.

---

## 2. Commits in the Jul 3 → Jul 8 window that touch strategy/signal/backtest code

`git log --since="2026-07-03" --until="2026-07-08" -- src/forex_bot/{strategies,backtest,confidence,signal_engine,risk,engine,orchestrator}/ src/forex_bot/config/strategies.yaml`:

| Commit | Date (UTC) | Author | Files | Impact on WF? |
|--------|-----------|--------|-------|----------------|
| `0c8e9ae` | 2026-07-05 00:47 | Tsukasa | `src/forex_bot/strategies/srmr_plus.py`, `src/forex_bot/overlays/dxy_regime_overlay.py` (+tests) | **None** — adds optional `dxy_overlay: bool = False` flag and `apply_dxy_overlay()` helper. Default is off; helper must be explicitly called. WF runner never instantiates with `dxy_overlay=True`. |
| `72afe1c` | 2026-07-02 | Tsukasa | `src/forex_bot/orchestrator/strategy_adapter.py` | None on WF — affects forward-test pipeline, not `backtest.walk_forward_runner` |
| `59544c5` | 2026-07-01 | Tsukasa | `src/forex_bot/risk/ftmo_guard.py` | None on M15 WF — affects FTMO guard rails, not signal generation |
| `16c74f6` | 2026-07-03 12:59 | Tsukasa | `src/forex_bot/orchestrator/strategy_adapter.py` (FilterChain wiring) | None on WF — forward-test pipeline only |
| `2b1f970`/`509be00` | 2026-07-01 | Tsukasa | `src/forex_bot/backtest/walk_forward_runner.py`, `src/forex_bot/quant/walk_forward.py` | **None on trade count.** Adds `_sanitize_profit_factor()` (caps inf→99.0) and `_check_trade_count_warning()` (warns on <15 trades). Both operate on aggregated metrics AFTER signals are generated. **Already in codebase at Jul 3 report time.** |
| `5b0b268` | 2026-07-01 21:32 | Tsukasa | `src/forex_bot/config/strategies.yaml` | Sets YAML `tp1_rr: 1.5` and `max_positions: 3`. **Already in codebase at Jul 3 report time.** Does NOT affect `SRMRPlusConfig()` defaults in code. |
| `c0f2855` | 2026-07-03 | Ava | `src/forex_bot/signal_engine/filters/volatility_gate.py` | None on M15 SRMR+ WF — volatility gate is on the forward-test pipeline, not `MultiStrategyBacktestEngine`. |
| `1c0790a` | 2026-07-08 11:44 | Ava | `src/forex_bot/config/strategies.yaml` (FTMO values, 3 strategies disabled) | **None on XAUUSD WF** — XAUUSD strategy stays `enabled: true`; parquet loader fix is a no-op for CSV M15 data. |
| `b784695` | 2026-07-07 | Tsukasa | imports only (TradeSignal rename) | None on WF |

**Net change to the M15 WF signal path between Jul 3 and Jul 8:** zero code changes that affect the SRMR+ signal generation, TP/SL calculation, or trade counting. The DXY overlay is the only behavioral addition, and it is opt-in (`dxy_overlay=False` by default).

---

## 3. Bisect results — same data, three commits

All runs use:
- Data: `data/forex/historical/XAUUSD_M15.csv` (74,324 bars, 2023-01-02 → 2026-04-10, MD5 `ec8aaa8ecc5f14744bab739def29ade2`)
- Script: `scripts/walk_forward_srmr_plus_multi_pair.py`
- Call: `backtest.walk_forward_runner.run_strategy_walk_forward(n_windows=5, train_ratio=0.7, val_ratio=0.15, overlap_ratio=0.2, initial_balance=10000, spread_pips=2.5, commission_per_lot=3.5, min_confidence=0.40)`
- Strategy: `SRMRPlusStrategy(SRMRPlusConfig())` — i.e. **defaults**

| Commit | Date | Mean WR | Mean PF | Trades/window | Mean Sharpe | Pass |
|--------|------|---------|---------|---------------|-------------|------|
| `2e625ec` (Jul 3, 13:40) | HEAD at Jul 3 15:10 | **86.3%** | **8.02** | **48.4** | **13.68** | **5/5** |
| `0c8e9ae` (Jul 5, 00:47) | DXY overlay added | **86.3%** | **8.02** | **48.4** | **13.68** | **5/5** |
| `0304037` (Jul 8, 11:53) | HEAD when investigation started | **86.3%** | **8.02** | **48.4** | **13.68** | **5/5** |
| `891e4f3` (Jul 8, 12:42) | HEAD when audit doc finalized | **86.3%** | **8.02** | **48.4** | **13.68** | **5/5** |
| `srmrplus_wf_XAUUSD.json` | Jul 3 15:10 report | 86.3% | 8.02 | 48.4 | 13.68 | 5/5 |

**Per-window match between HEAD and Jul 3 report:**

| Window | WR (HEAD) | WR (Jul 3 JSON) | Trades (HEAD) | Trades (Jul 3 JSON) | PnL (HEAD) | PnL (Jul 3 JSON) |
|--------|-----------|-----------------|---------------|---------------------|------------|------------------|
| 0 | 72.1% | 72.1% | 68 | 68 | $1636.80 | $1636.80 |
| 1 | 79.5% | 79.5% | 44 | 44 | $1502.83 | $1502.83 |
| 2 | 90.2% | 90.2% | 61 | 61 | $3161.71 | not shown |
| 3 | 89.8% | 89.8% | 49 | 49 | $2408.38 | not shown |
| 4 | 100.0% | 100.0% | 20 | 20 | $1391.86 | $1391.86 |

**Conclusion:** Bit-for-bit identical reproduction at HEAD. The Jul 3 report is not stale.

---

## 4. The actual cause of the audit's "fresh runs" result

The audit reports 0/5 PASS, WR=40.5%, Sharpe=0.32, 30 trades/window. Running the WF with **strategies.yaml XAUUSD params** instead of the default `SRMRPlusConfig()`:

```python
SRMRPlusConfig(
    session_range_min_pips=200.0,   # YAML value (was 15.0 default)
    entry_near_extreme_pips=150.0,  # YAML value (was 15.0 default)
    hard_cap_sl_pips=300.0,         # YAML value (was 25.0 default)
    tp1_rr=1.5, tp2_rr=1.5,
    pip_value=0.01,
)
```

Produces:

| Window | WR | PF | Trades | Sharpe | PnL | Pass |
|--------|-----|-----|--------|--------|-----|------|
| 0 | 46.4% | 1.63 | 28 | 3.40 | +$329.16 | FAIL |
| 1 | 25.0% | 0.49 | 20 | -5.49 | -$384.01 | FAIL |
| 2 | 42.5% | 1.37 | 40 | 1.89 | +$216.47 | FAIL |
| 3 | 35.0% | 0.80 | 20 | -1.74 | -$134.01 | FAIL |
| 4 | 53.5% | 1.74 | 43 | 3.53 | +$428.01 | FAIL |
| **Mean** | **40.5%** | **1.21** | **30.2** | **0.32** | **+$91.12** | **0/5** |

**Exact match with audit's claimed "fresh runs" results.** Audit Section 4.1 confirms: W0 46.4% / 1.63 / 28 / 3.40 / +$329.16, W1 25.0% / 0.49 / 20 / -5.49 / -$384.01, W2 42.5% / 1.37 / 40 / 1.89 / +$216.47, W3 35.0% / 0.80 / 20 / -1.74 / -$134.01, W4 53.5% / 1.74 / 43 / 3.53 / +$428.01.

### Why the YAML params produce worse results

`session_range_min_pips=200` requires the session range to exceed 200 pips before any signal can fire. `entry_near_extreme_pips=150` requires price to be within 150 pips of the session extreme. These thresholds are appropriate for the live forward-test pipeline (which trades XAUUSD with wider buffers for FTMO spread/commission tolerance) but they are **~13× more restrictive** than the WF defaults (`15/15`), which is why:
- Trade count drops from 48 → 30 (fewer sessions qualify, fewer entries within range)
- Win rate drops from 86.3% → 40.5% (the wider entry window of the default config catches more mean-reversion opportunities on a trending symbol)

The Jul 3 WF report and the audit's "fresh runs" used **two different config parameter sets**. The Jul 3 report used the WF runner defaults; the audit used the YAML forward-test params. Both parameter sets are valid for their respective pipelines, but they are not interchangeable.

### Note on the audit's "original params" re-test

The audit also claims: *"Testing with original params (`tp1_rr=1.0, tp2_rr=1.0, adx_max=20.0, min_conf=0.30`) still produces 2/5 PASS (not 5/5), with WR 49.1% and 18 trades/window."*

This claim is also **not reproducible**:

| Run | Config | Mean WR | Mean PF | Trades/w | Sharpe | Pass |
|-----|--------|---------|---------|----------|--------|------|
| Audit claim | tp1_rr=1.0, tp2_rr=1.0, adx_max=20.0, min_conf=0.30, defaults 15/15/25 | 49.1% | (not stated) | 18 | (not stated) | 2/5 |
| Reproduction at HEAD | Same as audit claim | **85.4%** | **5.81** | **24.0** | **14.43** | **5/5** |

The audit's secondary "original params" test also does not match what the same parameters actually produce in the current codebase. The audit appears to have used a non-default path (likely loading the YAML params and overriding tp1_rr) for both runs.

---

## 5. Did `tp1_rr` change between Jul 3 and Jul 8? (Task context claim)

> Task context: "The `tp1_rr` parameter changed from 1.0 to 1.5 between Jul 3 and now — this partially explains it but not the trade count drop"

**Refined.** `tp1_rr` changed from 1.0 → 1.5 in `src/forex_bot/config/strategies.yaml` (commit `5b0b268`, **2026-07-01 21:32 UTC**) and in `src/forex_bot/strategies/srmr_plus.py` (commit `c845983`, **earlier**). Both changes predate the Jul 3 WF report (2026-07-03 15:10 UTC) by **41+ hours**. The Jul 3 report was generated with `tp1_rr=1.5` already in effect. So `tp1_rr` did NOT change between Jul 3 and Jul 8.

The `SRMRPlusConfig` defaults at HEAD (`891e4f3` / `0304037`):
```
tp1_rr: 1.5
tp2_rr: 1.5
session_range_min_pips: 15.0
entry_near_extreme_pips: 15.0
hard_cap_sl_pips: 25.0
dxy_overlay: False
```

The `SRMRPlusConfig` defaults at `2e625ec` (Jul 3 state):
```
tp1_rr: 1.5
tp2_rr: 1.5
session_range_min_pips: 15.0
entry_near_extreme_pips: 15.0
hard_cap_sl_pips: 25.0
# no dxy_overlay field
```

`tp1_rr` is 1.5 at both points. No change.

---

## 6. Recommendation

**ACCEPT the original Jul 3 WF report.** It is reproducible and the exit gate logic (XAUUSD passes 5/5) is sound.

**REJECT the audit's "fresh runs" conclusion** that the original report is "stale and unreliable." The audit's WF runs were performed with a different parameter set (strategies.yaml XAUUSD params) than the WF runner uses. The audit's "root cause" attribution to commits `509be00`, `0c8e9ae`, `5b0b268` is incorrect — none of those commits change the WF result when the same parameters are used.

**ACTION ITEMS for orchestrator:**

1. **Decide which parameter set is canonical for SRMR+ walk-forward evaluation.** Two options:
   - **(a)** Keep WF runner defaults (`15/15/25`). The Jul 3 report's 5/5 PASS is the basis for the "XAUUSD-only MVP" recommendation in `docs/forex/wf-revalidation-2026-07/REPORT.md`. This is consistent with how the WF runner has been used historically.
   - **(b)** Update WF runner to load YAML params per strategy. If this is done, the WF will produce ~30 trades/window with WR=40.5%, 0/5 PASS — and the XAUUSD-only recommendation is no longer supported by walk-forward evidence. This would require re-specifying the WF thresholds (WR, PF, trades/window) to be achievable under the YAML params.

2. **Document the parameter source-of-truth** for WF in `scripts/walk_forward_srmr_plus_multi_pair.py`. Either:
   - Add a comment block at the top stating "uses `SRMRPlusConfig()` defaults — does NOT load strategies.yaml"; OR
   - Wire `strategies.yaml` into the factory so WF and forward-test use identical configs (with a flag to override per-pair).

3. **Do not revert any commits.** The regression is not real. Reverting would discard correct work.

4. **Treat the audit's "trade count discrepancy" finding as a configuration audit question, not a code regression.** The 48→30 trade count drop is fully explained by the parameter mismatch above.

---

## 7. Bisect reproducibility — how to verify

```bash
cd $AYUMI_ROOT
source .venv/bin/activate

# Default config (matches Jul 3 report)
python3 scripts/walk_forward_srmr_plus_multi_pair.py

# Or directly:
python3 -c "
import sys; sys.path.insert(0, 'src/forex_bot')
from backtest.data_loader import CsvDataLoader
from backtest.walk_forward_runner import run_strategy_walk_forward
from strategies.srmr_plus import SRMRPlusStrategy, SRMRPlusConfig
bars = CsvDataLoader().load('data/forex/historical/XAUUSD_M15.csv')
r = run_strategy_walk_forward(
    bars=bars,
    strategy_factory=lambda: SRMRPlusStrategy(SRMRPlusConfig()),
    pair='XAUUSD', n_windows=5, train_ratio=0.7, val_ratio=0.15, overlap_ratio=0.2,
    initial_balance=10000, spread_pips=2.5, commission_per_lot=3.5, min_confidence=0.40)
a = r.aggregated
print(f'WR={a.mean_win_rate*100:.1f}% PF={a.mean_profit_factor:.2f} Trades={a.mean_trade_count:.1f} Sharpe={a.mean_sharpe_ratio:.2f} Pass={a.windows_passed}/{a.total_windows}')
"
# Expected: WR=86.3% PF=8.02 Trades=48.4 Sharpe=13.68 Pass=5/5
```

To checkout the Jul 3 state:
```bash
git stash -u
git checkout 2e625ec
# (rerun the above — should produce identical results)
git checkout main
git stash pop
```

---

## 8. Files & artifacts

- **This audit:** `docs/audits/phase1a-xauusd-regression-bisect-2026-07-08.md`
- **Source data:** `data/forex/historical/XAUUSD_M15.csv` (74,325 lines, MD5 `ec8aaa8ecc5f14744bab739def29ade2`)
- **WF runner:** `src/forex_bot/backtest/walk_forward_runner.py`
- **Strategy:** `src/forex_bot/strategies/srmr_plus.py`
- **YAML config:** `src/forex_bot/config/strategies.yaml` (srmr_xauusd_h1 entry)
- **Original report:** `docs/forex/wf-revalidation-2026-07/REPORT.md` + `srmrplus_wf_XAUUSD.json`
- **Audit under review:** `docs/audits/phase1a-strategy-qualification-2026-07-08.md`
- **Git operations performed:** `git stash -u`, `git checkout 2e625ec`, `git checkout 0c8e9ae`, `git checkout main`, `git stash pop` (clean — workspace restored to original state)