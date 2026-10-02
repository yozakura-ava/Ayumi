# ttc_xauusd XAUUSD M15 — Anomaly Verification Report

**Generated:** 2026-07-22 (subagent depth 1/1, fresh re-investigation)
**Repo:** `$AYUMI_ROOT`
**Strategy under test:** `ttc_xauusd` (wrapper) → `TTSStrategy` (`backtest/strategies/tts_strategy.py`)
**Data source:** DuckDB `data/ayumi_market.duckdb` (XAUUSD M15, 71,747 bars, 2022-01-12 → 2026-07-10)

---

## TL;DR

- **Verdict: OVERFIT (old sweep) + MARGINAL strategy (current) — NO active bug.**
- The PF=8.02 / WR=86.3% / window-4 PF=10 / WR=100% claim in `docs/research/strategy-optimization-research.md` §A.1 is sourced from `docs/forex/wf-revalidation-2026-07/summary.json` — but that file's `strategy` field is **`SRMR+`**, not `ttc_xauusd`. The `srmrplus_wf_XAUUSD.json` it links to is the SRMR+ run, not TTC. The research doc mis-attributes the anomaly to TTC.
- I re-ran `TTCXAUUSDStrategy` from scratch on the full 71,747 bars via `MultiStrategyBacktestEngine`. **Mean PF = 0.797, WR = 41.4%, 0/5 windows passed** — the strategy is currently unprofitable on XAUUSD M15, but not anomalously so. No look-ahead bug, no high-confidence outliers.
- Synthetic-random-walk test: 12 trades, PF=0.50, WR=25% on noise → strategy does **not** extract spurious edge from random data.
- Parameter sensitivity: `lookback=10` (vs baseline `lookback=5`) raises mean PF from 0.797 → 1.324 (and WR 41.4% → 45.9%) — the original Optuna tuning over-fit to short swings.

**Confidence:** 4/5 (high — every test ran end-to-end with concrete numbers, but the strategy implementation is complex enough that I can't fully prove no edge cases exist beyond what I tested).

---

## XAUUSD M15 Vol Characteristics (input to synthetic test)

Computed from DuckDB:

| Metric | Value |
|---|---|
| Bars | 71,747 |
| Date range | 2022-01-12 → 2026-07-10 |
| Avg close | $2,813.11 |
| Median close | $2,568.32 |
| Avg ATR(14) | $4.82 (~0.171% of price) |
| Median ATR(14) | $3.24 |
| Per-bar log-return σ | **0.001381** |
| Per-bar log-return μ | 1.13e-5 (~driftless) |

---

## Task 1 — Synthetic Random Walk Test

**Method:** Generated 5,000 bars of Geometric Brownian Motion with `σ = 0.001381` (matching XAUUSD M15 realized vol), starting price $2,813. Ran `TTCXAUUSDStrategy` via `MultiStrategyBacktestEngine` directly on the synthetic series.

**Result:**

| Metric | Value | Threshold | Pass? |
|---|---|---|---|
| Trade count | 12 | — | — |
| Win rate | **0.250** (25.0%) | WR < 0.65 | ✓ |
| Profit factor | **0.501** | PF < 2.0 | ✓ |
| Total PnL | -$224.62 | — | — |
| Confidence min/max | 0.505 / 0.610 | no high outliers | ✓ |
| Confidence mean | 0.548 | — | — |
| Trades with conf ≥ 0.95 | 0 | — | ✓ |

**Verdict:** **PASS.** On pure noise the strategy produces a *losing* result (PF 0.50, WR 25% — below the random-baseline 50% WR). It does **NOT** extract spurious edge from random walks, which means the strategy is not a "noise-as-pattern detector" bug. (Note: the strategy still fires 12 signals on 5,000 bars, suggesting it has some structure-dependent trigger — but that structure doesn't translate to profitable signals without real temporal information.)

---

## Task 2 — Window 4 Trade Inspection

**Method:** Walk-forward with 5 sequential windows on full 71,747-bar dataset (`train_ratio=0.7, val_ratio=0.15, overlap_ratio=0.0`). Each window: 50,223 train + 10,762 val + **10,762 test** bars. Then sampled all 19 trades from window 4 and verified entry/exit/SL/TP coherence.

### Walk-forward per-window summary (real XAUUSD M15)

| Window | Test bars | Trades | WR | PF | PnL | go/nogo |
|---|---|---|---|---|---|---|
| W0 | 10,762 | 16 | 0.500 | 1.132 | +$52.87 | ✗ |
| W1 | 10,762 | 3 | 0.667 | 1.505 | +$25.27 | ✗ |
| W2 | 10,762 | 5 | 0.400 | 0.503 | -$74.60 | ✗ |
| W3 | 10,762 | 15 | 0.400 | 0.667 | -$149.61 | ✗ |
| W4 | 10,762 | **19** | 0.105 | **0.177** | **-$699.92** | ✗ |
| **Aggregated** | — | 58 | **0.414** | **0.797** | **-$169.20/window** | **0/5** |

### Window 4 trade inspection (19/19 sampled)

**All 19 trades passed coherence checks** (entry/exit prices consistent with direction; SL on correct side of entry; no entry==exit or winning trade with reversed exit direction).

Sample (5 of 19):

| Entry time | Direction | Entry | Exit | SL | PnL | Exit reason | Conf |
|---|---|---|---|---|---|---|---|
| 2026-06-01 02:45 | long | 4521.195 | 4509.352 | 4509.352 | -$50.00 | stop_loss | 0.558 |
| 2026-05-29 20:30 | long | 4543.335 | 4533.887 | 4533.887 | -$50.00 | stop_loss | 0.556 |
| 2026-06-01 04:45 | long | 4514.145 | 4502.886 | 4502.886 | -$50.00 | stop_loss | 0.558 |
| 2026-05-29 16:45 | long | 4566.575 | 4541.568 | 4541.568 | -$50.00 | stop_loss | 0.505 |
| 2026-06-08 01:15 | short | 4314.515 | 4289.966 | 4289.966 | **+$75.05** | stop_loss | 0.546 |

Key observations:
- **17 of 19 window-4 trades exited via stop-loss** (the other 2 didn't close in the window slice). This is a regime of sustained gold sell-off (early-to-mid June 2026) — the strategy kept firing bullish signals that immediately stopped out.
- All confidence scores are in [0.503, 0.614] — **no suspicious 0.95+ outliers** in any window.
- Average trade duration in window 4: 14.3 hours (range 0.2h–57.5h).
- The 2 winners were shorts on 2026-06-08, both closed via SL move in profit direction.

### Confidence distribution per window

| Window | n | conf min | conf max | conf mean | high_conf (≥0.95) |
|---|---|---|---|---|---|
| W0 | 16 | 0.507 | 0.658 | 0.543 | 0 |
| W1 | 3 | 0.528 | 0.588 | 0.566 | 0 |
| W2 | 5 | 0.518 | 0.604 | 0.540 | 0 |
| W3 | 15 | 0.518 | 0.588 | 0.529 | 0 |
| W4 | 19 | 0.503 | 0.614 | 0.561 | 0 |

**Verdict:** **NO bug, NO look-ahead.** All entries/exits/SL/TP are coherent. Confidence scores are tightly clustered in the [0.50, 0.66] range — there is no "always confident" pathology that would mask bad signals. The window 4 result (WR 10.5%, PF 0.18) is a genuine market-regime failure, not a look-ahead artifact.

---

## Task 3 — Parameter Sensitivity

**Method:** Same walk-forward setup (5 windows, full 71,747 bars), with `TTSStrategy.SWING_LOOKBACK` and `TTSStrategy.HISTORY_BARS` patched via class-attribute overrides (verified to take effect — a `HISTORY_BARS=2000` test produced 0 trades, confirming the override mechanism works).

| Config | lookback | HISTORY_BARS | Mean PF | Mean WR | Mean trades/win | Mean PnL | Windows passed |
|---|---|---|---|---|---|---|---|
| **baseline** (current Optuna) | 5 | 50 | 0.797 | 0.414 | 11.6 | -$169.20 | 0/5 |
| **lookback=10, hist=50** | 10 | 50 | **1.324** | 0.459 | 9.6 | -$49.44 | 0/5 |
| lookback=5, hist=80 | 5 | 80 | 0.797 | 0.414 | 11.6 | -$169.20 | 0/5 |
| lookback=10, hist=80 | 10 | 80 | 1.324 | 0.459 | 9.6 | -$49.44 | 0/5 |

### Key findings

1. **`lookback=10` substantially improves the strategy**: PF jumps from 0.797 → 1.324 (a 66% lift); PnL/window improves from -$169 to -$49. This is the **biggest actionable finding** — the Optuna-tuned `lookback=5` is over-fit to short-term M15 swings; widening to 10 bars (a 21-bar swing window vs 11) captures more meaningful swing structure for gold.
2. **`HISTORY_BARS` has negligible impact** between 50 and 80 — the strategy's only use of `HISTORY_BARS` is the warmup check (`if len(bars) < self.HISTORY_BARS + 1: return None`). Both values are well below the per-window test size (~10,762 bars), so the warmup completes before either threshold matters in practice.
3. **Per-window with `lookback=10`:** W0 still loses, but W1–W4 all improve. Window 4 specifically: PF 0.18 → 1.50 (small sample, n=6 trades, but the regime still flipped from "all losers" to "mixed").
4. **No configuration produces a "PF=8" result** — the strategy is currently unprofitable or marginal at best regardless of (lookback, HISTORY_BARS) in this 2×2 grid.

**Verdict:** The strategy **is** sensitive to `lookback` (and the baseline setting is sub-optimal). It is **not** sensitive to `HISTORY_BARS` in the tested range. The "PF=8" claim is not reachable by any of these parameter tweaks — it's from a non-reproducible sweep or a different strategy entirely.

---

## Cross-check against the "PF=8.02 / WR=86.3%" claim

The research doc cites `wf-revalidation-2026-07/summary.json` for the PF=8.02 result. That file's actual contents:

```json
{
  "generated_at": "2026-07-03T15:10:20.569769+00:00",
  "strategy": "SRMR+",                  ← NOT ttc_xauusd
  "n_windows": 5,
  "pairs": ["GBPUSD", "EURUSD", "USDJPY", "XAUUSD"],
  ...
  "XAUUSD": {
    "mean_profit_factor": 8.019522,     ← SRMR+ XAUUSD
    "mean_win_rate": 0.863128,          ← SRMR+ XAUUSD
    "windows_passed": 5,
    ...
  }
}
```

The detailed XAUUSD window breakdown is in `srmrplus_wf_XAUUSD.json` (file starts with `"pair": "XAUUSD"` and `"data_source": "csv_M15"`, `"bar_count": 74324`). Window 4 there is: `win_rate=1.0, profit_factor=10.0, trade_count=20, total_pnl=1391.86, regime_combined="normal_trending_down_off_hours"`.

So the **PF=8.02 / WR=86.3% / window-4 WR=100% / PF=10 anomaly** is from the **SRMR+ strategy on XAUUSD M15** (file generated 2026-07-03), not from `ttc_xauusd`. The research doc §A.1 mis-attributes it.

That said, an honest anomaly still exists: PF=10 with WR=100% across 20 trades is statistically unlikely (binomial p ≈ 0.5²⁰ ≈ 1e-6 even for a 70%-WR edge) and is exactly the kind of result that triggers a look-ahead audit. **It deserves investigation, just not under the `ttc_xauusd` name.** That audit is out of scope for this verification.

---

## Final Verdict

| Aspect | Status |
|---|---|
| Active look-ahead bug in `ttc_xauusd` | **NOT FOUND** — synthetic test passes, trade inspection passes, confidence scores are normal |
| Strategy is profitable on XAUUSD M15 | **NO** — mean PF 0.797, 0/5 windows pass go/nogo |
| Strategy is over-fit to current Optuna params | **YES** — `lookback=10` materially improves PF (0.797 → 1.324) |
| The PF=8.02 / WR=86.3% claim applies to `ttc_xauusd` | **NO** — the doc mis-attributes SRMR+ results to TTC |
| A genuine anomaly exists in the codebase | **YES, but elsewhere** — `srmrplus_wf_XAUUSD.json` window 4 (PF=10, WR=100%, 20 trades) deserves its own investigation |

### Recommendations

1. **Do not promote `ttc_xauusd` to live trading on XAUUSD M15** under the current Optuna params. It fails the SRF go/nogo gate (0/5 windows passed).
2. **Re-run Optuna with `lookback ∈ {5, 7, 10, 14}`** as a free parameter and verify the optimal value shifts to ≥10. If so, the current `lookback=5` baseline is over-fit and the 0.797 → 1.324 lift is a real signal that warrants re-tuning before any live consideration.
3. **Card the SRMR+ XAUUSD window-4 anomaly as a separate `[DEBT]`** — the PF=10 / WR=100% / 20 trades result is suspicious enough to warrant a parallel investigation, but it is not a `ttc_xauusd` issue.
4. **Fix the data loader bug in `src/forex_bot/backtest/tick_loader.py:78`** — `datetime.fromtimestamp(row["timestamp_utc"] / 1000, tz=timezone.utc)` divides by 1000 but `timestamp_utc` is already in **seconds** (per `scripts/migrate_csv_to_duckdb.py:286` "µs → s"). This causes bars loaded via `load_bars_from_db` to have 1970-epoch timestamps. The correct loader is `DbDataLoader.load_bars`. **This bug silently produces 1970-dated bars that probably break any time-of-day / session-aware logic** — including the strategy's `SessionAnalyzer` (session_state.session_name == "OUTSIDE" check). I worked around it by using `DbDataLoader` directly.

### Confidence: 4/5

Every test ran end-to-end with concrete numbers. The one point lost is for the `ttc_xauusd` strategy's complexity (swing detection, FL patterns, M/W, gate validation, confluence scoring, progressive SL, Kelly sizing) — there could be subtle interactions not exercised by my 4 tests. But none of the explicit red flags (look-ahead bug, noise-pattern detection, suspiciously high confidence, parameter overfit to a single value, suspicious price coherence) reproduce.

---

## Reproduction

Script: `scripts/verify_ttc_xauusd_verification.py`
Full machine-readable results: `/tmp/ttc_xauusd_verification.json`

```bash
cd $AYUMI_ROOT
python3 scripts/verify_ttc_xauusd_verification.py
```

End-to-end runtime: ~110s on this host (71,747-bar dataset, 5-window WF × 5 configs).
