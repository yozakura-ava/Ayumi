# Confidence Unification — Shared Scoring Abstraction Spec

**Version:** 1.1 (Rin delta review)
**Date:** 2026-10-14
**Author:** Reina (Sprint Orchestrator)
**Card:** df08ce02-0da9-4f52-85cf-e3c2fc845737
**Status:** SPEC ONLY — no implementation code. Rin review required before any implementation card is cut.
**Authority:** Craig ruling Q3 (FULL unification), grilling tree `data/sprint-plans/ayumi-PROJECT-2026-10-14-grilling.json`; Q2 (PBO 0.30 STRICT + tier labels).

**Change log — v1.0 → v1.1** (Rin verdict **REWORK**, runId `2d67f21e`, full verdict preserved at
`docs/audits/rin-review-confidence-unification-spec-2026-10-14.md`):

| # | Sev | Issue | Fix in v1.1 |
|---|-----|-------|--------------|
| C1 | CRITICAL | Founding audit citation `docs/audits/ayumi-audit-2026-10-14.md` does not exist in this repo | §1 now cites the real location (Ava workspace) and records that the path was propagated from the card notes, not independently verified |
| C2 | CRITICAL | §5.3 block-rate formula inverted (computed the *pass* rate) | §5.3 corrected to `len(gates_failed)/(passed+failed)` |
| C3 | CRITICAL | §7.2 schema missing 9 of the fields §4 declares, so it cannot satisfy §7.3 or §4.6(a) | §7.2 rewritten to full 1:1 coverage + new §7.2.1 mapping table proving coverage |
| M1 | MEDIUM | Blocked-impostor re-emerges at the aggregation layer | §5.3 + §7.2 split last/mean-unblocked, added `n_signals_scored` + `signal_block_rate` |
| M2 | MEDIUM | `pbo_tier: str` erased the type-level guard §5.1 mandates | §7.2 declares `TierLabel`, not `str` |
| M3 | MEDIUM | Dangling refs G2→§3.1, G6→§3.2, G4→§3.3 (§3 has no subsections) | §2 goal table re-pointed at real sections |
| M4 | MEDIUM | §6.2 ordering key was not a total order | §6.2 rewritten as an explicit total order |
| M5 | MEDIUM | Third conflation path missed: `dsr` vs `dsr_pvalue`; `pbo_score=None` collapses 3 causes | §4.6 + §5.1 split both; added `pbo_absent_cause` |
| M6 | MEDIUM | §7.4 "advisory" had no trip-wire and could persist forever | §7.4 gained a hard trip-wire with a named owner and expiry card |
| M7 | MEDIUM | §6.1 leaves ranking un-unified by default while Q3 names ranking | §6.1 now states the deferral explicitly with an exit criterion |
| M8 | MEDIUM | Migration item had no owner, no grep-guard, no test | §8 M3 gained all three |
| m1–m7 | MINOR | Imprecise `:~412`; §11 grep claim overstated; name divergence; `dsr` not-computed; legacy banner; `validity_source` union leak; M4 false-negative rate | Fixed across §4.5, §5.1, §7.2, §7.3, §8, §9, §11 |

**Rin UPHELD unchanged in v1.1** (deliberately not touched): §5.2 boundary resolution, single-source tier
function with no delegation to `is_overfit`, §4.3/§4.6 blocked-impostor + `dsr_pvalue` defences, §6.1
default-ranking freeze, §7.4 advisory premise, M6 no-inference-backfill.

---

## 1. Problem statement

The 2026-10-14 focused audit, finding C1 (still-open), established that Ayumi carries **three
disjoint quantities called "confidence"**, none of which reaches a tournament scorecard:

> **C1 provenance correction (v1.1).** The audit is **not in this repo.** Verified: `docs/audits/` in
> `/home/TacoPants/projects/Ayumi` contains no `2026-10-14` file other than Rin's review, and
> `git log --all -- docs/audits/ayumi-audit-2026-10-14.md` returns nothing — the path has never existed in
> this repository. The audit lives in **Ava's workspace**:
> `/root/.openclaw/workspace/docs/audits/ayumi-audit-2026-10-14.md` (12529 bytes).
> v1.0 cited it as a repo-relative path; that string originated in **card df08ce02's own notes** (Ava's
> card creation) and was propagated into the spec without a path check. Every *substantive* claim below
> was re-verified independently against the source tree and stands.

| # | Quantity | Producer (verified path) | Current consumer |
|---|----------|-------------------------|------------------|
| 1 | Signal-time confidence | `src/forex_bot/confidence/engine.py` → `ConfidenceResult` | `ProfileRouter` (live routing). **Zero** imports from `src/tournament/`, `reports/tournament/`, `scripts/run_tournament.py` (grep-verified empty). |
| 2 | ML meta-confidence | `src/forex_bot/factory/meta_labeling.py` → `MetaTradeContext.primary_confidence`, `predict_meta_probability()` | Built; not wired to verdicts or scorecards. |
| 3 | Backtest validity (PBO/CPCV/DSR) | `src/forex_bot/srf/pbo.py` → `PBOScore`; `src/forex_bot/backtest/cpcv.py` → `CPCVResult`/`CPCVPBOScore`/`CPCVDSRScore` | `src/forex_bot/factory/validation_runner.py` (`ValidationVerdict.pbo_score`), factory promotion only. Absent from scorecards. |

**The conflation risk Craig accepted.** Once all three are printed in the same table, a reader will
compare them as if they share a scale. They do not:

- Signal-time confidence is a **score in [0, 1] produced per live bar**, where **higher = better**.
- Meta-confidence is a **calibrated probability in [0, 1] produced per trade**, where **higher = better**.
- PBO is an **overfit probability in [0, 1] produced per study**, where **higher = worse**.

A PBO of `0.30` and a signal confidence of `0.30` are opposite-signed quantities that happen to share a
numeric range. **This spec's entire purpose is to make that impossible to conflate at the type and
schema level, not merely at the documentation level.**

### 1.1 Accepted-risk statement

Craig accepted the conflation risk (Q3) in exchange for the unification. The mitigation is not a warning
label on a table — it is that **no consumer of these values may ever receive them as a single
untyped `float` named "confidence".** Every field in this spec is named for what it measures, carries
its own units, and carries its own missing-data sentinel. See §3.

---

## 2. Design goals

| # | Goal | Verified against |
|---|------|-------------------|
| G1 | Three distinct typed axes behind one shared interface | §4 |
| G2 | No silent conflation: no single scalar named `confidence` exists in the shared interface | §4.2 (no `score`/`confidence`/`composite` field on `UnifiedCellScore`), §4.3–§4.5 (per-axis fields) |
| G3 | Existing scorecards, ranking, and verdict memo keep working **unchanged** | §6.1, §7.1 |
| G4 | Missing data is distinguishable from zero and from a computed value | §5.1 (three-state rule), §5.3 (aggregation exclusion) |
| G5 | PBO tier labels (Q2) are part of the scoring vocabulary, not a side table | §5.2 |
| G6 | Every field has exactly one producer and a named set of consumers | §4.3–§4.6 (producers), §7.2.1 (1:1 mapping) |

## 3. Non-goals

Explicitly **out of scope** for this spec:

1. **No implementation.** No Python in this document is normative for shipping; §4 pseudotypes are shape
   contracts only. Implementation decomposes into separate cards (§9).
2. **No change to `srf/pbo.py` or `backtest/cpcv.py` algorithms.** CSCV, CPCV splits, purge/embargo, and
   DSR computation are untouched. This spec adds *labels* and *transport*, not statistics.
3. **No live-trading wiring.** `ProfileRouter` routing thresholds are unchanged. Signal-time confidence
   enters the **scorecard/ranking/memo** path only, never the execution path.
4. **No retrospective re-verdict of Sprint E.** The Sprint E NO-GO
   (`docs/reports/ftmo-verdict-memo-sprint-e-2026-10-14.md`) stands on its own; it was produced without
   these axes and is not reopened by this spec.
5. **No change to pre-registered FTMO gates T1–T6** (`reports/tournament/ingest_and_verdict.py:35-39`,
   frozen at commit `03bdc36b`). Adding axes does not add, remove, or retune a gate. A future card may
   propose a gate, under the pre-registration discipline.
6. **No `cost_sensitivity` unification.** `ValidationVerdict.cost_sensitivity`
   (`src/forex_bot/factory/validation_runner.py:205`) is a gross-vs-spread penalty ratio — a *cost*
   quantity, not a confidence or validity quantity. It is referenced here (§4.2, sibling field) and stays
   where it is.
7. **No per-trade PBO.** PBO is a study/cell-level statistic. It is never attached to an individual trade.
8. **No backfill of historical scorecards by inference.** See §7.

---

## 4. The shared abstraction

### 4.1 Module placement and import convention

**Verified import convention:** `src/forex_bot/confidence/__init__.py:3` declares
`from confidence.engine import ConfidenceEngine, ConfidenceResult` — a **top-level** import resolved with
`src/forex_bot/` on `sys.path`. Live-checked:

```
$ python3 -c "import sys; sys.path.insert(0,'src/forex_bot'); \
  from confidence.engine import ConfidenceEngine; \
  print(ConfidenceEngine().score(0.7, symbol='XAUUSD', direction='long', spread=1.0, atr=2.0, hour_utc=14).final_score)"
0.7
```

Consequently `import forex_bot.confidence` **fails** (`ModuleNotFoundError: No module named 'confidence'`
when only `src/` is on the path). This is a path convention, not a defect — tests
(`tests/unit/risk/test_confidence_engine.py:31`) use the same top-level form. **The new module must use
the top-level form to match `src/tournament/` and `src/forex_bot/` consumers, and must not "fix"
`confidence/__init__.py` in passing** (out of scope, and it would break the 173-test suite's convention).

Proposed path: `src/forex_bot/scoring/__init__.py` and `src/forex_bot/scoring/unified_score.py`,
imported as `from scoring.unified_score import ...` by tournament consumers.

### 4.2 The three axes

One frozen record per `(strategy_id, symbol, timeframe)` **cell**. Frozen because every field is a
recorded observation, never mutated after emission.

```
UnifiedCellScore
├── signal_axis   : SignalAxis     # §4.3
├── meta_axis     : MetaAxis       # §4.4
└── validity_axis : ValidityAxis   # §4.5
```

**Type declarations (v1.2, Rin R5).** These are referenced as types in §7.2 but must be *declared*, or
§5.1's "the compiler catches it" guarantee has no spec-level source. `NOT_APPLICABLE` is a **member of
`TierLabel`** — §6.2's `tier_rank` assigns it rank 2, which is impossible if it is a bare string:

```
TierLabel      := "PROMOTE" | "CONDITIONAL" | "REJECT" | "NOT_APPLICABLE"   # S5.2 vocabulary
ValiditySource := "cscv" | "cpcv" | "NOT_APPLICABLE"                        # NOT a free str union
```

**`UnifiedCellScore` is a container, not a number.** There is deliberately no `score: float`, no
`confidence: float`, no `composite: float` on this record. Any single-scalar reduction is a *derived,
named* field produced by an explicit policy (§6), and must be labelled with its formula.

### 4.3 `SignalAxis` — signal-time confidence

| Property | Value |
|----------|-------|
| **Producer** | `ConfidenceEngine.score()` → `ConfidenceResult`, `src/forex_bot/confidence/engine.py:20-30`, `:59` |
| **Units** | Dimensionless score, `[0.0, 1.0]` |
| **Direction** | Higher = more confident |
| **Resolution** | Per live bar / per candidate setup (signal time) |
| **Consumers (post-spec)** | Scorecard column, memo line. **Not** the promotion gate on its own (§6.3). |

Fields (all mirroring `ConfidenceResult` exactly — no invented fields):

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `final_score` | `float` | `ConfidenceResult.final_score` | **Ambiguous on its own — must never be read alone.** |
| `strategy_score` | `float` | `ConfidenceResult.strategy_score` | Post-clamp, pre-boost. |
| `confluence_boost` | `float` | `ConfidenceResult.confluence_boost` | Additive term. |
| `gates_passed` | `list[str]` | `ConfidenceResult.gates_passed` | Gate names. |
| `gates_failed` | `list[str]` | `ConfidenceResult.gates_failed` | Gate names. |
| `blocked` | `bool` | `ConfidenceResult.blocked` | **Mandatory companion to `final_score`.** |
| `block_reason` | `str` | `ConfidenceResult.block_reason` | Human-readable. |

> **Critical grounding note (audit lesson applied).** `ConfidenceEngine.score()` returns
> `ConfidenceResult(final_score=0.0, ..., blocked=True, block_reason=...)` **immediately** on the first
> failing gate (`engine.py`, Layer 3 loop). Therefore **`final_score == 0.0` does not mean "zero
> confidence"** — it means "a gate blocked this signal." Any consumer that reads `final_score` without
> also reading `blocked` will report every gated signal as maximally unconfident. `blocked` and
> `gates_failed` are **required** fields on this axis for exactly this reason. A proposed
> `SignalAxis.final_score_is_blocked_impostor` convenience property MAY exist but MUST be implemented as
> `blocked or final_score == 0.0`, never as a bare read.

### 4.4 `MetaAxis` — ML meta-confidence

| Property | Value |
|----------|-------|
| **Producer** | `CalibratedMetaClassifier` / `predict_meta_probability()`, `src/forex_bot/factory/meta_labeling.py:389`, `:636` |
| **Units** | Calibrated probability `P(win | features)`, `[0.0, 1.0]` |
| **Direction** | Higher = more likely to win |
| **Resolution** | Per trade (primary model's trade context) → aggregated to cell |
| **Calibration** | `evaluate_meta_label_calibration()` (`meta_labeling.py:656`) returns Brier + reliability/resolution. **A calibrated probability without its calibration evidence is not reportable** — see `brier_score` below. |

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `meta_probability_mean` | `float` | mean of `predict_meta_probability()` over the cell's trades | Aggregation detail in §5.3. |
| `meta_probability_p10` | `float` | 10th percentile | Tail-risk view; a mean of 0.8 with p10 of 0.35 is a different story from p10 of 0.75. |
| `n_trades_scored` | `int` | `len(trades)` | Small-n warning threshold in §5.3. |
| `brier_score` | `float` | `evaluate_meta_label_calibration()["brier"]` | **Required** alongside any reported probability. |
| `primary_confidence_mean` | `float` | mean of `MetaTradeContext.primary_confidence` (`:220`) | The *primary* model's own confidence, i.e. the model's opinion of its own trade, not a calibration artifact. **Distinct from `meta_probability_mean` and must not be substituted for it.** |

### 4.5 `ValidityAxis` — PBO / CPCV / DSR

| Property | Value |
|----------|-------|
| **Producers** | `PBOScore` (`src/forex_bot/srf/pbo.py:35`, `compute_pbo` `:80`); `CPCVResult` / `CPCVPBOScore` (`:205`) / `CPCVDSRScore` (`:233`) (`src/forex_bot/backtest/cpcv.py`); `ValidationVerdict` (`src/forex_bot/factory/validation_runner.py:171`) |
| **Units** | Dimensionless probability `[0.0, 1.0]` for `pbo`; p-value for `dsr_pvalue` |
| **Direction** | **`pbo` higher = WORSE.** `dsr_pvalue` higher = better. **Opposite signs inside one axis.** |
| **Resolution** | Per study / per cell (matrix of Optuna trials), **never per trade** |

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `pbo` | `float` | `PBOScore.pbo` / `CPCVPBOScore.pbo` | Overfit probability. |
| `pbo_ci_lower` / `pbo_ci_upper` | `float` | `PBOScore.ci_lower` / `.ci_upper` | 95% CI. Available on `PBOScore`; `CPCVPBOScore` carries **no CI** — see §4.6. |
| `pbo_tier` | `TierLabel` | **New** — computed by this spec, §5.2 | The Q2 vocabulary. |
| `dsr` | `float` | `CPCVDSRScore.dsr` | Probability of non-superiority. |
| `dsr_pvalue` | `float` | `ValidationVerdict.dsr_pvalue` | Default `1.0` in that dataclass — **see §4.6 warning**. |
| `n_trials_cell` | `int` | `n_trials_used` / `CPCVPBOScore.n_strategies` | Below 2 trials → PBO `NOT_APPLICABLE`. |
| `cpcv_n_paths` | `int` | `CPCVResult.n_paths()` | |
| `validity_source` | `"cscv" \| "cpcv"` | provenance tag | Which machinery produced the numbers. |

**Sibling field (not part of the triple):** `cost_sensitivity: float | None` —
`ValidationVerdict.cost_sensitivity` (`:205`), gross-vs-spread cumulative penalty ratio. It travels in
the scorecard as its own column and is **never** folded into any confidence aggregate.

### 4.6 Four verified footguns this spec must route around

**(a) `ValidationVerdict.dsr_pvalue` defaults to `1.0`, not `None`.**
`validation_runner.py:195` — `dsr_pvalue: float = 1.0`. A verdict that never computed DSR and a verdict
that computed DSR=1.0 are **indistinguishable** in that dataclass. By contrast
`pbo_score: float | None = None` is correct. Therefore: the unified schema **must** add an explicit
`dsr_computed: bool`, and the migration (§7) must treat a legacy `dsr_pvalue == 1.0` as
`NOT_APPLICABLE`, not as a computed result. Flagged for the implementation card as a **source fix**,
not a spec fix.

**(b) `ValidationVerdict.pbo_tier_ceiling` defaults to `"N/A"`, not `"NOT_APPLICABLE"`.**
`validation_runner.py:200` — `pbo_tier_ceiling: str = "N/A"`, while the module constant is
`PBO_CEILING_NOT_APPLICABLE = "NOT_APPLICABLE"` (`:116`). Two sentinel spellings for one condition
inside the same file. This spec standardises on **`"NOT_APPLICABLE"`** (matching
`cpcv.py:98 CPCV_NOT_APPLICABLE`, which explicitly documents the mirror at `:97`) and files the
`"N/A"` default as an implementation-card correction.

**(c) `dsr` and `dsr_pvalue` are near-identical names for two different quantities (v1.1, Rin M5).**
`CPCVDSRScore.dsr` (`backtest/cpcv.py:233`) is a **probability of non-superiority** computed from the
per-path best-IS OOS Sharpe distribution. `ValidationVerdict.dsr_pvalue` (`validation_runner.py:195`)
is a **p-value** from a different code path. Same prefix, different producer, different interpretation,
and only `dsr_pvalue` had a not-computed guard — so `dsr` could render its dataclass default of `0.0`
as if it were a real measurement. Mitigation: the unified axis carries **both, separately named, each
with its own computed flag** (`dsr_value` + `dsr_computed`, `dsr_pvalue` + `dsr_pvalue_computed` —
§7.2). Neither may be substituted for the other, and neither may print under a heading that does not
name which one it is.

**(d) `pbo_score = None` collapses three distinct causes into one (v1.1, Rin M5).**
`validation_runner.py:199` — `pbo_score: float | None = None`. `None` currently means any of:
(i) not Optuna-derived, (ii) insufficient data, (iii) fewer than 2 trials recorded. Those demand
different responses (skip / re-run with more data / accept as not-applicable), so the unified schema
adds a sibling `pbo_absent_cause` carrying the specific reason, with `"PBO_PRESENT"` when a value
exists. A consumer must read `pbo_absent_cause` before deciding what `None` means.

---

## 5. Missing-data, sentinels, and the tier vocabulary

### 5.1 Sentinel rules

Three disjoint states must be representable for every numeric field. **Two of them must never be
collapsed.**

| State | Representation | Meaning | Consumer rule |
|-------|----------------|---------|---------------|
| **Computed** | a real `float` in range | The value was computed | Usable in arithmetic and ordering |
| **Absent** | `None` | The producer ran and produced nothing applicable | Never coerced to `0.0`; excluded from aggregates (§5.3). **The reason MUST be carried** — see `pbo_absent_cause` (§4.6d), because `None` collapses several distinct causes |
| **Not applicable** | the sentinel string `"NOT_APPLICABLE"` | The quantity is **meaningless here** (e.g. PBO for a cell with <2 trials) | Never coerced; **never ordered**; prints literally |

`"NOT_APPLICABLE"` is an existing, already-mirrored convention:
`factory/validation_runner.py:116` (`PBO_CEILING_NOT_APPLICABLE`) and
`backtest/cpcv.py:98` (`CPCV_NOT_APPLICABLE`, documented at `:97` as mirroring the former).
This spec adopts it rather than inventing a third sentinel.

**Ban:** `float | str` unions where the string is `NOT_APPLICABLE`, and any consumer that does
`float(x)` without first testing the sentinel. The implementation must type the tier as a distinct
`TierLabel` so the compiler/validator catches it.

**Third state, made explicit (v1.1, Rin M5).** `Absent` and `Not applicable` are both "no number" but
are **not** the same state: `Absent` means *we tried and got nothing*, `Not applicable` means *the
question does not apply*. Every `None` field in §7.2 therefore has a companion `*_absent_cause`
enumerating which of the producer's failure modes produced it. A row carrying an `Absent` field without
a cause is **malformed** and must fail validation rather than render.

### 5.2 PBO tier vocabulary (Craig Q2 — baked in, non-negotiable)

```
pbo_tier(pbo)  :=
    "PROMOTE"     if  pbo <  0.30
    "CONDITIONAL" if  0.30 <= pbo < 0.50
    "REJECT"      if  0.50 <= pbo <= 1.0
```

Threshold constants: `PBO_TIER_PROMOTE_MAX = 0.30`, `PBO_TIER_CONDITIONAL_MAX = 0.50`.

**Boundary resolution (spec decision, flagged for Rin).** Craig's ruling as recorded —
"promote <0.30 / conditional 0.30–0.50 / reject ≥0.50" — **overlaps at exactly 0.50**, which sits in both
the conditional and reject ranges. This spec resolves it as **half-open intervals, reject wins at 0.50**,
because `≥0.50` is the one bound Craig stated with an explicit inclusive operator, and because a cell at
the exact reject threshold must not be recoverable as "conditional". Therefore:
`pbo == 0.50 → "REJECT"`. Recorded here as an explicit ruling rather than an implicit convention;
Rin should confirm or overturn.

**Hard requirement — this function must NOT delegate to the existing default.**
`PBOScore.is_overfit(threshold: float = 0.5)` at `srf/pbo.py:47` defaults to `0.5` and is exactly the
silent-governance path Craig's ruling forbids. Two distinct defects:

1. **Wrong threshold** (0.5, not 0.30) and **no tiers at all** (bool, not 3-label).
2. **Wrong comparison operator.** `is_overfit` returns `self.pbo > threshold` — **strict `>`**. At
   `pbo == 0.5` it returns `False` (not overfit), while Q2 says `≥0.50` is REJECT.

`CPCVPBOScore.is_overfit` (`cpcv.py:~216`) has the identical `> 0.5` default and the identical defect.
The unified tier function must be a **new, single-source-of-truth** implementation; both existing
`is_overfit` methods must be left in place for existing callers (§8, M3) but **must not be called from
the new scoring path**.

### 5.3 Aggregation rules

**Per-axis aggregation from many to one cell:**

| Axis | Aggregator | Guard |
|------|-----------|-------|
| `SignalAxis` | **Three separate outputs, never combined into one field**: (1) `final_score_last` — last signal in the window; (2) `final_score_mean_unblocked` — mean over signals with `blocked is False`; (3) `block_rate` | Blocked signals are **excluded from (2)** — their `0.0` is a gate outcome, not a confidence measurement (§4.3) — but **counted in (3)**'s denominator. `n_signals_scored` records how many fed (2); `n_signals_blocked` how many were excluded. If **every** signal was blocked, (2) is `None`/`NOT_APPLICABLE`, **never `0.0`** |
| `MetaAxis` | mean + p10 of `predict_meta_probability()` | Require `n_trades_scored >= 30`, matching `T5_MIN_TRADES = 30` (`ingest_and_verdict.py:39`) so the scorecard and the pre-registered gate agree on "enough data". Below → `None` + `NOT_APPLICABLE` label, **never** a mean over 3 trades |
| `ValidityAxis` | **No aggregation.** PBO/DSR are already study-level | A cell has one PBO. Averaging PBO across sub-studies is statistically meaningless |

**Missing-data handling in aggregates:** `None` and `NOT_APPLICABLE` values are **excluded** from
means and the **exclusion is reported** (`n_signals_scored`, `n_signals_blocked`,
`n_trades_scored`, plus `n_missing` on every aggregated field). A mean over 12 of 20 signals is
`mean(12)` with `n_signals_blocked=8`, not `mean(12)` presented as the cell's confidence, and
not `mean(12)/20`. **A denominator of zero yields `None`/`NOT_APPLICABLE`, never `0.0` and never
`1.0`.**

> **C2 correction (v1.1).** v1.0 wrote the block rate as
> `1 - len(gates_passed)/(passed+failed)`, which computes the **pass** rate — so `1.0` meant
> "nothing blocked" inside a field named *block rate*. That is precisely the silent-inversion
> defect class §1.1 exists to eliminate, sitting inside the normative rule. Corrected above to
> `len(gates_failed)/(passed+failed)`. Boundary tests for this exact formula are named as **I1
> acceptance criteria (§9)**.

> **M1 resolution (v1.1).** v1.0's "last signal **plus** window mean" left `signal_final_score`
> ambiguous between the two, so the exclusion rule was unverifiable downstream and `0.0` could once
> again mean "blocked" or "genuinely zero". v1.1 splits them into `signal_final_score_last` and
> `signal_final_score_mean_unblocked` (§7.2) — no consumer can read one and guess the other.

---

## 6. Ranking semantics

### 6.1 Default ranking MUST NOT change (the single most important decision here)

The existing scorecard already refuses to present an unattested ordering.
`ranking_ordering_statement()` (`src/tournament/scorecard.py:501`) returns `NO_RANKING_STATEMENT` unless
`meta["ranking"]["method"] == RANKING_METHOD_STATISTICAL` (`"dsr_bh_fdr_selection_set"`, `:75`) **and**
every row carries a statistical verdict. Sprint E's verdict rests on that attested ordering.

**Rule:** these three axes are **additive columns only** under the default path. Existing rank order,
`RANKING_METHOD_STATISTICAL`, and the T1–T6 gates continue to be computed exactly as today. No existing
scorecard's `rank` value changes as a result of this spec. This is what makes the change safe to adopt
mid-lane and keeps `verdict_memo_sprint_e` reproducible.

**Scoped deferral, stated openly (v1.1, Rin M7).** Q3 names *ranking* among the three consumers, and by
default this spec does **not** unify ranking — the axes land as columns and the tiered ordering stays
switched off. That is a deliberate, scoped gap, not an oversight. **In scope and complete:** scorecard
columns (§7.2) and the verdict memo (§7.3). **Deferred:** default rank ordering. **Exit criterion** — the
tiered method is flipped on by default only once **all three** hold:

1. I5 has shipped and stayed green across one full tournament run;
2. at least one cell in that run carries a real `pbo_absent_cause == "PBO_PRESENT"` value (otherwise
   every cell sorts into the `NOT_APPLICABLE` partition and the ordering proves nothing);
3. a re-run **reproduces the prior `RANKING_METHOD_STATISTICAL` order for every cell whose tier is
   `PROMOTE`** — i.e. the new ordering is demonstrated not to perturb the attested head.

Until then §6.2 is opt-in, and any use must stamp `meta["ranking"]["confidence_tiered_opt_in"] = true`
so a reader can always tell which ordering produced a given scorecard.

### 6.2 Opt-in tiered ranking

A new method constant, proposed `RANKING_METHOD_CONFIDENCE_TIERED = "confidence_tiered_v1"`, enabled by an
explicit flag (never by default). When enabled, ordering is a **lexicographic partition**, not a weighted
blend. Weighted blending is explicitly rejected here: it would require putting PBO and signal confidence
on one scale, which is precisely the conflation this spec exists to prevent.

Ordering key, applied in order. v1.0 expressed this as two rules that could contradict each other
(step 1 "REJECT last **unconditionally**" vs step 2 "`NOT_APPLICABLE` last **within its partition**"),
so it was not a total order. **v1.1 collapses it into a single integer rank**, which is total by
construction:

```
tier_rank(cell) :=
    0   if pbo_tier == "PROMOTE"
    1   if pbo_tier == "CONDITIONAL"
    2   if pbo_tier == "NOT_APPLICABLE"
    3   if pbo_tier == "REJECT"
```

**Sort ascending on `tier_rank`.** This resolves the v1.0 ambiguity in favour of *evaluability beats the
measurement*: an unevaluable cell (`2`) sorts **ahead of** a measured `REJECT` cell (`3`), because a cell
that was never tested is not the same as a cell that was tested and failed. A measured `REJECT` still
sorts last, so no `REJECT` cell can ever reach the ranked head. The escape hatch
`--treat-not-applicable-as-pass` remaps `NOT_APPLICABLE` to rank `1` and **must** stamp
`meta["ranking"]["not_applicable_policy"]` so a memo shows it was done.

Full ordering:

1. `tier_rank` ascending.
2. Within a rank, **the existing DSR/BH-FDR statistical ordering is preserved** (this is the ordering the
   attestation already substantiates; it is not re-derived).
3. Tie-break, deterministic and total: `dsr_pvalue` desc (only where `dsr_pvalue_computed` is True) →
   `meta_probability_mean` desc → `signal_final_score_mean_unblocked` desc → `strategy_id` ascending.
   **(v1.2, Rin R1)** the final key is the **full cell key** `(strategy_id, symbol, timeframe)`
   ascending — **not** `strategy_id` alone. v1.1 wrongly claimed `strategy_id` was unique per cell;
   §4.2 keys a cell on the 3-tuple, so an M5 and an H1 cell for one strategy share it. A tie through
   the three preceding keys therefore fell through to **input row order**, which breaks §8 M9's
   byte-identical reproducibility guarantee. With the full 3-tuple as the last key the order is total:
   no two distinct cells can tie.

`meta["ranking"]` gains `{"method": ..., "confidence_schema_version": "1.0", "tier_thresholds":
{"promote_max": 0.30, "conditional_max": 0.50}, "not_applicable_policy": "exclude"|"rank_last",
"confidence_tiered_opt_in": bool}` so a
memo reading a scorecard can reconstruct exactly how the order was produced — the same attestation
discipline `ranking_ordering_statement` already enforces.

### 6.3 Promotion gate (the only place the axes combine)

`ValidationVerdict.tier` is currently `A`/`B`/`C`/`REJECT`/`INSUFFICIENT_DATA`
(`validation_runner.py:171-179`). The unified promotion rule:

```
promote(cell) :=
    pbo_tier == "PROMOTE"                                  # Q2, non-negotiable
  AND meta_axis is computable (brier_score present)         # calibrated, or abstain
  AND signal_axis.blocked is False for the cell's last signal
```

`CONDITIONAL` yields `ValidationVerdict.tier` `B`-class at best, with the tier label carried through —
**and never a silent promotion**. `REJECT` is terminal.

**This is a conjunction of gates, not an average.** A cell with excellent signal confidence and a REJECT
PBO tier does not average its way to a promotion. This is the direct, mechanical answer to Craig's
accepted conflation risk.

---

## 7. Integration map

### 7.1 Scorecard → ranking → verdict memo

```
PRODUCERS (unchanged)
  ConfidenceEngine.score()        src/forex_bot/confidence/engine.py:59
  CalibratedMetaClassifier        src/forex_bot/factory/meta_labeling.py:389
  compute_pbo / run_cpcv          srf/pbo.py:80 · backtest/cpcv.py:675
  ValidationRunner                factory/validation_runner.py:299
        │
        ▼
ADAPTER  (NEW) src/forex_bot/scoring/unified_score.py
  build_unified_cell(...) -> UnifiedCellScore   # maps producers → §4 axes
  pbo_tier(pbo)                  -> TierLabel    # §5.2, NEW single source of truth
        │
        ├──▶ ScorecardRow  (src/tournament/scorecard.py:90) gains §7.2 fields
        │        │                                      ── DEFAULT ORDER UNCHANGED (§6.1)
        │        ▼
        │    ranked_scorecard_from_selection_set (:396, meta["ranking"] at :486)
        │        │   [opt-in] tiered partition (§6.2)
        │        ▼
        │    ranking_ordering_statement (:501)  ── attestation, extended with tier provenance
        │
        └──▶ reports/tournament/ingest_and_verdict.py
                 evaluate_cells()  (:74)  ── gains the three axes as pass-through columns
                 apply_t4()        (:149)
                 decide_verdict()  (:221) ── GAINED: verdict_eligible now requires pbo_tier==PROMOTE
        │
        ▼
VERDICT MEMO  docs/reports/ftmo-verdict-memo-*.md
```

### 7.2 Scorecard schema delta

Added to `ScorecardRow` (`src/tournament/scorecard.py:90-129`). **All new fields default to `None` /
`"NOT_APPLICABLE"` so existing constructors and fixtures keep working unchanged** — this is the entire
migration story for producers.

```python
# ── Confidence unification (spec v1.1) ─────────────────────────────────────
# PRE-EXISTING ScorecardRow fields this spec REUSES and does NOT redeclare:
#   dsr_pvalue (scorecard.py:112) · n_trials_cell (:116) -- see the 1:1 table below.
# Every column below is NEW and defaults to None / "NOT_APPLICABLE" (M1).

# ── SignalAxis (S4.3) ──────────────────────────────────────────────────────
signal_final_score_last:           float | None = None
signal_final_score_mean_unblocked: float | None = None   # None if ALL signals blocked
signal_strategy_score_last:        float | None = None
signal_confluence_boost_last:      float | None = None
signal_gates_passed_last:          list[str] | None = None
signal_gates_failed_last:          list[str] | None = None
signal_blocked_last:               bool  | None = None   # mandatory companion
signal_block_reason_last:          str   | None = None
signal_block_rate:                 float | None = None   # len(failed)/(passed+failed)
n_signals_scored:                  int   | None = None
n_signals_blocked:                 int   | None = None
n_missing_signal:                  int   | None = None   # S5.3: excluded from the means
n_missing_meta:                    int   | None = None   # S5.3: excluded from the means

# ── MetaAxis (S4.4) ────────────────────────────────────────────────────────
meta_probability_mean:             float | None = None
meta_probability_p10:              float | None = None
meta_brier_score:                  float | None = None
meta_n_trades_scored:              int   | None = None
primary_confidence_mean:           float | None = None   # NOT a meta_probability

# ── ValidityAxis (S4.5) ────────────────────────────────────────────────────
pbo_value:                         float | None = None
pbo_ci_lower:                      float | None = None   # PBOScore only; None under CPCV
pbo_ci_upper:                      float | None = None   # PBOScore only; None under CPCV
pbo_tier:         TierLabel       = "NOT_APPLICABLE"   # M2: typed, NOT str (S5.1)
pbo_absent_cause:  str             = "PBO_PRESENT"      # M5 / S4.6d: why pbo_value is None
validity_source:   ValiditySource  = "NOT_APPLICABLE"   # "cscv" | "cpcv" | NOT_APPLICABLE
dsr_value:                         float | None = None   # S4.6c: CPCVDSRScore.dsr
dsr_computed:      bool | None = None                  # is dsr_value a measurement?
dsr_pvalue_computed: bool | None = None               # guards the REUSED dsr_pvalue
cpcv_n_paths:                      int   | None = None

validity_schema_version: str = "0"                     # "0" = legacy row, never inferred
```

### 7.2.1 Axis-to-column mapping — 1:1 coverage proof (v1.1, Rin C3)

Every field declared in §4.3–§4.5 appears exactly once below with its column and §7.3 destination.
`REUSED` = pre-existing `ScorecardRow` field (`scorecard.py:90-120`), not redeclared above.

| Axis | §4 field | §7.2 column | Status | §7.3 memo destination |
|------|----------|-------------|--------|-----------------------|
| Signal | `final_score` (last) | `signal_final_score_last` | NEW | Signal block: `final_last` |
| Signal | `final_score` (mean, unblocked) | `signal_final_score_mean_unblocked` | NEW | Signal block: `mean_unblocked` |
| Signal | `strategy_score` | `signal_strategy_score_last` | NEW | Signal block (diagnostic) |
| Signal | `confluence_boost` | `signal_confluence_boost_last` | NEW | Signal block (diagnostic) |
| Signal | `gates_passed` | `signal_gates_passed_last` | NEW | Signal block: `gates_failed` col |
| Signal | `gates_failed` | `signal_gates_failed_last` | NEW | Signal block: `gates_failed` col |
| Signal | `blocked` | `signal_blocked_last` | NEW | Signal block: `blocked` col |
| Signal | `block_reason` | `signal_block_reason_last` | NEW | Signal block: `gates_failed` col |
| Signal | block rate (§5.3) | `signal_block_rate` | NEW | Signal block: `block_rate` col |
| Signal | scored/blocked counts | `n_signals_scored`, `n_signals_blocked` | NEW | Signal block: `n` col |
| Signal | exclusion counts (§5.3) | `n_missing_signal`, `n_missing_meta` | NEW | Signal/Meta block: `n_missing` col |
| Meta | `meta_probability_mean` | `meta_probability_mean` | NEW | Meta block: `P(win) mean` |
| Meta | `meta_probability_p10` | `meta_probability_p10` | NEW | Meta block: `p10` |
| Meta | `n_trades_scored` | `meta_n_trades_scored` | NEW | Meta block: `n_trades` |
| Meta | `brier_score` | `meta_brier_score` | NEW | Meta block: `Brier` |
| Meta | `primary_confidence_mean` | `primary_confidence_mean` | NEW | Meta block: `primary_conf` |
| Validity | `pbo` | `pbo_value` | NEW | Validity block: `PBO` |
| Validity | `pbo_ci_lower` / `pbo_ci_upper` | `pbo_ci_lower` / `pbo_ci_upper` | NEW | Validity block: `CI` |
| Validity | `pbo_tier` | `pbo_tier` (`TierLabel`) | NEW | Validity block: `tier (Q2)` |
| Validity | absent cause (§4.6d) | `pbo_absent_cause` | NEW | Validity block, rendered when PBO is None |
| Validity | `validity_source` | `validity_source` | NEW | Validity block: `source` |
| Validity | `dsr` (§4.6c) | `dsr_value` + `dsr_computed` | NEW | Validity block: `DSR` |
| Validity | `dsr_pvalue` | **`dsr_pvalue`** (`scorecard.py:112`) | **REUSED** | Validity block: `DSR p` |
| Validity | computed guard for the above | `dsr_pvalue_computed` | NEW | gates the REUSED column |
| Validity | `n_trials_cell` | **`n_trials_cell`** (`scorecard.py:116`) | **REUSED** | Validity block: `n_trials` |
| Validity | `cpcv_n_paths` | `cpcv_n_paths` | NEW | Validity block: `n_paths` |
| Validity | schema marker | `validity_schema_version` | NEW | memo legacy banner (§7.3) |

**Coverage check:** every §4 field has exactly one column; every §7.2 column traces to a §4 origin or to
a REUSED field. `TierLabel` and `ValiditySource` are **distinct types, not `str`** (M2, per §5.1: the
type system — not a comment — must prevent coercion).


### 7.3 Verdict memo contract

The memo prints the three axes in **three separate labelled blocks**. Never one merged column, never a
single "Confidence" heading.

```markdown
## Signal-time confidence (src/forex_bot/confidence/engine.py)
| strategy | tf | final_last | mean_unblocked | blocked | gates_failed | block_rate | n | n_missing |
|---|---|---|---|---|---|---|---|
| killzone_momentum | M5 | 0.71 | 0.68 | false | — | 0.12 | 41 | 3 |

## ML meta-confidence (src/forex_bot/factory/meta_labeling.py)
| strategy | tf | P(win) mean | p10 | Brier | n_trades | primary_conf | n_missing |
|---|---|---|---|---|---|---|---|
| killzone_momentum | M5 | 0.63 | 0.51 | 0.218 | 214 | 0.59 | 0 |

## Backtest validity (srf/pbo.py · backtest/cpcv.py)
| strategy | tf | PBO | CI | tier (Q2) | source |
|---|---|---|---|---|---|
| killzone_momentum | M5 | 0.21 | [0.14, 0.29] | PROMOTE | cscv |
| ttc_xauusd | H1 | NOT_APPLICABLE | — | NOT_APPLICABLE | NOT_APPLICABLE |
```

Mandatory rules:
- `NOT_APPLICABLE` prints literally. It is never blank, `0`, `—`, or `N/A`.
- A `REJECT` tier cell is printed in the validity block **and** named in the verdict's rejection list.
- The memo states the tier thresholds inline (`promote <0.30 / conditional 0.30–0.50 / reject ≥0.50`)
  so a reader never has to guess which vocabulary is in force.
- If `verdict_eligible` was false, the memo must say **which** of the three axes blocked it.
- **Legacy banner (v1.1).** If any row in the memo has `validity_schema_version != "1.0"`, the memo
  MUST open with a banner: `LEGACY SCORECARD - confidence axes absent, values shown as
  NOT_APPLICABLE because this row predates the unified schema.` A reader must never mistake an
  unmeasured cell for a poorly-measured one.

### 7.4 Verdict-gate interaction

`decide_verdict()` (`ingest_and_verdict.py:221`) currently returns GO/NO-GO/INCONCLUSIVE from T1–T6.
Under this spec a cell becomes `verdict_eligible` **only if** `pbo_tier == "PROMOTE"` (§6.3). Because
the Sprint E corpus has no PBO data on cells (PBO was never wired to scorecards), applying this gate
would make every existing cell `NOT_APPLICABLE` — and per §6.2 that excludes, never promotes.
**Migration default therefore is: the promotion gate is advisory until the backing data exists**, with the
tier printed in the memo and a loud `NO_VALIDITY_DATA` banner. Making it binding is a separate, explicitly
pre-registered card (§9, **I4b**). This is stated here so the implementation cannot quietly make GO
unclaimable, which is exactly the failure mode card 7751d2e2 already documents for cost modelling.

**Trip-wire (v1.1, Rin M6) — advisory MUST NOT become permanent.** "Advisory" is a bridge, not a
destination, and an untracked advisory silently becomes the default forever. Therefore:

| Field | Value |
|-------|-------|
| **Owner** | Reina (sprint orchestrator), enforced by the primary-lane review board |
| **Trip-wire condition** | the gate becomes **binding** the moment the run that first reports `pbo_absent_cause == "PBO_PRESENT"` for any cell lands on `main` |
| **Expiry card** | **I4b** (§9) must add `pbo_tier` to `verdict_eligible`; until I4b lands, advisory |
| **Expiry trigger** (an event, not a date — v1.2, Rin R7) | I4b is cut **no later than the first tournament run after I3 ships** |
| **If missed** | I4 is escalated to high priority at the next sprint-planning review and the board is re-told that GO remains permanently unclaimable |

An advisory period with a named owner, a mechanical trip-wire, and a dated expiry card cannot
degenerate into the permanent soft-gate the audit already flagged once (finding C1, `cost_modeled`).

---

## 8. Migration path for existing scorecards

| # | Item | Action |
|---|------|--------|
| M1 | **Schema default** | Every new `ScorecardRow` field defaults to `None` / `"NOT_APPLICABLE"` (§7.2). No constructor signature changes. Existing tests and fixtures pass untouched. |
| M2 | **`validity_schema_version`** | Absent or `"0"` ⇒ **legacy row**. A legacy row is a first-class state, not a bug. |
| M3 | **Leave `is_overfit` alone** | `srf/pbo.py:47` and `cpcv.py:216` keep their `0.5` defaults for existing callers. **New code must not call them** (§5.2). Removal is a separate card once call sites are migrated. **Owner:** I1. **Grep-guard (I1 acceptance):** `rg 'is_overfit' src/forex_bot/scoring/` MUST return zero hits. **Test (I1 acceptance):** a unit test that fails if any module under `scoring/` imports or calls `is_overfit`, asserted by AST walk, not by grep alone. |
| M4 | **Legacy `dsr_pvalue == 1.0`** | Treated as **not computed** (§4.6a) — `dsr_pvalue_computed=False`. Never rendered as a computed p-value of 1.0. **Known cost (stated, not hidden, v1.1):** this rule has a **false-negative rate** — a legacy row whose DSR genuinely computed to exactly `1.0` will be reclassified as "not computed". DSR p-values are continuous, so the probability of an exact `1.0` is negligible, but the rate is **not zero** and is **not measured**. Acceptance of this error is the deliberate price of not rendering an unmeasured value as measured; it must be stated in the I6 migration tests, not assumed away. |
| M5 | **Legacy `pbo_tier_ceiling == "N/A"`** | Normalised to `"NOT_APPLICABLE"` at the adapter boundary (§4.6b). |
| M6 | **No inference backfill** | A legacy row's axes stay `NOT_APPLICABLE` forever unless a real producer runs. **Never** derive PBO from return series, never infer meta-confidence from `return_pct`. A derived number presented as a measured one is the exact failure this spec forbids. |
| M7 | **Backfill is a separate card** | Re-running historical cells through the unified schema is opt-in compute, needs its own resource approval (node-only per roadmap rev 7), and is **not** required to adopt this spec. |
| M8 | **Sprint E immutability** | The Sprint E grid, scorecards, and verdict memo are **not** rewritten. Migration is forward-only. |
| M9 | **Reproducibility** | A scorecard with `validity_schema_version == "1.0"` must reproduce byte-identically given the same inputs — no wall-clock timestamps inside the axis block, or the timestamp goes in `meta`, not in the row. |

---

## 9. Implementation decomposition (estimated after spec — **not** cut until Rin APPROVE)

| Card | Scope | Est. |
|------|-------|------|
| I1 | `scoring/unified_score.py` — `SignalAxis`/`MetaAxis`/`ValidityAxis`/`UnifiedCellScore`, frozen dataclasses, `pbo_tier()` single source of truth, `block_rate()` | 3 SP |
| I2 | `ScorecardRow` schema delta + JSON fixtures + targeted tests | 2 SP |
| I3 | Adapter wiring: `ValidationRunner` verdict → axes → scorecard | 3 SP |
| **I4a** | **Memo renderer only** — 3 blocks, `NOT_APPLICABLE` discipline, legacy banner. Golden-file tests | 2 SP |
| **I4b** | **`ingest_and_verdict.py` gate change** — `pbo_tier` → `verdict_eligible`, trip-wire wiring | 1 SP |
| I5 | Opt-in tiered ranking (§6.2) + attestation extension | 3 SP |
| I6 | Legacy normalizer (M4/M5) + migration tests | 2 SP |
| I7 | §4.6 source fixes: `dsr_pvalue` default, `pbo_tier_ceiling` default | 1 SP |

**I4 split (v1.1, Rin §9 finding).** v1.0 coupled the memo renderer to the verdict-gate change. The
renderer carries the **highest footgun density in the spec** — three blocks, sentinel discipline, the
legacy banner, the tier vocabulary — and shipping it entangled with a gate change means a renderer
regression and a gate regression fail together and are hard to bisect. I4a ships and is verified on
its own golden files first; only then does I4b change the gate. I4b is also the card the §7.4 trip-wire
names, so the split gives the trip-wire a single unambiguous owner.

**I7 stays separate** — flipping a dataclass default (`dsr_pvalue` `1.0` → `None`) can break existing
assertions, and bundling it with new code would bury that failure.

**I1 acceptance criteria (v1.1, Rin §9 finding) — named boundary tests:**

| Test | Assertion |
|------|-----------|
| `test_pbo_tier_boundaries` | `0.0→PROMOTE`, `0.299→PROMOTE`, **`0.30→CONDITIONAL`**, `0.499→CONDITIONAL`, **`0.50→REJECT`**, `0.501→REJECT`, `1.0→REJECT` |
| `test_pbo_tier_monotonic` | non-decreasing severity as `pbo` increases; more overfit never improves the label |
| `test_pbo_tier_does_not_delegate` | `pbo == 0.50` returns `REJECT` (both existing `is_overfit` methods return `False` here) |
| `test_no_is_overfit_import` | AST walk: no module under `scoring/` references `is_overfit` |
| `test_block_rate_direction` | all-pass ⇒ `0.0`; all-fail ⇒ `1.0` (C2 regression guard) |
| `test_block_rate_zero_denominator` | empty ⇒ `None`, **not** `0.0` and **not** `1.0` |

Each implementation card runs the **full** BQES lane with its own Rin review. This spec card is
**BQES-light**: checklist + Rin review of the spec only. **No implementation card is cut until Rin
APPROVES v1.1.**

---

## 10. Questions for Rin — RESOLVED in v1.1

| # | Question (v1.0) | Rin's verdict | Disposition in v1.1 |
|---|-----------------|---------------|---------------------|
| 1 | `pbo == 0.50` — REJECT or CONDITIONAL? | **UPHELD** (REJECT); half-open, and the tier must stay monotonic in `pbo` | §5.2 unchanged |
| 2 | Should an unevaluable cell outrank a measured `REJECT`? | **UPHELD** (yes — evaluability beats the measurement) | §6.2 rewritten as a single `tier_rank` integer so the rule is total; `NOT_APPLICABLE=2` sorts ahead of `REJECT=3` |
| 3 | `n_trades_scored >= 30` — reuse `T5_MIN_TRADES`? | **Not addressed** — carried forward | §5.3 unchanged; still open for implementation |
| 4 | Does §7.4 advisory-not-binding survive? | **UPHELD** on the premise, but advisory must not become permanent | §7.4 gained a named owner, a mechanical trip-wire, and a dated expiry card (I4b) |

**Newly opened by the v1.1 rework (needs a call at I1/I4a time, not now):** whether the
`n_trades_scored >= 30` floor is adequate for a *calibrated* probability — 30 trades is the
pre-registered trade-count gate, but calibration quality generally wants more, and §4.4 already
requires a Brier score alongside any reported probability. Left as-is deliberately: changing a
pre-registered threshold constant from inside a unification spec would be exactly the retuning §3
forbids.

---

## 11. Verification record

Paths cited in this spec were checked against the live tree before writing (audit lesson — never cite
an unverified path). **Rin's v1.0 review caught this spec breaking its own rule on exactly one citation
(C1, below), which is recorded here rather than quietly corrected.**

| Claim | Verified |
|-------|----------|
| `src/forex_bot/confidence/` — 7 modules | ✅ `engine.py confluence.py gates.py gate_tuner.py providers.py symbol_type_gating.py __init__.py` |
| `ConfidenceResult` fields + `score()` signature | ✅ `engine.py:20-30`, `:59` |
| Zero tournament **imports** of the confidence engine | ⚠️ **CORRECTED v1.1.** v1.0 claimed grep "→ no matches", which was **overstated**: `scorecard.py:377` does contain the string, inside the docstring of the *removed* `rank_scorecard_rows` sort keys (`trade_count (DESC) — more trades = higher confidence`). That is a prose mention in a function that now raises `RawReturnSortRemoved`, not an import. The substantive claim — **zero `import` statements** — holds and was re-verified |
| `PBOScore.is_overfit` default `0.5`, strict `>` | ✅ `srf/pbo.py:47` |
| `PBO_CEILING_NOT_APPLICABLE = "NOT_APPLICABLE"` | ✅ `factory/validation_runner.py:116` |
| `CPCV_NOT_APPLICABLE = "NOT_APPLICABLE"` mirrors it | ✅ `backtest/cpcv.py:98`, doc at `:97` |
| `ValidationVerdict.dsr_pvalue` default `1.0` | ✅ `validation_runner.py:195` |
| `ValidationVerdict.pbo_tier_ceiling` default `"N/A"` | ✅ `validation_runner.py:200` |
| `cost_sensitivity` field | ✅ `validation_runner.py:205` |
| `TrialReturnStore`, `<2 trials → NOT_APPLICABLE` | ✅ `validation_runner.py:222`, `:536-572` |
| `ScorecardRow` schema + `to_dict` via `asdict` | ✅ `src/tournament/scorecard.py:90-129` |
| `RANKING_METHOD_STATISTICAL` / `ranking_ordering_statement` | ✅ `scorecard.py:75`, `:501`; ranking meta at `:486` |
| T1–T5 pre-registered thresholds | ✅ `reports/tournament/ingest_and_verdict.py:35-39` |
| `decide_verdict` GO/NO-GO/INCONCLUSIVE | ✅ `ingest_and_verdict.py:221` |
| Confidence-engine import convention + live smoke run | ✅ `sys.path=['src/forex_bot']; from confidence.engine import …; score(...) → final_score=0.7` |
| `ranked_scorecard_from_selection_set` def line | ✅ `scorecard.py:396` (v1.0 wrote `~412` — imprecise, corrected) |
| `scorecard.py:377` contains the word "confidence" | ✅ docstring of the removed `rank_scorecard_rows` — the v1.0 grep claim was overstated; corrected above |
| **C1 — founding audit path** | ❌ **v1.0 was WRONG.** `docs/audits/ayumi-audit-2026-10-14.md` does **not** exist in this repo and never has: `docs/audits/` holds no such file and `git log --all -- docs/audits/ayumi-audit-2026-10-14.md` is empty. The audit lives at `/root/.openclaw/workspace/docs/audits/ayumi-audit-2026-10-14.md` (Ava's workspace). The string originated in **card df08ce02's own notes** and was propagated unverified. All three substantive claims it supported were re-verified independently against source and stand |

**Tests:** none. This is a specification card — no code was written, so there is nothing to test. Per
HR5 this is stated explicitly rather than passed over silently. No full-suite run was attempted.

---

## 12. Review record

| Round | Reviewer | Verdict | Outcome |
|-------|----------|---------|---------|
| v1.0 | Rin (`agent:rin`, runId `2d67f21e-58fa-40ac-93ee-3ea981f9c3ca`) | **REWORK** | 3 CRITICAL · 8 MEDIUM · 7 MINOR. Full verdict: `docs/audits/rin-review-confidence-unification-spec-2026-10-14.md`. All three safety calls (§5.2 boundary, single-source tier fn, §4.3/§4.6 defences, §6.1 freeze, §7.4 premise, M6 no-inference-backfill) **UPHELD**. |
| v1.1 | Rin (delta) | *pending* | Re-review is **delta-only** per Rin's instruction: C1–C3, M1–M8, m1–m7. |

**Standing rule from round 1, carried forward:** no implementation card (§9 I1–I7) is cut until Rin
APPROVES v1.1. A REWORK verdict means the spec moves, not that the decomposition starts.

**Note on the round-1 comment:** Rin could not post its verdict to card df08ce02 because Reina's claim
was live (`card is claimed by reina`). Rin correctly refused to force-release the claim or re-run the
review, and preserved the verdict to disk instead. Reina — as the claim holder — posted that verdict to
the card on Rin's behalf, attributed. No protocol violation: the verdict was not lost, not re-run, and
the claim was never stolen.

