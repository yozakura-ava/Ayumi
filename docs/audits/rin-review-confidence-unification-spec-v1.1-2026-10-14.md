# Rin Delta Re-Review — Confidence Unification Scoring Abstraction v1.1

**Card:** df08ce02-0da9-4f52-85cf-e3c2fc845737 (running, claimed by reina)
**Artifact:** `docs/specs/confidence-unification-scoring-abstraction.md` v1.1 (743 lines, SPEC ONLY)
**Reviewer:** Rin (Assurance Director) — independent of worker (reina) ✅
**Date:** 2026-10-14 · **runId:** `d9b1419e-c4a7-41ed-a409-284e953d27a6`
**Round 1 verdict:** REWORK — `docs/audits/rin-review-confidence-unification-spec-2026-10-14.md` (runId `2d67f21e`)
**Scope:** DELTA ONLY (C1–C3, M1–M8, m1–m7, §9 decomposition). UPHELD clauses spot-checked, not re-litigated.
**Tests:** none run, none expected (spec card, no code). Read-only verification only — `ls`/`wc`/`git log`/`sed`/`rg`. No suite run (HR5).

---

## VERDICT: APPROVE

All 18 findings fixed. 1 MEDIUM + 6 MINOR residuals remain, none blocking; all fold into I1/I4a
before their code lands. Implementation cards I1–I7 (+I4a/I4b) may now be cut.

---

## 1. CRITICAL — all three fixed, and honestly

### C1 — founding citation. FIXED. Honest, not cosmetic.

Verified against the live filesystem myself:

| Check | Result |
|---|---|
| `/root/.openclaw/workspace/docs/audits/ayumi-audit-2026-10-14.md` | **exists, 12529 bytes** — exact match to the spec's claim |
| `docs/audits/` in `/home/TacoPants/projects/Ayumi` | no `ayumi-audit-2026-10-14.md`; the only `2026-10-14` file is `rin-review-confidence-unification-spec-2026-10-14.md` (my own v1.0 review) |
| `git log --all -- docs/audits/ayumi-audit-2026-10-14.md` | **empty** — the path has never existed in this repo |

The spec does three things right, and these are what make the fix honest rather than cosmetic:
1. It cites the **real, locatable** location, and states the exact byte count so the next reviewer can
   check the claim without guessing.
2. It records the *failure* in §11 as an explicit ❌ row rather than deleting it — the audit lesson is
   "never cite an unverified path", and the spec now demonstrates it by keeping its own violation on
   the record.
3. It names the propagation source (**card df08ce02's own notes**, from Ava's card creation) instead of
   silently re-attributing the string to a document nobody wrote. That is the honest version of the fix:
   it assigns fault to the right place rather than papering over the origin.

The substantive claims the audit supported (three disjoint confidence axes; zero tournament imports)
were retained and re-verified independently, so the finding survives even though its citation did not.

### C2 — block-rate direction. FIXED.

- Normative formula is now `len(gates_failed)/(passed+failed)` — stated in the §5.3 `SignalAxis` row,
  in the C2 correction blockquote, and inline in the §7.2 schema comment
  (`signal_block_rate: float | None = None   # len(failed)/(passed+failed)`).
- The inverted expression `1 - len(gates_passed)/(passed+failed)` now survives **only** at line 337,
  inside the explanatory note that names it as the v1.0 error. It appears nowhere as a rule.
- Direction is now pinned by two named I1 acceptance tests:
  `test_block_rate_direction` (all-pass ⇒ `0.0`, all-fail ⇒ `1.0`) and
  `test_block_rate_zero_denominator` (empty ⇒ `None`, never `0.0`/`1.0`).

Direction re-derived by hand to confirm: all gates pass → failed = 0 → `0.0` = nothing blocked ✅.
All fail → `failed/(0+failed)` = `1.0` = everything blocked ✅. Inversion gone.

### C3 — schema coverage. FIXED in the direction that mattered.

§7.2.1 gives every field declared in §4.3–§4.5 exactly one column, and marks pre-existing fields
`REUSED` rather than redeclaring them. I verified the two `REUSED` claims against the live source,
since the whole 1:1 proof rests on them:

- `src/tournament/scorecard.py:112` → `dsr_pvalue: float | None = None` ✅
- `src/tournament/scorecard.py:116` → `n_trials_cell: int | None = None` ✅

The sharpest v1.0 symptom is gone: `dsr_computed` / `dsr_pvalue_computed` now guard a `dsr_pvalue`
field that §7.2 actually declares (as REUSED), and §7.3's `CI` column now traces to
`pbo_ci_lower`/`pbo_ci_upper`. **No §7.3 column lacks a schema source** — checked column by column
against §7.2 (see R4 for the reverse direction, which is a presentation drift, not a coverage hole).

---

## 2. MEDIUM — all eight fixed as claimed

| # | Claim | Verification |
|---|-------|--------------|
| M1 | last/mean-unblocked split | §7.2 declares `signal_final_score_last`, `signal_final_score_mean_unblocked`, `n_signals_scored`, `n_signals_blocked`, `signal_block_rate`. The ambiguous `signal_final_score` name is gone. §5.3 states the exclusion rule is downstream-verifiable and that all-blocked ⇒ `None`, never `0.0` |
| M2 | `TierLabel` / `ValiditySource` distinct types | §7.2 declares `pbo_tier: TierLabel` and `validity_source: ValiditySource`; §7.2.1 asserts "distinct types, not `str`". The `float \| str` self-contradiction with §5.1 is resolved (see R5 for the residual: neither type is formally declared) |
| M3 | G2/G4/G6 re-pointed | G2 → §4.2 + §4.3–§4.5 ✅ · G4 → §5.1 + §5.3 ✅ · G6 → §4.3–§4.6 + §7.2.1 ✅. All three now name sections that exist. §3 is flat and is no longer cited by a goal |
| M4 | §6.2 total order | `tier_rank` is now one integer over all four tier values: PROMOTE 0, CONDITIONAL 1, NOT_APPLICABLE 2, REJECT 3. It **is** total over the tier dimension, ascending sort, REJECT provably last, escape hatch stamps `not_applicable_policy`. Residual on the tie-break chain → R1 |
| M5 | §4.6 now four footguns | (a) `dsr_pvalue` default ✅ (b) `pbo_tier_ceiling` "N/A" ✅ (c) **new** — `dsr` vs `dsr_pvalue` split into `dsr_value`+`dsr_computed` and `dsr_pvalue`+`dsr_pvalue_computed`, neither substitutable ✅ (d) **new** — `pbo_absent_cause` names which of three causes produced `None` ✅. §5.1 also generalises the rule: every `None` needs a companion `*_absent_cause`, and a row without one is malformed |
| M6 | §7.4 trip-wire | Named owner (Reina, enforced by the primary-lane review board), mechanical condition (`PBO_PRESENT` landing on `main`), expiry card, and a stated failure action. An untracked advisory can no longer become permanent |
| M7 | §6.1 scoped deferral | Deferral named explicitly; in-scope (scorecard + memo) vs deferred (default rank order) stated; 3-condition exit criterion, including the good one that at least one cell must carry a real PBO or the ordering proves nothing |
| M8 | M3 owner + guards | Owner I1; `rg 'is_overfit' src/forex_bot/scoring/` must return zero hits; **AST-walk test** so the guard cannot be defeated by a string match. This is the correct strength of enforcement for the spec's own thesis |

## 3. MINOR — all seven fixed

| # | Claim | Verification |
|---|-------|--------------|
| m1 | `:~412` → `:396` | `sed -n '396p' src/tournament/scorecard.py` → `def ranked_scorecard_from_selection_set(` ✅. `:~412` survives only in the changelog and §11 as the recorded correction |
| m2 | §11 grep claim corrected | `scorecard.py:377` verified: docstring line `2. ``trade_count`` (DESC) — more trades = higher confidence`, inside the removed `rank_scorecard_rows` ✅. Zero **imports** claim verified: `rg '^\s*(from\|import)\s+.*confidence' src/tournament/ reports/tournament/ scripts/run_tournament.py` → **zero hits** ✅. The spec now says "zero `import` statements" instead of "no matches" — accurate |
| m3 | name-divergence mapping table | §7.2.1 maps `pbo`→`pbo_value`, `brier_score`→`meta_brier_score`, `n_trades_scored`→`meta_n_trades_scored`, `dsr`→`dsr_value` |
| m4 | M4 false-negative rate | §8 M4 states the rate is **not zero and not measured**, and requires it be stated in the I6 migration tests rather than assumed away |
| m5 | §7.3 legacy banner | Required when `validity_schema_version != "1.0"`, with the "unmeasured vs poorly-measured" rationale |
| m6 | `validity_source` union leak | Now a distinct `ValiditySource` type whose third member is the sentinel, so a consumer must handle it at type level rather than by string comparison |
| m7 | `dsr` not-computed | `dsr_computed` flag + §4.6(c) prohibition on rendering a dataclass default `0.0` as a measurement |

## 4. §9 decomposition — as instructed

- **I4 split into I4a** (memo renderer only — 3 blocks, `NOT_APPLICABLE` discipline, legacy banner,
  golden-file tests, 2 SP) **and I4b** (gate change `pbo_tier` → `verdict_eligible`, trip-wire wiring,
  1 SP). The rationale is correct: the renderer carries the highest footgun density in the spec, and
  shipping it entangled with a gate change makes renderer and gate regressions fail together and hard
  to bisect. I4b is also the single unambiguous owner the §7.4 trip-wire needs.
- **6 named I1 boundary tests** present and correctly targeted: `test_pbo_tier_boundaries`
  (0.0/0.299/0.30/0.499/0.50/0.501/1.0), `test_pbo_tier_monotonic`, `test_pbo_tier_does_not_delegate`,
  `test_no_is_overfit_import`, `test_block_rate_direction`, `test_block_rate_zero_denominator`.
  These cover exactly the three CRITICALs and the no-delegation rule.
- **I7 kept separate** — correct; flipping a dataclass default can break existing assertions and
  bundling it would bury the failure.

## 5. UPHELD clauses — spot-check, not re-litigated

§5.2 half-open boundary with REJECT at `pbo == 0.50`; single-source tier function with no `is_overfit`
delegation; §4.3 blocked-impostor grounding; §4.6(a)/(b) `dsr_pvalue`/ceiling defences; §6.1 default-ranking
freeze; §7.4 advisory premise; §8 M6 no-inference-backfill. **All byte-identical, none disturbed by the
rework.** The change log's "deliberately not touched" claim is accurate.

---

## 6. Residual issues (none blocking)

### R1 — MEDIUM — §6.2's tie-break chain is still not total
`tier_rank` is genuinely total over the four tiers. The tail of the ordering key is not: step 3 ends
`dsr_pvalue` desc → `meta_probability_mean` desc → `signal_final_score_mean_unblocked` desc →
`strategy_id` ascending, justified by "**`strategy_id` is unique per cell**". §4.2 keys a cell on
`(strategy_id, symbol, timeframe)`, so an M5 cell and an H1 cell for the same strategy **share**
`strategy_id`. If those cells also tie on the three preceding keys, the final order falls back to input
row order — which collides with §8 M9's requirement that a `validity_schema_version == "1.0"` scorecard
reproduce byte-identically. **Fix (one line):** tie-break on `(strategy_id, symbol, timeframe)`.
Contained by M7's opt-in gate and exit criterion 3, but it should be fixed in I5's spec before I5 code.

### R2 — MINOR — `n_missing` is mandated but never declared
§5.3 requires "the exclusion is reported (`n_signals_scored`, `n_signals_blocked`, `n_trades_scored`,
**plus `n_missing` on every aggregated field**)". No `n_missing` column exists in §7.2 and no row maps
it in §7.2.1. This is the same §5.3 → §7.2 trace gap C3 was raised for, in miniature. Declare it or drop
the clause.

### R3 — MINOR — §7.3's literal signal header still uses the retired ambiguous name
The signal block header is `| strategy | tf | final_score | blocked | gates_failed |`. §7.2.1 says the
destinations are `final_last` and `mean_unblocked`. An implementer copying §7.3 verbatim reintroduces
precisely the last-vs-mean ambiguity M1 closed.

### R4 — MINOR — §7.2.1 names memo columns §7.3 does not have
Claimed destinations with no column in §7.3's tables: `mean_unblocked`, `block_rate`, `n`,
`primary_conf`, `DSR`, `DSR p`, `n_trials`, `n_paths`. Either widen §7.3's three tables or mark those
destinations "not printed in v1.1". The coverage direction that mattered (no §7.3 column without a
schema source) **holds**; this is the reverse over-claim.

### R5 — MINOR — `TierLabel` and `ValiditySource` are used but never declared
§5.1's entire enforcement story is "the compiler/validator catches it", but neither type has a normative
member list anywhere in the spec. Add:
`TierLabel = Literal["PROMOTE","CONDITIONAL","REJECT","NOT_APPLICABLE"]` and
`ValiditySource = Literal["cscv","cpcv","NOT_APPLICABLE"]`.
`NOT_APPLICABLE` **must** be a `TierLabel` member — §6.2's `tier_rank` has a case for it and §7.2
defaults `pbo_tier` to it. Without the declaration the type-level guarantee has no spec-level source.

### R6 — MINOR — trip-wire still names "I4" after the split
§7.4's trip-wire rows (expiry card, expiry date, missed-escalation) all say **I4**. The gate card the
trip-wire owns is **I4b**. §9 and §10 already say I4b.

### R7 — MINOR — "Expiry date" row is not a date
It holds an event-relative trigger ("no later than the first tournament run after I3 ships"). That is a
sound trigger; the row label is simply wrong. Rename to "Expiry trigger" or attach a calendar deadline.

---

## 7. Open item, deliberately not fixed — assessment

**`n_trades_scored >= 30` floor for a calibrated probability: NOT a defect as specced.** Reusing the
pre-registered `T5_MIN_TRADES = 30` is the non-retuning choice — changing a pre-registered threshold
constant from inside a unification spec is exactly what §3 forbids — and §10 carries the question openly
with the reasoning. I do not require a change to the floor.

One sharpening for I1/I4a time, non-blocking: at `n = 30`, `meta_probability_p10` is the 3rd order
statistic of 30 — noise-dominated, and it will be printed next to a mean as though it carried tail
information. The memo already prints `n_trades`, so the cheap mitigation is to require p10 be shown
only alongside `n`, or set a separate, higher p10 floor. Leave the 30-trade gate alone; address p10.

---

## 8. Verification record (delta)

Read-only. No suite run (HR5), no code changed, no file under review modified.

| Claim checked | Result |
|---|---|
| `/root/.openclaw/workspace/docs/audits/ayumi-audit-2026-10-14.md` = 12529 bytes | ✅ exact |
| repo `docs/audits/` has no `ayumi-audit-2026-10-14.md` | ✅ |
| `git log --all -- docs/audits/ayumi-audit-2026-10-14.md` | ✅ empty |
| `scorecard.py:396` = `ranked_scorecard_from_selection_set` | ✅ |
| `scorecard.py:377` = docstring "more trades = higher confidence" | ✅ |
| `scorecard.py:112` = `dsr_pvalue`, `:116` = `n_trials_cell` (REUSED claims) | ✅ |
| zero `import` of confidence in `src/tournament/`, `reports/tournament/`, `scripts/run_tournament.py` | ✅ |
| inverted block-rate expression survives only in the correction note (line 337) | ✅ |
| §7.2 block-rate comment = `len(failed)/(passed+failed)` | ✅ |
| `:~412` survives only in changelog + §11 as the recorded correction | ✅ |
| every §4.3–§4.5 field maps to exactly one §7.2 column | ✅ |
| every §7.3 column traces to a schema source | ✅ |
| §7.2.1 references memo columns absent from §7.3 | ⚠️ R4 |
| `TierLabel` / `ValiditySource` formally declared | ❌ R5 |
| `n_missing` declared in §7.2 | ❌ R2 |
| §7.3 signal header uses `final_last`/`mean_unblocked` | ❌ R3 |

---

## 9. Protocol note

This verdict could not be posted as a workboard comment: `workboard_comment` was rejected with
`card is claimed by reina.` (Reina's claim is live). Consistent with round 1, Rin did **not** force-release
the claim, did **not** steal it, and did **not** re-run or self-review. The verdict is preserved here for
**Reina to post on Rin's behalf**, in three parts (each under the length cap):

1. APPROVE header + C1/C2/C3 verification + M1–M8 and minors confirmed.
2. R1–R4 residuals.
3. R5–R7 residuals + open item + tests statement + APPROVE close.

---

## 10. Verdict

**APPROVE.** 18/18 findings genuinely fixed, verified against the live tree rather than against the
change log's own claims. 1 MEDIUM + 6 MINOR residuals, all contained and all fixable inside I1, I4a, or
I5 before their code lands. No implementation card is blocked by anything in this review; each
implementation card carries its own Rin review.