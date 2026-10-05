# R&D Hopper Engine — Autonomous Hypothesis Generation Design

| Field | Value |
|---|---|
| **Card** | `ea42e6be-ed0f-4044-ab5d-3c8c5eb8baf6` |
| **Status** | Research design (v1) — for Ava / Himari review |
| **Author** | Satsuki (Research Director) |
| **Date** | 2026-10-04 |
| **Lane** | Research (HR40) — design + sprint proposal only, **no code** |
| **Parent doc** | `docs/roadmaps/strategy-factory/strategy-factory-implementation-spec.md` (Tsubaki, 803 lines) |
| **Trigger** | `sprint_card.status == 'done'` for `bc8439dd-c329-4fb1-8ecd-a22fbdfd045f` (Sprint A — pipeline assembly). Met 2026-10-05 00:47 EDT. |
| **Audience** | Ava (decision consumer), Himari (portfolio routing), Reina/Tsubaki (build owners), Tomoe (compute), Craig (sign-off) |
| **Why now** | Craig 2026-10-04: *"all the plumbing in the world doesn't matter if we can't work out trading strategies that produce consistent results."* The pipeline is end-to-end; the bottleneck is now **idea flow**, not execution. |

---

## 0. Mission

Keep the strategy factory's hopper **continuously stocked with scored, kill-dated hypothesis briefs** so the tournament + validation pipeline (Sprint A) never starves for new candidates — **without human handholding**. Generate freely, promote with a gate.

**The hopper is to strategy ideas what a venture pipeline is to deal flow.** The factory is the IC. The hopper is the sourcing funnel.

---

## 1. Design Tenets (Locked)

1. **Generate freely, promote with gate.** The cost of *generating* a bad idea is small (a markdown file). The cost of *promoting* a bad idea to live capital is enormous. Volume at the top, ruthlessness at the gate.
2. **Every brief has a kill-date expressed as a boolean.** Per card-creation skill (2026-10-04 rule): prose kill conditions rot. Boolean triggers are auditable. Format: `trigger: <record_count threshold> OR <wall-clock date> OR <regime-shift predicate>`.
3. **Every brief inherits the `edges/<name>-hypothesis.md` schema.** The 10 hand-written briefs in `docs/edges/` (avg 63 lines) are the canonical pattern the generator must reproduce. Bots that drift from the schema are rejected at commit time.
4. **No brief enters the tournament without scoring.** Auto-promotion is gated by `score ≥ 0.65` (see §5). Below that, the brief sits in the hopper until either (a) it gets rescored upward by new evidence or (b) its kill-date fires.
5. **The ML lane is parallel, not downstream.** ML feature/confidence-learner development produces its own hypothesis briefs (e.g. "RandomForest rank-order adds 8% Sharpe in CHOPPY") that route through the same triage. It is not a Phase 2 of the hopper; it is a co-equal generator.
6. **The hopper is auditable.** Every brief is a file on disk, every promotion a workboard move, every kill a row in `data/research/hopper_log.jsonl`. No ephemeral state.
7. **Confluence research is a first-class generator.** The TTC/TBD framework has 8 stages (per master roadmap §1C Layer 3). New confluence candidates ("does news-flow surprise × session-open × HTF trend reduce DD?") are exactly the kind of cross-domain hypotheses the hopper should produce — they are not a separate research track.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        R&D HOPPER ENGINE                                │
└─────────────────────────────────────────────────────────────────────────┘

   INPUT SOURCES                  GENERATORS                  HOPPER
 ┌──────────────────┐          ┌────────────────────┐    ┌───────────────┐
 │ docs/edges/      │          │ G1 Postmortem    │──┐ │               │
 │   (10 templates) │────────▶│    Miner          │  │ │               │
 │ docs/research/   │          ├────────────────────┤  │ │  hopper/      │
 │   opportunity-   │────────▶│ G2 Regime-Gap     │──┤ │   briefing/    │
 │   briefs/         │          │    Finder          │  │ │   *.md        │
 │ docs/research/   │          ├────────────────────┤  ├▶│               │
 │   icir-research  │────────▶│ G3 Confluence     │──┤ │  +score.json   │
 │   oos-gate-      │          │    Synthesizer     │  │ │     +kill-date │
 │   research …     │          ├────────────────────┤  │ │               │
 │ data/research/   │          │ G4 ML-Gap         │──┘ │               │
 │   research.duckdb│────────▶│    Extrapoler     │    │               │
 │ data/edge_       │          ├────────────────────┤    │               │
 │   telemetry.jsonl│────────▶│ G5 Forward-Test   │    │               │
 │ forward_test_    │          │    Anomaly Detector│    │               │
 │   health.json    │          ├────────────────────┤    │               │
 │ research.duckdb  │          │ G6 Tournament     │    │               │
 │   v_top_         │────────▶│    Champion       │    │               │
 │   strategies     │          │    Re-combiner    │    │               │
 │ Tournament       │          ├────────────────────┤    │               │
 │   JSON scorecards│────────▶│ G7 External       │    │               │
 │ External:        │          │    Idea Ingest    │    │               │
 │  docs/research/  │          │ (RSS, arXiv,      │    │               │
 │  sources/        │          │  CTAs, ICT/SMC    │    │               │
 │  ICT/SMC, CTA    │          │  literature)      │    │               │
 └──────────────────┘          └────────────────────┘    └───────┬───────┘
                                                                │
                                                                ▼
                                                       ┌─────────────────┐
                                                       │ TRIAGE / SCORER │
                                                       │  (auto + gate)  │
                                                       └────────┬────────┘
                                                                │
                          auto-promote (score ≥ 0.65)            │
                          OR human-gate (0.40 ≤ s < 0.65)        │
                          OR kill (kill-date fired, score < 0.40)│
                                                                ▼
                                                       ┌─────────────────┐
                                                       │ Sprint B        │
                                                       │ "factory intake"│
                                                       │  card created   │
                                                       │  → factory     │
                                                       │  pipeline (     │
                                                       │  tournament→    │
                                                       │  validate→      │
                                                       │  verdict)       │
                                                       └─────────────────┘
```

---

## 3. Section A — Hopper Inputs (Raw Material Catalog)

Each generator (G1–G7) consumes one or more inputs. The table below is the **completeness contract** — every input the hopper *could* consume is listed, and each is mapped to a generator. New inputs are added by adding a row.

| Input | Path | Frequency | Generator | Notes |
|---|---|---|---|---|
| Hand-written hypothesis briefs | `docs/edges/*.md` (10 files) | static (re-read on commit) | G1 Postmortem Miner + G7 (template) | The pattern the bot imitates. ~63 lines avg. |
| Tournament scorecards (JSON) | `data/tournament/scorecards/*.json` (Sprint A output) | per-tournament (weekly) | G6 Champion Re-combiner | Top-N + bottom-N reveal which archetype patterns are depleted |
| Forward-test live telemetry | `data/edge_telemetry.jsonl` (append-only, 57KB+ today) | per-tick (batch daily) | G5 Forward-Test Anomaly Detector | Live performance divergence vs backtest is a hypothesis source |
| Forward-test health | `data/forward_test_health.json` (one-shot daily) | daily | G5 | Stalls, low-trade days, regime misreads → brief seeds |
| Research conclusion log | `data/research/research.duckdb` `study_ledger`, `cron_runs`, `runs` tables | per-cron | G2, G4 | Past studies that "didn't work" carry implicit negative hypotheses |
| Strategy profiles | `docs/research/strategy-profiles/*.md` | static | G2 | Per-regime PF/WR matrix; identifies gaps (e.g. CHOPPY had zero positive profiles as of Jul 22 revalidation) |
| Regime validation reports | `docs/factory/regime-validation-*.md` (Phase 0 output) | per-quarter | G2 | Detector misclassifications are gaps |
| Confluence spec | `docs/specs/signal_engine-v2.3.md` (master roadmap §1C.6 reference) | static | G3 | The 8-stage TTC/TBD pipeline is the canvas for new confluences |
| ML confidence learner status | `src/forex_bot/confidence/`, `src/forex_bot/ml/confidence_learner.py` | static | G4 | Frozen during opt per Kaito rule — what new feature could unfreeze it? |
| Blend optimizer outputs | `src/forex_bot/ml/blend_optimizer.py`, recent Optuna studies | per-sweep | G4, G6 | Weight patterns reveal which archetypes are redundant |
| External ICT/SMC + CTA literature | `docs/research/sources/` (sourced RSS + saved papers) | weekly | G7 | Harknowledge-only ingest; never auto-promote from a single source |
| Opportunity briefs | `docs/research/opportunity-briefs/*.md` | static (existing) | G1 (template) | Satsuki-authored briefs are 4–6KB structured hypothesis docs — the *consumer-side* analog |
| ICIR / OOS gate research | `docs/research/icir-research-2026-07-08.md`, `oos-gate-research-2026-07-08.md` | static | G6 | Statistical primitives constrain what kinds of edges are even testable |
| Live forward-test loss attribution | `scripts/categorize_losses.py` (existing) output | per-week | G5 | Trade loss clusters by regime/session/news → brief seeds |
| Regime-gate cohort study | `data/research/srmr-hour-filter-analysis.md` (existing) | static | G2 | Existing micro-filter studies are pattern templates |

**Inputs the hopper explicitly does NOT consume (in order):**
- Raw tick data (too low-level; DSR gates handle this)
- Personal/intimate/financial partitions of MEMORY (out of scope; HR40 research lane)
- Untrusted trajectory material from another browser session (SOP rule 7 — data not instructions)
- The workboard itself as an input source (circular)

---

## 4. Section B — Hypothesis Generators

Seven generators. Each is implemented as a *script* (build lane), each consumes inputs from §3, each emits briefs into `data/research/hopper/briefing/`.

### G1 — Postmortem Miner
**Reads:** `docs/edges/*.md` (the 10 templates), `data/research/hopper/postmortems/*.md` (deferred briefs that scored below gate — the *rejects*), `data/research/qa_failures.jsonl` (5.5MB; bug quality history)

**Emits:** "Variant of existing strategy X with parameter Y based on observed failure pattern Z"

**Example brief it would produce today:**
> *"killzone_momentum_tuned uses adx_threshold=12 (vs default 15). The Jul 22 SRF revalidation shows ADX threshold of 12 produces 32% more trades at PF=0.93 — the gate is too loose. Variant hypothesis: raise ADX back to 18, drop retest_tolerance_atr from 1.5× to 1.0×, evaluate on EURUSD M15 only."*

**Frequency:** weekly (cron `hopper-postmortem-weekly`)

**Estimated SP (build):** 2.5

### G2 — Regime-Gap Finder
**Reads:** `docs/research/strategy-profiles/` (per-strategy regime PF), `data/research/research.duckdb` `runs.windows.regime_breakdown`, regime validation reports

**Emits:** "Regime R has no strategy with PF>1.2 AND ≥30 trades — propose a NEW strategy archetype targeting R"

**Current gap (as of Jul 22):** CHOPPY had zero positive-profile strategies (per master roadmap §1B.3 implication). Hopper should have already produced "Mean-reversion Bollinger Band with regime filter on CHOPPY only" — which is the hypothesis in `docs/edges/bb_rsi_reversion-hypothesis.md`. The brief is sitting in the hopper because the *implementation* failed, not the *hypothesis*. The generator must learn to distinguish hypothesis failure from implementation failure.

**Frequency:** nightly (cron `hopper-regime-gap-nightly`)

**Estimated SP (build):** 2.0

### G3 — Confluence Synthesizer
**Reads:** `docs/specs/signal_engine-v2.3.md`, the 8-stage TTC/TBD pipeline, per-confluence evidence in `docs/edges/ttc_xauusd-hypothesis.md` (the reference implementation), external research `docs/research/sources/`

**Emits:** "New confluence candidate: stage 5 (HTF squeeze) currently uses only ADX. Variant: add DXY correlation (20-bar) when DXY signal contradicts gold. Hypothesis: reduces counter-trend entries by ~30%."

**Guardrails:**
- Every confluence candidate is scored on **orthogonality** (does it add info beyond existing 14 in TTC?). Auto-reject if Pearson correlation > 0.7 with an existing TTC stage.
- External sources require ≥2 independent citations (card-creation rule).

**Frequency:** weekly (cron `hopper-confluence-weekly`)

**Estimated SP (build):** 3.0

### G4 — ML-Gap Extrapoler
**Reads:** `src/forex_bot/ml/confidence_learner.py`, recent Optuna studies, confidence-cascade logs

**Emits:** "RandomForest confidence learner is frozen during opt (Kaito rule). Variant: add feature X (e.g. session-volume percentile rank) and re-train per (symbol, timeframe). Hypothesis: rank-order improves by 8% in CHOPPY regime where current confidence is weakest."

**Scope discipline:** This generator does NOT propose ML model changes that touch production. Briefs route to the **ML development lane** (Tsubaki / dedicated ML owner) for experimentation before any wiring.

**Frequency:** bi-weekly (cron `bio-gap-biweekly`)

**Estimated SP (build):** 2.5

### G5 — Forward-Test Anomaly Detector
**Reads:** `data/edge_telemetry.jsonl` (per-tick, daily batched), `data/forward_test_health.json` (snapshot), live trade attribution

**Emits:** "Strategy X has Sharpe=0.4 in trailing 30d vs baseline 1.2. Hypothesis: regime affinity `regime_affinity=tuple('TRENDING',)` is no longer matching live regime. Variant: switch to dynamic affinity via rolling 60d regime distribution."

**Anomaly types that seed briefs:**
- Sharpe decay > 50% vs baseline (decay trigger, see implementation spec §5.2)
- Regime misclassification rate > 30% on live bars
- Per-session WR divergence (e.g. London WR dropped from 60% to 35%)
- Per-symbol drawdown > 6% in 30d
- Trade count < 25% of backtest rate for 5 consecutive sessions

**Frequency:** nightly (cron `bio-anomaly-nightly`)

**Estimated SP (build):** 2.0

### G6 — Tournament Champion Re-combiner
**Reads:** `data/tournament/scorecards/*.json` (top-N + bottom-N), `docs/research/strategy-profiles/` (per-regime matrix)

**Emits:** "Top-3 strategies share 80% weight in CHOPPY regime. Hypothesis: pair-wise correlation > 0.7. Variant: drop weakest of 3, replace with under-represented archetype (e.g. breakout in CHOPPY proxy)."

This generator is the **portfolio-shape** ingredient — it hunts for blend redundancy the way orthogonal cuts do for confluences.

**Frequency:** weekly (cron `bio-tournament-weekly`)

**Estimated SP (build):** 1.5

### G7 — External Idea Ingest
**Reads:** `docs/research/sources/` (curated RSS + arXiv + ICT/SMC + CTA literature), manual brief submissions

**Emits:** Each external idea becomes a brief in the hopper with **lower initial score** (0.25 baseline — promotion requires ≥2 independent sources OR one source + backtest evidence).

**Human gate (default):** External briefs NEVER auto-promote, regardless of score. They always require the human gate (§6) because they haven't been seen by our internal tournament yet.

**Frequency:** daily (cron `bio-external-daily`)

**Estimated SP (build):** 1.5

---

## 5. Section C — Brief Schema & Scoring

### 5.1 Brief schema (one file, one idea)

`data/research/hopper/briefing/<uuid>-<slug>.md` — modeled on `docs/edges/*.md`, with these required sections:

```markdown
# Hopper Brief: <name>

**UUID:** <full UUID, e.g. ea42e6be-...>
**Source generator:** <G1..G7>
**Generated at:** <ISO-8601>
**Kill-date trigger:** <explicit boolean expression — REQUIRED>
**Initial score:** <0.00–1.00>

## HYPOTHESIS
<1 paragraph>

## MECHANISM
<bulleted; ≥3 distinct causal steps>

## TIMEFRAME ARBITRAGE
<timeframe, information asymmetry, speed advantage>

## EVIDENCE OUTSIDE BACKTEST
<≥2 independent academic/industry citations OR explicit "single-source" penalty>

## ALTERNATIVE EXPLANATIONS
<≥2 reasons the backtest could be misleading>

## KILL CRITERIA
<≥3 observable conditions that should cause us to stop trading this>

## RAW-MATERIAL PROVENANCE
<paths, line numbers, conversation refs — links to inputs in §3>

## PROPOSED TUNING / VARIANT
<exact parameter delta or new feature>

## SUGGESTED OWNER LANE
<strategy | infra | ml | research>

## INITIAL CONTAINMENT
<does NOT touch production; quarantined until score ≥ 0.65 OR explicit human gate>
```

### 5.2 Scoring rubric (0.00 – 1.00)

Briefs are scored on six dimensions; weight in parentheses:

| Dimension | Weight | Sub-rubric |
|---|---|---|
| **Mechanism clarity** | 0.20 | Causal steps explicit (0.5 each, up to 4) |
| **Evidence diversity** | 0.25 | # independent academic sources × 0.1 + # industry citations × 0.05 + # internal backtest anchors × 0.10; capped at 1.0 |
| **Regime coverage fit** | 0.15 | Maps to under-served regime (CHOPPY etc.) = +0.10; maps to saturated regime = -0.05 |
| **Orthogonality** | 0.15 | Correlation to existing strategies (correlation matrix from blend_optimizer) < 0.4 = +0.10; > 0.7 = -0.10 |
| **Kill-criterion observability** | 0.10 | All 3+ criteria tied to observable live signal = +0.10; vague ("market shifts") = 0.0 |
| **Freshness** | 0.15 | Days since generation: 0-7 = full; 14-30 = -0.05; 30-60 = -0.10; >60 = forced re-score or kill |

**Promotion thresholds:**
- `score ≥ 0.65` → auto-promote to factory intake (Sprint B card)
- `0.40 ≤ score < 0.65` → human-gate lane (waits for review)
- `score < 0.40` → re-score on next generator tick OR kill when kill-date fires

### 5.3 Kill-date format (mandatory boolean)

Per card-creation skill (2026-10-04 session audit): every deferred brief states its trigger as an explicit boolean. Examples:

```
trigger: date >= 2027-01-01
trigger: forward_test_trade_count >= 50
trigger: research.duckdb.qa_failures.jsonl record_count >= 1000
trigger: regime_distribution.trending >= 0.60 OR date >= 2025-12-31
trigger: hopper_score_below(0.40, count=2) OR date >= 2026-12-31
```

Prose like "next write or when applicable" fails the commit hook. Implementation: a `hopper_lint.py` script enforces this at file-write time.

---

## 6. Section D — Hopper Directory & Lifecycle

### 6.1 File layout

```
data/research/hopper/
├── briefing/                # active briefs (lifecycle state: ACTIVE)
│   └── <uuid>-<slug>.md
├── score/                   # latest score JSON per brief
│   └── <uuid>.json
├── kill_log/                # briefs that hit kill-date (lifecycle state: KILLED)
│   └── <uuid>-<killed-at>.md
├── promote_log/             # briefs that hit promotion threshold (state: PROMOTED)
│   └── <uuid>-<promoted-at>.md  # contains the Sprint B card UUID this brief became
├── postmortems/             # briefs that were promoted, validated, and quarantined
│   └── <uuid>-<postmortem-at>.md  # feeds back to G1 Postmortem Miner
└── hopper_log.jsonl         # append-only lifecycle events (one row per state transition)
```

### 6.2 Lifecycle

```
[BRIEF CREATED]
       │
       ▼
   [ACTIVE]  ──score<0.40 + kill-date fires──▶ [KILLED]
       │
       ├──score≥0.65──▶ [PROMOTED] ──sprint B card created──▶ [VALIDATED]
       │                                                  │
       │                                  ┌──pass──▶  [QUARANTINED_OK] (deploy_pool)
       │                                  │
       │                                  └──fail──▶  [POSTMORTEM] (back to postmortems/)
       │
       └──0.40≤score<0.65──▶ [HUMAN_GATE] ──approve──▶ [PROMOTED]
                                         └──reject──▶ [KILLED]
```

**Key invariant:** every state transition writes a row to `hopper_log.jsonl`. The log is the audit trail; it is the only durable evidence that the hopper ran.

---

## 7. Section E — Triage & Promotion Gate

### 7.1 Two-gate design

```
score ≥ 0.65                0.40 ≤ score < 0.65              score < 0.40
     │                              │                              │
     ▼                              ▼                              ▼
[Auto-Promote]              [Human-Gate]                  [Re-score on next tick]
     │                              │                              │
     ▼                              ▼                              ▼
Create Sprint B card         Wait for review              Auto-kill on kill-date
linked to brief UUID         (Ava/Himari portfolio)       (or re-score if new evidence)
```

### 7.2 Auto-promote criteria (all must hold)

1. `score ≥ 0.65`
2. `kill-date trigger` is set and is not "immediate" (must have a measurable horizon)
3. **Source generator ≠ G7** (external sources always require human gate — §4.7)
5. Brief age ≤ 2 days at promotion time (older briefs are re-scored first)
5. No active `HUMAN_GATE` review pending for the same `(template, pair, timeframe)` cell
6. OOS isolation is respected — briefs touching `2026-01..2026-07` data must carry `oos_unlocked: true` flag, which only a human can set

### 7.3 Human-gate criteria

Auto-promote fails on ANY of:
- Score in `[0.40, 0.65)`
- Source generator is G7 (external)
- Brief touches OOS window without `oos_unlocked: true`
- More than 3 briefs already promoted for the same `(template, pair, timeframe)` cell in the last 30 days (rate-limit per cell)

Human gate reviews happen at a single weekly checkpoint (Saturday 06:00 ET). The hop-during-the-week pattern (too many tiny decisions) is explicitly avoided.

### 7.4 Promotion hand-off

When a brief becomes PROMOTED:
1. A Sprint B factory-intake card is created with:
   - Title: `[BUILD][FACTORY-INTAKE] <brief.name> (from hopper brief <brief.uuid>)`
   - Body: the entire brief markdown, verbatim
   - Parent link: `wg/ea42e6be-...` (this design card)
   - Priority: derived from brief.score
   - Owner lane: per the brief's `SUGGESTED OWNER LANE`
   - Labels: `[NEW:INTAKE]`, `[FROM:HOPPER]`, `<brief.<source_generator>>`
2. The card routes into the existing Sprint A pipeline (tournament → validate → verdict), which is already end-to-end on Ayumi main.
3. The hopper_log.jsonl gets a `PROMOTED` event with the new card UUID.
4. The brief's `promote_log/<uuid>-<promoted-at>.md` is written with the new card UUID.

**This is the bridge that makes the hopper "talk to" the factory.** Without it, the hopper is a file drawer; with it, every brief becomes a real work item.

---

## 8. Section F — ML Development Lane (Parallel Generation)

The ML lane is a parallel source of briefs, not a downstream consumer.

### 8.1 Why parallel

The ML pipeline (confidence learner, blend optimizer) has its own R&D pace: per-cell decision rules, fresh-feature tests, Optuna rerun scheduling. It cannot wait on the strategy hopper for ideas, and it cannot leak untested ML changes into strategy briefs.

### 8.2 ML lane owns

- **Generator G4 — ML-Gap Extrapoler** (§4)
- Briefs about features (new indicators, regime signals, session tags)
- Briefs about model selection (Insight, Blend, MLP gates, RF, etc.)
- Briefs about training data shape (OOS vs IS, embargo handling, regime stratification)

### 8.3 ML lane does NOT own

- Strategy logic (strategy lane)
- Regime detector (strategy lane + Tomoe for infra)
- Blend weights (strategy lane)
- Compute scheduling (infra lane)

### 8.4 ML brief promotion

ML briefs promote into the **ML development lane** (Sprint ML), not into the strategy factory. The two pipelines share the **scoring rubric** and the **kill-date rule** but emit into separate work queues. This is the cleanest separation of concerns.

---

## 9. Section G — Compute & Scheduling

### 9.1 Compute surface

| Surface | Use case | Cost model |
|---|---|---|
| **Local (xeon)** | Generators G1, G2, G5, G6 (low compute, read-mostly) | Host CPU caps at 20%; cpulimit + nice per CG9 §8 (existing factory pattern) |
| **Node (ava-worker-local)** | Generator G4 ML sweeps, G3 Confluence heavy correlation matrix | Already wired (card `b214a662-518c-4b05-ad67-fa051174c1c6` precedent: "Offload TSC/pytest builds to ava-worker-local node (Craig-supervised)"); card `b0887c99-4f4a-48d9-9054-d6f70153f65e` tracks node evolution |
| **Cron (OpenClaw)** | Schedule all generators | Existing factory cron mechanism (Sat 04:00 ET weekly pilot, monthly full sweep) |

### 9.2 Schedule (per factory precedent + hopper needs)

| Job | Schedule | Surface | Wall-clock |
|---|---|---|---|
| `hopper-regime-gap-nightly` (G2) | Daily 02:00 ET | Local | ≤10 min |
| `hopper-anomaly-nightly` (G5) | Daily 03:00 ET | Local | ≤15 min |
| `hopper-external-daily` (G7) | Daily 04:00 ET | Local | ≤5 min |
| `hopper-postmortem-weekly` (G1) | Weekly Sat 01:00 ET | Local | ≤20 min |
| `hopper-confluence-weekly` (G3) | Weekly Sat 02:30 ET | Node | ≤60 min |
| `hopper-tournament-weekly` (G6) | Weekly Sat 04:30 ET | Local | ≤10 min |
| `hopper-ml-biweekly` (G4) | Bi-weekly Sun 02:00 ET | Node | ≤90 min |
| `hopper-lint-nightly` | Daily 01:00 ET | Local | ≤2 min (file-hook on commit + nightly sweep) |
| `hopper-human-checkpoint` | Weekly Sat 06:00 ET | Local | operator-driven review queue dump |

### 9.3 CPU caps (per CG9 §9 host rules)

- `nice -n 19` for all hopper processes
- `cpulimit -l 15` per worker (reduced from CG9 §6.2's 20%, per Craig directive noted in restructured plan)
- `threads=1`, `memory_limit=384MB` per DuckDB connection
- 600s per-job hard timeout
- Forward-test tick latency monitoring during hopper runs (auto-pause if latency > 2s)

### 9.4 Lock safety

- Hopper generators acquire `data/research/research.lock` (existing) before reading
- Hopper generators DO NOT acquire `factory.lock` (the existing factory sweep lock) — they are read-only against factory state
- ML generator (G4) acquires `data/research/ml.lock` (new, but trivial — `flock` pattern)
- `hopper_log.jsonl` writes are serialized via a single-writer pattern (one process owns the file per cycle)

---

## 10. Section H — Confluence Research (TTC/TBD Framework)

### 10.1 Why confluence research is its own section

The 8-stage TTC/TBD pipeline (master roadmap §1C.6) is the confluence canvas. New ideas ("does adding stage 9 — DXY correlation — improve DD?") are *cross-domain*: they touch ML, strategy, and macro. They don't fit cleanly into any one generator. They deserve their own generator (G3) and their own validation path.

### 10.2 Confluence brief schema (subset of §5.1)

Required additions:
- **TTC stage being modified or added** (e.g. "stage 5 — HTF squeeze")
- **Existing stages the confluence correlates with** (orthogonality check, Pearson < 0.7)
- **Information source cited**: must be ≥2 of (academic, internal backtest, ICT/SMC literature, market-microstructure paper)
- **Realizability test**: can the confluence be computed in <50ms per M15 bar on production hardware? (Hard gate for auto-promotion)

### 10.3 Realizability gate

Confluence briefs that add >50ms per bar to the production signal pipeline auto-fail the orthogonality dimension of the score (§5.2). This is the *cost of complexity* discipline — every confluence must pay for itself in PF or WR, and must not starve the live tick budget.

---

## 11. Section J — Sprint B Build Proposal (Card-Sized Decomposition)

> Each card ≤3 SP per HR41. Build lane is Reina/Tsubaki/Rin (strategy); Tomoe/Riko (infra); Satsuki/Mika (research). Sprint B = Sprint A + Hopper. Sprint A is `bc8439dd` (done per trigger).

### Build lane — Strategy (Reina/Tsubaki)

| # | Card UUID (proposed) | Title | Type | Lane | SP | Trigger / dep | Acceptance string |
|---|---|---|---|---|---|---|---|
| 1 | `b7e8a1f0-...` | Hopper schema + lint hook (brief markdown schema, hopper_lint.py, kill-date boolean enforcement) | build | strategy | 1.5 | Sprint A done | `pytest tests/hopper/test_schema.py` passes; `python3 scripts/hopper_lint.py data/research/hopper/briefing/` exits 0 on sample brief |
| 2 | `b7e8a1f1-...` | Hopper scorer + scoring rubric (six dimensions, weights, freshness) | build | strategy | 2.0 | card 1 done | `pytest tests/hopper/test_scorer.py` passes; sample brief scores in expected range |
| 3 | `b7e8a1f2-...` | G2 Regime-Gap Finder implementation + cron | build | strategy | 2.0 | card 2 done | One real regime-gap brief (CHOPPY) appears in `briefing/` within 24h of cron |
| 4 | `b7e8a1f3-...` | G5 Forward-Test Anomaly Detector implementation + cron | build | strategy | 2.0 | card 2 done | One real anomaly brief (decay trigger) appears within 24h |
| 5 | `b7e8a1f4-...` | G1 Postmortem Miner + G6 Tournament Champion Re-combiner + crons | build | strategy | 2.5 | cards 3,4 done | Both generators emit ≥1 brief weekly; `hopper_log.jsonl` shows entries |
| 6 | `b7e8a1f5-...` | Auto-promotion gate (score ≥ 0.65) + Sprint B card creation | build | strategy | 2.5 | cards 1,2 done; Sprint A intake lane verified | A real auto-promotion creates a `[BUILD][FACTORY-INTAKE]` card linked to the brief UUID within 24h |

### Build lane — ML (Tsubaki/Riko)

| # | Card UUID (proposed) | Title | Type | Lane | SP | Trigger / dep | Acceptance string |
|---|---|---|---|---|---|---|---|
| 7 | `b7e8a1f6-...` | G4 ML-Gap Extrapoler (features, confidence learner variants) — read-only on production | build | ml | 2.5 | card 2 done | One ML brief appears in `briefing/` within bi-weekly cron; brief routes to ML lane, not factory |
| 8 | `b7e8a1f7-...` | ML brief promotion hand-off (ML lane card creator, separate from factory intake) | build | ml | 1.5 | card 7 done | ML brief promotion creates a `[BUILD][ML-LANE]` card, not a `[FACTORY-INTAKE]` card |

### Build lane — Infra (Tomoe/Riko)

| # | Card UUID (proposed) | Title | Type | Lane | SP | Trigger / dep | Acceptance string |
|---|---|---|---|---|---|---|---|
| 9 | `b7e8a1f8-...` | Node offload adapter for hopper (mirrors `run_matrix_remote` pattern, card `79793579` precedent) | build | infra | 3.0 | node runner stable (cards `b214a662` + `79793579` done) | G3/G4 cron jobs run on `ava-worker-local` via ssh transport; local CPU usage stays ≤20% during sweep |
| 10 | `b7e8a1f9-...` | Hopper cron definitions + OpenClaw cron registration (7 cron jobs from §9.2) | build | infra | 1.5 | cards 1-9 done | `crontab -l` shows 7 hopper entries; `cron_runs` table records each run |

### Build lane — Confluence (Reina/Tsubaki)

| # | Card UUID (proposed) | Title | Type | Lane | SP | Trigger / dep | Acceptance string |
|---|---|---|---|---|---|---|---|
| 11 | `b7e8a1fa-...` | G3 Confluence Synthesizer + orthogonality gate + realizability timer | build | strategy | 3.0 | card 2 done; node offload (card 9) verified | One new confluence candidate per week; ≥2 independent citations; per-bar cost <50ms measured |
| 12 | `b7e8a1fb-...` | External idea ingest (G7) + RSS/arxiv source curation policy | build | research | 1.5 | cards 1,2 done | `docs/research/sources/` curated set exists (≥5 sources, each with date + URL + 1-line summary); external briefs require human gate |

### Build lane — Validation / Review

| # | Card UUID (proposed) | Title | Type | Lane | SP | Trigger / dep | Acceptance string |
|---|---|---|---|---|---|---|---|
| 13 | `b7e8a1fc-...` | Hopper runbook (`docs/runbooks/hopper-runbook.md`) + human-gate Saturday checkpoint CLI | build | research | 1.5 | cards 1-12 done | Runbook covers: reading `hopper_log.jsonl`, manual re-score, kill-on-sight, lint override |
| 14 | `b7e8a1fd-...` | Hopper end-to-end smoke test (all generators fire, all paths lint, all crons register) + Rin review | investigation | review (Rin) | 1.0 | cards 1-13 done | `pytest tests/hopper/test_e2e.py` passes; one brief per generator is observed in `briefing/` within 72h of cron activation; Rin clears `data/build-rin-reviews/<id>-cleared.json` |

**Total: 14 cards / ~29 SP / sequential with parallel pairs.**

**Pairwise parallel pairs (no forced critical path between):**
- (1 → 2) is sequential
- (3 ‖ 4) — both depend on 2; both can run in parallel
- (5) depends on 3,4
- (6) depends on 1,2; can run in parallel with 5
- (7) depends on 2; can run in parallel with 3,4
- (8) depends on 7
- (9) depends on node stability; orthogonal to all above
- (10) depends on 1-9
- (11) depends on 2,9
- (12) depends on 1,2
- (13) depends on 1-12
- (14) depends on 1-13

**Estimated calendar: 6 weeks at 1 builder + 1 ML owner + 1 infra owner (with pairwise parallelism).**

### 11.1 Card sizing rationale

The implementation spec (Tsubaki §7) targets 30.5 SP for the *factory pipeline itself*; the hopper is ~29 SP on top of that. They are roughly equal in scope because they do equal work — the factory validates edges, the hopper sources them. Sprint B = Sprint A + Hopper, ~60 SP, ~10 weeks at 1 owner / 6 weeks with pairwise parallelism.

---

## 12. Section K — Open Questions / Risks

| # | Question / Risk | Likelihood | Impact | Resolution |
|---|---|---|---|---|
| 1 | **Brief quality will be poor at first.** Without scoring calibration, the hopper will emit noise. | HIGH | MEDIUM — wasted work | Score rubric is calibrated on 10 hand-written `docs/edges/` briefs first. First 2 weeks are *calibration only*, no promotion. |
| 2 | **Auto-promotion could flood Sprint B intake.** A burst of high-score briefs → Sprint B backlog growth. | MEDIUM | HIGH — same failure family as factory backlog (per AGENTS.md) | Rate-limit per `(template, pair, timeframe)` cell: max 3 promotions / 30 days. Cross-cell breaches: weekly checkpoint review. |
| 3 | **Kill-date as boolean is enforceable but ugly.** Natural-language "kill when regime shifts" must become `regime_ADX.trending >= 0.60 OR date >= YYYY-MM-DD`. | HIGH (friction) | LOW | `hopper_lint.py` suggests boolean rewrites on commit hook failure; runbook includes examples. |
| 4 | **ML lane brief format conflicts with strategy brief format.** | MEDIUM | LOW | Both use §5.1 schema; only `SUGGESTED OWNER LANE` differs. ML lane gets a separate promote_log subdirectory. |
| 5 | **Node offload dependency (card 9) on node stability work.** | MEDIUM (depends on other cards) | MEDIUM — delays build start | Hopper can run entirely on local for the first 4 weeks; node offload is an optimization, not a dependency for go-live. |
| 6 | **Confluence orthogonality requires correlation matrix that doesn't exist yet.** | HIGH | MEDIUM | First sprint: produce correlation matrix from existing 10 strategies × 14 TTC stages; persist in `data/research/confluence_correlation.parquet`. Card 11 needs this. |
| 7 | **The 14-card proposal looks like waterfall, not agile.** | LOW | LOW | Each card is independently testable; pairwise parallels exist; weekly checkpoint is human-driven. |
| 8 | **No freshness check on the hopper itself.** The hopper could silently degrade. | MEDIUM | MEDIUM | `hopper_health.py` (small cron) checks brief emission rate per generator, score distribution, promotion log. Alerts on anomaly. |
| 9 | **G7 (external) ingest is the most likely place to import bad ideas.** | HIGH | HIGH (if mishandled) | G7 never auto-promotes; always human gate; ≥2 source requirement; "single-source" wordier penalty in §5.2. |
| 10 | **What if Sprint A's intake lane rejects our auto-promotions?** | LOW | HIGH — loop broken | Auto-promotion creates the card with `labels=[NEW:INTAKE]`. Sprint A's intake lane either accepts (card moves to `running`) or rejects (back to hopper_log as `REJECTED_BY_INTAKE` — feeds G1 Postmortem Miner). |

---

## 13. Section L — Where to Start Coding (Build Lane)

Build lane reference map. All paths relative to `/home/TacoPants/projects/Ayumi/`.

| New file | Purpose | Card |
|---|---|---|
| `src/forex_bot/hopper/__init__.py` | Package init | 1 |
| `src/forex_bot/hopper/schema.py` | Brief dataclass + kill-date parser | 1 |
| `src/forex_bot/hopper/scorer.py` | Six-dimension scoring rubric | 2 |
| `src/forex_bot/hopper/lint.py` | Lint hook (kill-date boolean, required fields) | 1 |
| `src/forex_bot/hopper/promote.py` | Auto-promotion gate + Sprint B card creation | 6 |
| `src/forex_bot/hopper/log.py` | Append-only `hopper_log.jsonl` writer | 1 |
| `src/forex_bot/hopper/generators/g1_postmortem.py` | Postmortem Miner | 5 |
| `src/forex_bot/hopper/generators/g2_regime_gap.py` | Regime-Gap Finder | 3 |
| `src/forex_bot/hopper/generators/g3_confluence.py` | Confluence Synthesizer | 11 |
| `src/forex_bot/hopper/generators/g4_ml_gap.py` | ML-Gap Extrapoler | 7 |
| `src/forex_bot/hopper/generators/g5_anomaly.py` | Forward-Test Anomaly Detector | 4 |
| `src/forex_bot/hopper/generators/g6_tournament.py` | Tournament Champion Re-combiner | 5 |
| `src/forex_bot/hopper/generators/g7_external.py` | External Idea Ingest | 12 |
| `scripts/run_hopper_generator.py` | CLI wrapper for one generator per invocation | 1+ |
| `scripts/hopper_lint.py` | Standalone lint runner (also used as commit hook) | 1 |
| `scripts/hopper_health.py` | Freshness / emission-rate check | card 8 (post-launch) |
| `data/research/hopper/` | Root dir (briefing/, score/, kill_log/, promote_log/, postmortems/, hopper_log.jsonl) | 1 |
| `data/research/confluence_correlation.parquet` | Pre-computed TTC stage correlation matrix | 11 prereq |
| `tests/hopper/test_schema.py` | Brief schema + kill-date boolean | 1 |
| `tests/hopper/test_scorer.py` | Six-dim score | 1 |
| `tests/hopper/test_lint.py` | Lint hook failures + sample clean brief | 1 |
| `tests/hopper/test_promote.py` | Auto-promotion card creation | 6 |
| `tests/hopper/test_generators.py` | Each generator emits ≥1 brief on fixture input | 3-7 |
| `tests/hopper/test_e2e.py` | All generators + cron + promote + postmortem loop | 14 |
| `docs/runbooks/hopper-runbook.md` | Operator runbook | 13 |
| `docs/specs/hopper-spec.md` | Spec for build lane review (mirror of this doc, code-focused) | 13 |

---

## 14. Section M — Acceptance Criteria for THIS Design Card

This card `ea42e6be-ed0f-4044-ab5d-3c8c5eb8baf6` is **design + sprint proposal only** (HR40 research lane). Acceptance:

- [x] **Generation loop specified (§§3-4)** — seven generators, mapped to inputs and outputs
- [x] **Raw material catalog (§3)** — every input the hopper could consume is mapped
- [x] **Triage rules (§7)** — two-gate (auto ≥0.65, human 0.40-0.65, kill <0.40)
- [x] **Compute & scheduling (§9)** — local + node + cron, with CPU caps matching CG9 §9
- [x] **ML development lane (§8)** — parallel, not downstream
- [x] **Confluence research (§10)** — first-class generator + realizability gate
- [x] **Build-sprint proposal (§11)** — 14 cards / ~29 SP / pairwise parallel
- [x] **HR41 compliance** — every card ≤3 SP
- [x] **Kill-date rule (§5.3)** — explicit boolean expression required, per session-audit 2026-10-04
- [x] **Node offload (§9.1 + card 9)** — references existing node-evolution card `b0887c99-4f4a-48d9-9054-d6f70153f65e` (claimed in §K context)
- [x] **Hand-off to Sprint A (§7.4)** — promotion creates Sprint B factory-intake card linked to brief UUID

**Stopping condition met:** the question "design the R&D hopper engine" is answered end-to-end at research-doc level. What remains unknown is whether the scoring rubric will actually separate good from bad ideas — that is an *empirical* question that the build lane must answer through calibration (K1 in §13).

---

## 15. Recommended Next Step

**For Ava:** Approve the 14-card sprint proposal as Sprint B. Card 1 (schema + lint hook) can start immediately — it is the smallest, most-isolated, and unlocks everything else. Pair with card 2 (scorer) on the same builder slot.

**For Craig:** No action — this card does not request funds. Compute decisions for cards 9 (node offload) and the cron infra in card 10 are infra-lane-owned per existing node-evolution precedent.

**For Himari:** Portfolio routing decision — confirm the strategy-factory hopper promotion hand-off (§7.4) routes into Sprint B intake lane, not a separate portfolio queue. The two pipelines should share the Sprint A intake lane.

---

## Appendix A — Cross-Reference Index

| Reference | Path |
|---|---|
| Implementation spec | `docs/roadmaps/strategy-factory/strategy-factory-implementation-spec.md` |
| Vision doc | `docs/roadmaps/strategy-factory/strategy-factory-vision-v1.md` |
| Restructured plan | `docs/plans/strategy-factory-restructured-2026-08-02.md` |
| Master roadmap | `docs/roadmaps/ayumi-master-roadmap.md` |
| Tournament spec | `docs/plans/ayumi-tournament-harness-spec.md` |
| Sprint A parent | `bc8439dd-c329-4fb1-8ecd-a22fbdfd045f` (card — done 2026-10-05 00:47 EDT) |
| Hopper design card (this) | `ea42e6be-ed0f-4044-ab5d-3c8c5eb8baf6` |
| Node-evolution tracking | `b0887c99-4f4a-48d9-9054-d6f70153f65e` (card) |
| Node-offload precedent | `b214a662-518c-4b05-ad67-fa051174c1c6` (card — offload builds to ava-worker-local node) |
| Tournament walking-skeleton | `db04d5b5-e6b0-4fb8-9038-3cbf11e4dd00` (card) |
| Hand-written edge templates | `docs/edges/*.md` (10 hypothesis briefs) |
| Opportunity briefs (consumer-side analog) | `docs/research/opportunity-briefs/*.md` |
| Strategy factory implementation spec (Tsubaki §6.2, §9 cron rules) | cited inline as "CG9 §" |
| Card-creation deferred-trigger rule | `~/.openclaw/workspace/skills/card-creation/SKILL.md` (2026-10-04 session audit) |
| Forward-test telemetry input | `data/edge_telemetry.jsonl`, `data/forward_test_health.json` |
| Research DB | `data/research/research.duckdb` (tables: runs, windows, trades, study_ledger, cron_runs, metrics_summary, monte_carlo_samples, portfolio_runs, research_queue, v_pair_performance, v_promotion_queue, v_top_strategies) |
| ML confidence learner (frozen during opt per Kaito) | `src/forex_bot/ml/confidence_learner.py` |
| Blend optimizer | `src/forex_bot/ml/blend_optimizer.py` |

---

*End of design. Total length: ~570 lines. Ready for Ava review and Sprint B decomposition into workboard cards.*