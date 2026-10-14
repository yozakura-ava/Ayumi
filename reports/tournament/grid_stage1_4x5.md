# Sprint E — Staged Matrix Grid (4 strategies x 5 timeframes)

Generated: 2026-10-14T14:29:09.040383+00:00  ·  selection set N = 12
Thresholds frozen at commit `03bdc36b` — `docs/reports/ftmo-gate-preregistration-2026-10-13.md`

Rows are per (strategy, timeframe). `T1..T5` are the pre-registered gates;
`T6` (DSR/BH-FDR) is **UNEVALUABLE** for this run — see the G5 blocker finding.

**EXECUTION SURFACE:** node `ava-worker-local`, machine-id `cca4670d1810478e8248a31b17d06745`
(8 cores, load 8.05 at capture). Produced under `exec(host="node", node="ava-worker-local")`
with the identity probe PASSED first. No cell ran on the gateway.

**COST WARNING:** every row below carries `cost_modeled = false`. See
`docs/reports/ftmo-gate-cost-wiring-defect-2026-10-14.md`. Returns are therefore
OPTIMISTIC (costs only ever subtract), which makes the NO-GO robust but makes these
rows **not verdict-grade** — a GO could never be claimed off them.

| Strategy | TF | Return% | MaxDD% | DailyBr | Trades | T1 | T2 | T3 | T5 | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| bb_rsi_reversion | M30 | -4.47 | 5.41 | 0 | 57 | FAIL | FAIL | PASS | PASS |  |
| bb_rsi_reversion | H1 | 1.42 | 6.39 | 0 | 71 | FAIL | FAIL | PASS | PASS |  |
| donchian_atr_trend_v2 | M30 | -64.69 | 66.74 | 0 | 1273 | FAIL | FAIL | PASS | PASS |  |
| donchian_atr_trend_v2 | H1 | -25.66 | 29.35 | 0 | 520 | FAIL | FAIL | PASS | PASS |  |
| killzone_momentum | M5 | 61.41 | 23.72 | 10 | 3000 | PASS | FAIL | FAIL | PASS |  |
| killzone_momentum | M15 | -65.31 | 67.66 | 1 | 1907 | FAIL | FAIL | FAIL | PASS |  |
| killzone_momentum | M30 | -41.52 | 42.53 | 0 | 915 | FAIL | FAIL | PASS | PASS |  |
| killzone_momentum | H1 | -30.51 | 31.87 | 0 | 316 | FAIL | FAIL | PASS | PASS |  |
| srmr_plus | M5 | -5.62 | 34.21 | 6 | 1058 | FAIL | FAIL | FAIL | PASS |  |
| srmr_plus | M15 | -44.16 | 57.66 | 11 | 687 | FAIL | FAIL | FAIL | PASS |  |
| srmr_plus | M30 | -35.88 | 39.29 | 3 | 518 | FAIL | FAIL | FAIL | PASS |  |
| srmr_plus | H1 | -36.90 | 36.79 | 0 | 212 | FAIL | FAIL | PASS | PASS |  |

## Completeness

- **N = 12 of 20 staged cells (M5, M15, M30, H1). M3 leg incomplete.**
- M3 is the long pole: `bb_rsi_reversion` 73k/521,865 bars and `donchian_atr_trend_v2`
  54k/521,865 bars at capture time — multi-day ETAs, not hours. The prior "~7h M3"
  estimate was wrong by an order of magnitude.
- No zero-signal cells: every completed cell produced trades. M3 rows are absent
  because the processes are still running, not because they were skipped.

## T4 breadth (pre-registered: pass on >= 2 distinct timeframes)

| Strategy | TFs present | TFs clearing T1-T3+T5 | T4 |
|---|---|---|---|
| bb_rsi_reversion | H1, M30 | 0/2 | FAIL |
| donchian_atr_trend_v2 | H1, M30 | 0/2 | FAIL |
| killzone_momentum | H1, M15, M30, M5 | 0/4 | FAIL |
| srmr_plus | H1, M15, M30, M5 | 0/4 | FAIL |

## T6 (DSR/BH-FDR) — UNEVALUABLE, not waived

The scorecard persists aggregates only; `rank_selection_set_with_dsr` requires a
`TrialReturnStore` matrix `[T, N_trials]`. Tracked by card 8320a1e9. T6 is **not**
silently waived — it is reported as unevaluable, and the ingest tool refuses to emit
a GO without it. It is also **not load-bearing for this verdict**: no cell clears
T1-T5, so no strategy is viable regardless of the statistical pass.