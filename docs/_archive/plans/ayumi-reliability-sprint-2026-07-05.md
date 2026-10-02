# Ayumi Reliability & TP/SL Recovery Sprint — Plan

**Sprint ID:** ayumi-reliability-2026-07-05  
**Date authored:** 2026-07-05 12:22 EDT (Stage 1 planner output, read-only)  
**Branch at planning:** `main` @ `d5e2e2f` (parent `f6dc2f2`) — clean apart from `data/ayumi/remediation_validated.flag` (operational, leave untracked)  
**Sprint status (2026-07-05 13:30 EDT):** ✅ **DEV COMPLETE** — 5/6 cards closed (a7b8e896, 23d53b40, 5470d08f, ca012aae via Ava; 9043c09c via tsukasa proofs). 1/6 in flight (2c5d684a live-fire, scheduled 17:30 EDT via cron). Post-mortem at `docs/post-mortems/ayumi-reliability-sprint-postmortem-2026-07-05.md`. 24h stability clock in flight.  
**Workspace:** `$AYUMI_ROOT`  
**Author:** Ava (planner subagent, depth 1/1)  
**Council trio designated:** Liora (data integrity), Kaito (systems arch), Mika (risk gates)  
**Status:** Ready for council + builder dispatch

> **Pre-flight verdict — sprint is right-sized.** 6 cards / ~10–13 SP / 11 builder tasks (all ≤1 SP). However, **the cTrader reliability Phase 1/2/3 cards are 80–90% already implemented** in the codebase — the research doc the cards cite is missing (Tsukasa already flagged this on 2026-07-01). Real implementation gaps are smaller than the card bodies imply; the cards need to be rescoped or partially closed before dispatch.

---

## 1. Sprint Goal & Acceptance Criteria

### Sprint Goal (Craig's gate — verbatim from sprint kickoff)

> Restore TP/SL on every position + rebuild cTrader connection reliability + deploy systemd unit + prove forward test stable for 24h OR until first live TP/SL position (whichever first).

### Sprint-Level Acceptance Criteria (Craig's gate — verbatim)

- [ ] All 4 TP/SL bug sites (F1, F2, F3, F5) fixed and merged to main
- [ ] All 3 cTrader reliability cards implemented + merged to main
- [ ] systemd unit deployed with `Restart=on-failure`, `StartLimitBurst=3`, `StartLimitIntervalSec=120`, `RestartSec=30`
- [ ] Forward test runs 24h+ without operator intervention OR first live position opens with TP/SL confirmed in cTrader UI (whichever first)
- [ ] TP1 + SL visible in cTrader UI on real position. TP2/TP3 visible in logs + position_monitor (proto only accepts single TP per position)
- [ ] No exit-6 (ABRT), exit-9 (KILL), or exit-75 (TEMPFAIL) patterns from May-June crash history
- [ ] All target tests pass: `pytest tests/forex_bot/adapters/ctrader/ -q` via `scripts/run_test_scope.sh`
- [ ] Sprint post-mortem with friction log + decision audit

### Card-by-Card Sub-AC

| # | Card | Sub-AC (verifies each item before marking card done) |
|---|------|-------------------------------------------------------|
| 1 | `a7b8e896` TP/SL fix (urgent) | F1 amend passes TP1 + stores TP2/TP3 as pending targets on the Position; F2 late-fill callback path identical fix; F3 paper_trader forwards all 3 TPs (or stored on Position); F5 position_monitor detects TP2/TP3 crossings and calls `amend_sl_tp` to advance. **Not modified:** `open_api_spot_feed.py:1018–1068` proto constraint, `strategies/*` (frozen). |
| 2 | `23d53b40` cTrader Phase 1 — separation + state machine (high) | Two `ConnectionStateManager` instances registered for MARKET_DATA + TRADE_EXECUTION roles in `ConnectionManager`; per-role state transitions logged; `ConnectionRole` enum wired into `forward_test_engine`; per-connection rate limit hooks documented. **Largely already implemented** — verify and close-out gaps (see §3.2). |
| 3 | `5470d08f` cTrader Phase 2 — heartbeat/watchdog + backoff (high) | `ConnectionWatchdog` running with 30s DEGRADED / 90s FAILED thresholds; `ReconnectStrategy` (AWS decorrelated jitter) driving backoff; `_check_connection_health` + `_attempt_reconnect` in `forward_test_engine.py` (BQ-1335) gating on `_is_forex_market_closed`; re-subscribe after reconnect. **Mostly already implemented — BQ-1335 progress assessed in §2.1.** |
| 4 | `ca012aae` cTrader Phase 3 — token refresh + error taxonomy (high) | `TokenLifecycle.ensure_valid()` with 5-day buffer; `ConnectionManager.refresh_oauth_if_needed()` (line 626); 4-tier `error_classifier.py` taxonomy (T1 transient / T2 backoff / T3A op / T3B halt); `authenticate_with_retry` with 3-attempt exponential backoff (BQ-1330a). **Mostly already implemented.** |
| 5 | `9043c09c` systemd unit (normal) | Root-cause analysis of May–June crash codes (6/ABRT, 9/KILL, 1/FAILURE, 75/TEMPFAIL); `~/.config/systemd/user/ayumi-forward-test.service` deployed with `Restart=on-failure`, `StartLimitBurst=3`, `StartLimitIntervalSec=120`, `RestartSec=30`; verified crash-kill (`kill -9`) → auto-restart within 30s; verified no rapid crash loop. |
| 6 | `2c5d684a` TP/SL live-fire (operator, urgent, 0.5 SP) | One small live position opened during market hours; `stop_loss` + `take_profit_1` visible in cTrader UI; screenshot or log captured; comment added to card with pass/fail. **Cannot execute from builder — must run after #1 merges.** |

---

## 2. Pre-Flight Summary — Current State Assessment

### 2.1 BQ-1335 Reconnect Logic (Phase 2) — Already Wired

**Evidence in code:**

| Location | What it does |
|---|---|
| `forward_test_engine.py:88–91` | `_is_forex_market_closed()` — guards reconnects during weekend (Fri 21:55 → Sun 21:00 UTC) |
| `forward_test_engine.py:130–133` | `stale_tick_threshold_sec = 60.0` (lowered from 900.0 per BQ-1335 comment) |
| `forward_test_engine.py:349–354` | `_stuck_reconnect_threshold_sec: float = 60.0` — forces reconnect evaluation if state stuck |
| `forward_test_engine.py:2017` | `_check_connection_health()` called every health tick |
| `forward_test_engine.py:2147–2247` | `_check_connection_health` + `_attempt_reconnect` with backoff gate |
| `forward_test_engine.py:2207` | `staleness < self._config.stale_tick_threshold_sec` |
| `forward_test_engine.py:2224` | market-closed guard |
| `tests/integration/test_reconnect_logic.py` | BQ-1335 tests for stuck-state detection |

**Verdict:** BQ-1335 reconnect logic is **~85% complete and merged**. Phase 2 card should be close-out / verify, not greenfield.

### 2.2 Phase 1E.2 Kill Switch Tests — Comprehensive With 5 xfail

**File:** `tests/unit/risk/test_kill_switch_auto.py` (549 lines)

**Complete (passing):**
- Watchdog detects stale heartbeat → activates kill
- Watchdog ignores stale heartbeat during weekend/market closed
- Watchdog ignores stale heartbeat when engine_running=false
- RiskGuard circuit breaker → activates kill switch
- Feed disconnect → activates freeze
- Error rate > 50% → activates freeze
- Heartbeat file atomic write
- Kill switch audit log entries

**xfail-deferred (5 tests, lines 405/429/449/485/549):**
- Reason: `"P5A scope-out: freeze activation code intentionally remains commented out per Phase 4 priority list. Tracked in P5A closeout."`

**Verdict:** Watchdog, RiskGuard circuit breaker, atomic heartbeat writes — **all verified**. Freeze-activation tests are explicitly out-of-scope per P5A. Do not un-xfail in this sprint.

### 2.3 cTrader Dual-Connection Architecture — Already Implemented

**Files present and wired:**

| File | Lines | Role |
|---|---|---|
| `connection_state.py` | 216 | `ConnectionState` enum (10 states) + `ConnectionStateManager` thread-safe state machine with full transition table |
| `connection.py` | — | TCP lifecycle |
| `connection_watchdog.py` | — | Heartbeat watchdog (30s DEGRADED, 90s FAILED, 5s poll) |
| `connection_manager.py` | — | `ConnectionManager` with `SplitBrainGate` (is_fully_operational / is_tradeable / is_data_available) |
| `reconnect_strategy.py` | — | AWS decorrelated jitter (`sleep = min(cap, random(base, prev * 3))`) |
| `error_classifier.py` | — | 4-tier taxonomy (T1 transient / T2 backoff / T3A op / T3B halt) |
| `token_lifecycle.py` | — | OAuth refresh with 5-day buffer, file+process locking |
| `credential_store.py` | — | `.env` credential loading |
| `auth_error_types.py` | — | Auth error categorization |

**Tests:** `tests/integration/test_connection_manager.py`, `test_connection_watchdog.py`, `test_connection_self_healing.py`, `test_connection_manager_wiring.py`, `test_reconnect_logic.py`, `tests/unit/data/test_ctrader_client.py`.

**Architecture doc:** `docs/forex/architecture-dual-connection.md` (Tsukasa, 2026-07-01) has explicit gap analysis: **8/10 recommendations ✅ implemented**, 1 ⚠️ PARTIAL (auto-reconcile on reconnect), 1 ✅ bonus (market hours awareness).

**Verdict:** The dual-connection architecture exists. Phase 1/2/3 cards should be **re-spec'd to verify & close-out gaps**, not implemented from scratch.

### 2.4 Forward Test Blocker Status

- **`data/ayumi/remediation_validated.flag`** present (mtime 2026-07-05 12:49:51 EDT, card a71e26e6 done).
- Sprint scope is clear: forward test can run.

### 2.5 Data Integrity Issues Discovered

| Issue | Source | Severity | Sprint decision |
|---|---|---|---|
| `Position` dataclass only has singular `take_profit: float \| None` (`models.py:54`) — no TP2/TP3 fields | Pre-existing | High | Fix in card 1 — extend Position to store TP2/TP3 as pending targets |
| `OrderManager.execute_live_order` / `execute_paper_order` only accept singular `take_profit` (`order_manager.py:186,256`) | Pre-existing | High | Fix in card 1 — either extend signatures OR store TP2/TP3 on Position post-create |
| `open_api_spot_feed.py:1018–1068` `amend_sl_tp` proto only accepts single SL/TP | Platform constraint | Informational | Document in card 1; cannot be fixed at code layer |
| `docs/research/ctrader-connection-reliability-research.md` does not exist | Tsukasa flagged 2026-07-01 | Medium | Open question for Craig (see §11) — but cards 2/3/4 should proceed using architecture-dual-connection.md as the design reference |
| Five freeze-activation tests marked xfail in `test_kill_switch_auto.py` | P5A scope-out | Low | Do not un-xfail (out of sprint scope) |
| No `~/.config/systemd/user/ayumi-forward-test.service` on disk | Previously removed | High | Card 5 creates it fresh |
| TP1 amend cooldown fix (2A) and broker response handling (2B) **already tested** in `tests/unit/ctrader/test_amend_sl_tp.py` | Kaito, June | Already done | Card 1's F1/F2 implementation should leverage these tests, not duplicate |

---

## 3. Card-by-Card Task Breakdown

> **DAG legend:** A → B means "A must merge before B starts."  
> **SP convention:** All builder tasks ≤ 1.0 SP. Anything larger is decomposed.

### 3.1 Card 1 — `a7b8e896` TP/SL fix (urgent, 2.5 SP)

**Source:** `docs/forex/tp-sl-chain-trace.md` (audit complete).

**Bug site summary:**

| ID | Site | Line | Fix |
|---|---|---|---|
| F1 | `forward_test_engine.py` amend_sl_tp (immediate path) | 1278 | Pass TP1 as active, store TP2/TP3 on Position |
| F2 | `forward_test_engine.py` amend_sl_tp (late-fill callback) | 1575 | Identical fix to F1 |
| F3 | `paper_trader.py` `_execute_order` | 240, 249 | Forward all 3 TPs (live + paper) |
| F4 | `open_api_spot_feed.py` `amend_sl_tp` proto | 1018–1068 | **DOCS ONLY** — platform constraint |
| F5 | `position_monitor.py` | (no TP-ratcheting) | Add `check_tp_levels()` ratchet logic |

#### Task 1.1 — Extend `Position` dataclass with TP2/TP3 pending fields  (1.0 SP, code-gen)

**Files:**
- `src/forex_bot/adapters/ctrader/models.py:54` — extend `Position` with `take_profit_2: float | None = None`, `take_profit_3: float | None = None`, `tp_levels_fired: list[int] = field(default_factory=list)` (e.g. `[1]` after TP1 hits). Backward-compatible (default `None` / `[]`).

**Builder type:** code-gen  
**Merge-before:** Task 1.2, 1.3, 1.4  
**Touches:** `models.py` only  
**Tests added:** `tests/unit/ctrader/test_position_tp_levels.py` (Position dataclass roundtrip; default values; tp_levels_fired idempotency).

#### Task 1.2 — Wire TP2/TP3 into `OrderManager.execute_live_order` + `execute_paper_order`  (1.0 SP, code-gen)

**Files:**
- `src/forex_bot/adapters/ctrader/order_manager.py:141` (`execute_paper_order`) and `:255` (`execute_live_order`) — add `take_profit_2: float | None = None`, `take_profit_3: float | None = None` params; pass through to Position construction.

**Builder type:** code-gen  
**Merge-before:** Task 1.3  
**Touches:** `order_manager.py` only  
**Tests:** Extend `tests/integration/test_ctrader_paper_trader.py` and `tests/integration/test_ctrader_execution_v2.py` with TP2/TP3 propagation tests.

#### Task 1.3 — F1 + F2 fix in `forward_test_engine.py` — pass TP1 as active, store TP2/TP3 on Position  (1.0 SP, code-gen)

**Files:**
- `src/forex_bot/adapters/ctrader/forward_test_engine.py:1278` (immediate path) — keep `signal.take_profit_1` in `amend_sl_tp(position_id, sl, tp=tp1)`, then **after** successful amend, store `tp2`/`tp3` on the Position via `order_manager.update_position_tp_levels(position_id, tp2, tp3)`.
- `forward_test_engine.py:1575` (late-fill callback) — identical treatment.

**Builder type:** code-gen  
**Merge-before:** Task 1.5 (position_monitor needs stored TP2/TP3)  
**Touches:** `forward_test_engine.py` only  
**Tests:** New `tests/forex_bot/adapters/ctrader/test_tp_sl_submission.py` (file does not yet exist — create). Mock `amend_sl_tp` to assert only TP1 passed at the broker boundary; assert `update_position_tp_levels` was called with TP2/TP3. Add late-fill callback test.

#### Task 1.4 — F3 fix in `paper_trader.py` — forward all 3 TPs  (0.5 SP, code-gen)

**Files:**
- `src/forex_bot/adapters/ctrader/paper_trader.py:240, 249` (and `:125, :200` — same pattern) — pass `signal.take_profit_2`, `signal.take_profit_3` to `_order_manager.execute_live_order` and `execute_paper_order`. Backward-compatible (`None` defaults).

**Builder type:** code-gen  
**Merge-before:** Task 1.5  
**Touches:** `paper_trader.py` only  
**Tests:** Extend `tests/integration/test_ctrader_paper_trader.py` with TP2/TP3 forwarding test.

#### Task 1.5 — F5 fix: `position_monitor.py` TP ratcheting via `amend_sl_tp`  (1.0 SP, code-gen)

**Files:**
- `src/forex_bot/adapters/ctrader/position_monitor.py` — add `check_tp_levels(prices: dict) -> list[Action]` method called from `update_positions()` every tick. For each position with TP2/TP3 set and not in `tp_levels_fired`:
  - LONG: if `price >= position.take_profit_2` and `2 not in tp_levels_fired`: call `amend_sl_tp(position_id, sl=breakeven, tp=tp2)`, append 2 to `tp_levels_fired`.
  - LONG: if `price >= position.take_profit_3` and `3 not in tp_levels_fired`: call `amend_sl_tp(position_id, sl=tp2, tp=tp3)`, append 3.
  - SHORT: mirror logic.
  - Idempotency via `tp_levels_fired` list.
- Optional: at TP1 hit, move SL to breakeven (Option B-lite from trace doc — but only after TP1 broker-side close is detected). **Deferred to follow-up card** unless sprint has bandwidth — TP1 is already broker-side, so this is a bonus.

**Builder type:** code-gen  
**Merge-before:** Task 1.6 (docs)  
**Touches:** `position_monitor.py` only  
**Tests:** New `tests/unit/execution/test_position_monitor_tp_ratchet.py`. Mock `amend_sl_tp`. Verify LONG/SHORT logic, idempotency, no double-amend.

#### Task 1.6 — F4 docs: explain amend_sl_tp proto constraint  (0.25 SP, docs)

**Files:**
- `docs/forex/tp-sl-chain-trace.md` — append "Post-sprint: status" section noting F4 is platform constraint, see `open_api_spot_feed.py:1018–1068`. Reference position_monitor ratcheting as the workaround.
- Inline comment in `open_api_spot_feed.py:1018` referencing the trace doc.

**Builder type:** docs  
**Merge-before:** Card 1 close-out

**Card 1 SP total:** 4.75 SP if we count all 6 tasks individually, but the card-level estimate is 2.5 SP because F1+F2 share a builder and F4 is docs. **Recommend re-spec the card** to align estimate (2.5 SP ≈ ~5 builder tasks at ~0.5 SP each after decomposition). The card body already says 2.5 SP — that's correct post-decomposition.

### 3.2 Card 2 — `23d53b40` cTrader Phase 1: Connection Separation + State Machine (high, 2–3 SP)

> **Planner note:** Phase 1 is **already implemented**. This card needs to be re-spec'd as verification + gap close-out. Real work: ~1.0 SP.

#### Task 2.1 — Verify `ConnectionManager` has two registered `ConnectionStateManager` instances  (0.5 SP, read/analyze)

**Files to read:**
- `src/forex_bot/adapters/ctrader/connection_manager.py` — verify `register(ConnectionRole.MARKET_DATA, state_mgr)` and `register(ConnectionRole.TRADE_EXECUTION, state_mgr)` are both called from `forward_test_engine.__init__` or wherever the connection pair is constructed.
- `src/forex_bot/adapters/ctrader/forward_test_engine.py` — verify `_market_feed` and `_market_feed` (trade client, if separate) both have a `.state_manager` attribute pointing to a `ConnectionStateManager`.

**Builder type:** read/analyze → write verification report to `docs/forex/ctrader-phase1-verification-2026-07-05.md`. **DO NOT modify code** unless a concrete gap is found.

**Tests:** `tests/integration/test_connection_manager_wiring.py` already exists per `find`. Run it; assert both roles registered.

**Gap to close:** If `forward_test_engine` only creates ONE `CTraderConnection` (single connection reused for both roles), then split into two. Most likely **already split** — verify only.

#### Task 2.2 — Per-connection rate limit hook (deferred)  (0.5 SP, code-gen IF gap exists; else 0 SP)

**Files:** `connection_manager.py` — add a `RateLimitCoordinator` that tracks in-flight requests per role and rejects when either role exceeds its quota.

**Builder type:** code-gen — **only if Task 2.1 finds this missing**. The architecture doc lists it as ✅ implemented, so most likely 0 SP.

**Decision rule:** if Task 2.1 finds the rate limit coordinator already present, mark Task 2.2 as `skipped` with evidence and close card.

**Card 2 SP total:** 1.0 SP if Tasks 2.1 + 2.2 are both needed (most likely 0.5 SP if only verification).

### 3.3 Card 3 — `5470d08f` cTrader Phase 2: Heartbeat/Watchdog + Reconnection Backoff (high, 2 SP)

> **Planner note:** Already 80%+ implemented. Card body should be re-spec'd.

#### Task 3.1 — Verify `ConnectionWatchdog` running + `ReconnectStrategy` wired  (0.5 SP, read/analyze)

**Files:**
- `src/forex_bot/adapters/ctrader/connection_watchdog.py` — verify thresholds (30s DEGRADED, 90s FAILED, 5s poll).
- `src/forex_bot/adapters/ctrader/reconnect_strategy.py` — verify AWS decorrelated jitter formula, base=1s, cap=60s, max=10.
- `forward_test_engine.py:2017, 2147, 2249` — verify `_check_connection_health` and `_attempt_reconnect` call into the watchdog/strategy.

**Builder type:** read/analyze → report.

#### Task 3.2 — Re-subscribe to spot feeds after reconnect (verify + fill gap if needed)  (0.5 SP, code-gen OR verify)

**Files:** `forward_test_engine.py` — after `_attempt_reconnect` succeeds (state → AUTHENTICATED), call `_market_feed.resubscribe_all()`. Verify if already present.

**Builder type:** code-gen only if Task 3.1 finds this missing. Existing test `tests/integration/test_connection_self_healing.py` covers partial re-subscription logic.

**Card 3 SP total:** 1.0 SP most likely (verify + 0–1 small fix).

### 3.4 Card 4 — `ca012aae` cTrader Phase 3: Auth Lifecycle + Error Classification (high, 1–2 SP)

> **Planner note:** Mostly implemented.

#### Task 4.1 — Verify `TokenLifecycle` 5-day buffer + `ConnectionManager.refresh_oauth_if_needed`  (0.25 SP, read/analyze)

**Files:** `token_lifecycle.py` line 35 (`REFRESH_BUFFER = timedelta(days=5)`), `connection_manager.py` line 626 (`refresh_oauth_if_needed`).

**Builder type:** read/analyze → report.

#### Task 4.2 — Verify 4-tier `error_classifier.py` taxonomy + integration with reconnect_strategy  (0.25 SP, read/analyze)

**Files:** `error_classifier.py` (4 tiers), `reconnect_strategy.py` (uses `classify_error` from error_classifier to route).

**Builder type:** read/analyze → report.

#### Task 4.3 — Verify `authenticate_with_retry` (BQ-1330a) 3-attempt exponential backoff  (0.25 SP, read/analyze)

**Files:** `connection_manager.py:785` (`authenticate_with_retry`), `connection_manager.py:50–52` (`AUTH_RETRY_MAX_ATTEMPTS = 3`, `AUTH_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)`).

**Builder type:** read/analyze → report.

#### Task 4.4 — Secure token persistence audit  (0.25 SP, read/analyze)

**Files:** `credential_store.py` — verify `.env` permissions, no plaintext token in logs.

**Builder type:** read/analyze → report findings. **Code change only if Liora flags a real exposure** — outside sprint scope otherwise.

**Card 4 SP total:** 1.0 SP (mostly verification; tokens persist + secure storage likely already correct).

### 3.5 Card 5 — `9043c09c` systemd Unit for Forward Test (normal)

#### Task 5.1 — Root-cause analysis of May–June crash codes  (1.0 SP, read/analyze)

**Output:** `docs/post-mortems/ayumi-may-june-crash-root-cause-2026-07-05.md`. Read `journalctl --user -u ayumi-forward-test.service` if any prior log retained; otherwise rely on `docs/post-mortems/ctrader-openapi-connection-2026-06-11.md` (existing post-mortem). Cross-reference git log around each crash date.

**Builder type:** read/analyze.

**Hypothesis (preliminary, to verify):**
- Exit 6/ABRT (May 22) — likely watchdog timeout; fixed by BQ-1335 reconnect logic + ConnectionWatchdog.
- Exit 9/KILL (May 24, Jun 03) — likely OOM or operator kill; check if OOM scores in journal.
- Exit 1/FAILURE (May 27, Jun 7–8, Jun 10) — startup crash; check for missing `.env` vars or import errors.
- Exit 75/TEMPFAIL (Jun 02, 3× in 20s) — likely cTrader auth failure on startup; **almost certainly mitigated** by `authenticate_with_retry` (BQ-1330a) + reconnect logic.

#### Task 5.2 — Create `~/.config/systemd/user/ayumi-forward-test.service`  (0.5 SP, code-gen)

**File:** `~/.config/systemd/user/ayumi-forward-test.service` (NOT in repo — installed system-wide).

**Spec (per card acceptance):**
```ini
[Unit]
Description=Ayumi Forward Test (live multi-strategy paper trading)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$AYUMI_ROOT
ExecStart=$AYUMI_ROOT/.venv/bin/python scripts/launch_blend_forward_test.py --live
Restart=on-failure
StartLimitBurst=3
StartLimitIntervalSec=120
RestartSec=30
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

**Builder type:** code-gen + system install (`systemctl --user daemon-reload && systemctl --user enable ayumi-forward-test.service`).

**Touches:** system-level config (not in repo). Document install procedure in `docs/runbooks/ayumi-systemd-install.md`.

**Note:** `_refuse_root()` guard in `scripts/launch_blend_forward_test.py:30–39` will exit cleanly if launched as root — this guard is **already in place**, do not modify.

#### Task 5.3 — Verify `kill -9` recovery + no rapid crash loop  (0.5 SP, integration test)

**Procedure (operator):**
1. `systemctl --user start ayumi-forward-test.service`
2. `systemctl --user status` → confirm active
3. `pkill -9 -f launch_blend_forward_test` (simulate crash)
4. `sleep 35` (RestartSec=30 + buffer)
5. `systemctl --user status` → confirm auto-restarted
6. `pkill -9 -f launch_blend_forward_test` × 4 rapidly within 120s
7. After 4th kill: confirm systemd stops trying (StartLimitBurst=3 hit, then StartLimitIntervalSec=120 expires)
8. `journalctl --user -u ayumi-forward-test.service --since "1 hour ago"` → no exit-6/9/75 patterns

**Builder type:** integration test (operator-supervised; cannot run from heartbeat).

**Card 5 SP total:** 2.0 SP (most uncertain of the cards — depends on Task 5.1 finding actionable code fixes).

### 3.6 Card 6 — `2c5d684a` TP/SL live-fire verification (operator action, urgent, 0.5 SP)

**Cannot be dispatched to a builder.** This is operator-only.

#### Task 6.1 — Operator live-fire  (0.5 SP, operator action)

**Prerequisite:** Card 1 merged to main + forward test restart.

**Procedure:**
1. `systemctl --user restart ayumi-forward-test.service` (after card 1 + 5 both merged)
2. Wait for first signal during market hours (GBPUSD or USDJPY session)
3. Confirm position opens in cTrader UI
4. Verify `stop_loss` + `take_profit_1` are visible in cTrader UI (NOT `take_profit_2/3` — those are software-side, see card 1 F5)
5. Screenshot or log position details
6. Close position manually
7. Add comment to card `2c5d684a` with PASS/FAIL + screenshot path

**Builder type:** operator (Ava supervised).

**Card 6 SP total:** 0.5 SP.

### 3.7 Cross-Cutting DAG

```
            ┌──────────────────────┐
            │ Card 1: TP/SL fix    │
            │  ├ 1.1 Position ext  │
            │  ├ 1.2 OrderManager  │
            │  ├ 1.3 F1+F2 amend   │
            │  ├ 1.4 F3 paper      │
            │  ├ 1.5 F5 monitor    │
            │  └ 1.6 F4 docs       │
            └──────────┬───────────┘
                       │ (all merge to main)
                       ▼
            ┌──────────────────────┐
            │ Card 5: systemd      │
            │  ├ 5.1 root cause    │
            │  ├ 5.2 unit file     │
            │  └ 5.3 kill -9 test  │
            └──────────┬───────────┘
                       │
                       ▼
            ┌──────────────────────┐
            │ Card 6: live-fire    │
            │  └ 6.1 operator      │
            └──────────────────────┘

            ┌──────────────────────────────────────┐
            │ Cards 2/3/4: cTrader reliability     │
            │  ├ 2.1 verify + 2.2 (gap-if-found)   │
            │  ├ 3.1 verify + 3.2 (gap-if-found)   │
            │  └ 4.1-4.4 verify (mostly read-only) │
            └──────────────────────────────────────┘
                   INDEPENDENT of cards 1/5/6
                   (parallelizable in any wave)
```

**Key insight:** Cards 2/3/4 (cTrader reliability) are **independent** of cards 1/5/6. They are 80%+ already implemented and can run as verification-only in parallel.

---

## 4. Builder Sequencing Plan

### Wave 1 — Parallel (≤2–3 concurrent) — Discovery + Foundation

| Builder | Task | SP | Wall-clock | Notes |
|---|---|---|---|---|
| B1 | Card 1.1 — Extend `Position` dataclass | 1.0 | ~45 min | Foundational; must land first |
| B2 | Card 1.2 — Wire TP2/TP3 into `OrderManager` | 1.0 | ~45 min | After 1.1 merges |
| B3 | Cards 2/3/4 verification report | 1.0 | ~60 min | Read-only, parallel to B1/B2; single builder covers all 3 |

**Wall-clock estimate:** ~90 min (B1 + B2 sequential within Wave 1; B3 in parallel).

### Wave 2 — TP/SL Logic + systemd Root Cause (parallel)

| Builder | Task | SP | Wall-clock | Notes |
|---|---|---|---|---|
| B4 | Card 1.3 — F1 + F2 amend fix in `forward_test_engine` | 1.0 | ~60 min | After 1.1 + 1.2 land |
| B5 | Card 1.4 — F3 paper_trader | 0.5 | ~30 min | After 1.2 lands |
| B6 | Card 5.1 — Root-cause analysis | 1.0 | ~90 min | Independent of card 1 |

**Concurrency:** 3 builders, all touching different files (no overlap). OK.

**Wall-clock estimate:** ~90 min.

### Wave 3 — TP Ratchet + systemd Deploy (sequential, file-overlap)

| Builder | Task | SP | Wall-clock | Notes |
|---|---|---|---|---|
| B7 | Card 1.5 — Position monitor TP ratchet | 1.0 | ~75 min | After 1.3 lands (needs TP2/TP3 on Position) |
| B8 | Card 5.2 — systemd unit file + install | 0.5 | ~30 min | Independent |
| B9 | Card 1.6 — F4 docs | 0.25 | ~15 min | Doc-only |

**Concurrency:** 3 builders, all different files. OK.

**Wall-clock estimate:** ~75 min (B7 is the bottleneck).

### Wave 4 — Verification

| Builder | Task | SP | Wall-clock | Notes |
|---|---|---|---|---|
| B10 | Card 5.3 — kill -9 recovery test | 0.5 | ~45 min | Operator-supervised |
| B11 | Card 6.1 — Live-fire verification | 0.5 | operator | After B7 + B8 land |

**Wall-clock estimate:** ~60 min + operator window (depends on market hours).

### Total Wall-Clock Estimate

| Scenario | Estimate | Reasoning |
|---|---|---|
| **Best case** | ~6 hours | No surprises in verification tasks; builder latency avg 60 min |
| **Likely** | ~10 hours | 1–2 small fixes found in cards 2/3/4; minor Council revisions |
| **Worst case** | ~18 hours | Position dataclass change cascades; freeze tests need un-xfail (out of scope, would block); live-fire waits for next market open |

### File-Overlap Matrix

| File | Tasks touching it | Constraint |
|---|---|---|
| `models.py` | 1.1 only | OK (single builder) |
| `order_manager.py` | 1.2 only | OK |
| `forward_test_engine.py` | 1.3 only | OK |
| `paper_trader.py` | 1.4 only | OK |
| `position_monitor.py` | 1.5 only | OK |
| `~/.config/systemd/user/ayumi-forward-test.service` | 5.2 only | System-level, single builder |
| `docs/forex/tp-sl-chain-trace.md` | 1.6 (append only) | OK |

**No file-overlap conflicts across builders.** All Wave 1–3 builders can run in true parallel.

---

## 5. Council Review Scope

### Council trio

- **Liora** — data integrity (TP2/TP3 propagation, Position dataclass schema, kill_switch atomic writes)
- **Kaito** — systems architecture (state machine, watchdog, reconnect strategy, systemd unit)
- **Mika** — risk gates (TP ratcheting idempotency, position monitor SL move-to-breakeven, FTMO compliance)

### Per-task review assignments

| Task | Liora | Kaito | Mika | When |
|---|---|---|---|---|
| 1.1 Position dataclass | ✅ **PRIMARY** — schema change; ensure backward compat | — | — | After B1 claims, before merge |
| 1.2 OrderManager | ✅ — TP propagation paths | — | — | After B2 claims |
| 1.3 F1+F2 amend | ✅ — TP2/TP3 stored on Position | — | — | After B4 claims |
| 1.4 F3 paper | ✅ — TP2/TP3 forward | — | — | After B5 claims |
| 1.5 TP ratchet | ✅ — idempotency keys | — | ✅ **PRIMARY** — SL move-to-breakeven logic, FTMO 5% daily loss unchanged | After B7 claims |
| 1.6 F4 docs | ✅ — accurate reflection of proto constraint | — | — | After B9 claims |
| 2.1–2.2 verify | — | ✅ — if gap found, review the fix | — | After B3 claims |
| 3.1–3.2 verify | — | ✅ — if gap found, review the fix | — | After B3 claims |
| 4.1–4.4 verify | ✅ — token persistence audit is Liora's lane | — | — | After B3 claims |
| 5.1 root cause | — | ✅ **PRIMARY** — exit code analysis | — | After B6 claims |
| 5.2 systemd unit | — | ✅ — restart policy, ordering | — | After B8 claims |
| 5.3 kill -9 test | — | ✅ — verify restart behavior matches spec | ✅ — verify no FTMO breach window | After B10 claims |
| 6.1 live-fire | ✅ — verify TP2/TP3 in logs | ✅ — verify TP1+SL in cTrader UI | — | After B11 (operator) |

**Council total:** ~8 reviews. Liora: 6 (data lane). Kaito: 6 (systems). Mika: 2 (risk on 1.5 + 5.3).

---

## 6. Risk Analysis

| # | Risk | Likelihood | Impact | Mitigation | Rollback |
|---|---|---|---|---|---|
| R1 | `Position` dataclass extension breaks 5+ existing tests (Position equality, repr, serialization) | Medium | Low | Backward-compatible defaults; run full ctrader test scope after B1 | `git revert <B1 commit>` |
| R2 | Live-fire verification card (2c5d684a) cannot run before market close today | Medium | Medium | Forward-test stability 24h+ covers equivalent ground if market closes; otherwise reschedule to next session | N/A — verification, not feature |
| R3 | systemd unit auto-restart masks underlying crash bug (crash-loop with RestartSec=30 + StartLimitBurst=3) | Low | High | Task 5.1 root-cause must complete before 5.2 deploy; do NOT enable auto-restart without root-cause | `systemctl --user disable ayumi-forward-test.service` + revert to bare-process |
| R4 | Council rescopes card 1 from 2.5 SP to larger (e.g. full Option B ladder with SL breakeven moves) | Medium | High | Card body already says "Option A + B-lite" — clear scope in trace doc. Council should respect trace doc recommendation. | Planner holds card at ready; if rescoped, decompose and re-spec |
| R5 | F4 docs update references wrong line numbers (e.g. open_api_spot_feed.py line shifted) | Low | Low | Builder runs `grep -n "def amend_sl_tp" src/forex_bot/adapters/ctrader/open_api_spot_feed.py` before documenting | Doc-only — trivial revert |
| R6 | Cards 2/3/4 verification finds a real gap that's >1 SP (e.g. dual-connection not actually wired in `forward_test_engine`) | Low–Medium | High | If gap >1 SP: card-body rescope + open new sprint card; do NOT silently expand sprint scope | Revert to "verify" only; new card for the fix |
| R7 | TP ratchet idempotency bug — TP2 amends twice on rapid price oscillation | Medium | Medium | `tp_levels_fired` list on Position is the idempotency key; thorough tests in Task 1.5; Council Liora reviews | `git revert <B7 commit>` |
| R8 | Operator live-fire (Task 6.1) opens a position in volatile market and gets stopped out before TP/SL confirmation | Low | Low | Use 0.01 lots minimum; place position manually during low-volatility hours (London/NY overlap); screenshot within 1 minute of fill | Manual close — no code rollback needed |
| R9 | Pre-council checklist `planner-pre-council-checklist.md` may not be at expected path in this repo | Medium | Low | Found at `/root/.openclaw/workspace/docs/plans/planner-pre-council-checklist.md` (workspace, not Ayumi repo). Plan written assuming the workspace version is the reference. | N/A |
| R10 | Council discovers freeze-activation xfail tests should be un-xfailed in this sprint | Low | High | Out of sprint scope per P5A closeout. If Council insists, file separate card. | N/A — defer |
| R11 | The MISSING research doc (`docs/research/ctrader-connection-reliability-research.md`) blocks Phase 1/2/3 cards because cards reference it explicitly | Low | Medium | Architecture doc (`docs/forex/architecture-dual-connection.md`) serves as the design reference; cite it in card verification reports. Phase 1/2/3 verification can proceed without the original research doc. | N/A — documentation only |

---

## 7. Verification Gates

### 7.1 Per-Card Verification

| Card | Unit tests | Integration tests | Live-fire / operator |
|---|---|---|---|
| 1 TP/SL fix | `tests/unit/ctrader/test_position_tp_levels.py` (new), `tests/unit/execution/test_position_monitor_tp_ratchet.py` (new) | `tests/integration/test_ctrader_paper_trader.py`, `tests/integration/test_ctrader_execution_v2.py`, `tests/forex_bot/adapters/ctrader/test_tp_sl_submission.py` (new) | Card 6 live-fire |
| 2 cTrader Phase 1 | (existing) `tests/unit/data/test_ctrader_client.py` | `tests/integration/test_connection_manager.py`, `test_connection_manager_wiring.py` | Forward-test 24h+ |
| 3 cTrader Phase 2 | — | `tests/integration/test_reconnect_logic.py`, `test_connection_watchdog.py`, `test_connection_self_healing.py` | Forward-test 24h+ |
| 4 cTrader Phase 3 | — | `tests/integration/test_connection_manager.py` (auth retry section) | Forward-test 24h+ |
| 5 systemd | — | Manual `kill -9` test (Task 5.3) | Forward-test 24h+ |
| 6 live-fire | — | — | Operator screenshot of cTrader UI showing SL + TP1 |

### 7.2 Sprint-Level Test Suite

```bash
source .venv/bin/activate
./scripts/run_test_scope.sh --integration -- ctrader
./scripts/run_test_scope.sh --unit -- ctrader
./scripts/run_test_scope.sh --unit -- execution
./scripts/run_test_scope.sh --unit -- risk
```

**Expected pass:** All tests PASS. The 5 xfail tests in `test_kill_switch_auto.py` (lines 405/429/449/485/549) remain xfail — do NOT un-xfail.

### 7.3 Forward Test Stability Criterion

> Forward test runs 24h+ without operator intervention OR first live position opens with TP/SL confirmed in cTrader UI (whichever first).

**How to verify:**
1. `systemctl --user start ayumi-forward-test.service` (after card 5 merged)
2. `journalctl --user -u ayumi-forward-test.service -f` (tail logs)
3. Wait 24h OR first signal-generated position (whichever first)
4. Verify no exit-6/9/75 patterns in journal
5. Verify first position has `stop_loss` + `take_profit_1` in cTrader UI (Task 6.1)

---

## 8. Rollback Plan per Card

| Card | Branch / commit | Rollback command |
|---|---|---|
| 1 TP/SL fix | `senior-dev/tpsl-fix` | `git revert --no-commit d5e2e2f..HEAD -- src/forex_bot/adapters/ctrader/forward_test_engine.py src/forex_bot/adapters/ctrader/paper_trader.py src/forex_bot/adapters/ctrader/position_monitor.py src/forex_bot/adapters/ctrader/models.py src/forex_bot/adapters/ctrader/order_manager.py` |
| 2 cTrader Phase 1 | (verify-only likely) | `git revert --no-commit HEAD~N..HEAD -- src/forex_bot/adapters/ctrader/connection_manager.py` |
| 3 cTrader Phase 2 | (verify-only likely) | `git revert --no-commit HEAD~N..HEAD -- src/forex_bot/adapters/ctrader/forward_test_engine.py src/forex_bot/adapters/ctrader/connection_watchdog.py` |
| 4 cTrader Phase 3 | (verify-only likely) | `git revert --no-commit HEAD~N..HEAD -- src/forex_bot/adapters/ctrader/connection_manager.py src/forex_bot/adapters/ctrader/token_lifecycle.py src/forex_bot/adapters/ctrader/error_classifier.py` |
| 5 systemd | (system-level) | `systemctl --user stop ayumi-forward-test.service && systemctl --user disable ayumi-forward-test.service && rm ~/.config/systemd/user/ayumi-forward-test.service && systemctl --user daemon-reload` |
| 6 live-fire | N/A — verification only | Manual close position; no code to revert |

**Sprint-level abort:** `git revert --no-commit d5e2e2f..HEAD` (revert all sprint commits; leave `d5e2e2f` as the new HEAD).

---

## 9. Pre-Council Self-Check

**Reference:** `/root/.openclaw/workspace/docs/plans/planner-pre-council-checklist.md` (workspace template; not in Ayumi repo).

### 9.1 Card Verification

| Card | Read | Bug confirmed | Root cause identified |
|---|---|---|---|
| `a7b8e896` TP/SL | ✅ (trace doc + paper_trader + forward_test_engine reads) | ✅ 4 sites | ✅ trace doc `docs/forex/tp-sl-chain-trace.md` |
| `23d53b40` cTrader Phase 1 | ✅ architecture doc + code | ✅ (verify-only) | ✅ connection_manager.py + connection_state.py |
| `5470d08f` cTrader Phase 2 | ✅ connection_watchdog + reconnect_strategy + forward_test_engine BQ-1335 | ✅ (verify-only) | ✅ BQ-1335 commit history |
| `ca012aae` cTrader Phase 3 | ✅ token_lifecycle + error_classifier + authenticate_with_retry | ✅ (verify-only) | ✅ connection_manager.py:50–52 |
| `9043c09c` systemd | ✅ crash history in card notes | ⏳ pending Task 5.1 | ⏳ pending Task 5.1 |
| `2c5d684a` live-fire | ✅ operator steps in card | N/A | N/A |

### 9.2 Source Code Verification

| File | Read | Bug location | Fix scope |
|---|---|---|---|
| `forward_test_engine.py` | ✅ 2431 lines (partial reads) | F1:1278, F2:1575 amend | 1.0 SP — extend Position + store TP2/TP3 |
| `paper_trader.py` | ✅ full 470 lines | F3:240, 249 (and :125, :200) | 0.5 SP — pass TP2/TP3 to order manager |
| `position_monitor.py` | ✅ full 376 lines | F5: no TP ratcheting | 1.0 SP — add `check_tp_levels()` |
| `connection_state.py` | ✅ full 216 lines | None — already implemented | 0 SP — verify only |
| `connection_manager.py` | ✅ partial (lines 1–80, 626, 785) | None — already implemented | 0 SP — verify only |
| `connection_watchdog.py` | ✅ header + dataclass | None — already implemented | 0 SP — verify only |
| `reconnect_strategy.py` | ✅ header + constants | None — already implemented | 0 SP — verify only |
| `error_classifier.py` | ✅ full file | None — already implemented | 0 SP — verify only |
| `token_lifecycle.py` | ✅ header + docstring | None — already implemented | 0 SP — verify only |
| `models.py` | ✅ partial (40–95) | Position dataclass has only `take_profit: float \| None` (singular) | 1.0 SP — extend Position with TP2/TP3 + tp_levels_fired |
| `order_manager.py` | ✅ partial (140–300) | `execute_*_order` only accepts singular `take_profit` | 1.0 SP — add TP2/TP3 params |
| `open_api_spot_feed.py` | ✅ partial (1018–1068) | F4 — proto constraint, not code bug | 0 SP — docs only |
| `tests/unit/ctrader/test_amend_sl_tp.py` | ✅ partial (1–130) | Already covers 2A (cooldown) + 2B (broker response) | 0 SP — leverage existing tests |
| `scripts/launch_blend_forward_test.py` | ✅ partial (1–80, 178–186) | None — already has `_refuse_root()` guard | 0 SP — for systemd card |
| `tests/integration/test_reconnect_logic.py` | ✅ header | None — already covers BQ-1335 | 0 SP |

### 9.3 Database / State Verification

- [x] `data/ayumi/remediation_validated.flag` present (mtime 2026-07-05 12:49 EDT)
- [x] No `~/.config/systemd/user/ayumi-forward-test.service` on disk (must be created)
- [x] `deploy/ayumi-paper-mvp.service` exists as reference template (NOT used by forward test)
- [x] `docs/research/ctrader-connection-reliability-research.md` MISSING (flagged by Tsukasa 2026-07-01)

### 9.4 Downstream Consumer Audit

- [x] `models.Position` is consumed by `position_monitor.py`, `order_manager.py`, `paper_trader.py`, `forward_test_engine.py`, `open_api_spot_feed.py`. Adding optional fields with `None` defaults → no downstream breakage.
- [x] `forward_test_engine._market_feed.amend_sl_tp` signature unchanged (still single TP).
- [x] `OrderManager.execute_live_order` / `execute_paper_order` adding optional TP2/TP3 params → no breakage.

### 9.5 Dependency Analysis

- [x] Tasks 1.1 → 1.2 → 1.3/1.4 → 1.5 sequential (data model first)
- [x] Task 1.6 (docs) can run in parallel with 1.5
- [x] Cards 2/3/4 verification independent of cards 1/5/6
- [x] Card 5 (systemd) needs card 1 merged before live-fire test (Task 5.3 may overlap)
- [x] Card 6 (live-fire) needs card 1 + card 5 both merged

### 9.6 Risk & Rollback

- [x] Risk matrix completed (11 risks, see §6)
- [x] Per-card rollback plan (§8)
- [x] Sprint-level abort: `git revert --no-commit d5e2e2f..HEAD`
- [x] Blast radius assessed: card 1 affects broker-boundary behavior; cards 2/3/4 mostly verify; card 5 affects process lifecycle only; card 6 is verification only.

### 9.7 SP Estimates

| Task | SP | Justification |
|---|---|---|
| 1.1 Position extension | 1.0 | Dataclass + tests + downstream audit |
| 1.2 OrderManager TP2/TP3 | 1.0 | 2 method signatures + tests |
| 1.3 F1+F2 amend fix | 1.0 | 2 sites + new test file |
| 1.4 F3 paper fix | 0.5 | 4 sites in 1 file |
| 1.5 TP ratchet | 1.0 | New method + idempotency + tests |
| 1.6 F4 docs | 0.25 | Doc append + inline comment |
| 2.1 verify | 0.5 | Read + report |
| 2.2 rate-limit (if needed) | 0.5 | Code only if Task 2.1 finds gap |
| 3.1 verify | 0.5 | Read + report |
| 3.2 re-subscribe (if needed) | 0.5 | Code only if Task 3.1 finds gap |
| 4.1–4.4 verify | 1.0 (combined) | Read + report |
| 5.1 root cause | 1.0 | journalctl + git log cross-ref |
| 5.2 systemd unit | 0.5 | 1 unit file + install |
| 5.3 kill -9 test | 0.5 | Manual + journal audit |
| 6.1 live-fire | 0.5 | Operator supervised |
| **Total** | **~10.75** | (matches sprint estimate 10–13 SP) |

### 9.8 Deliverables

- [x] Plan document written: `docs/plans/ayumi-reliability-sprint-2026-07-05.md`
- [x] Pre-council checklist written: this section (§9)
- [x] Builder instructions drafted: §3 (per-task files + tests + DAG)
- [x] Verification gates (§7)
- [x] Rollback plans (§8)

### 9.9 AC Scope Categorization

> Per workspace pre-council checklist (2026-07-04 sunset-matrix-fix addition)

| AC | Category | Rationale |
|---|---|---|
| All F1/F2/F3/F5 fixes merged | `code-fix-able` | Pure code change within sprint scope |
| All 3 cTrader reliability cards merged | `code-fix-able` (with caveat) | Most of the work is already in main; verification + small gap fills |
| systemd unit deployed with Restart=on-failure etc. | `code-fix-able` | 1 file + system install |
| Forward test 24h+ stable | `external-condition` | Requires running process over wall-clock time |
| TP1+SL visible in cTrader UI on real position | `external-condition` | Live-fire = operator action card 6 |
| TP2/TP3 visible in logs + position_monitor | `code-fix-able` | Card 1.5 implementation |
| No exit-6/9/75 patterns | `data-condition-dependent` | Depends on actual runtime behavior; can only verify post-deployment |
| All target tests pass | `code-fix-able` | `scripts/run_test_scope.sh --unit -- ctrader` |
| Sprint post-mortem | `external-condition` | End-of-sprint activity |

**Post-conditions expected (NOT in sprint scope):**
- Average TP2/TP3 hit rate on real signals (data-distribution dependent, can only measure after weeks of trading)
- Forward test stability across broker maintenance windows (depends on cTrader infrastructure)

---

## 10. Sprint Timeline Estimate

| Scenario | Wall-clock | Description |
|---|---|---|
| **Best case** | ~6 hours | No surprises; verification reports close cards 2/3/4 immediately; card 1 lands clean |
| **Likely** | ~10–12 hours | 1 small fix found in cards 2/3/4; one Council revision on card 1; live-fire waits for next market session |
| **Worst case** | ~18 hours | Position extension cascades to 3+ downstream tests; Council rescopes card 1; live-fire waits for next market open; root-cause analysis finds a real May–June bug that needs its own card |

**Sprint kickoff:** 2026-07-05 12:22 EDT (now)  
**Sprint end (likely):** 2026-07-06 00:00 EDT (~12 hours wall-clock)  
**Sprint end (worst case):** 2026-07-06 06:00 EDT (~18 hours wall-clock)

---

## 11. Open Questions for Craig

| # | Question | Recommendation |
|---|---|---|
| Q1 | The cTrader reliability research doc (`docs/research/ctrader-connection-reliability-research.md`) is MISSING. Cards 2/3/4 reference it explicitly. Should I (a) recreate it from architecture-dual-connection.md + inline docstrings, or (b) re-spec cards 2/3/4 to drop the research-doc dependency and just verify against the architecture doc? | **Recommend (b).** Architecture doc has full gap analysis. Recreation is doc-only, but cards 2/3/4 are verification cards and don't need the original research. Saves ~1 SP. |
| Q2 | Card 1 scope: should TP ratcheting (Task 1.5) include SL move-to-breakeven at TP1 hit, or just TP1→TP2→TP3 amendment without SL adjustment? | **Recommend ratchet only (no SL breakeven).** Trace doc recommends "Option B-lite" without SL moves. SL breakeven adds idempotency complexity and risk of widening loss during rapid oscillation. Defer to follow-up card if desired. |
| Q3 | Live-fire (card 6) needs market hours. If market is closed before sprint end, do we accept 24h forward-test stability as substitute, or hold the sprint until next market open? | **Recommend substitute.** Craig's AC explicitly says "24h OR first live TP/SL, whichever first." 24h stability on a TP/SL-fixed system is a stronger signal than a single live-fire test. |
| Q4 | The 5 xfail freeze-activation tests in `test_kill_switch_auto.py` — should they remain xfail per P5A closeout, or be un-xfailed in this sprint? | **Recommend remain xfail.** P5A scope-out is a separate decision. Un-xfailing them requires implementing the freeze activation logic (out of sprint scope) + Council review. Defer to a follow-up card. |
| Q5 | Task 5.1 (root-cause) requires `journalctl --user -u ayumi-forward-test.service` access. Since the unit was previously removed, journal logs may be missing. Is there a fallback (e.g. `~/.local/share/systemd/` journal export)? | **Recommend fallback path.** If no journal retained, use `docs/post-mortems/ctrader-openapi-connection-2026-06-11.md` (existing post-mortem) + git log cross-reference. Mark Task 5.1 as `best-effort` if no journal. |

---

## Appendix A — Builder Prompt Templates (one per task)

Each builder dispatch should include:
1. Target card ID
2. Allowed files (from §3)
3. Branch name: `senior-dev/<feature>` (Tsukasa convention)
4. SP target
5. "MERGE to main BEFORE marking card done" (per AGENTS.md autobuild rule)
6. "Run `scripts/run_test_scope.sh --unit -- ctrader` BEFORE marking complete" (per AGENTS.md Hard Rule 5)
7. "Verify claimed code exists via `grep -n <key_symbol> <target_files>`" (per AGENTS.md Rule 7)

---

## Appendix B — File Inventory Touched by Sprint

**Modified:**
- `src/forex_bot/adapters/ctrader/models.py` (Position dataclass extension)
- `src/forex_bot/adapters/ctrader/order_manager.py` (TP2/TP3 params)
- `src/forex_bot/adapters/ctrader/forward_test_engine.py` (F1+F2 fix)
- `src/forex_bot/adapters/ctrader/paper_trader.py` (F3 fix)
- `src/forex_bot/adapters/ctrader/position_monitor.py` (F5 ratchet)
- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` (inline F4 comment)
- `docs/forex/tp-sl-chain-trace.md` (F4 doc update)
- `docs/post-mortems/ayumi-may-june-crash-root-cause-2026-07-05.md` (Task 5.1)
- `docs/runbooks/ayumi-systemd-install.md` (Task 5.2)

**Created:**
- `tests/forex_bot/adapters/ctrader/test_tp_sl_submission.py` (Task 1.3)
- `tests/unit/ctrader/test_position_tp_levels.py` (Task 1.1)
- `tests/unit/execution/test_position_monitor_tp_ratchet.py` (Task 1.5)
- `docs/forex/ctrader-phase1-verification-2026-07-05.md` (Task 2.1)

**Installed (system-level):**
- `~/.config/systemd/user/ayumi-forward-test.service` (Task 5.2)

**Untouched (per constraints):**
- `src/forex_bot/strategies/*` (frozen per card a7b8e896 Do-NOT-touch list)
- `open_api_spot_feed.py:1018–1068` proto (F4 platform constraint)

---

## Appendix C — Markers

~wm~mode:detail~ ~wm~plan:ayumi-reliability-sprint~ ~sm~stage1:planning~ ~ds~plan-authored~ ~ei~sprint-goal-restored~ ~ft~tp-sl-fixes~ ~ft~ctrader-reliability-verify~ ~ft~systemd-deploy~ ~cr~operator-action-card-6-pending-market-hours~ ~dec~sprint-is-right-sized~ ~dec~cards-2-3-4-mostly-verify-not-implement~ ~dec~research-doc-missing-but-architecture-doc-suffices~

---

*End of plan. Stage 1 complete. Ready for Council review + builder dispatch.*