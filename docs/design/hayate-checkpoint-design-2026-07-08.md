# Hayate Checkpoint Catalog & Daily Audit Framework

| Attribute | Value |
|---|---|
| **Date authored** | 2026-07-08 |
| **Author** | Ava (subagent, Phase 1B design sprint) |
| **Sprint** | ayumi-ftmo-quest-2026-07-07 (Phase 1B — Hayate Checkpoint Design, 2 SP) |
| **Quest reference** | `docs/plans/quest-ayumi-ftmo-2026-07-07.md` (v2) §Phase 1B |
| **Companion docs** | `HEARTBEAT.md`, `AGENTS.md`, `ROADMAP.md` in `/root/.openclaw/ayumi-overseer-workspace/` |
| **Status** | Design complete — implementation is a separate Phase 2 card (SP 6-9) |
| **Scope** | Catalog of 25 checkpoints across 5 categories, plus a daily audit framework + drift detector. **DESIGN ONLY — no source files modified.** |

---

## 1. Executive Summary

Craig's three asks for the FTMO Quest map to three concrete deliverables in this doc:

1. **"Hayate being more proactive"** → Promote Hayate from "monitors and recommends" to "monitors and remediates" inside a hard-bounded auto-remediation surface (the 5 action classes in §3.2).
2. **"Hayate handling simple things (like forward test being down) on its own"** → Define a `REPAIR_AUTHORITY` matrix (§3.2) that gates auto-remediation by action class, blast radius, and circuit-breaker state.
3. **"Automated daily audit between 1-4pm EDT"** → Specify a 1×/day Hayate Daily Audit cron (§4) that produces a structured report at `reports/hayate-daily-audit/YYYY-MM-DD.md` covering all 25 checkpoints, with auto-remediation actions, escalations, and recommendations.

The checkpoint catalog (§3) covers **25 checkpoints** in **5 categories** (System, Trading, Data, Pipeline, Kanban). Of those, **14 already have data sources** in the codebase (mostly thanks to Phase 1 reliability sprint outputs: `HealthMonitor`, `AnomalyMonitor`, `KillSwitchManager`, `ftmo_guard.py`, `risk_guard.py`, `signal_stats.py`). **11 are net-new** and require build effort (total ~6-9 SP).

The drift detector (§5) implements the 3d/7d/2d/5d escalation rules that Craig specified: 3-day card staleness → flag in audit, 7-day → auto-create `[STALE]` card; 2-day phase staleness → flag, 5-day → escalate to Craig.

This doc is **design only** — no source files were modified, per task spec. The implementation work spawns a separate Phase 2 card.

---

## 2. Background — What Hayate Is and Isn't

### 2.1 Current state (as of 2026-07-08)

Hayate is a heartbeat-driven monitoring agent at `/root/.openclaw/ayumi-overseer-workspace/`. Its current operational loop is 7 steps (HEARTBEAT.md): Boot Check → Forward Test Health (market-open only) → Crypto Ops → System Hygiene → Code & Test Health → Work Card Creation → State Update + Reporting. It writes to a structured outbox (`overseer-outbox.jsonl`) and creates work cards via the OpenClaw workboard tool.

**What Hayate does today:**
- Monitors forward test process (PID, uptime, ticks, signals)
- Emits `status_update`, `alert`, `card_created`, `repair_report` events
- Honors `kill_switches.json` circuit breakers (`allow_restart_service=true`, `allow_repair=true`, etc.)
- Max 3 repair attempts per issue with 0s/120s/300s backoff
- Max 5 open Hayate-created cards at a time (Phase 1 cap)

**What Hayate does NOT do today (the gap this design closes):**
- Has no checkpoint *catalog* — current monitoring is heartbeat-driven, ad-hoc by step
- Has no daily *audit* with structured report output
- Has no drift detector for card/phase staleness
- Has no proactive remediation beyond "restart forward test" — the existing 3-repair rule only triggers on crash
- Has no per-checkpoint thresholds / warning / critical levels

### 2.2 Existing implementations Hayate can build on

| Component | Location | Lines | What it does | Reuse for checkpoint |
|---|---|---|---|---|
| `HealthMonitor` | `src/forex_bot/engine/health_monitor.py` | 206 | Periodic `[B5 Health]` / `[S1 Health]` log lines on daemon thread | System health (FT-001) |
| `AnomalyMonitor` | `src/forex_bot/engine/anomaly_monitor.py` | 257 | Detects data_silence, zero_signals, zero_pnl_variance | Trading health (FT-003, FT-004) |
| `KillSwitchManager` | `src/forex_bot/adapters/ctrader/kill_switch.py` | 760 | Global + per-strategy freeze, file-persisted | Trading health (FT-006), Drift (D-001) |
| `FTMOGuard` | `src/forex_bot/risk/ftmo_guard.py` | 534 | Daily loss, total DD, best-day, action levels | Trading health (FT-007, FT-008, FT-009) |
| `RiskGuard` | `src/forex_bot/adapters/ctrader/risk_guard.py` | ~500 | FTMOConfig, FTMOProfile (0.5%/5%/10%/1.5R/3pos/50%) | Trading health (FT-007..FT-011) |
| `PositionMonitor` | `src/forex_bot/adapters/ctrader/position_monitor.py` | 350+ | TP ratchet, MAE/MFE, portfolio exposure | Trading health (FT-002, FT-011) |
| `SignalStatsRecorder` | `src/forex_bot/signal_engine/signal_stats.py` | 370 | JSONL per-signal log (atomic temp+rename writes) | Data health (D-003, D-004) |
| `scripts/health_check_tick_pipeline.py` | scripts/ | 90+ | Tick stall detection via state diff | System health (FT-001 tick flow) |
| `scripts/kill_switch_watchdog.py` | scripts/ | 295 | Reads heartbeat file, activates kill on stale | Trading health (FT-005 watchdog) |
| `scripts/restart_forward_test.sh` | scripts/ | 90+ | Stop → start → verify loop with health check | Auto-remediation class A1 (restart) |
| `docs/runbooks/ayumi-systemd-install.md` | docs/ | 200+ | Systemd unit install + verify + operations | (Reference only — systemd is not yet installed) |

### 2.3 Hard boundaries (from Hayate AGENTS.md) that constrain this design

- **NEVER** touch anything outside `$AYUMI_ROOT/` and own workspace
- **NEVER** modify system-level config (systemd, firewall, packages) without Ava approval
- **NEVER** push to `main` directly (Phase 1: branch only)
- **NEVER** pip install or modify shared venv
- **NEVER** spawn subagents outside Ayumi domain
- **NEVER** communicate directly with Craig
- **Max 3 repair attempts per issue** (backoff 0s/120s/300s)
- **Max 5 open Hayate-created cards at a time**
- **Max 10 service restarts per day**
- **Max 5 builds per day**

These constraints are non-negotiable and the design respects all of them.

---

## 3. Checkpoint Catalog

### 3.1 Naming convention

Each checkpoint has a 4-character ID:
- **FT-NNN** = Forward test (Forex Trading) — Trading health
- **SH-NNN** = System health
- **DH-NNN** = Data health
- **PH-NNN** = Pipeline health
- **KH-NNN** = Kanban health

### 3.2 Auto-remediation classes (the action surface)

Every checkpoint maps to one of these action classes. The class determines what Hayate can do without escalation, what requires an Ava card, and what requires Craig approval.

| Class | Action | Examples | Authority |
|---|---|---|---|
| **A1 — Service control** | Restart forward test service; toggle systemd if installed | FT-001, FT-002 | Hayate (with circuit-breaker + repair-attempt caps from kill_switches.json) |
| **A2 — File hygiene** | Touch flag files, clear stale PIDs, fix ownership (chown to $USER), rotate logs | SH-002, SH-003, SH-005, KH-005 | Hayate (no escalation) |
| **A3 — Card lifecycle** | Create cards, comment on cards, archive done cards >72h | KH-001..KH-007 | Hayate (per workboard rules + max-5 cap) |
| **A4 — Notification** | Emit alert to overseer-outbox.jsonl; ping Ava | All `critical` thresholds | Hayate (auto) |
| **A5 — FTMO guard action** | Activate global freeze, fire per-strategy freeze, close positions | FT-006, FT-007, FT-008, FT-009 | **NEVER auto** — only Ava (with Craig approval) |
| **A6 — Config change** | Change risk params, switch profile, set new max positions | FT-007, FT-008 | **NEVER auto** — Craig only |
| **A7 — Strategy lifecycle** | Enable/disable strategy, change signal thresholds | FT-010, FT-011 | **NEVER auto** — Ava card → Craig approval |

> **Critical rule:** Auto-remediation MUST be idempotent. If the same checkpoint fires the same class-A action twice in 5 minutes, Hayate logs it as `noop_already_applied` and stops. This prevents restart-storm patterns documented in `docs/post-mortems/ayumi-may-june-crash-root-cause-2026-07-05.md` (entry #6 — restart storm 47s).

---

### 3.3 System Health (SH)

| ID | Check | Description | Data source | Healthy | Warning | Critical | Auto-remediation | Escalation | Frequency |
|---|---|---|---|---|---|---|---|---|---|
| **SH-001** | Forward test process status | PID exists; matches `data/forward_test.pid`; `ps aux` shows it | `ps -p $(cat data/forward_test.pid)`, `data/heartbeat_trading.json.engine_running` | running | n/a (binary) | down | A1 — restart via `scripts/restart_forward_test.sh` (counts toward 3-attempt cap and 10-restart/day cap) | A4 — if 3 repair attempts fail: emit `priority: high` to overseer-outbox.jsonl → A3 — card for Tsukasa | real-time (heartbeat 30 min market / 2 hr off-hours) |
| **SH-002** | Tick flow | `ticks_received` increasing in `data/heartbeat_trading.json` over 5-min window | `data/heartbeat_trading.json`, `scripts/health_check_tick_pipeline.py` | > 1 tps 5min avg | 0.1-1 tps 5min avg | 0 tps for 5+ min during market hours | A1 — restart if 0 ticks >5min (counts toward 3-attempt cap). Note: NOT a restart-storm; the 5min threshold prevents that | A4 — alert if restart fails or ticks still 0 after restart | real-time (heartbeat) |
| **SH-003** | Log rotation | `logs/*.log` files older than 7 days; `data/forward_test.log` size >500MB | `find logs/ -name "*.log" -mtime +7`, `du -sh data/forward_test.log` | 0 files >7d, log <500MB | 1-10 files >7d OR 500MB-2GB | >10 files >7d OR >2GB | A2 — gzip+archive oldest 10 files to `logs/archive/YYYY-MM-DD/` | A3 — card if archive fails | daily (audit cron) |
| **SH-004** | Disk usage | `$AYUMI_ROOT/` partition | `df -h $AYUMI_ROOT/` | <70% | 70-85% | >85% | A2 — clear tmp files, rotate logs, vacuum old reports | A4 — alert Ava at >85%; A3 — card for storage investigation at >90% | daily (audit cron) + heartbeat spot-check |
| **SH-005** | Stale PID files | `*.pid` files in `data/` not matching a running process | `find data/ -name "*.pid" -mtime +1` + `ps -p $(cat data/*.pid)` | 0 stale | 1-2 stale | >2 stale or any `data/forward_test.pid` with dead process | A2 — delete stale `*.pid` files; ensure `data/forward_test.pid` reflects reality | A4 — if `forward_test.pid` references dead process: treat as FT-001 down, restart | daily (audit cron) |
| **SH-006** | Zombie / orphan processes | `ps aux | grep ayumi | grep -v grep` showing Z state, or PPID=1 ayumi processes not under systemd | `ps aux | grep -E "ayumi|launch_blend"` | 0 zombies, 0 orphans | 1-2 | >2 zombies OR >5 orphans | A2 — kill orphan PIDs >1h old (graceful SIGTERM, then SIGKILL after 30s) | A4 — alert Ava if >5 orphans; A3 — card for process-tree investigation | daily (audit cron) |
| **SH-007** | Memory and CPU of forward test | RSS in MB; CPU % over 5min | `ps -p $(cat data/forward_test.pid) -o %mem,rss,pcpu` | <1.5GB RSS, <50% CPU | 1.5-2.0GB, 50-80% | >2.0GB OR >80% sustained 5min | A1 — graceful restart (OOM-class risk documented in crash-history §4, May-24-2026 event) | A4 — alert; A3 — card for memory-leak investigation if recurring | real-time (heartbeat) |
| **SH-008** | Log error rate | ERROR + Traceback count in `data/forward_test.log` over 5min | `tail -1000 data/forward_test.log \| grep -cE "ERROR\|Traceback"` | 0 | 1-5 | >5 in 5min | A2 — none (errors are diagnostic signal, not auto-fixable) | A4 — alert; A3 — card with last 5 tracebacks if >5 in 5min | real-time (heartbeat) |
| **SH-009** | cTrader credential health | Token expiry, refresh status from `data/ops/ctrader_credential_health.jsonl` | last line of `data/ops/ctrader_credential_health.jsonl` | days_remaining > 14 | 7-14 | <7 | A2 — run `scripts/ctrader_credential_probe.py` to refresh (no destructive op) | A4 — alert at <7 days; A3 — card for manual re-authorization at <3 days | daily (audit cron) |

### 3.4 Trading Health (FT)

All FT checkpoints are MARKET-OPEN only. Outside market hours (Fri 21:00 UTC → Sun 21:00 UTC), FTs are skipped with `skipped_market_closed: true` in the audit. (Per `scripts/kill_switch_watchdog.py:_is_forex_market_closed()` logic — this is the canonical market-hours predicate.)

| ID | Check | Description | Data source | Healthy | Warning | Critical | Auto-remediation | Escalation | Frequency |
|---|---|---|---|---|---|---|---|---|---|
| **FT-001** | Forward test process status | (mirror of SH-001, Trading-priority view) | `data/heartbeat_trading.json`, `data/forward_test_health.json` | running | degraded (e.g., `running_degraded` with auth errors >0) | down | A1 — restart with degraded-class triage (note: `running_degraded` from cTrader auth errors does NOT auto-restart — that's an Ava card per heartbeat precedent ovs-20260704-1448) | A4 — high-priority alert + A3 — card for `running_degraded > 30 min` | real-time (heartbeat) |
| **FT-002** | Open positions vs limits | `len(open_positions)` vs `FTMOConfig.max_positions=3` | `data/forward_test_health.json.open_positions` (if present) or live query | 0-2 | 3 (= max) | >3 (limit violation) | A5 — never auto-close (FTMO rule territory). A2 — none | A5 — Ava card, A3 — escalate to Craig within 15 min if >3 (FTMO rule breach in progress) | real-time (heartbeat) |
| **FT-003** | Daily P&L vs FTMO 3% limit | `(daily_start_balance - current_balance) / starting_balance` vs 0.03 (FTMO 1-Step target per Quest Phase 0) | `data/state/risk_guard_state.json`, `FTMOGuard.state.daily_loss_pct` | <1.5% | 1.5-2.5% | >2.5% (within 0.5pp of 3% hard limit) | A5 — never auto-activate freeze at warning (FTMO limit territory). A2 — log + observe | A5 — at warning: card for Ava review; at critical: high-priority alert + A3 — card for Craig, recommend manual freeze | real-time (heartbeat) |
| **FT-004** | Total drawdown vs 10% limit | `FTMOGuard.state.current_dd_pct` vs 0.10 | `data/state/risk_guard_state.json` | <5% | 5-8% | >8% (within 2pp of 10% hard limit) | A5 — never auto-activate freeze. A2 — log + observe | A5 — at warning: Ava card; at critical: high-priority alert + A3 — card for Craig | real-time (heartbeat) |
| **FT-005** | Best-day ratio | best day's profit / total positive-days profit vs 0.50 (50% cap) | `FTMOGuard.state.daily_pnl_history` | <40% | 40-48% | >48% | A5 — never auto. A2 — log + observe | A5 — Ava card at warning (could affect FTMO 1-Step qualification); Craig card at critical | daily (audit cron) — not real-time because daily_pnl rolls over at CET midnight |
| **FT-006** | Kill switch state | `data/kill_switches/global.state` — global_pause, forex_halt, freeze flags | `data/kill_switches.json`, `data/kill_switches/global.state` | all false | any circuit_breaker=false but allow_restart_service=false | global_pause=true OR forex_halt=true | A5 — read-only; never modify `kill_switches.json` | A4 — log state in heartbeat; A3 — card only if unexpected state (e.g., forex_halt=true but no Ava card explaining) | real-time (heartbeat) |
| **FT-007** | FTMO action level | `FTMOGuard.state.action_level` ∈ {ALLOW, REDUCE_50, FREEZE, KILL} | `data/state/risk_guard_state.json.action_level` (if present), `FTMOGuard` | ALLOW | REDUCE_50 | FREEZE or KILL | A5 — never auto-reverse an action level set by the engine (engine is authoritative; FTMO rules are operator territory) | A3 — Ava card at REDUCE_50; high-priority + Craig card at FREEZE/KILL | real-time (heartbeat) |
| **FT-008** | Live fills vs signals | `live_fills / signals_generated` over 24h window | `data/forward_test_health.json.live_fills`, `signals_generated`; `data/signal_stats.jsonl` | >50% (live mode) | 25-50% | <25% (suggests execution path issue) | A2 — none (signal→trade ratio is diagnostic) | A3 — card for Tsukasa investigation at <25% sustained 4h | real-time (heartbeat, 4h rolling window) |
| **FT-009** | Signal-to-trade ratio | `trades / signals` over 24h (most signals are rejected by risk gate) | `data/forward_test_health.json`, `data/signal_stats.jsonl` | 0.05-0.30 (rejection-by-risk is normal) | <0.02 OR >0.50 | = 0.0 over 24h with >5 signals (suggests sizing-gate stuck) | A2 — none | A3 — card for sizing-gate investigation if >0.0 zero over 24h with >5 signals | daily (audit cron) |
| **FT-010** | Fill latency | median time from signal generation to live fill, last 100 fills | derived from `data/signal_stats.jsonl` (paired open/close lines) | <5s | 5-30s | >30s (suggests cTrader API degradation) | A2 — none | A3 — card for cTrader API investigation if median >30s over 4h | daily (audit cron) |
| **FT-011** | Position age | oldest open position age vs configured max-hold (e.g., 24h for SRMR+ mean-reversion) | `data/forward_test_health.json.open_positions` (age field), or derived from `signal_stats.jsonl` open lines | <12h | 12-24h | >24h (stale position risk) | A5 — never auto-close (Ava/Craig territory) | A3 — Ava card at >24h | real-time (heartbeat) |

**Note on FT-003 / FT-004 thresholds:** The user's task spec says "FTMO 3% limit" and "10% limit" — that's the **FTMO 1-Step** target per the Quest Phase 0 (current `FTMOProfile.daily_loss_limit_pct=0.05` is 2-Step, and Phase 0 lists "Update all 5 sources to 1-Step canonical values (3%/10%/$100K)" as a deliverable). This design uses **3% daily / 10% total** as the canonical per the task spec. After Phase 0 reconciles the config, FT-003/FT-004 thresholds will use the live `FTMOConfig` value rather than hard-coded 0.03/0.10. **Open question for Phase 0 owner:** should the audit thresholds come from `FTMOConfig` (dynamic, mode-dependent) or stay hard-coded (mode-blind but stable)? Recommend dynamic with hard-coded fallback to 0.03/0.10 if `FTMOConfig` is missing.

### 3.5 Data Health (DH)

| ID | Check | Description | Data source | Healthy | Warning | Critical | Auto-remediation | Escalation | Frequency |
|---|---|---|---|---|---|---|---|---|---|
| **DH-001** | Tick feed latency | wall-clock now - `last_tick_time` from heartbeat | `data/heartbeat_trading.json.last_beat`, `data/forward_test_health.json.last_tick_time` | <2s | 2-30s | >30s during market hours | A1 — same as SH-002 (restart if >5min) | A4 — alert at >30s; A3 — card for cTrader connection investigation if >5min | real-time (heartbeat) |
| **DH-002** | Bar building rate | `bars_built` increasing over 5min | `data/forward_test_health.json.bars_built` | >0.1 bars/min 5min avg | 0.01-0.1 | 0 bars/min for 5+ min during market | A1 — restart if zero bars >10min during market | A3 — card for pipeline investigation if restart fails | real-time (heartbeat) |
| **DH-003** | signal_stats.jsonl write success | file mtime within 60s; row count increasing during market hours | `data/signal_stats.jsonl` (mtime, line count) | mtime <60s, line_count increasing | mtime 60-300s | mtime >300s OR mtime >60s with no new lines for 30min during market | A2 — none (write failure is engine-internal) | A3 — card for `SignalStatsRecorder` investigation if mtime >5min during market | real-time (heartbeat) |
| **DH-004** | risk_guard_state.json freshness | file mtime vs heartbeat age | `data/state/risk_guard_state.json.last_save_ts` | mtime <60s | 60-300s | >300s during market | A2 — none (engine writes this) | A3 — card for `RiskGuard` investigation if mtime >5min | real-time (heartbeat) |
| **DH-005** | Persistence vs in-memory drift | compare `risk_guard_state.json` (persisted) to `data/forward_test_health.json` (in-memory) for current_balance | diff <$1 | diff $1-$100 | diff >$100 (suggests in-memory reader bug — known card 8e81574d) | A2 — none (drift is engine-internal, currently known) | A3 — comment on existing card 8e81574d if drift >$100; new card if drift >$500 sustained 4h | real-time (heartbeat) |

### 3.6 Pipeline Health (PH)

These checkpoints cover Hayate's own data pipeline (the OpenClaw pipeline that powers the learning system), not Ayumi's trading pipeline.

| ID | Check | Description | Data source | Healthy | Warning | Critical | Auto-remediation | Escalation | Frequency |
|---|---|---|---|---|---|---|---|---|---|
| **PH-001** | Extraction cron | 30-min extraction cron ran within last 35min | `data/learning/trajectories.jsonl` mtime; cron log `/tmp/extractor-cron.log` | mtime <35min | 35-90min | >90min OR cron error in log | A2 — touch idle state to allow re-extract on next cron (idempotent) | A3 — card for cron investigation at >90min | real-time (heartbeat) |
| **PH-002** | Trajectory write rate | `data/learning/trajectories.jsonl` line count increasing | file line count diff | >0/day with active sessions | 0/day with >3 active sessions | 0/day for 3+ days | A2 — none (writes are session-driven) | A3 — card for session-instrumentation investigation at critical | daily (audit cron) |
| **PH-003** | Warmth marker routing | latest warmth marker present in `data/learning/warmth_markers.jsonl` | marker file mtime; line count | mtime <2h, count increasing | 2-12h | >12h OR missing file | A2 — none (warmth is session-driven) | A3 — card for warmth-pipeline investigation at critical | daily (audit cron) |

> **Note on PH-NNN scope:** These are intentionally narrow. The full learning-system health check lives in `/root/.openclaw/workspace/scripts/threshold_monitor.py` (cron `5,35 * * * *`). Hayate's checkpoint reports only the three above; threshold_monitor owns the rest. This avoids duplicate monitoring.

### 3.7 Kanban Health (KH)

All KH checkpoints query the OpenClaw workboard. Hayate uses `workboard_list` (paginated, limit=100) and `workboard_stats` for these.

| ID | Check | Description | Data source | Healthy | Warning | Critical | Auto-remediation | Escalation | Frequency |
|---|---|---|---|---|---|---|---|---|---|
| **KH-001** | Stale `todo` cards | cards in `todo` status with `updated_at` >3 days | `workboard_list status=todo limit=100` + parse `updated_at` | 0 stale | 1-3 stale | >3 stale | A3 — comment on each stale card "stale 3+ days" + add to audit | A3 — at critical: auto-create `[STALE]` card for each (>3d, no action) | daily (audit cron) |
| **KH-002** | Stale `ready` cards | cards in `ready` status with `updated_at` >3 days | same | 0 stale | 1-3 stale | >3 stale | A3 — same as KH-001 | A3 — at critical: auto-create `[STALE]` card for each | daily (audit cron) |
| **KH-003** | 7-day stale escalation | cards in todo/ready with `updated_at` >7 days | derived from KH-001/KH-002 | 0 | n/a | ≥1 | A3 — auto-create `[STALE]` card listing all 7d+ cards with links | A3 — same | daily (audit cron) |
| **KH-004** | Stalled phases | quest phases with no `audit_trail` entry >2 days | `docs/plans/quest-*.md` `audit_trail` field, parsed | 0 stalled | 1-2 stalled | >2 stalled | A4 — emit alert to overseer-outbox | A3 — at critical: escalate to Craig (>5d) | daily (audit cron) |
| **KH-005** | Dispatch failures | workboard_dispatch failures in last 24h (cards stuck in `dispatch_failed` or repeated claim expirations) | `workboard_list status=running` + check `claim_expired_at` field | 0 failures | 1-3 failures | >3 failures | A3 — comment on affected cards | A3 — at critical: card for dispatcher investigation | daily (audit cron) |
| **KH-006** | Hayate open-card cap | count of Hayate-created cards in `todo`/`ready`/`running` | `workboard_list agentId=hayate status=todo` + `ready` + `running` | 0-3 | 4 (= cap-1) | 5 (= cap) | A2 — none (cap is from Hayate AGENTS.md, enforced by creation protocol) | A4 — alert at 5; refuse to create more until count drops | real-time (heartbeat — Hayate checks before every create) |
| **KH-007** | Done-card archive | done cards >72h old not archived | `workboard_list status=done` + parse `updated_at` | 0 | 1-10 | >10 | A3 — auto-archive (move to `archived=true`) | A4 — at >10: log warning, archive is best-effort | daily (audit cron) |

> **Important note on KH-001..KH-003 vs the user's spec:** The user's task specifies "3-day card staleness → flag in audit, 7-day → auto-create `[STALE]` card." This design implements exactly that. The 5-day phase staleness in §5 below is separate and maps to KH-004.

---

## 4. Daily Audit Framework

### 4.1 Schedule

- **Cron entry** (added to /root crontab, alongside the existing /root/.openclaw/workspace crons):
  ```cron
  # Hayate Daily Audit (between 1-4pm EDT = 17:00-20:00 UTC; run at 18:00 UTC = 2pm EDT)
  0 18 * * * cd $AYUMI_ROOT && $AYUMI_ROOT/.venv/bin/python /root/.openclaw/ayumi-overseer-workspace/scripts/hayate_daily_audit.py >> /tmp/hayate-daily-audit.log 2>&1
  ```
- **Random jitter:** Add 0-15 min random delay (implemented inside the script via `time.sleep(random.randint(0, 900))`) so a daily flurry of cron jobs doesn't fire all at 18:00:00 sharp.
- **Timezone:** Server is UTC (canonical per Ava directive 2026-07-03, see `overseer_state.json.clock_note`). 18:00 UTC = 2:00 PM EDT = inside the 1-4pm EDT window.
- **Backoff on failure:** If the script fails, retry once at 19:00 UTC and again at 20:00 UTC (still inside the window). If all three attempts fail, emit a high-priority alert to overseer-outbox.jsonl and skip until next day.

### 4.2 Script location and architecture

`/root/.openclaw/ayumi-overseer-workspace/scripts/hayate_daily_audit.py`

Architecture (rough):

```
hayate_daily_audit.py
├── main()
│   ├── load kill_switches (skip if global_pause)
│   ├── load overseer_state (skip if heartbeat_count=0 and not first run)
│   ├── collect_system_health()  # SH-001..SH-009
│   ├── collect_trading_health()  # FT-001..FT-011, market-open only
│   ├── collect_data_health()    # DH-001..DH-005
│   ├── collect_pipeline_health() # PH-001..PH-003
│   ├── collect_kanban_health()  # KH-001..KH-007
│   ├── collect_drift_report()   # §5
│   ├── determine_remediations() # walk all WARNING/CRITICAL, apply action classes A1-A7
│   ├── write_audit_report()     # reports/hayate-daily-audit/YYYY-MM-DD.md
│   ├── emit_audit_summary()     # overseer-outbox.jsonl type=audit_summary
│   └── emit_escalations()       # overseer-outbox.jsonl type=audit_escalation per escalation
├── helpers/
│   ├── read_forward_test_health() # parses data/forward_test_health.json + heartbeat_trading.json
│   ├── read_risk_state()         # parses data/state/risk_guard_state.json
│   ├── list_workboard_cards()    # workboard_list pagination
│   ├── apply_remediation(action) # A1-A7 dispatcher
│   └── check_idempotency()       # 5-min window guard
```

The script uses only the stdlib + existing Ayumi venv (no new dependencies). Total estimated code: **~600 lines** including docstrings and tests.

### 4.3 Report structure

Output: `$AYUMI_ROOT/reports/hayate-daily-audit/YYYY-MM-DD.md`

```markdown
# Hayate Daily Audit — 2026-07-08

| Attribute | Value |
|---|---|
| **Date** | 2026-07-08 (Tue) |
| **Run time** | 14:02:13 EDT (18:02:13 UTC) |
| **Market state** | OPEN (forex 24/5, crypto 24/7) |
| **Heartbeat count** | 33 |
| **Cards open (Hayate-created)** | 0 / 5 |
| **Auto-remediations applied** | 0 |
| **Escalations** | 0 |
| **Critical findings** | 0 |

## Executive Summary

(1 paragraph — overall system health verdict, key risks, key actions taken)

Forward test running healthy (PID 2125800, 11h34m uptime, 5.10 tps 5min avg, 9 signals / 2 live fills / 2 stats_fails, balance in-sync cTrader=$9,314.08 = risk.balance, dd=6.86% well below 8% warning threshold). All 5 categories green or yellow-no-action. No auto-remediation triggered. No escalations. 0 stale cards in workboard. Forward test fully self-recovered from yesterday's 0-signals warmup window; live_fills resumed at 02:48Z per hb#32 heartbeat evidence.

## Checkpoint Results

| ID | Check | Status | Value | Notes |
|---|---|---|---|---|
| SH-001 | Forward test process | ✅ GREEN | running, PID 2125800 | uptime 11h34m |
| SH-002 | Tick flow | ✅ GREEN | 5.10 tps 5min avg | within normal range |
| SH-003 | Log rotation | ✅ GREEN | 0 files >7d, log 488MB | approaching 500MB warn |
| ... |
| FT-003 | Daily P&L vs 3% | ✅ GREEN | -$0 / 0.00% | no losses today |
| ... |
| KH-001 | Stale todo cards | ✅ GREEN | 0 stale |  |
| ... |

Status legend: ✅ GREEN, ⚠️ YELLOW (warning), 🛑 RED (critical), ⏸️ SKIPPED (market closed / out of scope)

## Remediation Actions Taken

| Time (EDT) | Class | Action | Target | Result |
|---|---|---|---|---|
| 14:01:55 | A2 | Log rotation: gzip+archive | logs/forward_test.log.2026-07-07.gz | success |
| 14:01:58 | A2 | Stale PID cleanup | data/old_orphan.pid (1 file) | success |

(Empty if no actions taken.)

## Items Escalated

| Time (EDT) | Priority | Target | Subject | Outbox ID |
|---|---|---|---|---|
| 14:02:10 | high | ava | FT-002: open position count approaching max (2/3) | ovs-20260708T1802Z |

(Empty if no escalations.)

## Drift Report (3d/7d/2d/5d)

- **3d stale cards:** 0 (warning at 1, critical at >3)
- **7d stale cards:** 0 (auto-create `[STALE]` at ≥1)
- **2d stalled phases:** 0 (warning at 1, critical at >2)
- **5d stalled phases:** 0 (escalate to Craig at ≥1)
- **Auto-created cards today:** 0

## Recommendations

(Plain-English list, max 5 bullets, sorted by priority.)

1. (none if all green)

## Appendix: Data Sources Queried

- data/forward_test_health.json (Hayate-managed snapshot)
- data/heartbeat_trading.json (engine writes)
- data/state/risk_guard_state.json
- data/signal_stats.jsonl
- data/overseer_state.json
- data/overseer-outbox.jsonl (last 24h, for context)
- data/decision_log.jsonl (last 24h, for context)
- data/lifecycle_log.jsonl (last 24h, for context)
- data/kill_switches.json, data/kill_switches/global.state
- data/ops/ctrader_credential_health.jsonl
- workboard_list (paginated, default board, agent=any)
- workboard_stats
- ps -p $(cat data/forward_test.pid) (process check)
- df -h $AYUMI_ROOT/
- date (server clock check)

---
*Auto-generated by `scripts/hayate_daily_audit.py` at 2026-07-08T18:02:13Z. Hayate heartbeat #N.*
```

### 4.4 Remediation action classes — explicit list

The 5 auto-remediation action classes from §3.2, with the explicit concrete actions Hayate can take WITHOUT Craig approval. Anything not in this list is NOT auto-remediation; it goes through A3 (card) or higher escalation.

| Class | Concrete actions | Counts toward | Reversible? |
|---|---|---|---|
| **A1 — Service control** | (1) Run `scripts/restart_forward_test.sh` to restart forward test; (2) if systemd unit is installed (Phase 2+), use `systemctl --user restart ayumi-forward-test.service` | 3-attempt cap (per issue), 10-restart/day cap | Yes (next restart; A1 idempotent) |
| **A2 — File hygiene** | (1) Recreate missing `data/ayumi/remediation_validated.flag` (with audit-trail log line — see precedent ovs-20260708); (2) gzip+archive `logs/forward_test.log` to `logs/archive/YYYY-MM-DD/`; (3) clear stale `data/*.pid` files where `ps -p` shows no process; (4) `chown -R $USER:$USER data/` if ownership drifted to root; (5) clear `data/.tmp/` if size >100MB | per-file idempotency window (5 min) | Yes (recreate, re-chown) |
| **A3 — Card lifecycle** | (1) Create card via `workboard_create`; (2) comment on existing card via `workboard_comment`; (3) auto-archive done cards >72h via `workboard_list status=done` + manual `archived=true` flag (or new tool if available); (4) move stale cards from `todo`→`backlog` if `>14d no activity` (defer to A3 escalation) | 5-open-card cap (Hayate) | Yes (un-archive) |
| **A4 — Notification** | (1) Append to `data/overseer-outbox.jsonl`; (2) append to `data/lifecycle_log.jsonl`; (3) append to `data/decision_log.jsonl`; (4) NO direct Craig contact (per AGENTS.md hard rule) | none (notifications are free) | n/a (log only) |
| **A7 — Strategy lifecycle** | NOT AUTO. Always A3 → Ava → Craig. Examples: enable/disable a strategy in `StrategyRegistry`, change signal thresholds, modify Kelly parameters. | n/a | n/a |

**Actions that REQUIRE Craig escalation (A5, A6, A7):**

| Action | Why escalated | How escalated |
|---|---|---|
| Activate global kill switch (`kill_switch.activate_global_freeze` or `activate_global_kill`) | Affects live trading; FTMO rule territory | A3 — high-priority card to Ava → Craig |
| Per-strategy freeze (any of 9 strategies) | Affects production strategy performance | A3 — high-priority card to Ava → Craig |
| Modify `FTMOConfig` (max_daily_loss_pct, max_total_dd_pct, max_positions, risk_per_trade_pct) | FTMO rule territory | A3 — high-priority card to Ava → Craig |
| Modify `.env` (credentials, account ID) | Security territory | A3 — high-priority card to Ava → Craig |
| Switch trading profile (challenge vs funded) | Affects risk profile entirely | A3 — high-priority card to Ava → Craig |
| Set `live_mode=true` in production config | Trading with real money | A3 — high-priority card to Ava → Craig |
| Any FTMO rule violation in progress (FT-002 critical, FT-003 critical, FT-004 critical) | Account-level risk | A3 — high-priority card to Ava → Craig (within 15 min) |
| Disable a strategy that has open positions | Affects live positions | A3 — high-priority card to Ava → Craig |

### 4.5 Audit idempotency and concurrency

- **Idempotency window:** 5 minutes. If the same A1/A2/A3 action was applied within the last 5 min, log `noop_already_applied` and skip.
- **Concurrency guard:** At script start, write `data/audit_lock.json` with `{pid, started_at, run_date}`. If the file exists and the recorded process is alive, exit (another audit is running). If the recorded process is dead, take the lock and emit a `lock_recovered_from_dead_pid` event.
- **Partial failure:** If the report write fails after some checks have run, do NOT re-run; instead, write a partial report with a `partial_failure: true` header and emit a high-priority alert.
- **Manual override:** A `data/audit_skip_today.flag` file (created by Ava via workboard) skips today's audit entirely. Use case: Hayate is in active repair mode and a daily audit would interfere.

### 4.6 Audit retention

- Daily reports: kept for 90 days in `reports/hayate-daily-audit/`
- After 90 days: gzip and move to `reports/hayate-daily-audit/archive/YYYY/`
- After 1 year: delete (per Hayate disk-usage caps; SH-004 will warn earlier)

---

## 5. Drift Detection

### 5.1 Card staleness (KH-001, KH-002, KH-003)

| Threshold | Check | Action |
|---|---|---|
| **3 days** | Card in `todo` or `ready` with `updated_at` older than 3 days | **Flag in daily audit** (YELLOW status, count in Drift Report). Add comment "stale 3+ days" to each card. |
| **7 days** | Card in `todo` or `ready` with `updated_at` older than 7 days | **Auto-create `[STALE]` card** via `workboard_create`. One `[STALE]` card per audit cycle listing all 7d+ cards with links. Body format: `Cards in [todo/ready] stale 7+ days (auto-flagged YYYY-MM-DD):\n- [card-id] [title] (N days stale)\n...` |

**Stale tracking in audit:** Counted at 3d (flag), 7d (auto-card), 14d (high-priority alert to Ava via A4). At 30d, the card is auto-archived (A3) with a closing comment explaining why.

### 5.2 Phase staleness (KH-004)

Quest phases are documented in `docs/plans/quest-ayumi-ftmo-2026-07-07.md` (and successor plans). Each phase has an `audit_trail` field (or equivalent — TBD on standard format; recommend adding if not present) tracking per-phase events.

| Threshold | Check | Action |
|---|---|---|
| **2 days** | Any quest phase with no `audit_trail` entry in the last 2 days | **Flag in daily audit** (YELLOW status, list stalled phase names in Drift Report). Add `[STALLED-PHASE]` comment to quest plan. |
| **5 days** | Any quest phase with no `audit_trail` entry in the last 5 days | **Escalate to Craig** via A3 — high-priority card with subject `[STALLED-PHASE] <phase name> — no progress in 5 days` |

**Open question for Phase 1A owner (architecture audit):** What is the canonical `audit_trail` schema? The design assumes append-only JSONL or a structured field. If neither exists, this is a Phase 0 prerequisite (small SP, ~0.5 SP).

### 5.3 What drift is NOT

Drift detection does **NOT** cover:
- Heartbeat staleness on the forward test (that's SH-002/FT-001)
- cTrader API staleness (DH-001)
- Pipeline cron staleness (PH-001..PH-003)

Those are real-time health checks, not drift. Drift is about *meta-system* decay (cards, phases), not data freshness.

### 5.4 Drift report location

Each daily audit report includes a "Drift Report" section (§4.3). A consolidated weekly drift digest is **out of scope for this design** — could be added later as a Sunday cron that summarizes the week's drift events. Phase 2+.

---

## 6. Implementation Map

This section maps every checkpoint to existing data sources and estimates build effort for missing pieces. **No source files were modified during this design — the table is the spec, not the code.**

### 6.1 Effort summary

- **Reuse-only (existing data source, just query it):** 14 checkpoints
- **Net-new data source (new file or new query):** 11 checkpoints
- **Total estimated build effort:** **6-9 SP** (3-5 working days for a single builder)

### 6.2 Per-checkpoint map

| ID | Existing data source? | If existing, where? | If net-new, what to build | Estimated SP |
|---|---|---|---|---|
| SH-001 | YES | `data/forward_test_health.json`, `data/heartbeat_trading.json.engine_running` | — | 0 |
| SH-002 | YES | `data/heartbeat_trading.json` (engine writes tick count); `scripts/health_check_tick_pipeline.py` (logic) | — | 0 |
| SH-003 | YES | `logs/`, `data/forward_test.log` (just need find + du) | — | 0 |
| SH-004 | YES | `df -h` (just subprocess) | — | 0 |
| SH-005 | YES | `data/*.pid` (find + ps cross-check) | — | 0 |
| SH-006 | YES | `ps aux` (just subprocess) | — | 0 |
| SH-007 | YES | `ps -p PID -o %mem,rss,pcpu` | — | 0 |
| SH-008 | YES | `tail -1000 data/forward_test.log \| grep -cE "ERROR\|Traceback"` | — | 0 |
| SH-009 | YES | `data/ops/ctrader_credential_health.jsonl` (last line) | — | 0 |
| FT-001 | YES | (mirror of SH-001) | — | 0 |
| FT-002 | PARTIAL | `data/forward_test_health.json` may have open_positions; otherwise needs live cTrader query | Add `open_positions` field to forward_test_health.json if missing | 0.5 |
| FT-003 | YES | `data/state/risk_guard_state.json` + `FTMOGuard.state.daily_loss_pct` | — | 0 |
| FT-004 | YES | `data/state/risk_guard_state.json` + `FTMOGuard.state.current_dd_pct` | — | 0 |
| FT-005 | YES | `FTMOGuard.state.daily_pnl_history` (computed daily at CET midnight) | — | 0 |
| FT-006 | YES | `data/kill_switches.json`, `data/kill_switches/global.state` | — | 0 |
| FT-007 | YES | `data/state/risk_guard_state.json.action_level` (if engine writes it) | Verify engine writes `action_level`; if not, derive from `daily_loss_pct` thresholds | 0.5 |
| FT-008 | YES | `data/forward_test_health.json.live_fills` + `signals_generated` | — | 0 |
| FT-009 | YES | derived from `data/signal_stats.jsonl` (count outcomes) | — | 0.5 (parsing logic) |
| FT-010 | NO | `data/signal_stats.jsonl` has open/close pairs but no `filled_at` field | Add `filled_at` field to `SignalRecord` + populate on fill | 1.5 |
| FT-011 | NO | `data/forward_test_health.json` may have open_positions; otherwise none | Add `opened_at` to each position in `forward_test_health.json`; OR derive from `signal_stats.jsonl` open line timestamp | 1.0 |
| DH-001 | YES | `data/heartbeat_trading.json.last_beat` | — | 0 |
| DH-002 | YES | `data/forward_test_health.json.bars_built` | — | 0 |
| DH-003 | YES | `data/signal_stats.jsonl` (mtime + line count) | — | 0 |
| DH-004 | YES | `data/state/risk_guard_state.json` (mtime) | — | 0 |
| DH-005 | YES | compare `data/state/risk_guard_state.json.current_balance` to `data/forward_test_health.json.ctrader_balance` | — | 0.5 (drift detection logic) |
| PH-001 | YES | `data/learning/trajectories.jsonl` mtime | — | 0 |
| PH-002 | YES | `data/learning/trajectories.jsonl` line count | — | 0 |
| PH-003 | YES | `data/learning/warmth_markers.jsonl` (assumed path — verify) | Verify path; create if missing | 0.5 |
| KH-001 | YES | `workboard_list status=todo limit=100` + parse `updated_at` | — | 0.5 (pagination logic) |
| KH-002 | YES | `workboard_list status=ready limit=100` + parse `updated_at` | — | 0.5 (pagination logic, share w/ KH-001) |
| KH-003 | YES | derived from KH-001/KH-002 | — | 0 |
| KH-004 | PARTIAL | quest plans need `audit_trail` field; otherwise requires parsing whole file | Add `audit_trail` schema to quest plan template; populate during card work | 1.0 (depends on Phase 0 deliverable) |
| KH-005 | YES | `workboard_list` + check claim expirations | — | 0.5 |
| KH-006 | YES | `workboard_list agentId=hayate` filtered by status | — | 0 |
| KH-007 | YES | `workboard_list status=done` + parse `updated_at` | — | 0.5 |

**Net-new build effort:** 14 (reuse-only) + 11 (partial/new) = 25 checkpoints. **Total: 6.5 SP** (rounded to 6-9 SP for sprint estimation with overhead).

### 6.3 Components to build

1. **`/root/.openclaw/ayumi-overseer-workspace/scripts/hayate_daily_audit.py`** (~600 lines including docstrings/tests) — the audit script
2. **`/root/.openclaw/ayumi-overseer-workspace/scripts/checkpoint_collectors.py`** (~400 lines) — checkpoint query helpers, one function per checkpoint
3. **`/root/.openclaw/ayumi-overseer-workspace/scripts/checkpoint_remediation.py`** (~300 lines) — A1-A7 action dispatcher with idempotency
4. **`/root/.openclaw/ayumi-overseer-workspace/tests/test_hayate_daily_audit.py`** (~500 lines) — pytest coverage of all 25 checkpoints with mock data
5. **Cron entry** (1 line in /root crontab)
6. **`docs/runbooks/hayate-daily-audit.md`** (~200 lines) — operations runbook for the daily audit

**Plus data-source additions (small, optional but recommended):**
7. `FTMOGuard`: ensure `action_level` is written to `data/state/risk_guard_state.json` (0.5 SP, depends on engine)
8. `SignalStatsRecorder`: add `filled_at` field to `SignalRecord` (1.5 SP, depends on engine)
9. `forward_test_health.json`: add `open_positions` with `opened_at` and `position_id` (1.0 SP, depends on Hayate state writer)
10. Quest plan template: add `audit_trail` schema (0.5 SP, depends on Ava/card-authoring)

**Total build SP:** 6.5 (script + tests + runbook) + 3.5 (data-source additions) = **~10 SP** end-to-end. **Phase 1B (catalog + design) is the 2 SP for this doc; the 8 SP for build is Phase 2 work.**

### 6.4 Suggested implementation order (for Phase 2)

1. **Week 1, Day 1-2:** Build the 14 reuse-only checkpoints (SH-*, FT-001, FT-003, FT-004, FT-005, FT-006, FT-008, DH-*, PH-*, KH-006). Verify against today's data. Test idempotency. (3 SP)
2. **Week 1, Day 3:** Build the data-source additions (engine-side: action_level, filled_at, open_positions). Verify FT-007, FT-009, FT-010, FT-011. (2 SP)
3. **Week 1, Day 4-5:** Build the kanban + drift checkpoints (KH-001..KH-005, KH-007, KH-004 with audit_trail). Test card creation. (2 SP)
4. **Week 2, Day 1-2:** Wire the cron, write the runbook, do end-to-end dry run on a Saturday (forex market closed = safe). (1 SP)
5. **Week 2, Day 3:** Production run, monitor for one full week. Adjust thresholds based on false-positive rate. (1 SP)

---

## 7. Risks and Open Questions

### 7.1 Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Auto-restart loop on flaky forward test (5+ restarts in 1h) | Low (per HEARTBEAT.md 3-attempt cap) | High (FTMO challenge disruption) | Strict 3-attempt cap; 0s/120s/300s backoff already enforced. Audit must check `data/decision_log.jsonl` for recent repair attempts before allowing another. |
| False-positive "stale" alerts flood the workboard | Medium | Medium (operator fatigue) | 3d/7d/14d/30d escalation ladder; only 7d+ auto-creates cards. 14d+ gets a high-priority alert. |
| Audit script fails silently (cron swallowed output) | Low | High (Craig gets no signal) | Audit must end with `emit_audit_summary` even if internal checks fail; high-priority alert on no summary within 24h |
| Threshold drift over time (e.g., 5 tps is "healthy" today, but 3 tps becomes the new normal) | Medium | Low | Quarterly review of warning/critical thresholds against trailing 30-day distributions. Add this to Phase 4 (Operational Learning Loop). |
| Race between heartbeat and daily audit (heartbeat writes state mid-audit) | Low | Low | Audit uses read-only file access; partial-read returns are OK because we re-parse on next run. Add atomic-snapshot at audit start as defense-in-depth. |
| FTMOGuard threshold disagreement (3% from task spec, 5% in current code) | High (known) | Medium (audit uses wrong threshold) | Phase 0 deliverable resolves this. Until then, audit defaults to 0.03/0.10 with a `ftmo_mode: assumed_1step` field in the report. |
| Drift detection requires `audit_trail` schema that doesn't exist | Medium | Low (drift is nice-to-have) | Phase 0 deliverable adds schema. If not done by audit build time, drift detection is gated off (KH-004 shows "schema missing"). |

### 7.2 Open questions for Phase 0 / Phase 1A owners

1. **FTMO threshold source:** Should the audit pull thresholds from `FTMOConfig` (dynamic) or hard-code 0.03/0.10? **Recommendation:** dynamic with hard-coded fallback. Resolve in Phase 0 reconciliation card.
2. **`audit_trail` schema:** What is the canonical format for quest phase `audit_trail`? **Recommendation:** append-only JSONL with `{phase, event, timestamp, card_id?}`. Resolve in Phase 0 plan-template update.
3. **Auto-archive authority:** Can Hayate move a done card to `archived=true`? Today the workboard API doesn't seem to support this directly. **Recommendation:** add `workboard_archive_card` tool, OR add a "soft archive" via a label `archived: true`. Resolve with workboard tool owner.
4. **Systemd or bare-process:** Auto-remediation A1 currently uses `scripts/restart_forward_test.sh` (bare-process mode). If/when systemd unit is installed (Phase 5.2 of reliability sprint, card `9043c09c`), the script should prefer `systemctl --user restart`. **Recommendation:** add a probe at script start to detect which mode is active.
5. **Crypto checkpoints:** This design covers forex FTMO checkpoints. Crypto (Cabal paper trading) is Phase 2+. Add a placeholder section in the catalog with 5-10 crypto checkpoints when Phase 2 begins.
6. **Hayate's "alerts" surface:** Today Hayate writes to `overseer-outbox.jsonl` and Ava consumes it. If Ava is offline (e.g., on vacation), alerts stack up. **Recommendation:** add a "max-unacknowledged-alerts" cap (e.g., 5) that escalates to a Telegram-style channel after the cap is hit. Out of scope for this design.

### 7.3 Out of scope (explicitly)

- **Council integration** (Ren, Hina, Sora, etc. reviews) — not part of the checkpoint catalog
- **Sakura / PE / LCC / XCL** — different agents, different domains
- **ML pipeline health** (model drift, retraining triggers) — separate concern, lives in /root/.openclaw/workspace monitoring
- **Memory infrastructure** (Honcho ingestion, decay) — separate concern
- **Workboard tool evolution** — the audit consumes existing workboard tools; if those tools need new capabilities (e.g., `workboard_archive_card`), that's a separate workstream

---

## 8. Acceptance Criteria

This design is complete when:

- [x] All 5 categories (System, Trading, Data, Pipeline, Kanban) have documented checkpoints with IDs, sources, thresholds, auto-remediation, and escalation
- [x] Daily audit framework specifies schedule, script structure, report format, and idempotency
- [x] Drift detector implements 3d/7d/2d/5d rules per user spec
- [x] Each checkpoint mapped to existing or new data source with effort estimate
- [x] Auto-remediation action classes (A1-A7) enumerated with authority matrix
- [x] Risks and open questions documented
- [x] Total build effort estimated (6.5 SP for scripts + 3.5 SP for data sources = ~10 SP)
- [x] **No source files modified** (design only, per task spec)

---

## 9. Next Steps

1. **Phase 1A:** Architecture audit continues in parallel; resolve FTMOGuard config reconciliation (3% vs 5%) and `audit_trail` schema.
2. **Phase 1B completion:** This doc is the design deliverable; it does NOT block Phase 2 build.
3. **Phase 2 card creation:** A new sprint card (TSK-NNN) to build `hayate_daily_audit.py` + `checkpoint_collectors.py` + `checkpoint_remediation.py` + tests + runbook. Estimated 8 SP. Should be created after Phase 1A completes (so the data-source additions can be coordinated).
4. **Phase 2 subagent dispatch:** Tsukasa per standard pattern. Verify build via the same test scope (`scripts/run_test_scope.sh`) used in the reliability sprint.
5. **Phase 3 operational hardening:** Add 90-day threshold-tuning cron (review warning/critical thresholds against trailing 30-day distributions).

---

*End of design. Implementation is a separate Phase 2 card.*
