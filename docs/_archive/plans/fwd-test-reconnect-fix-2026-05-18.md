# Plan: Forward Test Reconnect & PID Fix

**Date:** 2026-05-18
**Author:** Ava (Planner Subagent)
**SP Estimate:** 3 SP
**Status:** Draft

---

## Problem Summary

Two issues in the forward test:

1. **PID file permission race** — root-launched process creates PID file that $USER systemd can't open read-write
2. **Connection cascade** — dual reconnection mechanisms leak orphaned `Client` objects, each reconnecting independently, creating 20+ TCP connections that cTrader kills, triggering more disconnects

---

## Root Cause Analysis

### Issue 1: PID File Permission

**File:** `src/forex_bot/adapters/ctrader/pid_guard.py` lines 67 and 165

`os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o644)` — the `0o644` mode only applies on *creation*. When a root process creates the file (mode `rw-r--r--`, owner root), a subsequent $USER process cannot open it `O_RDWR` because group/other only have read permission.

The primary cause is running the forward test manually as root while systemd manages it as $USER. But the PID guard should be resilient to this — a root-created stale PID file shouldn't permanently block the $USER service.

**Fix:** Change file creation mode from `0o644` to `0o666` in both `_acquire()` and `_force_clear_stale()`.

### Issue 2: Connection Cascade (CRITICAL)

The cTrader `Client` class extends Twisted's `ClientService`, which has **built-in auto-reconnect** with exponential backoff (default when `retryPolicy=None`).

`OpenApiSpotFeed` adds a **second** application-level reconnect via `_schedule_reconnect()` → `_do_reconnect()`. The cascade:

1. Server disconnects (any reason)
2. `ClientService` auto-reconnect fires on the existing Client
3. `_on_disconnected` callback fires → `_schedule_reconnect()` starts a timer
4. `_do_reconnect()` calls `self._client.stopService()` (fire-and-forget, no wait) then creates a **new** `Client` object, overwriting `self._client`
5. The **old** `Client` object is orphaned — no Python reference, but its `ClientService` event loop continues reconnecting
6. Each orphaned client opens a new TCP connection
7. cTrader demo server sees 20+ connections per account → kills excess → triggers more disconnects → cascade

**The cascade IS the primary source of disconnects.** The cTrader server is killing connections because there are too many. Once the cascade is fixed, connection stability should improve dramatically.

### Underlying Disconnect Investigation

Even without the cascade, occasional disconnects may occur due to:

1. **TCP keepalive** — Twisted's TLS client strings don't configure TCP keepalive by default. On long-lived connections through NATs/firewalls, idle periods can cause silent drops.
2. **cTrader demo server behavior** — demo servers may have shorter idle timeouts than live servers.
3. **Token expiry** — already handled by proactive refresh (80% of lifetime).

The cascade fix should be validated first. If disconnects persist at a low rate (<1/hour) with clean reconnect, that's acceptable. If disconnects remain frequent, a follow-up SP for TCP keepalive investigation.

---

## Proposed Changes

### Change 1: Remove Application-Level Reconnect from `OpenApiSpotFeed`

**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`

**Remove entirely:**
- `_schedule_reconnect()` method (lines 1077–1103)
- `_do_reconnect()` method (lines 1107–1135)
- `_reconnect_attempts` state variable
- `_reconnect_delay` state variable
- Constants `_MAX_RECONNECT_ATTEMPTS`, `_INITIAL_RECONNECT_DELAY`, `_MAX_RECONNECT_DELAY`, `_STABLE_CONNECTION_SECONDS`

**Modify `_on_disconnected` callback** to NOT schedule reconnect — Twisted's `ClientService` handles reconnection automatically:

```python
def _on_disconnected(self, client, reason):
    """Callback: TCP connection lost."""
    logger.warning("Disconnected: %s", reason)
    self._connected.clear()
    self._authed.clear()
    self._app_authed.clear()
    # No application-level reconnect — Twisted ClientService handles reconnection.
    # When it reconnects, _on_connected fires, and we re-auth + re-subscribe there.
```

**Add re-auth logic to `_on_connected`** — currently it just sets the event. After a reconnect (not initial connect), it needs to re-authenticate and re-subscribe:

```python
def _on_connected(self, client):
    """Callback: TCP connection established."""
    logger.info("Connected to %s:%d", self._host, self._port)
    self._connected_at = time.monotonic()
    self._connected.set()

    if self._running and not self._authed.is_set():
        # Reconnection — re-auth and re-subscribe
        logger.info("Reconnected — re-authenticating...")
        if self._auth():
            for symbol_id in list(self._subscribed_symbol_ids):
                self._subscribe_by_id(symbol_id)
            logger.info("Re-subscribed to %d symbols after reconnect", len(self._subscribed_symbol_ids))
```

**Note:** `_on_connected` fires on Twisted's reactor thread (via `ClientService`). The `_auth()` and `_subscribe_by_id()` methods use `reactor.callFromThread` internally where needed, but `_send_and_wait` blocks the calling thread. Since `_on_connected` runs ON the reactor thread, calling `_send_and_wait` from it would deadlock (it does `event.wait()` while the reactor needs to process the response).

**Alternative approach — defer re-auth to a worker thread:**

```python
def _on_connected(self, client):
    """Callback: TCP connection established."""
    logger.info("Connected to %s:%d", self._host, self._port)
    self._connected_at = time.monotonic()
    self._connected.set()

    if self._running and not self._authed.is_set():
        # Reconnection — spawn re-auth in a worker thread to avoid
        # blocking the reactor (which would deadlock _send_and_wait)
        t = threading.Thread(target=self._reconnect_restore, daemon=True)
        t.start()

def _reconnect_restore(self):
    """Re-auth and re-subscribe after a Twisted-managed reconnect."""
    logger.info("Reconnected — re-authenticating in worker thread...")
    if self._auth():
        for symbol_id in list(self._subscribed_symbol_ids):
            self._subscribe_by_id(symbol_id)
        # Reset reconnect tracking
        logger.info("Re-subscribed to %d symbols after reconnect", len(self._subscribed_symbol_ids))
    else:
        logger.error("Re-auth failed after reconnect — feed will be unhealthy")
```

### Change 2: Fix PID Guard File Permissions

**File:** `src/forex_bot/adapters/ctrader/pid_guard.py`

Line 67 in `_acquire()`:
```python
# BEFORE:
fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o644)

# AFTER:
fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o666)
```

Line 165 in `_force_clear_stale()`:
```python
# BEFORE:
fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)

# AFTER:
fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o666)
```

### Change 3: Kill Root Process (Manual — Not in Code)

**Action for Craig or Ava during deploy:**
```bash
# Kill the root-run duplicate process
kill 3308885

# Clean up any stale PID file
rm -f $AYUMI_ROOT/data/forward_test.pid

# Verify systemd picks it up cleanly
systemctl status ayumi-forward-test
```

---

## Files to Modify

| File | Change | Risk |
|------|--------|------|
| `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` | Remove `_schedule_reconnect`, `_do_reconnect`, state vars, constants. Add `_reconnect_restore` and modify `_on_connected` | Medium — core reconnection path |
| `src/forex_bot/adapters/ctrader/pid_guard.py` | Change `0o644` → `0o666` in two locations | Low — purely defensive |

---

## Acceptance Criteria

1. **AC1 — No connection leak:** After fix, `ss -tnp | grep <pid> | grep ctraderapi | wc -l` shows exactly 1 TCP connection (2 at most during reconnect transition)
2. **AC2 — Clean reconnect:** On disconnect, logs show exactly one reconnect cycle: `Disconnected → Connected → Re-authenticated → Re-subscribed`. No repeated `Disconnected: ConnectionDone` every 2-4 seconds.
3. **AC3 — PID guard resilient:** After a root-created PID file exists, $USER process can acquire the lock without PermissionError
4. **AC4 — Existing tests pass:** `pytest tests/ -q` — all existing tests green
5. **AC5 — Feed remains functional:** Ticks continue flowing after a reconnect; no tick gap > 30 seconds during market hours after a single reconnect
6. **AC6 — Forward test engine watchdog still works:** Engine-level `_attempt_reconnect()` (stale tick detection) continues to function — it calls `feed.stop()` + `_start_market_feed()` which creates a fresh `OpenApiSpotFeed`, unrelated to the feed-internal reconnect

---

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `_on_connected` called on reactor thread → deadlock in `_send_and_wait` | **High** if not handled | **Critical** — feed hangs | Use `threading.Thread` to spawn re-auth off the reactor thread (shown in Change 1) |
| Twisted `ClientService` retryPolicy not aggressive enough | Low | Medium — slower reconnect than current 5-120s backoff | `ClientService` default backoff is reasonable (1s → 128s cap). If needed, pass explicit `retryPolicy` to Client constructor |
| Auth state race — `_on_connected` fires but old auth session is still "valid" on server | Low | Low — cTrader handles multi-session per connection gracefully | ALREADY_LOGGED_IN handler already exists in `_on_message` |
| Forward test engine watchdog and feed-internal reconnect overlap | Medium | Medium — double reconnect | After this fix, feed-internal reconnect is removed. Only engine-level watchdog remains. They no longer conflict. |
| Removal of reconnect constants breaks something referencing them | Low | Low | Grep confirms constants are only used within `open_api_spot_feed.py` |

---

## Rollback Plan

If the fix introduces instability:
1. Revert the two file changes (`git revert` or manual revert)
2. The original `_schedule_reconnect` / `_do_reconnect` methods are restored
3. Kill the root process to eliminate the immediate cascade
4. Deploy the PID guard fix independently (low risk, no dependency on reconnect changes)

---

## What This Does NOT Fix (Follow-up)

- **TCP keepalive tuning** — if disconnects persist at low frequency after cascade fix, add TCP keepalive to the Twisted endpoint (requires a custom endpoint wrapper or `prepareConnection` callback)
- **Root process prevention** — no guard prevents manual root launch. Consider adding a user check to the launch script
- **Forward test engine watchdog** — the engine-level `_attempt_reconnect()` is separate and untouched. It works by stopping and recreating the entire `OpenApiSpotFeed`, which is correct behavior for stale-tick detection

---

## Test Coverage

**Existing tests:** The reconnect logic in `open_api_spot_feed.py` has no dedicated unit tests (reconnection is hard to unit test due to Twisted reactor + threading). The forward test engine's `_attempt_reconnect` is also untested in the suite.

**New tests needed:**
- Unit test for PID guard with `0o666` mode (create file as one user, verify another can open it) — 1 test
- Integration test for reconnect behavior would require mocking the cTrader server — out of scope for this SP, but should be noted as tech debt

**Recommended validation:** Manual verification per acceptance criteria above, plus a 1-hour soak test watching connection count via `ss -tnp`.
