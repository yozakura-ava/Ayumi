# Rin Spec Review — Confidence Unification Scoring Abstraction

**Card:** df08ce02-0da9-4f52-85cf-e3c2fc845737
**Artifact:** `docs/specs/confidence-unification-scoring-abstraction.md` v1.0 (512 lines, SPEC ONLY)
**Reviewer:** Rin (Assurance Director) — independent of worker (reina) ✅
**Date:** 2026-10-14
**Basis:** full read of the spec + line-level verification of every `path:line` citation against the live tree.
**Tests:** none exist, none expected (spec card, no code). No suite run (HR5).

## VERDICT: REWORK

Not a rejection of the design. The architecture is sound and the three safety calls I was asked to
adjudicate are all correct. The defects are mechanical (one broken provenance citation, one inverted
formula, one incomplete schema table) plus spec-integrity gaps. Cheap to fix; re-review is delta-only.

---

## Confirmed (questions put to Rin)

**§5.2 boundary — UPHELD.** Half-open intervals, REJECT wins at `pbo == 0.50`. `>=0.50` was the only
bound Craig stated with an explicit inclusive operator, and the tier must be monotonic in `pbo` so that
more overfit never improves the label.

**§5.2 single-source ban — CORRECT.** Verified `srf/pbo.py:47` and `backtest/cpcv.py:216` both declare
`threshold: float = 0.5` with strict `>`. At exactly `pbo == 0.5` both return NOT-overfit while Q2 says
REJECT. Neither can emit a 3-label; wrapping either would preserve the silent 0.5 governance.

**§4.3 / §4.6 defences — SUFFICIENT for the two named footguns.** Verified the blocked-impostor is real:
`confidence/engine.py:113-125` returns `final_score=0.0, blocked=True` immediately on the first failing
gate. Verified `validation_runner.py:195` `dsr_pvalue: float = 1.0`.

**§6.1 additive-only default — AGREE.** §7.4 advisory-not-binding — AGREE. §8 M6 no-inference-backfill
— SOUND. §7.4's factual premise verified: `scorecard.py` has no `pbo` field, so Sprint E cells carry none.

---

## CRITICAL

**C1 — the founding citation does not exist.** §1 cites
`docs/audits/ayumi-audit-2026-10-14.md` finding C1. That file is not on disk and has no git history for
the path (`git log -- <path>` returns empty; `docs/audits/` contains nothing dated 2026-10-14).
In fairness, this string originates in the **card notes** (Ava's card creation), not the worker's
drafting — the spec propagated it. But §11's header claims "never cite an unverified path" while
carrying the one citation that cannot be verified. The substantive claims are independently true (all
three axes + zero tournament imports verified by me), so this is a provenance defect, not a false finding.
C1 must cite real, locatable evidence.

**C2 — §5.3 block-rate formula is inverted.** Spec: "gate-block rate
`(1 - len(gates_passed)/(len(gates_passed)+len(gates_failed)))`". That expression computes the fraction of
gates that **PASSED**. Block rate is `len(gates_failed)/(passed+failed)`. As written, `1.0` means
"nothing blocked" under a field named block rate, so every consumer reads it backwards. This is the exact
defect class §1.1 claims to eliminate — a direction inversion on a named quantity — sitting in the
normative aggregation rule.

**C3 — §7.2 schema cannot satisfy §7.3 or §4.6(a).** `ValidityAxis` (§4.5) defines 8 fields; §7.2 adds
columns for 2 (`pbo_value`, `pbo_tier`) plus `pbo_source`. Missing: `pbo_ci_lower`, `pbo_ci_upper`,
`dsr`, `dsr_pvalue`, `n_trials_cell`, `cpcv_n_paths`. Also missing `meta_probability_p10` (printed by the
§7.3 memo) and §5.3's `signal_block_rate` / `n_signals_scored`.
Sharpest symptom: §4.6(a) adds `dsr_computed: bool` as a disambiguator **for a `dsr_pvalue` field that
§7.2 never declares**, and §7.3's memo table prints a `CI` column with no schema source. Card I2 as
specced cannot produce §7.3. Fix: add an explicit §4.x → §7.2 field-mapping table proving 1:1 coverage.

---

## MEDIUM

**M1 — the blocked-impostor footgun returns at the aggregation layer.** `signal_final_score` is ambiguous
between "last signal" and "window mean over unblocked signals". §5.3 excludes the 0.0 impostors from the
mean, but no field carries the mean separately and none carries block-rate or `n_scored` — so the
exclusion rule is unverifiable downstream and `signal_final_score == 0.0` once again means two different
things. The spec's own central defence does not survive its own aggregation step.

**M2 — §5.1 vs §7.2 self-contradiction.** §5.1 bans `float | str` unions and mandates a distinct
`TierLabel` "so the compiler/validator catches it" — the spec's only type-level enforcement. §7.2 declares
`pbo_tier: str` and `pbo_source: str`. Use `TierLabel` and a source enum.

**M3 — dangling cross-references.** G2 → §3.1, G6 → §3.2, G4 → §3.3. §3 is "Non-goals": 8 flat items,
no subsections. Three design goals cite sections that do not exist.

**M4 — §6.2 ordering key is not a total order.** Step 1: REJECT sorts last *unconditionally*. Step 2:
NOT_APPLICABLE sorts last *within its own partition*. The NOT_APPLICABLE-vs-REJECT relative order is
therefore undefined (correctly raised as an open question in §10 Q2, but §6.2 reads as normative and
settled). Give the 4-value partition one explicit order.

**M5 — a third conflation path was missed.** `dsr` (`CPCVDSRScore.dsr`) and `dsr_pvalue`
(`ValidationVerdict.dsr_pvalue`) sit on one axis with near-identical names and different producers, yet
only the latter receives a not-computed guard. State which is authoritative and give `dsr` the same
treatment. Separately, `pbo_score=None` collapses three distinct causes (not-Optuna-derived / <2 trials /
matrix None) into one value.

**M6 — §7.4 needs an exit condition.** Advisory-by-default is correct and upheld, but "advisory" with no
trip-wire can persist indefinitely, shipping Q3 with validity never gating anything. Add a hard trigger
(e.g. once ≥N% of cells carry a real PBO, the binding card MUST be cut before any GO is issued).

**M7 — §6.1 leaves ranking un-unified by default.** Under the default path the axes are inert for ranking
(opt-in off), yet Q3 names ranking explicitly. This is not a reason to change §6.1 — additive-only is
correct given the attested `RANKING_METHOD_STATISTICAL` ordering and frozen T1–T6 — but it must be stated
as a scoped deferral with a named exit criterion for flipping §6.2, so "FULL unification into ranking"
is verifiable rather than paper-satisfied.

**M8 — M3 has no owner.** "New code must not call `is_overfit`" is a rule with no card, no grep-guard,
and no test. Assign it as an I1 acceptance criterion plus a test asserting the new path never calls
either method.

---

## MINOR

1. `ranked_scorecard_from_selection_set` is at `scorecard.py:396`; §7.1 cites `:~412`, which lands inside
   the docstring.
2. §11 states grep over `src/tournament/` etc. yields "no matches". It actually returns one hit
   (`scorecard.py:377`, a docstring mention of "confidence"). Zero **imports** is true — say that instead.
3. §4.5 → §7.2 name divergence with no mapping table (`pbo`/`pbo_value`,
   `brier_score`/`meta_brier_score`).
4. M4 folds a genuinely computed `dsr_pvalue == 1.0` into "not computed". Safe direction, but the
   false-negative rate should be stated rather than left implicit.
5. §7.3 has no legacy/unmeasured-row banner requirement (only §7.4's `NO_VALIDITY_DATA`).
6. `validity_source: "cscv" | "cpcv"` yet §7.3's table shows `NOT_APPLICABLE` in that column — the
   sentinel leaks into a union-typed field.
7. §4.5's `dsr` field has no specified not-computed handling.

---

## Citation verification

All verified **exact**: `srf/pbo.py:47`; `cpcv.py:98`, `:216`, `:233`;
`validation_runner.py:116`, `:195`, `:200`, `:205`, `:222`, `:299`;
`scorecard.py:75`, `:90`, `:122`, `:127`, `:486`, `:501`;
`ingest_and_verdict.py:35-39`, `:74`, `:149`, `:221`;
`confidence/engine.py:20-30`, `:59` (blocked path `:113-125`);
`meta_labeling.py:220`, `:389`, `:636`, `:656`; `confidence/__init__.py:3`;
commit `03bdc36b` exists (FTMO gate pre-registration freeze).
Imprecise: `scorecard.py:~412` (M1 above).

## Decomposition (§9)

Sane overall — 7 cards / 16 SP is well-cut. One change: **split I4.** The memo renderer (three-block
discipline + NOT_APPLICABLE printing) has the highest footgun density in the spec and deserves its own
golden-file tests rather than shipping coupled to the gate-interaction change. Name boundary tests
(0.29 / 0.30 / 0.49 / 0.50 / 0.51) as I1 acceptance criteria. I7 (dataclass default flip) can break
existing assertions — keeping it separate as specced is right.

## Re-review scope

v1.1 delta only. The 3 CRITICALs are mechanical; the 8 MEDIUMs are roughly one paragraph each.
No implementation card should be cut before APPROVE.