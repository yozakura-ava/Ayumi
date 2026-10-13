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
| Stage 1 launched | (recorded at launch time) |
| Promotion decision | (recorded after Stage 1 completes, against P1/P2 only) |