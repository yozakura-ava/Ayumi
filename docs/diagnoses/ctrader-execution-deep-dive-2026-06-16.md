# cTrader Order Execution Deep Dive — 2026-06-16

## 1. Executive Summary

Three live orders were sent to the cTrader demo account during the forward test (PID 1269497). **All three orders timed out locally before the cTrader server response arrived, then the server's error response was dropped because the pending-order tracking maps had already been cleaned up.** The root cause is a compound failure: (a) a 10-second timeout that is too short for the demo server's response latency, (b) a protobuf field mismatch where `ProtoOAOrderErrorEvent` has no `clientOrderId` field (it uses `orderId`), making the fallback `clientMsgId` lookup the only viable match path, and (c) the `clientMsgId` lookup also fails because the timeout already purged the entry. The paper trader reports `$10,000.00` balance because it never reads the real cTrader account balance — its balance is a local variable updated only by local position P&L. The cTrader balance of `$9,999.27` is a `$0.73` discrepancy likely from a previous test run. **Risk of open positions is LOW** — all three responses were `ProtoOAOrderErrorEvent` (2132), which means the orders were rejected/errored, not filled.

---

## 2. Order Execution Path

### 2.1 Signal routing (blend mode)

**File:** `scripts/launch_blend_forward_test.py`

The `BlendForwardTestEngine` (subclass of `ForwardTestEngine`) overrides `_evaluate_strategies()`. When a strategy generates a signal:

1. `_evaluate_strategies()` (`launch_blend_forward_test.py:131`) — iterates strategies, calls `adapter.evaluate_and_trade()`
2. If a signal is produced → `_route_signal(signal, strategy_name)` (`launch_blend_forward_test.py:178`)
3. `_route_signal()` passes through:
   - **Correlation Gate** → checks for duplicate (symbol, direction)
   - **Blend Runner** → `on_signal()` → sizes the order, returns `order.lots`
   - **PaperTrader** → `process_signal(exec_signal, spread=...)` ← *this is where live orders are sent*

### 2.2 Live order execution chain

**File:** `src/forex_bot/adapters/ctrader/paper_trader.py:218`

```python
def _execute_order(self, signal, volume, spread, bid, ask):
    if self.is_live_mode:                              # ← TRUE (api_client = OpenApiSpotFeed, is_paper_mode=False)
        return self._order_manager.execute_live_order(...)
    return self._order_manager.execute_paper_order(...)
```

Chain: `PaperTrader.process_signal()` → `_execute_order()` → `OrderManager.execute_live_order()` → `OpenApiSpotFeed.send_order()` → `OpenApiSpotFeed.new_order()`

### 2.3 The `new_order()` method (where orders are sent and awaited)

**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py:676`

```python
def new_order(self, symbol_id, side, volume, ..., timeout=_ORDER_TIMEOUT_SEC) -> Order:
    request_id = uuid.uuid4().hex
    # ... build Order object ...
    
    req = ProtoOANewOrderReq()
    req.clientOrderId = request_id          # ← Set on outgoing request
    
    event = threading.Event()
    client_msg_id = f"order_{uuid.uuid4().hex}"
    self._pending_orders[request_id] = (event, order)          # ← Track by request_id
    self._pending_client_msg_ids[client_msg_id] = request_id   # ← Track by clientMsgId
    
    def do_send():
        d = client.send(req, clientMsgId=client_msg_id, responseTimeoutInSeconds=timeout)
        d.addErrback(lambda f: logger.debug("Order send errback: %s", f))  # ← BUG: no event.set()
    reactor.callFromThread(do_send)
    
    if not event.wait(timeout=timeout):     # ← Blocks for 10 seconds
        # TIMEOUT: clean up pending maps
        self._pending_orders.pop(request_id, None)
        self._pending_client_msg_ids.pop(client_msg_id, None)
        order.status = OrderStatus.PENDING
        order.comment = "timeout_awaiting_event"
    return order
```

`_ORDER_TIMEOUT_SEC = 10.0` (line 53)

### 2.4 Execution event handling

**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py:799`

The `_on_message()` callback routes incoming messages by payload type:

| Payload Type | Handler | Description |
|---|---|---|
| 2126, 2151 | `_handle_execution_event()` | Order filled/cancelled/rejected |
| 2132 | `_handle_order_error_event()` | Order error event |
| 2142 | `_handle_pending_order_error()` (via `_handle_error`) | General error response |

### 2.5 The `_handle_pending_order_error()` method (where matching happens)

**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py:849`

```python
def _handle_pending_order_error(self, message, envelope) -> bool:
    client_order_id = getattr(message, "clientOrderId", "")    # ← BUG: field doesn't exist on 2132
    client_msg_id = getattr(envelope, "clientMsgId", "")
    
    logger.info("[ORDER_ERROR] clientOrderId=%r clientMsgId=%r pending_keys=%s",
                 client_order_id, client_msg_id, ...)
    
    if not client_order_id and client_msg_id:
        client_order_id = self._pending_client_msg_ids.get(client_msg_id, "")  # ← Fallback
    
    if not client_order_id or client_order_id not in self._pending_orders:
        logger.warning("[ORDER_ERROR] DROP — no match ...")
        return False
    # ... handle matched order ...
```

---

## 3. Root Cause: clientOrderId Mismatch

### 3.1 ProtoOAOrderErrorEvent does NOT have `clientOrderId`

Confirmed via protobuf descriptor inspection:

```
ProtoOAOrderErrorEvent (2132) fields:
  payloadType
  ctidTraderAccountId
  errorCode
  orderId           ← NOT clientOrderId
  positionId
  description
```

The code reads `getattr(message, "clientOrderId", "")` which always returns `""` because the field does not exist on this protobuf message type. The correct field is `orderId`.

**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py:852`
```python
client_order_id = getattr(message, "clientOrderId", "")  # Always "" for ProtoOAOrderErrorEvent
```

### 3.2 The `clientMsgId` fallback also fails (timeout race)

The fallback tries to look up `clientMsgId` in `_pending_client_msg_ids`:

```python
if not client_order_id and client_msg_id:
    client_order_id = self._pending_client_msg_ids.get(client_msg_id, "")
```

But `pending_keys=[]` in all three ORDER_ERROR log lines, meaning `_pending_orders` is empty. Since `_pending_orders` and `_pending_client_msg_ids` are always cleaned up together (in `new_order()` timeout and in `stop()`), `_pending_client_msg_ids` is also empty.

### 3.3 Timeline evidence (exact 10-second gap)

| Signal Accepted | ORDER_ERROR | Gap |
|---|---|---|
| 13:30:00 | 13:30:10 | **10s** (exact timeout) |
| 14:00:02 | 14:00:12 | **10s** (exact timeout) |
| 14:15:00 | 14:15:10 | **10s** (exact timeout) |

The perfectly consistent 10-second gap = `_ORDER_TIMEOUT_SEC`. The `event.wait(timeout=10)` expires, cleans up pending maps, returns `Order(status=PENDING)`, and THEN the ORDER_ERROR arrives from the server.

### 3.4 The errback doesn't set the event

**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py:728`

```python
def do_send():
    d = client.send(req, clientMsgId=client_msg_id, responseTimeoutInSeconds=timeout)
    d.addErrback(lambda f: logger.debug("Order send errback: %s", f))  # ← No event.set()!
```

Only an errback is registered — no success callback. Even if the library's deferred fires (success or error), the event is never set by the deferred chain. The event can only be set by `_handle_execution_event()` or `_handle_pending_order_error()` finding a match in `_pending_orders`.

### 3.5 The `execute_live_order` return path masks the failure

**File:** `src/forex_bot/adapters/ctrader/order_manager.py:155-180`

When `new_order()` returns with `status=OrderStatus.PENDING` (timeout):

```python
# After send_order returns:
if order.status == OrderStatus.FILLED:
    # create position (NOT reached)
elif order.status == OrderStatus.REJECTED:
    # handle rejection (NOT reached)
# else (PENDING):
    return OrderExecutionResult(
        success=True,   # ← MASKS THE FAILURE
        order=order,
        error_message="Order sent, awaiting execution report"
    )
```

`success=True` means `PaperTrader.process_signal()` counts it as a successful trade (`trades_executed++`) and logs `[PAPER] Executed: ...`. The "Trade executed" log is misleading — the order was never confirmed filled.

---

## 4. Root Cause: Balance Discrepancy

### 4.1 PaperTrader balance is local-only

**File:** `src/forex_bot/adapters/ctrader/paper_trader.py:115`

```python
self._current_balance = starting_balance   # = $10,000.00
```

The balance is only updated by:
- `process_signal()` → `_current_balance += position.unrealized_pnl` (but `position` is `None` for PENDING orders)
- `update_market_prices()` → recalculates from `starting_balance + realized_pnl + unrealized_pnl`
- `close_position()` → `_current_balance += position.closed_pnl`

**The PaperTrader never queries the cTrader account balance.** There is no code path that reads the actual broker balance. The `$10,000.00` is just the initial starting value.

### 4.2 cTrader balance: $9,999.27

The `$0.73` discrepancy is NOT from the current forward test's 3 orders (which were all rejected with ORDER_ERROR). Likely causes:

1. **Previous test run** — an earlier forward test may have placed and closed a trade, incurring a small spread cost
2. **Demo account setup** — some demo accounts start with a slightly reduced balance to account for initial spread/commission

Without access to the cTrader account history (requires the Open API `ProtoOACashRateHistoryReq` or the cTrader desktop app), we cannot determine the exact source. The `$0.73` is small enough to be spread cost on a single micro-lot trade from a previous session.

### 4.3 No balance sync mechanism

The `OpenApiSpotFeed` class has no method to fetch the account balance. The cTrader Open API provides `ProtoOATraderReq` (payload 2148) for this purpose, but it is not implemented anywhere in the codebase.

---

## 5. Open Positions Risk Assessment

### Risk: LOW

**Evidence:**

1. All three server responses are `ProtoOAOrderErrorEvent` (payload 2132) — these are ERROR events, not FILL events. If the orders had been filled, we would see `ProtoOAExecutionEvent` (payload 2126) instead.

2. Zero `[EXEC_EVENT]` log lines in the entire log file:
   ```
   grep -c "EXEC_EVENT" logs/forward_test-stderr.log → 0
   grep -c "ORDER_ERROR" logs/forward_test-stderr.log → 6 (3 INFO + 3 WARNING)
   ```

3. The ORDER_ERROR events have `clientMsgId` values matching our outgoing orders, confirming they are responses to OUR orders (not unrelated events).

4. Unfortunately, the specific `errorCode` and `description` from the ORDER_ERROR events are **not logged** — the handler drops the event before reading those fields. This is a diagnostic gap.

### Recommendation

Until confirmed via cTrader desktop or reconcile API, **assume positions COULD be open**. Run:

```python
# Via the running spot feed or a new connection:
positions = spot_feed.reconcile()
for p in positions:
    print(f"  {p.position_id} {p.symbol} {p.direction} vol={p.volume}")
```

---

## 6. Recommended Fixes (Ranked by Priority)

### P0 — Fix the ORDER_ERROR handler to log error details (diagnostic)

The handler currently drops ORDER_ERROR events without logging `errorCode` or `description`. This makes it impossible to know WHY cTrader rejected the orders.

**File:** `open_api_spot_feed.py:849-865`

```python
# BEFORE:
if not client_order_id or client_order_id not in self._pending_orders:
    logger.warning("[ORDER_ERROR] DROP — no match for clientOrderId=%r clientMsgId=%r",
                   client_order_id, client_msg_id)
    return False

# AFTER:
error_code = getattr(message, "errorCode", "UNKNOWN")
description = getattr(message, "description", "")
if not client_order_id or client_order_id not in self._pending_orders:
    logger.warning("[ORDER_ERROR] DROP — no match for clientOrderId=%r clientMsgId=%r errorCode=%r description=%r",
                   client_order_id, client_msg_id, error_code, description)
    return False
```

### P1 — Fix the clientOrderId field mismatch

`ProtoOAOrderErrorEvent` has `orderId`, not `clientOrderId`. The handler should also check `orderId` as a fallback.

**File:** `open_api_spot_feed.py:852`

```python
# BEFORE:
client_order_id = getattr(message, "clientOrderId", "")

# AFTER:
client_order_id = getattr(message, "clientOrderId", "")
if not client_order_id:
    # ProtoOAOrderErrorEvent uses orderId, not clientOrderId
    broker_order_id = getattr(message, "orderId", "")
    # orderId is a broker-assigned ID, not our clientOrderId — can't match directly
    # but log it for diagnostics
    logger.debug("[ORDER_ERROR] orderId=%r (broker-assigned)", broker_order_id)
```

Note: `orderId` in the error event is the broker-assigned order ID (if the order was accepted before erroring). It cannot be used to match against our `_pending_orders` which is keyed by `clientOrderId`. The `clientMsgId` envelope lookup is the only viable match path. So fixing this is primarily about diagnostics.

### P2 — Fix the timeout race condition

Increase the order timeout AND register a proper deferred callback.

**File:** `open_api_spot_feed.py:725-731`

```python
# BEFORE:
def do_send():
    d = client.send(req, clientMsgId=client_msg_id, responseTimeoutInSeconds=timeout)
    d.addErrback(lambda f: logger.debug("Order send errback: %s", f))

# AFTER:
def do_send():
    d = client.send(req, clientMsgId=client_msg_id, responseTimeoutInSeconds=timeout)
    def on_success(result):
        # The library matched a response — _on_message will handle it
        # But set a shorter fallback timer in case _on_message doesn't fire
        pass  # Response handling is done in _on_message
    def on_error(failure):
        logger.warning("Order send deferred error: %s", failure)
        # Don't set event here — let _on_message handle it if it arrives
    d.addCallbacks(on_success, on_error)
```

Also increase the timeout:

```python
# BEFORE:
_ORDER_TIMEOUT_SEC = 10.0

# AFTER:
_ORDER_TIMEOUT_SEC = 30.0  # cTrader demo server can take >10s to respond
```

### P3 — Fix the `clientMsgId` lookup to be resilient to timeout cleanup

The `clientMsgId` → `request_id` mapping should survive beyond the `event.wait()` timeout, so that late-arriving ORDER_ERROR events can still be matched and logged with full details.

**File:** `open_api_spot_feed.py:748-752`

```python
# BEFORE:
if not event.wait(timeout=timeout):
    if request_id in self._pending_orders:
        self._pending_orders.pop(request_id, None)
        self._pending_client_msg_ids.pop(client_msg_id, None)

# AFTER:
if not event.wait(timeout=timeout):
    # Remove from _pending_orders (blocking slot) but KEEP in _pending_client_msg_ids
    # for an additional 60s grace period so late error events can be matched/logged
    self._pending_orders.pop(request_id, None)
    # Schedule cleanup of client_msg_id after grace period
    def _late_cleanup():
        self._pending_client_msg_ids.pop(client_msg_id, None)
    timer = threading.Timer(60.0, _late_cleanup)
    timer.daemon = True
    timer.start()
```

### P4 — Fix `execute_live_order` to not mask timeouts as success

**File:** `order_manager.py:155-180`

```python
# BEFORE (PENDING status):
return OrderExecutionResult(
    success=True,
    order=order,
    error_message="Order sent, awaiting execution report",
)

# AFTER:
return OrderExecutionResult(
    success=False,  # ← Timeout is NOT success
    order=order,
    error_message="Order timed out awaiting execution report (10s)",
    rejection_reason="timeout_awaiting_event",
)
```

### P5 — Fix the `get_symbols` method name mismatch

**File:** `launch_blend_forward_test.py:99`

```python
# BEFORE:
symbols = client.get_symbols()

# AFTER:
symbols = client.get_all_symbols()
```

### P6 — Implement cTrader balance query

Add a method to `OpenApiSpotFeed` to fetch the real account balance using `ProtoOATraderReq` (payload 2148), and have the PaperTrader call it on startup and periodically.

---

## 7. Files to Modify

| File | Line(s) | Change |
|---|---|---|
| `adapters/ctrader/open_api_spot_feed.py` | 852 | Fix `clientOrderId` read to also log `orderId`, `errorCode`, `description` |
| `adapters/ctrader/open_api_spot_feed.py` | 855-861 | Log errorCode/description before DROP |
| `adapters/ctrader/open_api_spot_feed.py` | 728 | Add deferred callback (not just errback) |
| `adapters/ctrader/open_api_spot_feed.py` | 53 | Increase `_ORDER_TIMEOUT_SEC` from 10 to 30 |
| `adapters/ctrader/open_api_spot_feed.py` | 748-752 | Keep `_pending_client_msg_ids` entry for grace period after timeout |
| `adapters/ctrader/order_manager.py` | 175-180 | Return `success=False` for PENDING/timeout status |
| `scripts/launch_blend_forward_test.py` | 99 | Fix `client.get_symbols()` → `client.get_all_symbols()` |

---

## Appendix A: Log Evidence

### ORDER_ERROR events (all 3)

```
2026-06-16 13:30:10 | INFO  | ORDER_ERROR clientOrderId='' clientMsgId='order_9b70ed02...' pending_keys=[]
2026-06-16 13:30:10 | WARN  | ORDER_ERROR DROP — no match for clientOrderId='' clientMsgId='order_9b70ed02...'
2026-06-16 14:00:12 | INFO  | ORDER_ERROR clientOrderId='' clientMsgId='order_352bc35f...' pending_keys=[]
2026-06-16 14:00:12 | WARN  | ORDER_ERROR DROP — no match for clientOrderId='' clientMsgId='order_352bc35f...'
2026-06-16 14:15:10 | INFO  | ORDER_ERROR clientOrderId='' clientMsgId='order_827ecf40...' pending_keys=[]
2026-06-16 14:15:10 | WARN  | ORDER_ERROR DROP — no match for clientOrderId='' clientMsgId='order_827ecf40...'
```

### Trade execution (misleading "success")

```
2026-06-16 13:30:00 | Signal accepted: session_breakout_ny short GBPUSD @ 1.34093 conf=0.90 lots=0.4400
2026-06-16 13:30:10 | Trade executed: session_breakout_ny short 0.4400 lots
2026-06-16 14:00:02 | Signal accepted: srmr_plus short USDJPY @ 160.39850 conf=0.64 lots=0.3900
2026-06-16 14:00:12 | Trade executed: srmr_plus short 0.3900 lots
2026-06-16 14:15:00 | Signal accepted: session_range_mr short GBPUSD @ 1.34055 conf=0.58 lots=0.4600
2026-06-16 14:15:10 | Trade executed: session_range_mr short 0.4600 lots
```

### Balance (never changes from $10,000.00)

```
All B5 Health logs show: balance=10000.00
```

### No execution events

```
grep -c "EXEC_EVENT" logs/forward_test-stderr.log → 0
```

## Appendix B: Architecture Diagram

```
Signal Generated
       ↓
  Correlation Gate
       ↓
   Blend Runner (sizes lots)
       ↓
  PaperTrader.process_signal()
       ↓
  _execute_order()
       ↓
  is_live_mode? → YES
       ↓
  OrderManager.execute_live_order()
       ↓
  OpenApiSpotFeed.send_order()
       ↓
  OpenApiSpotFeed.new_order()
       ↓
  ┌──────────────────────────────────┐
  │ req.clientOrderId = request_id   │
  │ _pending_orders[request_id] = …  │
  │ _pending_client_msg_ids[cid] = … │
  │ reactor.callFromThread(do_send)  │
  │ event.wait(timeout=10)           │ ← BLOCKS HERE
  └──────────────────────────────────┘
       ↓ (10s timeout)
  _pending_orders.pop(request_id)    │ ← CLEANUP
  _pending_client_msg_ids.pop(cid)   │ ← CLEANUP
  return Order(status=PENDING)       │
       ↓                              │
  OrderManager: success=True (!)     │ ← MASKS FAILURE
  PaperTrader: trades_executed++     │ ← WRONG
  "Trade executed" logged            │
                                     │
     ─── Meanwhile (reactor) ──────  │
  cTrader responds: 2132 ORDER_ERROR │
  _on_message → _handle_order_error  │
  _pending_orders is EMPTY ──────────┘
  → DROP (no match)
```

---

*Generated: 2026-06-16 by cTrader execution deep dive subagent*
