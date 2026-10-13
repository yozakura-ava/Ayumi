# G5 Feasibility Finding — DSR/BH-FDR pass cannot consume tournament scorecards

**Card:** 37078a83-2af3-4e5c-9b6e-0ad419192c72 · **Date:** 2026-10-13 · **Status:** BLOCKER for AC3 / T6
**Context:** discovered while G4 Stage 1 was running; verified against merged code at `8bd76760` / `03bdc36b`.

---

## 1. Finding

The pre-registered gate T6 requires each candidate cell to clear
`rank_selection_set_with_dsr`. That function **cannot be run on the artifacts
`scripts/run_tournament.py` produces.** Two independent gaps:

### Gap A — the scorecard persists no per-observation series

`rank_selection_set_with_dsr` reads returns via `store.matrix(cell_key)`, which must
return a `[T, N_trials]` array of per-observation returns
(`src/forex_bot/factory/risk_adjusted_ranking.py:1273-1281`).

The scorecard JSON persists **only aggregate scalars**. Full key inventory of a real
scorecard (`scorecard_XAUUSD_H1_lead4.json`):

```
meta.harness_meta : bars_loaded, start_date, end_date, first_bar_utc, last_bar_utc,
                    raw_duckdb_rows, symbol, timeframe
meta.run_meta     : per-strategy {signals, trades, skipped}
rows[]            : strategy_id, symbol, timeframe, return_pct, max_dd_pct,
                    daily_dd_breaches, total_dd_breaches, trade_count, source, rank
```

There is no equity curve, no per-bar return, not even per-trade `pnl_fraction`.
`_simulate_trades` computes exactly that data (`harness.py:478-500`, each trade carries
`pnl_fraction`) and `build_scorecard_row` then **discards it** — only aggregates survive.

`grep -c 'TrialReturnStore\|score_selection_set\|dsr' scripts/run_tournament.py` → **0**.

### Gap B — the merged gate-6 code expects ≥2 trials per cell

`TrialReturnStore.matrix()` returns `None` when a cell holds fewer than 2 trials
(`validation_runner.py:265-280`), and `rank_from_trial_return_store` **skips** any cell
whose matrix is `None` (`risk_adjusted_ranking.py:567-570`).

So the cell/trial mapping matters and is not the obvious one:

- ❌ cell = `(strategy_id, XAUUSD, TF)` → 1 run per cell → `matrix()` returns `None` →
  **every cell is skipped** → empty ranking → DSR never runs.
- ✅ cell = `(archetype, XAUUSD, TF)` with **one trial per strategy** → 17 trials per
  timeframe cell → BH-FDR ranks strategies within each TF, and the DSR deflation
  denominator is the cross-cell total (**N = 5 × 17 = 85**), which is exactly the N
  written into the pre-registration.

The mapping in the pre-registration is therefore sound; the plumbing is what is missing.

---

## 2. What this means for the card

- **T1, T2, T3, T4, T5 are all computable from the aggregates already being produced** —
  the running matrix delivers them. Only **T6 is blocked**.
- **AC3 ("statistical pass output over the selection set, N reported") cannot be satisfied
  from the current run.** N is known (85 full / 20 staged) but the pass cannot be executed.
- This is a **pre-existing gap in the merged gate-6 work**, not something Stage 1 caused:
  `score_selection_set` was added to the harness but the standalone CLI never calls it, and
  no caller feeds it a store built from tournament output.

## 3. Options (require a decision — none is silently taken here)

**Option 1 — instrument the CLI (recommended).** `scripts/run_tournament.py` is already in
the card's `allowed_files`. Add an opt-in `--emit-returns <path>` that writes a per-bar
return series per strategy (or per-trade `pnl_fraction`), then ingest into a
`TrialReturnStore` keyed `(archetype, XAUUSD, TF)` with one trial per strategy and call
`harness.score_selection_set(...)`.
- Cost: a code change → BQES (pre-build checklist, Rin review) + a **re-run** to capture series.
- Note: `_per_bar_sharpe` expects **per-bar** returns; per-trade returns are a different
  (weaker) statistic and would need to be declared, not silently substituted.

**Option 2 — declare T6 unevaluable for this matrix.** Publish T1-T5 + the grid, and
record in the verdict memo that T6 could not be executed, with this file as the evidence.
- Consequence: the verdict cannot be a clean **GO**, because the pre-registration defines
  viability as T1–T6 *simultaneously*. Missing T6 ⇒ **NO-GO / inconclusive**, stated plainly.

**Option 3 — separate capture run.** Re-run the grid through
`front_door`/`ValidationRunner`, which already build a real `TrialReturnStore`
(`validation_runner.py:321`). Highest fidelity, most expensive.

## 4. Recommendation

Option 1 for the next card, with Option 2 recorded in this cycle's memo. Under no option
should T6 be waived silently: the whole point of merging gate 6 was that an 85-cell
selection is selection-on-the-maximum, and dropping it would return the matrix to exactly
the failure mode the pre-registration exists to prevent.