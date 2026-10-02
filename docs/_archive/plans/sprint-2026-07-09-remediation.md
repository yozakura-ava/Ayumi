# Sprint: Ayumi Trade-Execution + Ops Health Remediation — 2026-07-09 (v2)

**Sprint ID:** ayumi-ops-remediation-2026-07-09
**Version:** v2 (post-council-revision)
**Author:** Ava (main session)
**Date authored:** 2026-07-09 22:42 EDT (v1) / 23:18 EDT (v2)
**Total cards:** 12 (was 10 in v1)
**Total SP:** ~24 (was ~21 in v1)
**Sprint goal:** Close all defects uncovered by the 2026-07-09 trade-execution audit and the parallel ops-alert verification, restoring live-trading signal integrity on Ayumi and silencing a 7-nudge ops false-alarm pile.

---

## v2 Changelog (Council-Revision)

Both council reviewers returned **APPROVE_WITH_CHANGES** (0 blocking). 13 changes integrated before execution:

### From Domain Lead (6 required)
- **DL-1** Card 3 widened: XAUUSD impossible prices affect **both SRMR+ AND session_range_mr** (live log evidence: `entry=4126545.00000` at 01:00 UTC from session_range_mr)
- **DL-2** Reclassified cTrader comment-length rejection: it's OUR adapter bug, currently load-bearing as second safety net. **Added new Card 11 [FIX]** to actually fix it (one-line truncation)
- **DL-3** Card 4 verification hardened: tail signal_stats.jsonl + mtime check as hard AC (not just "0 stats_fails")
- **DL-4** Wave 2 sequencing: Card 5 → Card 6 (serialized to avoid `sl_position_sizer.py` file conflict)
- **DL-5** Card 9 outcome branch: tightened Card 3 recommendations to `{ship-fix-now, ship-guardrail-now}` (no monitor-only escape)
- **DL-6** Card 3 risk: `medium` → `high`

### From Implementation Auditor (7 changes — 1 HIGH, 3 MEDIUM, 3 LOW)
- **IA-1 [HIGH]** Card 6 rebuild: `data/risk_guard_state.json` **does not exist**. Real state file is `data/risk_state_blend.json`. More fundamentally, `positions_carried` is **in-memory only** — computed from `sl_position_sizer.py:157` `_open_positions: dict[str, float]`. The bug is in `_open_positions` lifecycle, not state-file reconciliation.
- **IA-2 [MEDIUM]** Card 5 pip values corrected to `10.0` for all three (match `adapters/ctrader/models.py:198-204` SYMBOL_METADATA — authoritative broker-side values)
- **IA-3 [MEDIUM]** Cards 1 + 9 kill-switch check: `cat data/kill_switches/global.state` (no `.json` files exist — only `global.state` + `history.jsonl`)
- **IA-4 [MEDIUM]** Card 5 test path: `tests/unit/risk/test_sl_position_sizer.py` (not `test_position_sizer.py`); AUDUSD/USDCAD/USDCHF test cases don't exist
- **IA-5 [LOW]** Card 7 2-step Drive query (recurse into date folder for tarballs; Drive natural sort ≠ date-desc)
- **IA-6 [LOW]** Card 9 `systemctl is-active` returns `active` (not full `Active: active (running)`)
- **IA-7 [LOW]** Card 10 grep both `forward_test.log` AND `forward_test-stderr.log` (+ `.gz` archives)

### New cards added (2)
- **Card 11 [FIX]** Adapter comment-length truncation (was DL-2 + DL-5 prerequisite)
- **Card 12 [DEBT]** Audit recommendation #5: persist closed-trade P&L (per Findings & Debt Protocol)

### Recommendations (R1–R5 from Domain Lead) — deferred or inlined
- **R1** Audit rec #5 → new Card 12 (DEBT) ✅
- **R2** Card 6 "nuke + rebuild" approach → adopted as default strategy ✅
- **R3** 7-nudge pile cleanup → noted as post-Card-7 follow-up, not a new card (will use existing cleanup pattern)
- **R4** Cross-reference audit findings ↔ cards → Card 2 already has parent links; pattern established
- **R5** Card 4 root-owned writer check → added as preventive step in Card 4 verification

---

## Context

Two parallel investigations surfaced real defects on 2026-07-09:

1. **Ayumi trade-execution audit** (subagent, full report at `audit/2026-07-09-trade-execution-investigation.md`):
   - 4 distinct defects causing 99→4 fill-ratio degradation
   - One **latent bomb**: SRMR+ + session_range_mr XAUUSD signals with physically impossible entry prices (`entry=4130495`, `entry=4126545`) — kept safe by combined cTrader comment-length rejection (broker side) + sizing gate rejection (our side). Either safety net could fail at any time.
   - One **silent bug**: `signal_stats.jsonl` ownership denying every stats write, forcing all confidence scores to a stale fallback
2. **Ops health-check false-alarm investigation** (in-session):
   - `ops_health_check.py:check_personality_backup` is **Drive-blind** — only checks local tarballs
   - Result: 7 false-positive auditor findings piled up since Jun 10 chasing a phantom
   - Pipeline is fine — Drive has backups through 2026-07-08. Only Jul 9 + 10 missing (event-driven, low urgency).

The Ayumi defects are live-trading safety issues. The ops defect is noise reduction.

---

## Sprint Cards (12)

| # | Card | Wave | SP | Priority | Agent | v2 Change |
|---|------|------|----|----------|-------|-----------|
| 1 | [OPS] Pause ayumi-forward-test before any code edits | 0 | 1 | urgent | main | IA-3: kill-switch cmd fix |
| 2 | [DEBT] File trade-execution audit report onto workboard | 0 | 1 | low | main | — |
| 3 | [INVESTIGATE] XAUUSD impossible entry prices (SRMR+ + session_range_mr) — root cause | 1 | 5 | urgent | tsukasa | DL-1, DL-5, DL-6, IA hypothesis shift |
| 4 | [FIX] `signal_stats.jsonl` ownership (root:root → $USER) | 2 | 1 | high | tsukasa | DL-3, R5 preventive |
| 5 | [FIX] INSTRUMENTS dict — add AUDUSD/USDCAD/USDCHF | 2 | 1 | high | tsukasa | IA-2 pip values, IA-4 test path |
| 6 | [FIX] SLPositionSizer `_open_positions` reconciliation (in-memory, not state file) | 2 | 3 | high | tsukasa | **IA-1 [HIGH] rebuild** |
| 7 | [FIX] `ops_health_check.py:check_personality_backup` — Drive-aware | 3 | 3 | normal | main | IA-5 2-step query |
| 8 | [INVESTIGATE] Why 2026-07-09 (and 10) personality backup didn't reach Drive | 3 | 2 | low | main | DL-3 reframe |
| 9 | [OPS] Restart ayumi-forward-test with verification | 4 | 2 | high | main | IA-3 kill-switch, IA-6 status, DL-5 outcome branch |
| 10 | [VERIFY] First-hour post-restart — first signal must fill cleanly | 4 | 2 | high | tsukasa | IA-7 grep both logs |
| 11 | **[FIX] cTrader adapter comment-length truncation — remove load-bearing broker dependency** | **2** | **1** | **high** | **tsukasa** | **NEW (DL-2)** |
| 12 | **[DEBT] Persist closed-trade P&L to data/trading.db (audit rec #5)** | **5** | **2** | **low** | **ava** | **NEW (Findings & Debt Protocol)** |

---

## Sequencing (Waves)

```
Wave 0 (immediate, 2 SP)
├─ Card 1: Pause forward test ← prereq for all Wave 2 fixes
└─ Card 2: File audit report on workboard (no service dependency)

Wave 1 (must complete before Wave 4 restart, 5 SP)
└─ Card 3: XAUUSD root cause ← latent-bomb risk; outcome must be fix or guardrail (not monitor-only)

Wave 2 (serialized after Wave 0, 6 SP) ← serial ordering required by IA-1 + DL-4
├─ Card 4: chown signal_stats.jsonl (sudo needed) — standalone
├─ Card 5: INSTRUMENTS dict additive edit (lines 42-55 only) — standalone
├─ Card 11: cTrader adapter comment-length truncation (NEW) — standalone
└─ Card 6: SLPositionSizer `_open_positions` reconciliation (edit sl_position_sizer.py lines 157, 367, 174-185, 284) — LAST
   ⚠ Card 6 explicitly excluded from lines 42-55 (Card 5 territory); Card 5 excluded from 157+ (Card 6 territory)

Wave 3 (independent, parallel, 5 SP)
├─ Card 7: ops_health_check Drive-aware
└─ Card 8: investigate missing Jul 9+10 backups

Wave 4 (after Wave 1+2 complete, 4 SP)
├─ Card 9: restart forward test (with comment-length guardrail verification per DL-5)
└─ Card 10: first-hour verification (grep both logs + heartbeat_trading.json)

Wave 5 (deferred — non-blocking, 2 SP)
└─ Card 12: [DEBT] P&L persistence — does not block Ayumi restart; backlog priority
```

---

## Acceptance Criteria (Sprint-Level — v2)

- Forward test running with **0 stats_fails** sustained for 1 hour post-restart
- Forward test produces **at least one clean fill** in first hour with all 7 blend symbols enabled
- Zero cTrader `INVALID_REQUEST: Field comment is too long` rejections (Card 11 fix removes the dependency)
- XAUUSD SRMR+ AND session_range_mr signals both produce sane entry prices (~$3,300, not $4,130,495)
- `INSTRUMENTS` dict covers all 7 blend symbols with `pip_value_per_lot=10.0` (matches broker metadata)
- `_open_positions` count matches `len(open_positions_from_ctrader)` exactly (post-restart reconciliation)
- `ops_health_check.py:check_personality_backup` returns `OK` when Drive has a tarball from the last 24h, regardless of local state
- Audit report has a discoverable workboard card with 4 child cards linked
- **Card 12 [DEBT] P&L persistence filed** (Findings & Debt Protocol compliance)

---

## Verification Gates (Hard Rule 8)

Every card completion MUST include:
1. Builder-attached proof (test command, file diff, log excerpt)
2. **Orchestrator independent verification** (Ava runs `py_compile` on modified files + targeted test + log spot-check)
3. Acceptance criteria checklist signed off

Skipping the quality gate = skipping Hard Rule 8. Do not mark done until both layers of verification pass.

---

## Risk Profile (v2)

| Card | Risk | Mitigation |
|------|------|------------|
| 1 (pause) | Low — operational toggle | Corrected kill-switch cmd: `cat data/kill_switches/global.state` |
| 3 (XAUUSD investigate) | **HIGH** (was medium) | Read-only code inspection; tightened to fix-or-guardrail outcome |
| 4 (chown) | Low — single file | Plus: search for root-owned writer scripts (preventive per R5) |
| 5 (INSTRUMENTS dict) | Low — additive, lines 42-55 only | pip values verified against broker metadata |
| 6 (SLPositionSizer) | Medium — state reconciliation | Default to nuke+rebuild; lines 157, 367, 174-185, 284 only |
| 7 (ops Drive-aware) | Low — alerting logic | 2-step Drive query (recurse into date folder) |
| 9 (restart) | Medium — re-enters live mode | All Wave 1+2 cards complete + verified; comment-length guardrail check |
| 10 (verify) | Low — observation | Grep both `forward_test.log` AND `forward_test-stderr.log` |
| 11 (comment-length) | Low — one-line truncation | Adapter-side fix; verified end-to-end before restart |

---

## North-Star Alignment

| Card | Star | Confidence | Rationale |
|------|------|------------|-----------|
| 1, 3, 4, 5, 6, 9, 10, 11 | ayumi-revenue | high | Live-trading safety + integrity — A-lane revenue path |
| 7, 8, 12 | none_eligible | medium | Ops health + debt discovery — supporting infra |

---

## Out of Scope

- **Reconnect-cycle pattern investigation** (audit Finding 4) — separate `[DEBT]` candidate for next sprint
- **Signal quality improvements** (separate workstream — ICIR gate already shipping)
- **Full forward-test re-validation** (separate sprint — Phase 11+ work)
- **Audit-to-action pipeline formalization** (BQ-carded separately as `3b175cf1`)
- **Operationalizing Jul 9+10 missing backups** (depends on Card 8 finding)

---

## Cross-References

- Audit report: `$AYUMI_ROOT/audit/2026-07-09-trade-execution-investigation.md`
- Ops health-check source: `/root/.openclaw/workspace/scripts/ops_health_check.py:1338`
- Personality backup script: `/root/.openclaw/workspace/scripts/personality_backup.py:414` (Drive upload), :423 (unlink)
- Drive folder: `10zjW70L16PMxb0w6szGvrzLSLGdTJ0vl` (Personality Backups)
- Kill switch state: `$AYUMI_ROOT/data/kill_switches/global.state` (current: `{}`)
- Risk state file: `$AYUMI_ROOT/data/risk_state_blend.json` (Jun 30 — stale, Card 6 will nuke+rebuild)
- Broker metadata (authoritative for pip values): `$AYUMI_ROOT/src/forex_bot/adapters/ctrader/models.py:198-204`
- INSTRUMENTS dict target: `$AYUMI_ROOT/src/forex_bot/risk/sl_position_sizer.py:42-50`
- SLPositionSizer state: `$AYUMI_ROOT/src/forex_bot/risk/sl_position_sizer.py:157, 367, 174-185, 284`
- Test file: `$AYUMI_ROOT/tests/unit/risk/test_sl_position_sizer.py` (existing, 306 lines)
- Council reviews:
  - Domain lead: `/root/.openclaw/council-workspace/data/reviews/2026-07-09-sprint-domain-lead-review.md`
  - Implementation auditor: `/root/.openclaw/council-workspace/data/reviews/2026-07-09-sprint-implementation-auditor-review.md`
- Prior sprint reference: `docs/plans/ayumi-reliability-sprint-2026-07-05.md`

---

*Sprint plan v2 authored by Ava main session, post-council-revision (2026-07-09 23:18 EDT). Awaiting Craig's go-ahead before execution begins.*