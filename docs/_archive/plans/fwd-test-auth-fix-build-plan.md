---
status: active
date: 2026-05-12
owner: ava
total_sp: 5
priority: P0
---

# Forward Test Auth Fix Build Plan

## Problem Statement

The forward test is **completely non-functional** with three interrelated issues:

1. **ALREADY_LOGGED_IN infinite loop** — `open_api_spot_feed.py:602` treats `ALREADY_LOGGED_IN` as an auth error, triggering `_refresh_token_and_reauth()`. The refresh succeeds, sends a new `ProtoOAAccountAuthReq`, but the server returns `ALREADY_LOGGED_IN` again immediately because the session is still active. No backoff, no circuit breaker, no deduplication. Log evidence: 25,864 occurrences on May 11.

2. **Duplicate instances** — Two `launch_blend_forward_test.py` processes running simultaneously (PID 168828 as root, PID 168829 as $USER) sharing the same cTrader account. This is almost certainly **causing** the `ALREADY_LOGGED_IN` errors — both instances authenticate to the same account, and the server rejects the second as already logged in.

3. **Zero signals** — Even during brief connected windows, no signals are generated. Likely causes: tick flow interrupted by auth loop spam, or the duplicate instances' event loops interfering.

## Root Cause Analysis

```
Duplicate instances (root cause)
  → Both authenticate to same cTrader account
    → Server returns ALREADY_LOGGED_IN on second auth
      → _handle_error treats it as auth failure
        → Triggers token refresh + re-auth
          → Still ALREADY_LOGGED_IN (because the other instance holds the session)
            → Infinite loop (no backoff, no dedup, no awareness of concurrent sessions)
              → Logs fill with 25K+ error cycles
                → Tick flow disrupted → zero signals
```

## Fix Order (Dependency Chain)

1. **Kill duplicate instances** → eliminates root cause of ALREADY_LOGGED_IN spam
2. **Fix ALREADY_LOGGED_IN handler** → prevents future loops even if duplicates reappear
3. **Add PID file / single-instance guard** → prevents duplicates from starting
4. **Confirm tick flow and signal generation** → validation that fixes work end-to-end

---

## Build Items

### B1: Single-Instance Guard (PID File Lock) — 1 SP

**Problem:** Nothing prevents two `launch_blend_forward_test.py` processes from running simultaneously. The root and $USER user can both launch it.

**Scope:** Add PID file locking to `launch_blend_forward_test.py` and `scripts/launch_forward_test.py`. On start, write PID to a well-known file (`data/forward_test.pid`). If file exists and process is alive, refuse to start with a clear message.

**Files:**
- `scripts/launch_blend_forward_test.py` — add PID guard at entry point
- `scripts/launch_forward_test.py` — same guard
- `src/forex_bot/adapters/ctrader/pid_guard.py` — **NEW** reusable PID guard module

**Acceptance criteria:**
- [ ] `pid_guard.py` implements `acquire_pid_lock(path) -> context manager` with stale-PID detection (check `/proc/{pid}`) and clean unlock on exit
- [ ] Both launcher scripts call `acquire_pid_lock` before initializing the engine
- [ ] If a live process holds the lock, script prints PID and exits with code 1
- [ ] If stale PID file (process dead), lock is cleaned and acquired
- [ ] Lock released on clean shutdown and SIGINT/SIGTERM handlers
- [ ] PID file path: `data/forward_test.pid` (relative to project root)

---

### B2: Fix ALREADY_LOGGED_IN Handler — 1 SP

**Problem:** `open_api_spot_feed.py:599-607` treats `ALREADY_LOGGED_IN` identically to `CH_OAUTH_TOKEN_EXPIRED`, triggering an infinite refresh loop. `ALREADY_LOGGED_IN` actually means "your session is still active" — it's not an error requiring token refresh.

**Scope:**
1. Remove `ALREADY_LOGGED_IN` from `auth_errors` set
2. Handle `ALREADY_LOGGED_IN` explicitly: log as info, set `_authed` event (session IS valid), skip refresh
3. Add a cooldown/dedup to `_handle_error` so even genuine auth errors don't rapid-fire refresh — minimum 60s between reactive refresh attempts

**Files:**
- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`

**Acceptance criteria:**
- [ ] `ALREADY_LOGGED_IN` removed from `auth_errors` set in `_handle_error`
- [ ] New handler for `ALREADY_LOGGED_IN`: logs at INFO, sets `_authed` event, returns without refresh
- [ ] `_handle_error` tracks `_last_reactive_refresh_time`; skips refresh if <60s since last attempt
- [ ] No unit test required (integration test covered by B4)
- [ ] Log message clarifies: "Session already active, no action needed"

---

### B3: Auth Error Backoff & Circuit Breaker — 1 SP

**Problem:** Even with B2 fixed, genuine auth failures (expired token, invalid credentials) will still retry indefinitely with no backoff. The `_refresh_token_and_reauth` method has no failure counter or backoff.

**Scope:**
1. Add `_auth_error_count` counter to `OpenApiSpotFeed`
2. On each reactive refresh failure, increment counter and apply exponential backoff (min 10s, max 300s)
3. After 5 consecutive failures, enter circuit-breaker state: stop refreshing, log CRITICAL, signal the engine that feed is unhealthy
4. Reset counter on successful auth
5. Add `_stable_auth_time` tracking — if auth has been stable for >30s, reset backoff

**Files:**
- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`

**Acceptance criteria:**
- [ ] `_auth_error_count` initialized to 0 in `__init__`
- [ ] `_refresh_token_and_reauth` applies backoff: `min(10 * 2^count, 300)` seconds between attempts
- [ ] After 5 consecutive failures: CRITICAL log, `_auth_circuit_open = True`, no further refresh attempts
- [ ] On successful auth (account auth response sets `_authed`): reset `_auth_error_count = 0`, `_auth_circuit_open = False`
- [ ] `_schedule_proactive_refresh` checks circuit breaker state before scheduling
- [ ] Health endpoint (`get_health` or equivalent) reflects circuit breaker state

---

### B4: Kill Orphan & Deploy — 1 SP

**Problem:** Two instances are currently running. Need to kill the orphan, deploy fixes, and restart cleanly.

**Scope:**
1. Kill PID 168829 ($USER orphan — root-owned PID 168828 should be the one that stays)
2. Actually: kill BOTH, deploy code, restart single instance
3. Verify single process via `ps aux | grep launch_blend`
4. Verify PID file created
5. Monitor logs for 10 minutes: confirm tick flow, no ALREADY_LOGGED_IN spam, auth success

**Files:**
- Operational (no code files)

**Acceptance criteria:**
- [ ] Both existing forward test processes killed
- [ ] Code from B1-B3 deployed (git pull or restart)
- [ ] Single process launched
- [ ] `data/forward_test.pid` exists with correct PID
- [ ] Logs show: successful auth, tick flow, no ALREADY_LOGGED_IN errors
- [ ] After 10 min: ticks received > 0, no auth loop in logs

---

### B5: Signal Generation Validation — 1 SP

**Problem:** Even with ticks flowing, need to confirm the full pipeline works: ticks → bars → strategy evaluation → signals → paper trades.

**Scope:**
1. Add a startup diagnostic log: on engine start, log strategy count, symbol count, bar period, min confidence
2. Verify `ForwardTestEngine._on_tick` is wired correctly to the feed's tick callbacks
3. Add a periodic health log (every 60s): ticks received, bars built, evaluations run, signals generated
4. Monitor for 30 minutes post-deploy: confirm signals are being evaluated

**Files:**
- `src/forex_bot/adapters/ctrader/forward_test_engine.py` — startup diagnostic
- `scripts/launch_blend_forward_test.py` — verify tick callback wiring

**Acceptance criteria:**
- [ ] On startup: log strategy list, symbols, bar_period, min_confidence
- [ ] Every 60s: log tick count, bar count, evaluation count, signal count
- [ ] After 30 min observation: at least 1 evaluation logged per symbol per bar period
- [ ] If zero evaluations after 30 min: diagnostic identifies which step is failing (ticks? bars? strategy?)

---

## File Tree

```
src/forex_bot/adapters/ctrader/
├── open_api_spot_feed.py        # MODIFY: B2 (ALREADY_LOGGED_IN fix), B3 (backoff/circuit breaker)
├── pid_guard.py                  # NEW: B1 (reusable PID file lock)
└── forward_test_engine.py        # MODIFY: B5 (startup diagnostics, periodic health)

scripts/
├── launch_blend_forward_test.py  # MODIFY: B1 (PID guard), B5 (health wiring)
└── launch_forward_test.py        # MODIFY: B1 (PID guard)

data/
└── forward_test.pid              # RUNTIME: B1 (PID lock file, gitignored)
```

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Killing wrong process during B4 | Forward test goes down | Kill both, restart clean — no ambiguity |
| PID file on NFS/shared filesystem | Stale locks | Use `/proc/{pid}` check — only valid on same host |
| ALREADY_LOGGED_IN sometimes genuinely means session conflict | Mis-handled error | Log at WARNING level; if it occurs >3 times in 60s, escalate to reconnect |
| Token refresh API rate limits | Refresh blocked by cTrader | Circuit breaker stops after 5 attempts; 300s max backoff |
| Strategy never fires signals even with ticks | False negative on B5 | B5 includes diagnostic logging to identify WHERE the pipeline stalls |

---

## SP Breakdown

| Item | Description | SP | Priority |
|------|-------------|----|----------|
| B1 | Single-instance guard (PID file lock) | 1 | P0 |
| B2 | Fix ALREADY_LOGGED_IN handler | 1 | P0 |
| B3 | Auth error backoff & circuit breaker | 1 | P0 |
| B4 | Kill orphan & deploy | 1 | P0 |
| B5 | Signal generation validation | 1 | P1 |
| **Total** | | **5** | |

---

## Execution Order

```
B1 (PID guard) ─────────────────────────────────┐
                                                  │
B2 (ALREADY_LOGGED_IN fix) ─────────────────────┤  ← can be parallel with B1
                                                  │
B3 (auth backoff) ──────────────────────────────┤  ← depends on B2 (same file)
                                                  │
B4 (kill & deploy) ──── depends on B1 + B2 + B3 ─┤
                                                  │
B5 (signal validation) ── depends on B4 ──────────┘
```

B1 and B2+B3 can be built in parallel. B4 is the deployment gate. B5 is post-deploy validation.

---

## Out of Scope

- Multi-account support (forward test runs one account)
- Moving to systemd service management (separate initiative)
- Strategy tuning or signal quality analysis
- Backtest infrastructure changes
