# FTMO Verdict Memo — Sprint E Staged Matrix

**Card:** 37078a83-2af3-4e5c-9b6e-0ad419192c72
**Date:** 2026-10-14
**Thresholds:** pre-registered at `docs/reports/ftmo-gate-preregistration-2026-10-13.md`,
frozen in commit `03bdc36b` — **before** any cell ran and before any result was observed.
**Grid:** `reports/tournament/grid_stage1_4x5.md`

---

# VERDICT: **NO-GO**

No strategy in the selection set is FTMO-viable. Stated plainly, because the card
names NO-GO as a valid completing deliverable.

**This verdict does not depend on T6.** Zero of 12 cells clear T1-T5, and FTMO
viability requires T1-T6 *simultaneously*. With no cell clearing even the
computable thresholds, no amount of statistical rescue can produce a viable
strategy. T6 is reported UNEVALUABLE (see below) — it is **not** load-bearing here,
and it is **not** being waived.

---

## 1. What the grid says

Selection set **N = 12** cells (4 strategies x 4 timeframes; M5/M15/M30/H1), of 20 staged.

| Threshold | Result |
|---|---|
| T1 total return >= +10.0% | **1 of 12 pass** — `killzone_momentum` M5 at **+61.41%** |
| T2 max daily DD <= 3.0% | **0 of 12 pass** — best is 5.41% (`bb_rsi_reversion` M30) |
| T3 daily-DD breaches == 0 | **6 of 12 fail** |
| T4 pass on >= 2 distinct TFs | **0 of 4 strategies pass** (0/2, 0/2, 0/4, 0/4) |
| T5 >= 30 closed trades | **12 of 12 pass** (range 57–3000) |
| T6 DSR/BH-FDR @ alpha=0.05 | **UNEVALUABLE** |

**The single T1-passing cell fails on every other axis.** `killzone_momentum` M5
clears +10% by a wide margin but carries **23.72% max daily drawdown** (T2 limit 3.0%
— over by ~8x) and **10 daily-DD breaches** (T3 limit 0). Under the pre-registration's
own derivation, a daily-DD breach is disqualifying rather than a margin: FTMO terminates
at the limit. A +61% return earned through a 23.7% daily drawdown is not a pass.

**T2 is the binding constraint, and it is absolute.** Not one of 12 cells holds daily
drawdown under 3.0%. Twelve for twelve is not a near-miss distribution; it says the
harness's 1R = 0.5% risk sizing combined with these strategies' signal quality produces
drawdowns an order of magnitude past the FTMO limit. No amount of further compute on
this roster changes that.

## 2. T6 — UNEVALUABLE, not waived

The scorecard persists aggregates only. `rank_selection_set_with_dsr` requires a
`TrialReturnStore` matrix `[T, N_trials]` (per-bar or per-trial return series), which
does not exist in the scorecard format. Two gaps, documented at
`docs/reports/g5-dsr-blocker-finding-2026-10-13.md`, tracked for build by card
`8320a1e9-2cd5-4645-9a30-17d53baca334`.

The ingest tool **refuses to emit a GO without T6**; it returned `NO-GO` with
`T6_state=UNEVALUABLE` rather than degrading T6 to a pass. That is the guard working.
T6 remains open and must be evaluated before any future GO.

## 3. Cost-model defect — found during ingest, direction of bias is favourable here

**Every one of the 12 rows carries `cost_modeled: false`.** Root cause:
`scripts/run_tournament.py:211` constructs `TournamentHarness(...)` without a
`cost_model=` argument, so it defaults to `None` and costs are never subtracted.
`cost_model_for(symbol)` exists and is correct but is never called from this path.
Full analysis: `docs/reports/ftmo-gate-cost-wiring-defect-2026-10-14.md`.

**Consequence for this verdict: none — it strengthens it.** Costs only ever subtract,
so every return in the grid is an **upper bound**. A cost-free return that already
fails T1 cannot be rescued by adding costs. The NO-GO is robust to the defect.

**Consequence for the rows themselves: they are not verdict-grade.** Had any cell
passed, it still could not have supported a GO without cost modeling. Gate 2's
stated goal — removing the cost-free-row fallback — is **not fully met**: the
fallback was removed at the harness layer but the production entrypoint still
defaults to the unmodelled path. Filed as a follow-on defect, not silently absorbed.

## 4. Provenance and honesty notes

- **Execution surface is genuine.** Identity probe run first per
  `docs/runbooks/ava-worker-local-node.md`: `host="node", node="ava-worker-local"`
  printed the `Node:` header and returned machine-id `cca4670d…` (node). Gateway
  baseline is `5b247455…` (xeon) and was measured separately. No cell ran on the
  gateway.
- **The scorecards in `data/tournament/sprintE/` are node-produced, not fallback
  artifacts.** Verified: the gateway has **no** `data/tournament/sprintE/` directory
  at all, the 8 live processes are node-resident, and node load was 8.05 on 8 cores
  with distinct `--output` per process. The gateway does have an unrelated stale
  `/opt/ayumi-tournament/ayumi` (dated Oct 4, not a git repo) — path existence alone
  is not residency evidence, which is why the check was done on processes, not paths.
- **G3 cross-machine parity is WITHDRAWN and NOT re-run here.** Requirement 3 of the
  2026-10-14 standing rules demands a genuine cross-machine re-run before any grid
  result is cited as cross-machine evidence. Requirement 5 forbids tournament compute
  on xeon. **These two requirements are in direct tension: a cross-machine parity
  check requires running on both machines.** This memo does not claim cross-machine
  parity evidence, and the grid is therefore cited as **node-only single-surface**
  evidence. Resolving this tension is an authority question, not mine to paper over.
  See the escalation note on the card.
- **M3 leg incomplete.** N=12, not 20. The prior "~7h M3 long pole" estimate was wrong
  by an order of magnitude: measured progress is ~2.7k bars/h for `bb_rsi_reversion`
  against a 521,865-bar target — a multi-day ETA, not hours. The M3 cells are absent
  because the processes are still running, **not** because they were skipped.
- **Promotion decision unchanged and not result-conditioned.** NO promotion to the
  full 17x5 grid, declared pre-launch on measured cost, roster unpruned. **This memo
  makes no claim about strategies 5–17.**
- **The grid is staged, not the full 17x5.** AC2 is met in its declared alternative
  form ("staged subset + written promotion decision"), not in full.

## 5. Recommendation

**Do not pursue FTMO viability on this roster.** T2 (max daily DD <= 3.0%) failed in
12 of 12 cells across 4 strategies and 4 timeframes. That is a structural signal about
risk sizing and strategy behavior, not a tuning gap. The correct next step is to fix
the risk model (position sizing relative to the FTMO daily limit), not to run more
cells. Running the remaining 8 M3 cells and the full 17x5 grid would consume ~400
core-hours to add M3 data that cannot overturn a 12-for-12 T2 failure.

Before any future cycle can return a GO, three things must land:
1. **Cost wiring fix** (`run_tournament.py:211`) — otherwise no row is verdict-grade.
2. **DSR series persistence** (card `8320a1e9`) — otherwise T6 is unevaluable.
3. **A ruling on cross-machine parity vs the never-compute-on-xeon rule.**

---

## Verdict machine-readable

```
verdict          = NO-GO
selection_set_N  = 12
expected_cells   = 20
complete         = false
tfs_present      = [M5, M15, M30, H1]
T1 >= +10.0%     = 1/12 pass (killzone_momentum M5, +61.41%)
T2 <= 3.0% DD    = 0/12 pass
T3 == 0 breaches = 6/12 fail
T4 >= 2 TFs      = 0/4 strategies pass
T5 >= 30 trades  = 12/12 pass
T6 DSR/BH-FDR    = UNEVALUABLE (not waived, not load-bearing)
cost_modeled     = false on 12/12 rows (bias is favourable: upper-bound returns)
surface          = node ava-worker-local, machine-id cca4670d...
```