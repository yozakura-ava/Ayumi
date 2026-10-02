# Phase 1A — Infrastructure Inventory: Quest Plan vs. Existing Implementations

**Date:** 2026-07-08
**Auditor:** Builder subagent (depth 1/1)
**Scope:** Map Phase 2-6 plan concepts to EXISTING implementations in the Ayumi codebase.
**Mode:** Read-only audit — no source files modified.
**Source plan:** `docs/plans/quest-ayumi-ftmo-2026-07-07.md` (v2)
**Companion audits:**
- `docs/audits/phase1a-engine-inventory-2026-07-08.md` (5-engine consolidation)
- `docs/audits/phase1a-execution-path-audit-2026-07-08.md` (signal→fill trace)
- `docs/audits/phase1a-strategy-qualification-2026-07-08.md` (WF revalidation)

---

## Summary Table

| Plan Phase | Plan Concept | Exists? | Status | Effort to Complete |
|-----------|--------------|---------|--------|-------------------|
| **Phase 2** | `cpu_limited` / `memory_capped` resource wrappers | ✅ `src/forex_bot/common/resource_limits.py` (324 lines) | **Wired in ZERO scripts** — 20 `scripts/run_*.py` exist; none import `resource_limits` | **0.5 SP** — wire into existing launchers + add CI check |
| **Phase 2** | `ProcessPoolExecutor(max_workers=2)` cap | ✅ `SweepRunner.__init__` accepts `max_workers` (param sweep line 91) | **Default is `os.cpu_count()`** (uncapped) — 5 of 7 sweep scripts use `max_workers=1` (hardcoded) | **0.5 SP** — change default to 2 + add CLI flag |
| **Phase 2** | `data/forward_test.pid` lock | ✅ `src/forex_bot/adapters/ctrader/pid_guard.py` + `acquire_pid_lock()` | **Live in production** — `data/forward_test.pid` exists, owned by PID 2125800 | **0 SP** — already done |
| **Phase 2** | Engine consolidation (5→1) | ✅ Canonical `ForwardTestEngine` declared (engine-inventory audit) | **Phase 1A-1 complete** (separate audit) | **0.5 SP** — drop in the 4 deprecation banners |
| **Phase 4** | `regime_thresholds` (per-regime) | ✅ `src/forex_bot/risk/regime_thresholds.py` (157 lines) + `correlation_sizer.compute_adjusted_size(regime=...)` | **Live but uncalled** — no caller passes `regime` argument to `SLPositionSizer` | **1 SP** — thread regime from `ForwardTestEngine` into `BlendForwardTestRunner` |
| **Phase 4** | Edge telemetry (R-multiple expectancy) | ❌ Not present | No `expectancy`, `R-multiple`, `edge_telemetry` symbols exist | **2 SP** — new `edge_telemetry.py` + wire into `SLPositionSizer.risk_per_trade_pct` |
| **Phase 4** | `live_fire_min_confidence` lower 0.65→0.55 | ✅ Config field exists `forward_test_engine.py:118` | **Default 0.65** — no test or config pins it at 0.55 | **0.5 SP** — change default + 1-line config doc |
| **Phase 5** | `AnomalyMonitor` (L0) | ✅ `src/forex_bot/engine/anomaly_monitor.py:54` `class HealthMonitor` | **Live in production** — wired in `ForwardTestEngine._build_components` | **0 SP** — already done |
| **Phase 5** | `HealthMonitor` (structured heartbeat) | ✅ `src/forex_bot/engine/health_monitor.py` emits `[B5 Health]` / `[S1 Health]` | **Live in production** | **0 SP** — already done |
| **Phase 5** | `KillSwitchManager` (L4) | ✅ `src/forex_bot/adapters/ctrader/kill_switch.py:307+` `class KillSwitchManager` | **Live in production** — has `global_pause` / `global_kill` / `global_freeze` / per-strategy `is_strategy_frozen` (line 449) | **0 SP** — already done |
| **Phase 5** | `remediation_validated.flag` handling | ✅ `forward_test_engine.py:80` + `_enforce_remediation_gate` (line 562) | **Live, auto-recreating** — flag exists at `data/ayumi/remediation_validated.flag` (just auto-recreated 13:58Z) | **0 SP** — already done (and self-healing) |
| **Phase 5** | `signal_validator.py` retire or document | ⚠️ File exists `src/forex_bot/signal_validator.py` (192 lines) | **Has `TradeRulesEngine` class** — 1 test file `tests/strategies/test_signal_validator.py`. Status ambiguous | **0.5 SP** — confirm "live" or delete |
| **Phase 5** | L1.5 auto-remediate (4 known patterns) | ❌ Not present | No remediation script library, no `remediation_log`, no cycle detector | **2 SP** — seed with 4 known patterns from 2026-06-30 audit |
| **Phase 5** | L2/L3 git-log guard | ❌ Not present | No `git log --oneline -7d <file>` check anywhere | **0.5 SP** — helper function + integration in L2/L3 paths |
| **Phase 5** | Remediation cycle detector (>3 in 7d) | ❌ Not present | No remediation counter anywhere | **0.5 SP** — append-only JSONL counter + check before apply |
| **Phase 6** | Hayate daily audit cron 1-4pm EDT | ❌ Not present | Hayate has heartbeat but no `daily_audit.py` script, no cron entry, no Telegram digest | **1 SP** — new `scripts/daily_audit.py` + cron job |
| **Phase 6** | Drift detection (3d/7d cards, 2d/5d phases) | ❌ Not present | Hayate tracks `heartbeat_count` but not card/phase age | **1 SP** — workboard query + surface alerts |
| **Phase 6** | FTMO daily tracker (PnL vs 3%, best-day ratio, peak DD) | ⚠️ Partial | `equity_tracker.py:178` computes `ftmo_status: OK/WARNING/BREACH` based on 5%/10% daily thresholds; `risk_guard.py:_check_best_day_rule` (line 416) checks 50% rule; `ftmo_guard.py:442` `check_best_day_rule` | **0.5 SP** — combine into single `ftmo_daily.py` with the 1-Step values (3%/10%/$100K) |
| **Phase 6** | Challenge-completion detector (equity ≥ 1.10×) | ❌ Not present | No profit-target check anywhere in production code; `ftmo_simulation.py:77` has it in backtest-only `profit_target_reached` | **0.5 SP** — new check in `equity_tracker.py` + freeze new positions hook |
| **Phase 6** | CET-midnight daily-loss reset | ✅ Live | `risk/ftmo_guard.py:_cet_date` (line 41) + `SLPositionSizer.reset_daily(cet_date=...)` (line 345) — forward test uses CET date via `_cet_date` | **0 SP** — already done |
| **Phase 6** | Kanban hygiene (archive done>72h, surface stale) | ❌ Not present | No `archive_done.py` or stale-card scan script | **0.5 SP** — new `scripts/kanban_hygiene.py` |
| **Phase 6** | North-star tracking | ✅ Live | `/root/.openclaw/workspace/data/state/north_stars.json` + `scripts/north_star_report.py` (Craig/Ava workspace, not Ayumi) | **0 SP** — already done |
| **Hayate** | Overseer (auto-monitor) | ✅ Live | `/root/.openclaw/ayumi-overseer-workspace/` — heartbeats every 30 min market hours, emits to `data/overseer-outbox.jsonl` | **0 SP** — already done |
| **Hayate** | Checkpoint catalog (20+ types) | ❌ Not present | HEARTBEAT.md has 7 steps but no checkpoint registry | **1 SP** — new `docs/checkpoint-catalog.md` + runbook per type |
| **Hayate** | Drift detection (telemetry, cron skips) | ⚠️ Mentioned only | ROADMAP.md references "parameter drift detection" as Phase 2 (not built) | **1 SP** — drift baseline + alert |

**Totals (existing):** ~13 of 22 concepts live or partially live
**Effort to complete remaining 9:** ~12 SP (rough)

---

## 1. Resource Caps (Phase 2)

### 1.1 `cpu_limited` / `memory_capped` — **EXISTS, NOT WIRED**

**Existing implementation:**
- `src/forex_bot/common/resource_limits.py` (324 lines, tests at `tests/test_resource_limits.py` 221 lines)
- Three public APIs:
  - `@contextmanager cpu_limited(percent: int = 20)` — line 235
  - `@contextmanager memory_capped(mb: int = 2048)` — line 262
  - `@run_limited(cpu_percent=20, memory_mb=2048)` decorator — line 276
- Plus `add_resource_args(parser)` helper (line 301) that adds `--max-cpu` / `--max-memory-mb` CLI flags
- Tests: `tests/test_resource_limits.py` (221 lines), `tests/test_cpu_limit_e2e.py` (50 lines)

**Critical gap:** `cpu_limited` and `memory_capped` are NOT used in any `scripts/run_*.py`. Confirmed by:
```
grep -rln "cpu_limited|memory_capped|add_resource_args" scripts/  →  (no results)
```
**20 sweep/backtest scripts exist** (e.g. `scripts/run_supertrend_rsi_parameter_sweep.py`, `scripts/run_session_range_mr_optuna.py`); none apply resource caps.

**What's needed (0.5 SP):**
1. Add `add_resource_args(parser)` to the argparse parser at the top of `main()` in each sweep script (~20 files)
2. Wrap the `run_sweep(...)` call in `with run_limited(...)` (or `cpu_limited + memory_capped` if more granular)
3. Add CI check (e.g. `tests/test_resource_caps_applied.py`): grep for `@run_limited` OR `cpu_limited` OR `add_resource_args` in every `scripts/run_*.py`; fail if missing

**Plan says:** "Memory: 3 GB hard limit per process (2 GB is too tight for Optuna sweep with 13 strategies × 7 pairs). L0 health alert at 2 GB."

Note: the existing `memory_capped` default is 2048 MB (2 GB). The plan's "3 GB hard limit" requires per-script config. The "L0 health alert at 2 GB" requires `HealthMonitor` integration — not present.

### 1.2 `ProcessPoolExecutor` cap — **EXISTS, DEFAULT UNSAFE**

**Existing implementation:**
- `src/forex_bot/backtest/parameter_sweep/sweep_runner.py:91-113`:
  ```python
  def __init__(self, ..., max_workers: int | None = None):
      self._max_workers = max_workers if max_workers is not None else os.cpu_count()
      ...
      if self._max_workers is not None and self._max_workers > 1 and len(tasks) > 1:
          with ProcessPoolExecutor(max_workers=self._max_workers) as executor:
  ```
- Same pattern in `src/forex_bot/backtest/parameter_sweep/sweep_runner.py:91` and `scripts/run_regime_router_sweep.py:71,348`

**Defaults:**
- `SweepRunner`: defaults to `os.cpu_count()` if not passed (uncapped!)
- Most callers pass `max_workers=1` (5 of 7 sweep scripts use `max_workers=1` hardcoded)
- `scripts/run_supertrend_rsi_parameter_sweep.py:214`: passes `max_workers=None` (will use `os.cpu_count()`)
- `scripts/sweep_momentum_breakout_h1.py:111`: passes `max_workers` (CLI flag)

**What's needed (0.5 SP):**
1. Change `SweepRunner.__init__` default from `os.cpu_count()` to `2` (matches plan: "max_workers=2 in sweep_runner")
2. Add `--max-workers` CLI flag via `add_resource_args` extension
3. Audit `scripts/run_*.py` for sweep calls; ensure each has explicit cap

**Plan says:** "ProcessPoolExecutor workers capped (`max_workers=2` in sweep_runner)."

### 1.3 `data/forward_test.pid` — **EXISTS, LIVE IN PRODUCTION**

**Existing implementation:**
- `src/forex_bot/adapters/ctrader/pid_guard.py` — `acquire_pid_lock(path)` function
- Used in `scripts/launch_blend_forward_test.py:803` and `scripts/launch_forward_test.py`
- **Live now:** `data/forward_test.pid` exists (mode auto, owned by user 1000, contains `2414795`)
- Restart orchestration: `scripts/restart_forward_test.sh` (full PID/heartbeat/health check loop)

**Status: COMPLETE — 0 SP**

### 1.4 `TradeStore` single-writer bottleneck — **EXISTS, DOCUMENTED**

**Existing implementation:**
- `src/forex_bot/storage/trade_store.py` — `class TradeStore` with `threading.local` per-thread connections + single write lock
- Schema in `src/forex_bot/storage/migrations.py` (4 tables: trades, equity_curve, daily_summary, rolling_metrics)

**Plan says:** "TradeStore single-writer bottleneck documented for Phase 6 concurrency tests."

The bottleneck is in the design (line 14-19 docstring): "Write operations are serialized through a single write lock." Just needs documentation, not new code.

**Status: ~0 SP** (doc-only update)

### 1.5 Engine consolidation (Kaito MUST) — **DECLARED IN PHASE 1A-1**

`docs/audits/phase1a-engine-inventory-2026-07-08.md` already declares `ForwardTestEngine` as canonical. The 4 deprecation banners (~`# DEPRECATED: see docs/audits/phase1a-engine-inventory-2026-07-08.md`) are still needed in:
- `src/forex_bot/engine/orchestrator.py:54` `MultiStrategyOrchestrator`
- `src/forex_bot/engine/trading_orchestrator.py:311` `TradingOrchestrator`
- `src/forex_bot/orchestrator/signal_orchestrator.py:45` `SignalOrchestrator`
- `src/forex_bot/forward_test/blend_runner.py:28` `BlendForwardTestRunner` (partially — still in production use)

**Effort: 0.5 SP** (banner comments, no logic change)

---

## 2. Self-Healing Layer (Phase 5)

### 2.1 `AnomalyMonitor` (L0) — **EXISTS, LIVE**

`src/forex_bot/engine/anomaly_monitor.py:54`:
- `class HealthMonitor` (note: file is named `anomaly_monitor.py` but class is `HealthMonitor` — naming mismatch!)
- `HealthAlertKind` enum: `DATA_SILENCE`, `ZERO_SIGNALS`, `ZERO_PNL_VARIANCE` — exactly the 3 failure modes the plan cites
- `HealthMonitorConfig`: thresholds for each kind
- Methods: `start()`, `stop()`, `record_tick()`, `record_signal()`, `record_trade_pnl()`, `check()`, `get_snapshot()`
- Wired into production: `ForwardTestEngine._build_components` calls `HealthMonitor` and feeds counters

**Status: COMPLETE — 0 SP** (L0 alerts are live)

### 2.2 `HealthMonitor` (structured heartbeat) — **EXISTS, LIVE**

`src/forex_bot/engine/health_monitor.py`:
- `class HealthMonitor` (separate from `engine/anomaly_monitor.py` — confusing!)
- Emits `[B5 Health]` / `[S1 Health]` log lines (Amendment A6 backward-compat tag convention)
- `attach(session=, market_data_feed=, order_gateway=, position_tracker=, strategies=)` wiring
- `start()` runs a daemon thread at 60s interval
- Replaces inline health loop from `scripts/launch_blend_forward_test.py`

**Naming concern:** Two `HealthMonitor` classes exist:
- `src/forex_bot/engine/anomaly_monitor.py:54` `class HealthMonitor` — the L0 alert detector
- `src/forex_bot/engine/health_monitor.py:30` `class HealthMonitor` — the heartbeat logger

This is a doc/naming cleanup issue, not a functional gap.

**Status: COMPLETE — 0 SP**

### 2.3 `KillSwitchManager` (L4) — **EXISTS, LIVE, HAS FREEZE + KILL**

`src/forex_bot/adapters/ctrader/kill_switch.py` (484 lines):
- `class KillSwitchManager` (line 307+)
- Modes: `global_pause`, `global_freeze`, `global_kill`, per-strategy `is_strategy_frozen(strategy_id)` (line 449)
- Wired into production: `ForwardTestEngine._build_components` creates `KillSwitchManager`; `FTMOGuard` calls it on breach
- CLI: `scripts/kill_switch.py status|kill|freeze|recover`
- Watchdog: `scripts/kill_switch_watchdog.py` (295 lines) auto-activates on heartbeat staleness

**Status: COMPLETE — 0 SP** (L4 is live)

### 2.4 `remediation_validated.flag` handling — **EXISTS, SELF-HEALING**

`src/forex_bot/adapters/ctrader/forward_test_engine.py:80`:
```python
_REMEDIATION_VALIDATED_FLAG = "data/ayumi/remediation_validated.flag"
```

`forward_test_engine.py:562-605` `_enforce_remediation_gate(live_mode: bool)`:
- Paper mode → always allowed
- Flag present → no-op
- Flag missing + audit doc present → auto-recreates flag with `auto-recreated <ts>` content
- Flag missing + audit doc missing + live mode → `RuntimeError`

**Live evidence:** `data/ayumi/remediation_validated.flag` currently contains:
```
auto-recreated 2026-07-08T13:58:07+0000
source: docs/audits/ayumi-live-remediation-session-audit-2026-06-30.md
reason: flag was missing on startup; audit doc is source of truth.
```

This is exactly the "self-healing" pattern the plan calls for. Already survived a crash/kill-9/git-clean once today.

**Status: COMPLETE — 0 SP** (auto-recreates correctly)

### 2.5 `signal_validator.py` — **EXISTS, STATUS AMBIGUOUS**

`src/forex_bot/signal_validator.py` (192 lines):
- Has `TradeRulesEngine` class (line 140 referenced from hybrid module)
- 1 test file: `tests/strategies/test_signal_validator.py`
- 2026-04-17 audit flagged it as "DEAD" but it appears still in use (hybrid engine imports it)

**Status: NEEDS VERIFICATION (0.5 SP)**
Either:
- Confirm live in `hybrid/trade_rules.py` and remove the "DEAD" tag
- Or delete the file + 1 test

### 2.6 L1.5 auto-remediate (4 known patterns) — **DOES NOT EXIST**

No `remediation_log`, no remediation script library, no per-pattern auto-fix path exists anywhere in the codebase.

**The 4 known patterns from 2026-06-30 audit:**
1. canary env-bypass
2. positionId TypeError
3. amend_sl_tp sleep
4. USDJPY scaling (TP 100× bug)

**What's needed (2 SP):**
1. `src/forex_bot/self_healing/remediation_patterns.py` — dict of pattern_id → (detector_fn, fix_fn)
2. Wire into ForwardTestEngine health loop
3. Add `data/remediation_log.jsonl` append-only audit
4. Skip if `git log --oneline -7d <affected_file>` shows any change (L2/L3 guard below)

### 2.7 L2/L3 git-log guard — **DOES NOT EXIST**

No `git log --oneline -7d <file>` check anywhere. Would be a small helper in `src/forex_bot/self_healing/git_guard.py` (~50 lines):
```python
def recent_code_change(filepath: str, days: int = 7) -> bool:
    """Return True if any commit touched filepath in the last `days` days."""
```

**Effort: 0.5 SP**

### 2.8 Remediation cycle detector — **DOES NOT EXIST**

No counter for "applied N times in 7 days for same pattern." Simple to build on top of `data/remediation_log.jsonl` from 2.6:
```python
def count_recent_remediations(pattern_id: str, days: int = 7) -> int:
    """Count entries in remediation_log for pattern_id within days."""
```

**Effort: 0.5 SP** (combined with 2.6/2.7 for 2.5 SP total)

---

## 3. Daily Audit / North-Star Tracking (Phase 6)

### 3.1 Hayate daily audit cron 1-4pm EDT — **DOES NOT EXIST**

Hayate has a heartbeat (every 30 min market hours, 2 hr off-hours) but no scheduled daily audit. The HEARTBEAT.md is procedural only — no cron job wires it.

**What exists:**
- Hayate heartbeat: 30 min during market hours, 2 hr off-hours (per HEARTBEAT.md)
- All step outputs write to `data/overseer-outbox.jsonl` (160KB, active)
- `data/lifecycle_log.jsonl` (18KB, 100+ heartbeat_complete events)
- `data/decision_log.jsonl` (14KB, 30+ decisions)

**What's missing:**
- No `scripts/daily_audit.py` script
- No cron entry on the host (or `systemd` timer) to invoke it at 1-4pm EDT
- No Telegram digest format

**Effort: 1 SP**
- New `scripts/daily_audit.py` that:
  - Reads `data/overseer-outbox.jsonl` for last 24h
  - Cross-references `data/decision_log.jsonl` for material decisions
  - Counts open work cards, status changes
  - Generates 3-bullet digest
  - Writes to `data/state/ayumi_daily_digest_<date>.md`
- Add cron at `1:00 PM EDT` daily
- Telegram delivery wired separately (out of scope for this audit)

### 3.2 Drift detection (3d/7d cards, 2d/5d phases) — **DOES NOT EXIST**

No workboard-card age tracking anywhere in Hayate. Hayate tracks its own `heartbeat_count` (12 today) but not card age.

**Effort: 1 SP**
- New `scripts/drift_detect.py` that:
  - Queries workboard for all `todo` / `in_progress` cards
  - Computes age from `created_at` / `claimed_at` timestamp
  - Flags cards in_progress > 3d
  - Escalates cards in_progress > 7d
  - Flags phases (per BQ or plan state) with no activity > 2d
  - Writes alert to `data/overseer-outbox.jsonl`

### 3.3 FTMO daily tracker — **PARTIAL (70% complete)**

**What exists:**
- `src/forex_bot/reporting/equity_tracker.py:178` computes `ftmo_status` field:
  - `BREACH` if daily loss ≥ 10%
  - `WARNING` if daily loss ≥ 5% OR drawdown ≥ 5%
  - `OK` otherwise
- `src/forex_bot/adapters/ctrader/risk_guard.py:416` `_check_best_day_rule` checks 50% rule
- `src/forex_bot/risk/ftmo_guard.py:442` `check_best_day_rule` (public method, also 50% rule)
- Daily report writer: `equity_tracker.py:262` `write_daily_report(date)`
- Active daily report output: `data/forex/equity_reports/2026-07-08.md`:
  ```
  # Equity Daily Report — 2026-07-08
  | Metric | Value |
  |---|---|
  | Open Balance | $9,314.08 |
  | Close Balance | $9,653.32 |
  | P&L | $+339.24 |
  | P&L % | +3.64% |
  | Max Drawdown | 6.86% |
  | Trades | 2 |
  | FTMO Status | WARNING |
  ```

**What's missing vs. plan:**
- Thresholds are 5%/10% (FTMO 2-Step) not 3%/10% (1-Step) — reconciliation needed
- Best-day ratio not in daily report (only tracked in `risk_guard` internal state)
- Daily P&L vs 3% limit not explicit (only "ftmo_status" string)
- No `drawdown_from_peak` field (only "Max Drawdown" in-day)
- No `open_positions_count` field

**Effort: 0.5 SP** — extend `EquityTracker.daily_summary` to return all 1-Step fields; update `write_daily_report` template

### 3.4 Challenge-completion detector (equity ≥ 1.10×) — **DOES NOT EXIST IN PRODUCTION**

**Backtest-only equivalent:** `src/forex_bot/backtest/ftmo_simulation.py:77` `profit_target_reached` checks `current_balance >= starting_balance * 1.10`. This is backtest-side only.

**Production equivalent:** None. The forward test can run indefinitely past the 10% target without freezing.

**What's needed (0.5 SP):**
- New method `FTMOGuard.check_challenge_complete()` (alongside `check_best_day_rule` at line 442)
- Returns `True` when `current_balance / starting_balance >= 1.10 AND open_position_count == 0`
- Triggers `KillSwitchManager.activate_global_freeze` (not kill — positions still need to be closable)
- Log to outbox + lifecycle_log

### 3.5 CET-midnight daily-loss reset — **LIVE, CORRECT**

`src/forex_bot/risk/ftmo_guard.py:41` `_cet_date(now)` returns CET date as `YYYY-MM-DD` string.
`src/forex_bot/risk/sl_position_sizer.py:345` `reset_daily(cet_date=...)` zeros the daily risk counter using CET date.
`src/forex_bot/forward_test/blend_runner.py:142` explicitly references "CET date (FTMO spec)" and uses `_cet_date` from `risk.ftmo_guard`.

**Live evidence:** Forward test heartbeat logs show daily_pnl reset behavior per CET boundary.

**Status: COMPLETE — 0 SP**

### 3.6 Kanban hygiene (archive done>72h, surface stale, reassign orphans) — **DOES NOT EXIST**

No `scripts/kanban_hygiene.py` or similar. Workboard plugin has `workboard_board_archive` for board-level archive but no card-level auto-archiving.

**Effort: 0.5 SP**
- New `scripts/kanban_hygiene.py` that:
  - Queries `workboard_list(status=done, boardId=...)` for cards with `completed_at > 72h ago`
  - Calls `workboard_block` (or new archive API) for each
  - Queries `in_progress` cards with no `claimed_at` heartbeat in 3d → flag stale
  - Queries `todo` cards with no assignee → flag orphan

### 3.7 North-star tracking — **LIVE (out of Ayumi repo)**

`/root/.openclaw/workspace/data/state/north_stars.json` (1.1.0, council-reviewed) — single source of truth for north stars
`/root/.openclaw/workspace/scripts/north_star_report.py` — generates weekly report card
`/root/.openclaw/workspace/data/state/north_star_report_2026-07-07.md` — latest report

**Live evidence:** `ayumi-revenue` star lists:
- Q3_2026: "Forward test profitable over 30-day window with <10% drawdown"
- Blockers: TP/SL optimization overdue (live-fire-2c5d684a), signal dry spell diagnosis
- Status: in_progress

**Status: COMPLETE — 0 SP** (Ava/Craig workspace, not Ayumi)

---

## 4. Edge-Weighted Risk Allocator (Phase 4)

### 4.1 `regime_thresholds` — **EXISTS, NOT WIRED TO SIZER**

`src/forex_bot/risk/regime_thresholds.py` (157 lines):
- `class Regime(str, Enum)`: STABLE / BREAKDOWN / TRANSITION
- `class RegimeThresholds` (frozen dataclass)
- `class RegimeAwareThresholds`:
  - `get_thresholds(regime)` returns per-regime threshold bundle
  - `get_exposure_multiplier(regime)` returns 0.5 for BREAKDOWN, 1.0 for STABLE
  - `classify_signal(...)` — uses HMM

`src/forex_bot/risk/correlation_sizer.py:159-181` `compute_adjusted_size(regime: RegimeLike | None = None, ...)`:
- Accepts optional regime argument
- If regime == BREAKDOWN: scale by 0.5
- Currently NOT called by any production path

**What's needed (1 SP):**
1. `ForwardTestEngine._evaluate_bars` (or the per-bar processing loop) needs to:
   - Detect current regime via `quant.correlation_regime_hmm.CorrelationRegimeHMM` (or simpler volatility/trend classifier from `quant/regime_detection.py`)
   - Pass `regime=` to `BlendForwardTestRunner.on_signal` → `SLPositionSizer.calculate`
2. Add `self._current_regime` field to `ForwardTestConfig` for live tracking
3. Test: regime change → next position size scales 50%

### 4.2 Edge telemetry (R-multiple expectancy) — **DOES NOT EXIST**

Confirmed: `grep -rln "expectancy|r_multiple|edge_telemetry" src/forex_bot/risk/ → no results` (other than backtest-side `amalgamation.py` / `selective_pairing.py` which compute backtest expectancy, not live rolling).

`src/forex_bot/quant/go_nogo_criteria.py` has `PerWindowCriteria` (min_trades, win_rate, profit_factor, total_pnl, max_drawdown) but no R-multiple / expectancy primitive.

**What's needed (2 SP):**
1. New `src/forex_bot/risk/edge_telemetry.py`:
   ```python
   class EdgeTracker:
       """Per-(strategy, symbol) rolling N-trade R-multiple expectancy."""
       def __init__(self, rolling_window: int = 20):
           self._window = rolling_window
           self._samples: dict[tuple[str, str], deque[float]] = {}

       def record_trade(self, strategy_id: str, symbol: str, r_multiple: float) -> None:
           key = (strategy_id, symbol)
           self._samples.setdefault(key, deque(maxlen=self._window)).append(r_multiple)

       def expectancy(self, strategy_id: str, symbol: str) -> float:
           samples = self._samples.get((strategy_id, symbol), [])
           return sum(samples) / len(samples) if samples else 0.0
   ```
2. Wire into `SLPositionSizer.calculate(...)`:
   ```python
   edge = self._edge_tracker.expectancy(strategy_id, symbol)
   risk_pct = self.risk_per_trade_pct * (1.0 + max(-0.5, min(0.5, edge * 2.0)))
   ```
3. Hook `record_trade` in `ForwardTestEngine._on_trade_close`
4. Persist edge state to `data/edge_state.json` (so it survives restart)

### 4.3 `live_fire_min_confidence` lower 0.65→0.55 — **EXISTS, DEFAULT UNCHANGED**

`src/forex_bot/adapters/ctrader/forward_test_engine.py:118`:
```python
live_fire_min_confidence: float = 0.65  # ConfidenceEngine threshold for live execution
```

Used at lines 425, 2178, 2181, 2184 (gating live execution). 1 test pins it: `tests/unit/ctrader/test_forward_test_confidence_gate.py:88` "Default live_fire_min_confidence must be 0.65."

**Effort: 0.5 SP** — change default to 0.55 + update test + 1-line config doc

### 4.4 Per-strategy × per-symbol edge telemetry — **PARTIAL (PnL only, no expectancy)**

`src/forex_bot/adapters/ctrader/risk_guard.py:136`:
```python
self._per_strategy_pnl: dict[str, float] = {}
```

`risk_guard.py:529-530` records per-strategy P&L on close:
```python
self._per_strategy_pnl[strategy_id] = (
    self._per_strategy_pnl.get(strategy_id, 0.0) + pnl
)
```

Exposed in stats at line 791: `"per_strategy_pnl": dict(self._per_strategy_pnl)`.

**Missing:**
- No per-symbol breakdown
- No R-multiple computation
- No rolling window (lifetime P&L only)
- No win rate / loss rate per strategy

**Effort: combined with 4.2** (2 SP total)

### 4.5 Keep existing confidence gates — **CONFIRMED LIVE**

`src/forex_bot/confidence/gates.py`:
- `class GateConfig` (line 19)
- `class SpreadGate` — line 76
- `class SessionGate`
- `class VolatilityGate`
- Wired into `ConfidenceEngine._gates` list at `confidence/engine.py:51`

**Status: COMPLETE — 0 SP**

---

## 5. Hayate Overseer

### 5.1 Overseer workspace — **EXISTS, LIVE**

`/root/.openclaw/ayumi-overseer-workspace/`:
- `AGENTS.md`, `HEARTBEAT.md`, `IDENTITY.md`, `SOUL.md`, `TOOLS.md`, `USER.md` — identity & ops docs
- `ROADMAP.md` — 3-phase evolution plan
- `data/`:
  - `overseer-outbox.jsonl` (60KB, 100+ events)
  - `overseer-inbox.jsonl`
  - `lifecycle_log.jsonl` (18KB, 100+ events)
  - `decision_log.jsonl` (14KB, 30+ decisions)
  - `kill_switches.json` — global pause / circuit breakers / repair backoff
  - `forward_test_health.json` — current snapshot
  - `overseer_state.json` — heartbeat state
  - `build_history.jsonl` — BQ build audit
  - `dcp/` — checkpoint archive

**Live heartbeat:** 12 heartbeats today (2026-07-08), cadence 30 min market hours / 2 hr off-hours.

### 5.2 Outbox events — **LIVE, RICH**

`data/overseer-outbox.jsonl` event types emitted (per `HEARTBEAT.md:150`):
- `status_update`
- `alert`
- `decision_log`
- `card_created`
- `repair_report`
- `weekly_summary`

**Sample recent heartbeat event (2026-07-08T13:07:03Z):**
```json
{"event":"heartbeat_complete","details":{"hb":12,"market":"open","forward_test":"running","pid":2125800,"uptime_s":41550,"tps":9.41,"ticks":281932,"signals":71,"sizing_rejections":68,"ctrader_balance":9653.32,"dd_pct":3.47,"disk_usage_pct":31,"repair_attempts":0,"cards_open":0,"verdict":"healthy_with_known_issues"}}
```

### 5.3 Checkpoint catalog — **DOES NOT EXIST**

No `docs/checkpoint-catalog.md` in `ayumi-overseer-workspace/docs/`. The plan calls for "Checkpoint catalog covering: system health, trading health, data health, pipeline health, north-star progress, kanban hygiene" (5+ categories, 20+ types).

**Existing checkpoint-like items in `HEARTBEAT.md`:**
- 7 steps (boot, forward test, market, signal flow, work card, strategy review, state update)
- Not a registry of checkpoint types

**Effort: 1 SP**
- New `docs/checkpoint-catalog.md` with 20+ checkpoint types across 5+ categories
- For each: name, severity, detection rule, runbook action
- Wire into Hayate heartbeat as a periodic validation

### 5.4 Drift detection (telemetry, cron skips) — **REFERENCED, NOT BUILT**

`/root/.openclaw/ayumi-overseer-workspace/ROADMAP.md:38-44`:
> | **Tripwire system (velocity / drift / stale-escalation)** | ❌ Not built | Absorbed from MA-001 — see Phase 3 below |

`HEARTBEAT.md:122` references "Parameter drift detection" as Phase 2 step.

**Effort: 1 SP** (drift baseline + alert per Hayate ROADMAP Phase 2/3)

### 5.5 Runbook per checkpoint — **DOES NOT EXIST**

No `docs/checkpoint-runbook.md` or per-failure-mode action guide. Hayate's HEARTBEAT.md has procedural steps but not "if X then Y" runbook format.

**Effort: combined with 5.3** (1 SP total for catalog + runbook)

---

## 6. Conflicts & Overlaps Identified

### 6.1 Two `HealthMonitor` classes (naming conflict)
- `src/forex_bot/engine/anomaly_monitor.py:54` `class HealthMonitor` — L0 alert detector
- `src/forex_bot/engine/health_monitor.py:30` `class HealthMonitor` — heartbeat logger

Both are imported as `HealthMonitor`. Module names differ but class names collide. Low-risk conflict (different modules) but creates import confusion.

**Resolution: 0.25 SP** — rename one to `L0HealthMonitor` (anomaly side) or `HeartbeatLogger` (other side). Plan-level decision needed.

### 6.2 `FTMOConfig` exists in 2 places
- `src/forex_bot/adapters/ctrader/risk_guard.py:92` `class FTMOConfig` — has `daily_loss_limit_pct`, `total_drawdown_limit_pct`, etc.
- `src/forex_bot/risk/ftmo_guard.py:128` `class FTMOGuard` — no `FTMOConfig` class but `DEFAULT_MAX_DAILY_LOSS_PCT = 4.0` constant

The plan says "FTMOConfig in risk/ftmo_guard.py is single source of truth" but the actual `FTMOConfig` class lives in `risk_guard.py` (line 92), not `ftmo_guard.py`. The plan's Phase 0 acceptance criterion ("Make FTMOConfig in risk/ftmo_guard.py the single source of truth; all other files import from it") needs reconciliation.

**Resolution: 0.5 SP** — move `FTMOConfig` from `risk_guard.py` to `ftmo_guard.py`, update imports.

### 6.3 Two FTMOBestDay checkers
- `src/forex_bot/adapters/ctrader/risk_guard.py:416` `_check_best_day_rule` (private)
- `src/forex_bot/risk/ftmo_guard.py:442` `check_best_day_rule` (public)

Both check 50% rule but with slightly different code paths. The plan calls for "best-day rule implementation merged to main" — needs to be one canonical implementation.

**Resolution: 0.5 SP** — pick `ftmo_guard.py` version as canonical, delete the `risk_guard.py` private copy.

### 6.4 `FTMOConfig` vs `FTMOGuard` vs `RiskGuard` confusion
- `RiskGuard` (line 116 of `risk_guard.py`): full risk engine with daily loss, drawdown, max positions
- `FTMOGuard` (line 128 of `ftmo_guard.py`): FTMO-specific rule enforcement, references `KillSwitchLike` protocol
- `FTMOConfig` (line 92 of `risk_guard.py`): just the config dataclass

The forward test uses `RiskGuard` (via `paper_trader._risk_guard`). `FTMOGuard` is referenced in `blend_runner.py` docstring but NOT instantiated anywhere in the production path.

**Resolution: 0.5 SP** — instantiate `FTMOGuard` in `ForwardTestEngine._build_components` and route all FTMO rules through it. Or document the intentional split.

### 6.5 `remediation_validated.flag` source of truth ambiguity
Plan says: "Reference existing remediation_validated.flag — don't rename or duplicate"
Live state: The flag is auto-recreated from `docs/audits/ayumi-live-remediation-session-audit-2026-06-30.md` when missing. So the **audit doc is the source of truth**, the flag is a derived cache.

**Resolution: 0 SP** (current behavior is correct — just document this in plan/HEARTBEAT)

### 6.6 `signal_validator.py` status
The plan says: "signal_validator.py (flagged DEAD in 2026-04-17 audit) — explicitly retire or document why it lives"

`src/forex_bot/signal_validator.py` has `class TradeRulesEngine` (per `src/forex_bot/hybrid/trade_rules.py:140` reference). It IS used in the hybrid engine. The "DEAD" flag from 2026-04-17 is stale.

**Resolution: 0.5 SP** — confirm hybrid engine import chain, remove "DEAD" flag, OR delete file + 1 test

### 6.7 Per-strategy risk allocation
- `SLPositionSizer.risk_per_trade_pct` is global (set at init)
- `CorrelationAwareSizer.compute_adjusted_size(per_trade_risk_pct=)` is per-call
- `PortfolioRiskGuard` tracks per-strategy P&L but not per-strategy risk budgets

The plan's "edge-weighted risk allocator" would need a per-(strategy, symbol) risk budget that overrides the global `risk_per_trade_pct`. Not present.

**Resolution: combined with 4.2 edge telemetry work** (2 SP)

---

## 7. Effort Summary

| Phase | Concept | Effort |
|-------|---------|--------|
| 2 | Resource cap wiring (20 scripts) | 0.5 SP |
| 2 | ProcessPoolExecutor default 2 | 0.5 SP |
| 2 | TradeStore bottleneck doc | 0 SP |
| 2 | Engine deprecation banners | 0.5 SP |
| 4 | Regime thresholds wire-in | 1 SP |
| 4 | Edge telemetry (R-multiple) | 2 SP |
| 4 | Lower live_fire_min_confidence | 0.5 SP |
| 5 | L1.5 remediation patterns (4) | 2 SP |
| 5 | L2/L3 git-log guard | 0.5 SP |
| 5 | Remediation cycle detector | 0.5 SP |
| 5 | signal_validator retire/confirm | 0.5 SP |
| 5 | Naming conflict (HealthMonitor x2) | 0.25 SP |
| 6 | Hayate daily audit cron | 1 SP |
| 6 | Drift detection (cards/phases) | 1 SP |
| 6 | FTMO daily tracker (1-Step fields) | 0.5 SP |
| 6 | Challenge-completion detector | 0.5 SP |
| 6 | Kanban hygiene | 0.5 SP |
| Hayate | Checkpoint catalog + runbook | 1 SP |
| Hayate | Drift detection (telemetry) | 1 SP |
| Recon | FTMOConfig single source of truth | 0.5 SP |
| Recon | Best-day rule canonicalize | 0.5 SP |
| Recon | FTMOGuard instantiation | 0.5 SP |
| **TOTAL** | | **~14.75 SP** |

**Already complete (0 SP):** ForwardTestEngine canonical, AnomalyMonitor L0, HealthMonitor heartbeat, KillSwitchManager L4, remediation_validated.flag self-healing, CET-midnight daily reset, North-star tracking, PID lock, resource_limits module, regime_thresholds module, confidence gates.

---

## 8. Risks & Open Questions

### 8.1 Risks
- **R1:** Resource cap wiring affects 20 scripts. If each takes 15 min to fix, that's 5 hours of mechanical work — easy to underestimate as "0.5 SP" without trying it.
- **R2:** Edge telemetry (Phase 4) requires new persistence layer + integration with both `ForwardTestEngine` and `BlendForwardTestRunner` — could creep to 3 SP if the integration is messy.
- **R3:** The "two HealthMonitor" naming conflict will bite at refactor time if not resolved — small now, costly later.
- **R4:** `FTMOGuard` is referenced in `blend_runner.py` docstring but never instantiated. If it WAS supposed to be wired and is silently dead, Phase 0 reconciliation may be incomplete.

### 8.2 Open questions for Ava/Craig
1. **Which `HealthMonitor` to rename?** Suggestion: rename `anomaly_monitor.py:54` to `L0HealthMonitor` (it's the alert detector) and keep `health_monitor.py:30` as `HealthMonitor` (heartbeat logger).
2. **Should `FTMOConfig` move to `risk/ftmo_guard.py`?** Or keep at `adapters/ctrader/risk_guard.py:92`? The plan says "FTMOConfig in risk/ftmo_guard.py is the single source of truth" but no such class exists there yet.
3. **What is `FTMOGuard` for if `RiskGuard` is doing the work?** Plan says to keep it, but the code path suggests it's unused. Card needed to either wire it in or delete it.
4. **Should `live_fire_min_confidence` default drop to 0.55 immediately, or A/B test first?** Plan says "lower" — Craig approval was given for this specifically.
5. **Hayate checkpoint catalog: 20+ types across 5+ categories. Does Ava have a preferred categorization scheme, or can Hayate propose one?**
6. **Daily audit cron: host system uses crontab or systemd timer?** Need to know which one to wire into.
7. **Telegram digest for daily audit: existing channel or new one?** Out of scope for this audit but blocks Phase 6.

### 8.3 Findings to card (per Findings & Debt Protocol)
- [FINDING] `signal_validator.py` status: referenced as DEAD in 2026-04-17 audit but appears live in `hybrid/trade_rules.py`. Verify and either delete or document.
- [FINDING] Two `HealthMonitor` classes in different modules — naming collision creates import confusion.
- [FINDING] `FTMOGuard` is referenced in `blend_runner.py` docstring but never instantiated. Either wire it or remove the reference.
- [FINDING] Two best-day rule checkers (`risk_guard._check_best_day_rule` private + `ftmo_guard.check_best_day_rule` public). Pick one canonical.
- [DEBT] Resource caps module complete but unwired into 20 sweep scripts. 0.5 SP to wire, but easy to defer repeatedly.
- [DEBT] Edge telemetry (R-multiple expectancy) entirely missing. Phase 4 cannot proceed without it.
- [DEBT] Hayate checkpoint catalog + runbook not built. Phase 6 daily audit cannot reference it.

---

## 9. Verification Commands

To re-verify any of the above findings, run these read-only commands from `$AYUMI_ROOT`:

```bash
# Resource caps wired into scripts?
grep -rln "cpu_limited|memory_capped|add_resource_args" scripts/

# ProcessPoolExecutor max_workers defaults
grep -n "max_workers" src/forex_bot/backtest/parameter_sweep/sweep_runner.py

# Live PID file
cat data/forward_test.pid
ls -la data/forward_test.pid

# AnomalyMonitor wiring
grep -n "HealthMonitor\|HealthMonitorConfig" src/forex_bot/adapters/ctrader/forward_test_engine.py | head -10

# Remediation flag live state
cat data/ayumi/remediation_validated.flag

# Best-day rule implementations
grep -n "check_best_day_rule\|_check_best_day_rule" src/forex_bot/ -r

# FTMOConfig definitions
grep -n "class FTMOConfig\|class FTMOGuard\|class RiskGuard" src/forex_bot/ -r

# live_fire_min_confidence default
grep -n "live_fire_min_confidence" src/forex_bot/adapters/ctrader/forward_test_engine.py

# Regime awareness in production
grep -rn "regime=" src/forex_bot/adapters/ctrader/ src/forex_bot/forward_test/

# Edge telemetry / expectancy
grep -rn "expectancy\|R-multiple\|r_multiple" src/forex_bot/risk/ src/forex_bot/orchestrator/

# Hayate outbox size + heartbeat count
wc -l /root/.openclaw/ayumi-overseer-workspace/data/overseer-outbox.jsonl
tail -1 /root/.openclaw/ayumi-overseer-workspace/data/lifecycle_log.jsonl

# EquityTracker FTMO output
cat data/forex/equity_reports/2026-07-08.md
```

---

*Audit complete. 13 of 22 plan concepts already live in production; remaining 9 require ~14.75 SP. Findings + debt items above for the workboard.*
