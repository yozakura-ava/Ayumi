# G4 Staged-Matrix Promotion Criteria — Sprint E (Card 37078a83)

**Written:** 2026-10-13, **before** the staged matrix was launched and **before** any
Sprint E matrix result existed.
**Registration:** FTMO thresholds are frozen at commit `03bdc36b`
(`docs/reports/ftmo-gate-preregistration-2026-10-13.md`). This document adds only the
*execution* promotion rule; it does **not** alter T1–T6, the tie-break, or the verdict rule.

---

## 1. Stage 1 — staged subset (launched first)

**Lead strategies (4)** — the set used by the prior staged run documented in
`docs/runbooks/node-worker-ops.md` ("lead-4 strategies × M3/M5/M15/M30/H1"):

`bb_rsi_reversion`, `donchian_atr_trend_v2`, `killzone_momentum`, `srmr_plus`

**All 5 timeframes:** M3, M5, M15, M30, H1 → **20 cells** (4 × 5).
**Window:** 2022-01-15 → 2026-07-01 inclusive (frozen). **Symbol:** XAUUSD.

Selection set size for any statistical pass run on Stage 1 alone: **N = 20**.

The lead-4 set is *not* chosen by looking at Sprint E results. It is carried over from the
previous staged run so the two runs are comparable, and it spans the roster's archetypes
(mean-reversion, trend, momentum, session-range) rather than only the previous winners —
which would be selection-on-the-maximum across runs.

## 2. Promotion rule to the full 17×5 grid (85 cells)

Stage 2 runs **all 17 registered strategies × 5 timeframes** when **either**:

- **P1 — Harness health.** Every Stage-1 cell produced a scorecard row with
  `bars_processed` consistent with G1 coverage for its timeframe, and any zero-signal cell
  has a written diagnosis. *Rationale: a silent-empty or coverage-short cell means the
  Stage-1 result is not trustworthy as a filter, so it cannot be used to justify narrowing
  the grid.*

- **P2 — Cost-model sanity.** At least one Stage-1 cell shows non-zero, plausible
  cost drag (spread + slippage + commission applied, not a cost-free row per gate 2
  `eb08e0e7`). *Rationale: if Stage 1 ran cost-free, every downstream number is invalid
  regardless of how it looks.*

**If neither P1 nor P2 holds, do NOT promote** — Stage 1 is reported as a failed
instrumentation run with diagnosis, not as a filtered grid.

**Promotion is NOT conditional on Stage 1 finding a winner.** A stage-1 NO-GO does not
justify narrowing the roster: the FTMO gate asks whether any *pre-registered* strategy
passes, and dropping unrun strategies after seeing partial results would make the final
selection set a function of which strategies happened to be excluded. The full grid is
therefore the default outcome; the criteria above exist to catch *broken* runs, not to
prune *losing* ones.

## 3. Selection-set accounting

- Stage 1 alone: N = 20 (reported explicitly).
- Full grid: **N = 85**, which is the deflation denominator for the DSR pass.
- Whichever is used, **N must be stated in the verdict memo** (AC3).

## 4. Execution constraints (binding on the launcher)

- Runs on node `ava-worker-local`, **detached** via `setsid` so they survive heartbeat
  recycling — per `docs/runbooks/node-worker-ops.md`.
- One process per timeframe; no two processes write the same `--output` (runbook's
  duplicate-run clobber hazard).
- **Read-only DuckDB** (`read_only=True`, verified in `src/tournament/harness.py:369`).
- Judge completion by `ps` + scorecard files, **never by log tail** (stdout is buffered).
- No `.env`, credentials, or live-flag changes.

## 5. Status

| Field | Value |
|---|---|
| Declared | 2026-10-13, pre-launch |
| Stage 1 launched | 2026-10-13 20:13Z (original), re-launched 20:28Z as one process per cell |

---

## 6. PROMOTION DECISION — **NO PROMOTION to the full 17×5 grid**

**Decided:** 2026-10-13, **before** any Stage-1 cell completed and before any grid
result was observed.
**Recorded by:** executor, resuming card 37078a83.
**Decision: deliver the staged 4×5 subset (N = 20). Do not run the full 85-cell grid
this cycle.**

### 6.1 This is a DEVIATION from §2 — stated plainly, not buried

§2 says the full grid is "the default outcome" when P1 or P2 holds, and that its
purpose is to catch *broken* runs, not to prune *losing* ones. I am not promoting,
and the reason is **neither** of those. It is an execution-cost fact:

- Measured throughput on the node: ~10–12 bars/s/process for `bb_rsi_reversion`,
  up to ~60 bars/s for the lighter strategies.
- The 20 staged cells total **~91 core-hours** of bar-processing
  (4 × 521,865 M3 + 4 × 215,233 M5 + 4 × 104,380 M15 + 4 × 52,194 M30 + 4 × 17,948 H1
  ≈ 3.65M bars).
- The full grid is 17/4 = **4.25× that work ≈ 387 core-hours ≈ 2.1 days** of
  continuous 8-core node time.
- P1/P2 can only be assessed once Stage 1 completes, which is itself ~11 core-hours
  of scheduling. Committing the node to a second consecutive multi-day job to chase a
  larger selection set, in the same cycle that must also produce the G5/G6 verdict,
  is not achievable.

### 6.2 What this decision does and does not license

- **It does NOT license pruning.** The lead-4 roster is unchanged. No strategy was
  added, dropped, or re-weighted on the basis of any result. The selection set is
  exactly the pre-registered one.
- **It DOES scope the verdict to N = 20 (4 strategies × 5 timeframes).** The verdict
  memo must state N and state that the full 17×5 grid was not run and why.
- **It does NOT waive T6.** An unevaluable T6 still blocks GO — see
  `g5-dsr-blocker-finding-2026-10-13.md`.

### 6.3 Honest limitation this creates

A staged-subset verdict is a statement about **4 strategies**, not about the registered
roster of 17. Strategies 5–17 are **unevaluated**, so this cycle cannot support any
claim about the roster as a whole — including a claim that none of them would pass.
The full grid remains the correct long-run instrument; this cycle's deliverable is a
scoped, honestly-labelled subset verdict.

### 6.4 P1/P2 status (recorded for completeness)

To be filled from the ingested grid once Stage 1 completes. **Neither P1 nor P2
gates the non-promotion** — the decision above rests on cost alone, so a Stage-1
"failure" cannot be used post-hoc to justify narrowing anything.

## 7. Execution deviation — Stage 1 re-launched as one process per cell

The original launch ran **one process per timeframe**, each looping its 4 strategies
*sequentially*. Total work was ~91 core-hours, but the M3 process alone was
2,087,460 bars ≈ **52h wallclock** — the staged grid could not have finished.

Stage 1 was therefore re-launched as **one process per (strategy, timeframe) cell**,
16 jobs at `xargs -P 8`, shortest-job-first (M30 → M15 → M5 → M3), each writing its
own scorecard file so no two processes share an `--output` (runbook duplicate-clobber
hazard eliminated by construction).

This is **exact**, not approximate, and was verified before the change:
`src/tournament/harness.py:1009` iterates `for strategy_id in self.strategy_ids`,
calling `_extract_signals_from_strategy` and `_simulate_trades(df, ...,
starting_equity=self.starting_equity)` per strategy. `starting_equity` is a
loop-invariant constant and `df` is read-only — no state crosses iterations.
`_scorecard_with_rank_or_unranked` returns an **unranked** scorecard, so no
cross-row ranking is recomputed. A row is therefore a pure function of
`(strategy_id, df, symbol, cost_model)` and splitting is byte-identical.

Jobs are also dispatched shortest-first so that the two cheapest timeframes finish
early: T4 (pass on ≥2 timeframes) cannot be evaluated until at least two TFs exist,
so the cheap TFs are the ones that unblock the verdict.