# Sprint E — cost-model wiring defect: `run_tournament.py` produces cost-free rows

**Card:** 37078a83-2af3-4e5c-9b6e-0ad419192c72
**Found:** 2026-10-14, during G4 ingest of the staged matrix
**Severity:** HIGH — silently invalidates every verdict-grade row produced by the
only tournament entrypoint the Sprint E cards use.

## Finding

Gate 2 (card `c0f284b8`, merged `eb08e0e7`) built the FTMO cost model into the
harness and added a fail-closed `cost_modeled` flag on every row. The harness is
correct. **The CLI entrypoint never wires it up.**

- `scripts/run_tournament.py:211` constructs `TournamentHarness(...)` with **no
  `cost_model=` argument**.
- `TournamentHarness.cost_model` defaults to `None` (`src/tournament/harness.py:944`).
- With `cost_model is None`, `_simulate_trades` sets `cost_fraction = None` and
  `cost_modeled = False` for every trade (`harness.py:526-535`), so
  `pnl_fraction_net == pnl_fraction` — **costs are never subtracted**.
- `grep -c 'cost_model\|COST_MODEL\|spread' scripts/run_tournament.py` → **0 hits**.

`cost_model_for(symbol)` exists and is correct (`harness.py:868`) and reads the
single sourced Liora table — but **nothing in the `run_tournament.py` path calls it.**

## Evidence

All 12 completed staged cells emit `"cost_modeled": false`, e.g.
`data/tournament/sprintE/scorecard_stage1_XAUUSD_H1.json`. Aggregation over the
full staged set returns `all rows cost_modeled: {False}`.

## Why this matters beyond this card

Gate 2's stated purpose was to *remove the cost-free-row fallback*. It removed the
fallback at the harness layer but left the sole production entrypoint defaulting to
`None` — which re-creates exactly the defect gate 2 was filed to kill. The
`cost_modeled` flag does its job as a tripwire: it did not let the rows pass
unnoticed. That is the design working. But the wiring gap is real and gate 2's AC
is not fully met in practice.

## Direction of the bias

Costs only ever **subtract** from `pnl_fraction`. So every return in the grid is an
**upper bound** on the true cost-adjusted return.

- For a **NO-GO** this is sound: a cost-free return that already fails T1 cannot be
  rescued by subtracting costs. The verdict is robust to this defect.
- For a **GO** it would be unsound: a cost-free row could cross +10% and then fail
  once costs are applied. No GO may be claimed from rows carrying
  `cost_modeled = false`.

## Fix

One line at `scripts/run_tournament.py:211` — pass `cost_model=cost_model_for(args.symbol)`.
`cost_model_for` returns `None` for symbols outside the Liora table (by design), so
those runs stay flagged `cost_modeled = false` and remain correctly excluded.

Do **not** make cost modeling unconditional or default-on: `None` for an un-costed
symbol is the intended fail-closed state, not a bug to paper over.