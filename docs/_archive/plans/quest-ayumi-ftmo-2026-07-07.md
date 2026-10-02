# Quest: Ayumi FTMO Path (v2 — Revised)

**Status:** Revised per council review (Sora, Kaito, Rei — all APPROVE-WITH-FINDINGS)
**Date:** 2026-07-07 (v1) → 2026-07-08 (v2 revision)
**Owner:** Ava (orchestration), Craig (decisions)
**Trigger:** 3 months of struggling to get Ayumi off the ground. Need a coherent path to MVP and a true FTMO test.

**Craig decisions (approved 2026-07-08 00:10 EDT):**
- FTMO target: **1-Step** (3% daily / 10% total / no min trading days)
- Strategy scope: **XAUUSD-only** (USDJPY as Phase 3 fallback if M15 retunes)
- Pre-quest gate: **Close blockers first** as Phase 0
- Revision path: **Ava revises + re-reviews** with council before dispatch

---

## Objective

Get Ayumi to a state where it can pass an FTMO 1-Step forward test on a cTrader demo account, starting with a single validated strategy (SRMR+ on XAUUSD), with a self-healing operational layer.

## Completion Condition (verifiable)

A forward test on cTrader demo running under **FTMO 1-Step rules** that passes challenge criteria:
- Daily loss ≤ 3% of start-of-day equity
- Total drawdown ≤ 10% from peak equity
- Profit target: +10%
- Best-day rule: best day's profit ≤ 50% of total positive-days profit
- 30+ consecutive compliant days
- Self-healing log showing auto-remediation of common failures

---

## FTMO Profile (PINNED — canonical source of truth)

**FTMO 1-Step Challenge** — all risk config must reference this:

| Parameter | Value | Notes |
|-----------|-------|-------|
| Daily loss limit | 3% of start-of-day equity | Resets at CET midnight (17:00 America/Toronto during EDT) |
| Max total loss | 10% from peak equity | Peak equity trails upward with profits |
| Profit target | 10% of starting balance | |
| Best-day rule | ≤ 50% of total positive-days profit | Currently NOT implemented — Phase 0 deliverable |
| Max positions | 3 | Per FTMO standard |
| Risk per trade | 0.5% | × 3 positions = 1.5% max concurrent |
| Account size | $100,000 (demo) | Reconcile all configs to this |
| Min trading days | None (1-Step advantage) | |

**Canonical config location:** `src/forex_bot/risk/ftmo_guard.py` `FTMOConfig` — all other references (`config/strategies.yaml`, `sl_position_sizer.py`, `backtest/ftmo_simulation.py`) must import from this.

**Current config disagreement (must reconcile in Phase 0):**
| File | Daily loss | Total DD | Account |
|------|-----------|----------|---------|
| `config/strategies.yaml` | 4% | 7% | $10,000 |
| `risk_guard.py` FTMOProfile | 5% | 10% | $100,000 |
| `risk/ftmo_guard.py` | 4% (default) | — | — |
| `risk/sl_position_sizer.py` | — | 7% (account_dd_limit) | — |
| `backtest/ftmo_simulation.py` | 5% | 10% | — |

---

## Phases (~20-25 SP, 6-10 weeks wall-clock)

### Phase 0: Pre-Quest Gate (0.5 SP, 1 day)

**Why this exists:** Rei's review identified open pre-existing cards and config inconsistencies that will block the quest mid-sprint. Fix these first.

**Deliverable:** Clean baseline — forward test stable, FTMO config canonicalized, known blockers resolved.

**Acceptance criteria:**
- [ ] `2893597d` (remediation_validated.flag deletion) — **DONE** ✅ (completed 2026-07-07)
- [ ] `2c5d684a` (TP/SL live-fire verification) — **OPEN** — requires market-hours verification: open 1 position on cTrader demo, confirm TP/SL visible, screenshot, close. Needs forward test running + at least 1 signal.
- [ ] **FTMO risk config reconciliation:** Update all 5 sources to 1-Step canonical values (3%/10%/$100K). Make `FTMOConfig` in `risk/ftmo_guard.py` the single source of truth; all other files import from it.
- [ ] **Best-day rule implementation:** Add to `ftmo_guard.py` — track daily P&L, flag if best day > 50% of total positive-days profit
- [ ] **Disable strategies on symbols with no data:** `srmr_audusd_h1`, `srmr_usdchf_h1`, `srmr_usdcad_h1` in `config/strategies.yaml` (no historical data exists — latent bug inflating strategy count)
- [ ] **Commit or revert** the uncommitted `signals_failed_live reconciliation defense` (+10 lines in `forward_test_engine.py`)
- [ ] **Fix `CsvDataLoader.load_parquet.reset_index()`** — 1-line bug blocking FX-major WF revalidation at M15
- [ ] Forward test runs ≥24h uptime with no operator intervention
- [ ] `data/.credentials` exists, mode 0600, owner = runtime user

### Phase 1A: Architecture & Live-Execution Audit (3-4 SP, parallel with 1B)

**Why this exists (REVISED per Sora):** The plan's original premise ("10 strategies, 0 trades = coordination problem") is **wrong**. Per `data/forward_test_health.json`: 10 signals generated, 2 traded, 2 `signals_failed_live`. The bottleneck is **live-execution bugs** (balance oscillation, USDJPY TP 100x, Amend SL/TP timeouts), not strategy R&D. Phase 1A must audit the execution path first.

**Deliverable:** Architecture audit doc with prioritized gap list. Strategy qualification sub-gate with per-strategy PASS/FAIL verdicts.

**Acceptance criteria:**
- [ ] **Live-execution path audit:** Trace signal → risk check → sizing → order submission → fill → position registration. Identify where the 2 `signals_failed_live` fail. Check for uncommitted code, stale fixes, connection stability.
- [ ] **Account balance anomaly:** `risk_guard_state.json` shows balance 9314.08 from 10000.0 (-6.86%) with 0 closed trades. Where is the loss coming from? (Kaito finding)
- [ ] **Strategy qualification sub-gate (Rei):** Walk-forward evaluation of current 10 strategies against existing historical data. Numeric criteria: Sharpe ≥ 0.5, WR ≥ 55%, ≥3/5 WF windows positive, MaxDD < 5%. Per-strategy verdict: PASS / FAIL / NEEDS-RESEARCH. Reference existing WF revalidation report (`docs/forex/wf-revalidation-2026-07/REPORT.md`).
  - **Expected outcome:** SRMR+ XAUUSD = PASS (5/5, PF 8.0, Sharpe 13.68). Everything else = FAIL or NEEDS-RESEARCH at current resolution.
- [ ] **Signal-firing acceptance test (Kaito):** 30-min DRY_RUN must produce ≥1 live-grade signal. Zero signals = fail, not pass. Distinguish "engine running" from "real signals firing."
- [ ] **Engine inventory:** Document all 5 engine implementations (`ForwardTestEngine`, `TradingOrchestrator`, `MultiStrategyOrchestrator`, `SignalOrchestrator`, `BlendForwardTestRunner`) with status (production/deprecated/experimental) and consolidation plan
- [ ] **Existing infrastructure inventory:** Map plan concepts to existing implementations:
  - DRY_RUN → `ForwardTestConfig.execution_mode` (extend to `{"dry_run", "paper", "live"}`, not a third boolean)
  - Paper broker → existing `PaperTrader` (wrap, don't replace)
  - L0-L4 tiers → build on `AnomalyMonitor` + `KillSwitchManager` (not parallel)
  - Structured heartbeat → existing `HealthMonitor` `[B5 Health]` / `[S1 Health]`

### Phase 1B: Hayate Checkpoint Design (2 SP, parallel with 1A)

**Why this exists:** Hayate's daily audit needs a checkpoint registry. **Revised per Kaito:** Must reference existing `AnomalyMonitor` (3 failure modes: data_silence, zero_signals, zero_pnl_variance), `HealthMonitor`, and `KillSwitchManager` (kill/freeze/per-strategy freeze). Build on these, not beside them.

**Deliverable:** Checkpoint registry, runbook per failure mode, drift detection calibrated to actual sprint cadence.

**Acceptance criteria:**
- [ ] Checkpoint catalog covering: system health, trading health, data health, pipeline health, north-star progress, kanban hygiene
- [ ] Runbook per checkpoint (detection → action), referencing existing monitors where they exist
- [ ] **Drift detection calibrated (Rei):** 3d/7d for cards, 2d/5d for phases — calibrated to actual sprint cadence (1-3 day sprints, not 7-14 day assumptions)
- [ ] **`remediation_validated.flag` handling:** Document whether plan uses existing flag or introduces new one. (Existing flag auto-recreates per forward_test_engine.py:576.)
- [ ] `signal_validator.py` (flagged DEAD in 2026-04-17 audit) — explicitly retire or document why it lives

### Phase 2: Engine Consolidation + Resource Caps (2-3 SP)

**Why this exists (REVISED per Kaito):** Phase 2 was "modularize signal engine" — but there are **5 engine implementations** and Phase 2 risks adding a 6th. Consolidation must come before modularization.

**Deliverable:** Canonical engine declared, consolidation path defined, resource caps wired into production scripts.

**Acceptance criteria:**
- [ ] **Engine consolidation plan (Kaito MUST):** Declare canonical engine (likely `ForwardTestEngine` — most production usage, bar-close invariant verified). Path to consolidate the other 4 into mixins or delete. No 6th engine.
- [ ] **Resource cap wiring (Kaito MUST):** `cpu_limited` / `memory_capped` wired into every `scripts/run_*.py` at top of `main()`. ProcessPoolExecutor workers capped (`max_workers=2` in sweep_runner). CI check that fails if new script omits resource wrapper.
- [ ] Memory: 3 GB hard limit per process (2 GB is too tight for Optuna sweep with 13 strategies × 7 pairs). L0 health alert at 2 GB.
- [ ] CPU: 25% advisory (document as advisory, not hard cap — `nice` + `affinity` fallback, not cgroups)
- [ ] Single-process forward test lock enforced via `data/forward_test.pid`
- [ ] `TradeStore` single-writer bottleneck documented for Phase 6 concurrency tests

### Phase 3: Strategy Validation — XAUUSD-First (2-3 SP, reduced from 4-5)

**Why this exists (REVISED per Sora + Rei):** Original plan: "1 working strategy per category: grid, swing, short scalp, long-term." **This is unreachable on current data.** Per the 2026-07-03 WF revalidation: only XAUUSD passes (5/5, PF 8.0, Sharpe 13.68). EURUSD/GBPUSD are NO-GO at H1. The 4-category portfolio goal is a post-FTMO objective.

**Deliverable:** Single validated strategy (SRMR+ on XAUUSD) confirmed in forward test. USDJPY as Phase 3 stretch goal if M15 retune shows ≥3/5 WF windows.

**Acceptance criteria:**
- [ ] **SRMR+ XAUUSD forward validation:** ≥30 forward trades on cTrader demo, P&L non-negative, no FTMO rule violations
- [ ] **FX majors at M15:** Re-run SRMR+ WF on EURUSD/GBPUSD/USDJPY at M15 (current H1 parquet may be resolution artifact, not strategy failure — per Sora §1)
- [ ] **USDJPY stretch goal (optional):** If SRMR+ USDJPY at M15 retunes to ≥3/5 WF windows, add as second-pair experiment
- [ ] **Combination testing: DROPPED** — moved post-FTMO. Testing combinations of negative-Sharpe edges produces negative-Sharpe blends. Only valid after single-strategy edges are positive.
- [ ] **"1 per category" requirement: DROPPED** — replace with "1 validated single-strategy/single-symbol edge (SRMR+ XAUUSD)"
- [ ] **Crypto: explicitly out of scope** for FTMO quest (FTMO is a forex prop firm)
- [ ] **Grids: explicitly rejected** — FTMO DD rule punishes grid drawdowns; incompatible with 2% max concurrent risk

### Phase 4: Edge-Weighted Risk Allocator (2 SP, reduced from 2-3)

**Why this exists (REVISED per Sora):** Original plan: "confidence engine" with per-trade confidence score. **Per-trade confidence is the wrong primitive.** FTMO rewards realized edge per (strategy, symbol, regime) bucket. The existing `confidence/engine.py` and `confidence/gates.py` are correct filters but should not drive sizing.

**Deliverable:** Edge-weighted risk allocator that adjusts position sizing based on realized R-multiple expectancy.

**Acceptance criteria:**
- [ ] **Per-strategy × per-symbol edge telemetry:** Rolling N-trade expectancy in R-multiples. Wired into `sl_position_sizer.risk_per_trade_pct` so a strategy in drawdown shrinks.
- [ ] **Keep existing confidence gates** (Spread/Session/Volatility) — these are correct as final filters
- [ ] **Lower `live_fire_min_confidence`** from 0.65 to 0.55 (0.65 is starving the forward test — SRMR+ range is 0.40-0.75)
- [ ] **Drop "Sharpe improvement vs fixed sizing" acceptance criterion** — backtested Sharpe is exactly the metric this project has gotten wrong repeatedly. Validate on rolling realized R-multiples in forward test.
- [ ] **Per-regime awareness:** Tag each trade with regime (trend/range/volatile) from existing `signal_engine/regime_thresholds.py`. Track per-regime edge.

### Phase 5: Self-Healing Layer — Corrected Tier Model (2-3 SP)

**Why this exists (REVISED per Rei + Kaito):** Original plan: L0-L4 tiers. **Rei found the tier model is upside-down for this codebase** — 12 of 13 documented failure modes are code-level bugs, not operational drift. L2/L3 auto-remediation would mask code bugs. Kaito found existing `AnomalyMonitor` + `KillSwitchManager` already implement parts of L0/L4.

**Deliverable:** Tiered self-healing built on existing monitors, with git-log guards preventing auto-remediation of recent code changes.

**Acceptance criteria:**
- [ ] **L0 (alert only):** Build on `AnomalyMonitor` — data_silence, zero_signals, zero_pnl_variance
- [ ] **L0.5 (auto-recover transient):** Connection drops, tick gaps. Existing `_attempt_reconnect` pattern. Alert if not restored in 60s.
- [ ] **L1 (process restart):** Guard with StartLimitBurst=3/Interval=120s. Don't restart more than 3 times in 2 minutes.
- [ ] **L1.5 (auto-remediate known patterns):** Seed with the 4 known remediation patterns from 2026-06-30 audit (canary env-bypass, positionId TypeError, amend_sl_tp sleep, USDJPY scaling)
- [ ] **L2 (parameter tweak) — RESTRICTED:** Must check `git log --oneline -7d <affected_file>`. If any code change merged in last 7 days → escalate to L4 regardless. This prevents masking active code bugs.
- [ ] **L3 (disable strategy) — RESTRICTED:** Requires rolling 50-trade Sharpe < 0 (not a single bad day). Also subject to git-log guard.
- [ ] **L4 (kill + page):** Build on `KillSwitchManager`. Stop, then page. Not page and hope.
- [ ] **Remediation cycle detector:** Any auto-remediation applied >3 times in 7 days → page with "code-fix candidate, not self-heal candidate." Breaks the remediation-cycle death spiral.
- [ ] **Reference existing `remediation_validated.flag`** — don't rename or duplicate

### Phase 6: Daily Audit + North-Star Tracking (1-2 SP)

**Why this exists:** Hayate daily audit at 1-4pm EDT (Craig-approved window). Drift detection on workboard and quest phases.

**Deliverable:** Daily audit cron, drift detection, Ava digest, kanban hygiene.

**Acceptance criteria:**
- [ ] Hayate daily audit cron at 1-4pm EDT window
- [ ] Drift detection: card "in progress" >3d → flag, >7d → escalate (calibrated per Rei)
- [ ] Phase drift: 2d no activity → flag, >5d → surface
- [ ] Daily Ava digest (3 bullets: what moved, what's blocked, what needs Craig) — delivered via Telegram
- [ ] **FTMO daily tracker:** Daily P&L vs 3% limit, running best-day ratio, drawdown from peak, open positions count
- [ ] **Challenge-completion detector:** When equity ≥ account × 1.10 and positions flat → mark challenge complete, freeze new positions
- [ ] **CET-midnight daily-loss reset:** Ensure `CircuitBreakerState` resets at CET midnight (17:00 America/Toronto during EDT), not just on process restart
- [ ] Kanban hygiene: archive done >72h, surface stale, reassign orphans

### Phase 7: FTMO Forward Test Run (1-2 SP + 30+ days)

**Why this exists:** The actual gauntlet. Run forward test under FTMO 1-Step rules.

**Phase 7 Entry Gate (Rei — all must pass):**
- [ ] `2c5d684a` live-fire card closed (TP/SL verified on real position)
- [ ] ≥14-day paper-trading sub-gate: ≥30 forward trades, P&L non-negative, no FTMO rule violations
- [ ] Best-day rule implementation merged to main
- [ ] FTMO risk config reconciled and tested
- [ ] Demo credentials verified (no placeholders)
- [ ] cTrader demo account OpenAPI rate-limit budget confirmed for 30+ days

**Deliverable:** Forward test passing FTMO 1-Step criteria.

**Acceptance criteria (CONCRETE per Kaito — replace "variance < 10%"):**
- [ ] Forward test running under FTMO 1-Step rules
- [ ] 30+ consecutive compliant days
- [ ] Kill switch activations = 0 (goal) or ≤2 (acceptable with documented root cause)
- [ ] Remediation cycles = 0 (any 2026-06-30-style sprint cycle = Phase 7 fail)
- [ ] Trade count: ≥1 trade per session band (London, NY, Asia) within 2 weeks
- [ ] Per-strategy fill latency tracked (p50, p95)
- [ ] Daily P&L within FTMO 1-Step limits every day for 30 days
- [ ] Self-healing operational during run
- [ ] Daily reports to Craig (Telegram 3-bullet digest)

---

## Phase Dependencies & Parallelization

```
Phase 0 (pre-quest gate) ──→ 1A (arch + execution audit) ────┐
                              1B (checkpoints) ──────────────┤
                                                             ├──→ Phase 2 (engine consolidation) ──→ Phase 3 (XAUUSD validation) ──→ Phase 4 (edge allocator) ──┐
                                                             │                                                                                                  ├──→ Phase 7 (FTMO run)
                                                             │                    Phase 5 (self-healing) ──→ Phase 6 (daily audit) ────────────────────────────┘
```

- Phase 0 is sequential — must complete before Phase 1A/1B start
- Phase 1A and 1B run parallel
- Phase 2 can start once 1A completes (uses audit findings)
- Phase 5 can start once 1B completes (uses checkpoint design)
- Phase 6 depends on Phase 5
- Phase 7 is gated on Phase 0 entry gate + Phases 1-4 done + 5-6 done

**Realistic timeline:** 6-10 weeks wall-clock. FTMO forward test itself is 30+ days. Some phases overlap.

---

## Risks (revised per council review)

| ID | Risk | Severity | Mitigation |
|----|------|----------|------------|
| R1 | Architecture audit reveals 5-engine consolidation is 12+ SP | High | Cap Phase 1A findings. If consolidation >8 SP, defer to post-quest. Don't let audit scope creep kill the timeline. |
| R2 | SRMR+ XAUUSD edge doesn't hold in forward test | **FATAL** | Phase 3 has USDJPY as fallback. If both fail, quest pauses — cannot pass FTMO with negative-edge strategies. |
| R3 | Craig engagement drops mid-quest | High | Define "Craig unavailable" run-mode: Ava continues with conservative 1-Step defaults on non-FTMO-rule decisions. If no Craig response in 5 working days on a blocker → file [DEBT] and pause Phase 7. |
| R4 | Self-healing masks a code bug (L2/L3 auto-fixes symptoms) | Medium | Git-log guard on L2/L3. Remediation cycle detector (>3 in 7d → escalate to code-fix). |
| R5 | Backtest→forward gap invalidates Phase 3 conclusions | Medium | 14-day paper-trading sub-gate between Phase 3 and Phase 7. If backtest metrics don't reproduce within 20% in paper trading, investigate before live. |
| R6 | Live-execution bugs recur (balance oscillation, TP scaling, amend timeouts) | High | Phase 1A audits execution path first. Commit all uncommitted fixes. Phase 0 disables strategies with no data. |

---

## What Changed From v1 (summary)

| Area | v1 | v2 | Source |
|------|----|----|--------|
| Phase 0 | Didn't exist | New — pre-quest gate (0.5 SP) | Rei |
| Premise | "10 strategies, 0 trades = coordination" | Execution bugs blocking signal→fill pipeline | Sora |
| Phase 3 scope | 4-category portfolio, "1 per category" | XAUUSD-only, drop categories | Sora + Rei + Craig |
| Phase 4 framing | Per-trade confidence engine | Edge-weighted risk allocator (realized R-multiple) | Sora |
| Self-healing tiers | L0-L4, L2/L3 "safer" than L4 | L0-L4 with git-log guard, L1.5, remediation cycle detector | Rei + Kaito |
| Drift windows | 7d/14d cards, 5d/10d phases | 3d/7d cards, 2d/5d phases (calibrated to actual cadence) | Rei |
| FTMO rules | "FTMO rules" (unspecified) | Pinned: 1-Step (3%/10%/best-day rule) | Craig + all reviewers |
| Risk configs | 5 sources disagree | Canonical: `FTMOConfig` in `risk/ftmo_guard.py` | All 3 reviewers |
| Engine consolidation | Not mentioned (Phase 2 = "modularize") | Phase 2 = declare canonical + consolidate 5→1 | Kaito |
| Existing infra | Not referenced | AnomalyMonitor, HealthMonitor, KillSwitchManager, PaperTrader, resource_limits.py | Kaito |
| Phase 7 success | "Variance < 10%" | Concrete: kill activations, remediation cycles, trade count, fill latency | Kaito |
| Phase 7 entry | Not specified | 14-day paper-trading sub-gate + live-fire card closure + best-day rule | Rei |
| Resource caps | "CPU 20%, memory 2GB" | Wired into scripts/run_*.py, ProcessPoolExecutor capped, 3GB limit | Kaito |

---

*Revised 2026-07-08 00:10 EDT per Craig approval of recommended path.*
*Review files: `/root/.openclaw/council-workspace/data/{sora,kaito,rei}-reviews/quest-ayumi-ftmo-2026-07-07.md`*
*Synthesis: `$AYUMI_ROOT/docs/plans/quest-ayumi-ftmo-reviews-synthesis-2026-07-08.md`*
*v1 backup: `quest-ayumi-ftmo-2026-07-07-draft-v1.md`*