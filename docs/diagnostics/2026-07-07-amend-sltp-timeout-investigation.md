# Amend SL/TP Timeout Investigation — cTrader Demo Forward Test

**Date:** 2026-07-07 (log evidence: 2026-07-08 00:00–02:09 EDT)
**Author:** Ava (subagent, depth 1/1)
**Workspace:** $AYUMI_ROOT
**Repo state:** main, 7 commits ahead of origin/main, working tree clean

---

## 1. Current behavior

### 1.1 Code path for amend

The `amend_sl_tp()` method lives in:

```text
src/forex_bot/adapters/ctrader/open_api_spot_feed.py:1033
```

Key constants and surrounding code (lines 107–216):

```python
_ORDER_TIMEOUT_SEC = 20.0
_AMEND_TIMEOUT_SEC = 30.0  # SL/TP amends are not time-critical — use a longer timeout
...
# Serializes amend_sl_tp calls so multiple simultaneous fills don't
# overwhelm the cTrader connection with back-to-back requests.
self._amend_lock = threading.Lock()
```

The `amend_sl_tp()` implementation (lines 1033–1096):

```python
def amend_sl_tp(self, position_id, sl, tp, *, symbol_id=None, timeout=_AMEND_TIMEOUT_SEC) -> bool:
    ...
    # Wait-for-response amend: we need the actual broker outcome so callers
    # can detect rejection (e.g. TRADING_BAD_STOPS) and react. Serialization
    # via _amend_lock plus a 1s cooldown prevents back-to-back amend floods.
    with self._amend_lock:
        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.positionId = position_id
        if symbol_id:
            sl = self._round_price(symbol_id, sl)
            tp = self._round_price(symbol_id, tp)
        req.stopLoss = sl
        req.takeProfit = tp

        res = self._conn.send_and_wait(req, timeout=timeout, prefix="amend")
        if res is None:
            logger.warning(
                "Amend SL/TP timeout for position %s (sl=%s tp=%s): no response from broker",
                position_id, sl, tp,
            )
            return False

        payload = Protobuf.extract(res) if hasattr(res, "payloadType") else res
        error_code = getattr(payload, "errorCode", None)
        description = getattr(payload, "description", "") or ""
        if error_code:
            ...
            return False

        # 1s cooldown so the next amend doesn't immediately re-saturate the
        # connection. Held inside the lock to serialize spacing.
        time.sleep(1.0)
        return True
```

### 1.2 What the underlying connection does

`send_and_wait()` in `src/forex_bot/adapters/ctrader/connection.py:196` wraps the
Twisted client. It returns `None` when either:

* `event.wait(timeout=timeout + 5)` itself expires (event never set), or
* the deferred fires the errback (network/transport error), which logs
  `Send-and-wait failed: ...`.

There is **no** distinction in the return value between:

1. broker received request but did not respond in time,
2. broker actively rejected with an error response,
3. network/transport timeout or disconnect.

Cases 1 and 3 both appear as `res is None`. The `None` path logs the same
`Amend SL/TP timeout ... no response from broker` warning regardless.

### 1.3 Queue/retry mechanism

Inside `amend_sl_tp()` there is **no** internal retry or queue. Each call is a
single `send_and_wait()` attempt. Retry lives entirely in the caller(s):

* `ForwardTestEngine.execute_live_order()` (lines ~1355–1430 in
  `src/forex_bot/adapters/ctrader/forward_test_engine.py`) does up to 3
  attempts with linear backoff `time.sleep(0.2 * (attempt + 1))`.
* `ForwardTestEngine._register_late_fill_callbacks()` (lines ~1700–1860) does
  the same 3-attempt bounded retry for late fills.

The `_amend_lock` serializes all `amend_sl_tp` invocations, and the 1-second
sleep inside the lock adds head-of-line blocking between attempts (even from
separate callers). The three retries for a single late fill are therefore
serialized and spaced by roughly 30 s (amend timeout) + 1 s cooldown per attempt,
plus the caller's `time.sleep(0.2 * (attempt + 1))` outside the lock. A full
F2 retry burst can exceed 95 seconds.

### 1.4 Fallback behavior

Inline SL/TP on MARKET orders is the primary path (`fd219df`, 2026-07-06).
`execute_live_order()` sends SL/TP directly on `ProtoOANewOrderReq` and only
falls back to post-fill amend when the signal has no SL/TP or when the order
was sent without them. The late-fill callback path always amends after the
fact.

---

## 2. Failure pattern analysis

### 2.1 Counts and distribution

| Log file | Amend timeout events |
|---|---|
| `logs/forward_test.log.2026-07-06` | 15 |
| `logs/forward_test.log.2026-07-07` | 3 |
| `logs/forward_test.log` (2026-07-08, 00:00–02:37) | 26 |

The current live log (`logs/forward_test.log`) shows **9 late-fill sequences**
(i.e., 9 distinct positions), each producing exactly **3 amend timeouts**
(corresponding to the 3 caller-level retries). The 26 warnings are therefore not
26 independent failures — they are 9 positions × 3 timed-out retries.

### 2.2 Temporal clustering

All 9 late-fill amend failures in the current log occurred at **15-minute
intervals** on the hour boundary plus a few seconds:

```text
2026-07-08 00:01:01 / 00:16:01 / 00:31:01 / 00:46:01 / 01:01:02 / 01:16:03 / 01:31:01 / 01:46:02 / 02:01:01
```

These align with the Session-Range Mean Reversion strategy firing at
00:00:00, 00:15:00, 00:30:00, etc., exactly when that strategy's signal
window opens. So timeouts are **strongly clustered around the same periodic
signal window**, not randomly distributed.

### 2.3 Position ID pattern

All failing position IDs are in the same contiguous block:

```text
274048857, 274056766, 274064533, 274072936, 274080475, 274088900, 274096151, 274104217, 274112595
```

All are USDJPY shorts. The SL values are ~162.58–162.69 and TP values
~162.08–162.19. The timeouts are per-position, not per-symbol-wide.

### 2.4 Correlation with connection health

Directly before every amend timeout sequence, the connection is already in
trouble:

```text
2026-07-08 00:00:17 | ERROR    | ayumi.ctrader_connection | Send-and-wait timeout
2026-07-08 00:00:17 | WARNING  | adapters.ctrader.account_state | get_balance: no response from cTrader (timeout=5.0s)
2026-07-08 00:00:20 | WARNING  | ayumi.forward_test | Live order TIMEOUT: short USDJPY ...
2026-07-08 00:00:26 | INFO     | ayumi.openapi_spot_feed | [EXEC_EVENT] ...execType=2...   (late fill)
2026-07-08 00:01:01 | WARNING  | ayumi.openapi_spot_feed | Amend SL/TP timeout for position 274048857 ...
2026-07-08 00:01:06 | WARNING  | ayumi.ctrader_connection | No heartbeat for 39.9s — DEGRADED
2026-07-08 00:01:36 | WARNING  | ayumi.ctrader_connection | No heartbeat for 69.9s (threshold: 60s) — triggering reconnect
```

This pattern repeats for every timeout. The sequence is:

1. New order is sent at 00:00:00.
2. Order event window times out at 00:00:20 (`_ORDER_TIMEOUT_SEC = 20`).
3. Heartbeat stalls; connection goes `degraded`.
4. Late fill callback arrives at ~00:00:26.
5. Late-fill amend retry loop begins at 00:01:01.
6. Each amend attempt times out after 30 seconds (`_AMEND_TIMEOUT_SEC`).
7. The connection finally triggers reconnect at 00:01:36.

### 2.5 Inline vs amend success rate

The logs contain **zero** messages matching `SL/TP attached inline` or
`SL/TP attached to position` in the current forward-test log. The only amend
outcome logs are the `F2: late amend_sl_tp returned False after 3 attempts`
entries. This means:

* The synchronous inline path is not logging success, so we cannot compare
  success rates directly from the current log.
* However, the timeouts only appear on the **late-fill callback path** (F2),
  not on the synchronous inline MARKET-order path (F1). This suggests the
  inline path has not produced amend warnings during this window — but that
  is because no synchronous order has filled within the 20s event window; all
  fills arrived late.

So the comparison is currently **incomplete** due to low fill volume, but the
evidence points at the late-fill path being the only path that is triggering
amend attempts, and it is failing 100% of the time.

---

## 3. Root cause hypothesis

### 3.1 Primary cause: connection degradation before the amend attempt

The timeouts are **not** caused by a 30-second timeout being too aggressive in
isolation. The connection has already stopped receiving heartbeats by the time
the late-fill callback fires. The amend request is then sent into a dead or
stalled TCP session and waits the full 30 seconds. The subsequent retry
attempts hit the same dead connection.

### 3.2 Secondary cause: 1-second cooldown inside `_amend_lock`

Even if the connection recovers quickly, the `time.sleep(1.0)` at line 1094
inside the lock forces **head-of-line blocking**. The three retries for one
late fill cannot be paced by the caller's 0.2s/0.4s/0.6s backoff because the
lock serializes them and adds 1s between each. This extends the retry window
from ~0.6s to ~3.6s, increasing the chance that the late fill stays unprotected.

### 3.3 Tertiary cause: no pre-flight connection check

`amend_sl_tp()` does not check `self._conn` health or the state manager's
`is_operational` before sending. It acquires `_amend_lock`, builds the
request, and calls `send_and_wait()`. If the connection is already degraded,
30 seconds are wasted before returning `False`.

### 3.4 Why this is recent

`git log --oneline -20 src/forex_bot/adapters/ctrader/open_api_spot_feed.py`
shows commit `fd219df fix(ayumi): inline SL/TP on MARKET orders + positionId
stamping + amend retry` (2026-07-06). That commit:

1. Switched the main path to inline SL/TP on MARKET orders.
2. Added the `_amend_lock` and 1-second cooldown to the amend path.
3. Added bounded 3-attempt retry in the callers.

Before that commit, the code was sending naked MARKET orders and then doing an
immediate amend. After the commit, the immediate inline path works when fills
arrive in the 20s event window, but late fills still hit the amend path — and
now that amend path is serialized and cooled down, which amplifies the timeout
when the connection is already unhealthy.

**Confidence: medium.** We have strong temporal and connection-health
correlation, but we have not captured a packet-level trace that proves the
request was sent into a dead socket.

---

## 4. Fix recommendations

### 4.1 Low-effort fix: pre-flight connection check + shorter retry spacing

**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`  
**Function:** `amend_sl_tp()` (line 1033)

Add an early-exit health gate before acquiring `_amend_lock`. Note that
`CTraderConnection.is_connected` is a property (`src/forex_bot/adapters/ctrader/connection.py:109`),
and `OpenApiSpotFeed.is_connected` delegates to
`_state_mgr.is_authenticated` (`open_api_spot_feed.py:307`). A realistic gate
inside `amend_sl_tp()` would be:

```python
if not self._conn.is_connected or not self._state_mgr.is_authenticated:
    logger.warning(
        "Amend SL/TP skipped for position %s: connection not authenticated (state=%s)",
        position_id, self._state_mgr.state.value,
    )
    return False
```

Also reduce or remove the 1-second cooldown inside the lock (line 1094). The
intention was to pace back-to-back amends, but the caller already spaces retries
by 0.2–0.6s. Removing the cooldown lets the bounded retry complete quickly when
the connection is healthy.

**Rationale:** Fast-fail when the connection is dead, and speed up the retry
burst when it is healthy.

### 4.2 Medium-effort fix: classify the underlying error

**File:** `src/forex_bot/adapters/ctrader/connection.py`  
**Function:** `send_and_wait()` (line 196)

Currently `send_and_wait()` returns `None` for both timeout and errback
failures. Change it to return a sentinel object (or raise an exception) that
carries the failure type:

* `TimeoutError` — no response within `responseTimeoutInSeconds`.
* `ConnectionError` / `ConnectionDone` — Twisted errback due to disconnect.
* `BrokerError` — broker returned an error response payload.

Then `amend_sl_tp()` can log or handle each case differently:

* broker timeout → retry (broker may still process it).
* connection error → do not retry immediately; let the reconnect layer handle it.
* broker rejection → do not retry (TRADING_BAD_STOPS, etc.).

This also fixes observability: right now all three cases look identical in the
logs.

### 4.3 Thorough fix: asynchronous amend queue with reconciliation

**Files:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`,
`src/forex_bot/adapters/ctrader/forward_test_engine.py`

1. Add a small pending-amend queue per position in `OpenApiSpotFeed`.
2. On late fill, enqueue the desired `{position_id, sl, tp, symbol_id}`.
3. A worker thread (or scheduled callback) attempts the amend when the
   connection is operational, with exponential backoff and a max age.
4. On each successful connection restore (`_on_conn_connected`), drain the
   queue.
5. Add a reconciliation step: after reconnect, request open positions and
   verify that SL/TP match the desired levels; re-enqueue any mismatch.

This removes the blocking retry from the fill callback entirely, which is
important because late fills currently block inside `amend_sl_tp()` for more
than a minute across 3 attempts.

### 4.4 Suggested order of implementation

1. **Immediate:** implement 4.1 (health gate + cooldown reduction).
2. **Next:** implement 4.2 (error classification) to confirm whether timeouts
   are broker-latency or connection-loss.
3. **Later:** implement 4.3 if timeouts persist after 4.1 and 4.2.

---

## 5. Confidence assessment

**Confidence: medium.**

* **For:** timeouts are perfectly correlated with heartbeat-stale/degraded
  state; every timeout occurs during or immediately before a reconnect sequence;
  the pattern repeats across all 9 failing positions; the lock + 1s cooldown is
  visibly extending the retry window.

* **Against:** there is no packet capture proving the request leaves the box
  but never returns; the current forward test has very few fills, so we cannot
  compare inline vs amend success rates statistically; the 1-second cooldown
  may have been added for a reason not visible in this investigation.

**Evidence that would upgrade confidence to high:**

1. Add per-amend logging that records `_state_mgr.state`,
   `_conn.is_connected()`, and the Twisted deferred result type.
2. Capture a small sample of timed-out positions with Wireshark / tcpdump to
   confirm whether the TCP segment is transmitted and no reply is received.
3. Run a controlled test where the connection is intentionally degraded and
   observe whether 100% of amends time out (would confirm connection-loss as
   root cause) vs. some still succeeding (would point at broker latency).

---

## 6. Related files

* `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` (lines 1033–1096)
* `src/forex_bot/adapters/ctrader/connection.py` (lines 196–233)
* `src/forex_bot/adapters/ctrader/forward_test_engine.py` (lines 1355–1430, 1700–1860)
* `logs/forward_test.log`
* `logs/forward_test.log.2026-07-06`
* `logs/forward_test.log.2026-07-07`

