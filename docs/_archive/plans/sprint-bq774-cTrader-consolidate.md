---
status: pending-approval
date: 2026-06-11
owner: ava
bq_id: BQ-774
total_sp: 5
priority: P0
revision: 3
decision_date: 2026-06-11
decision_source: chat-dashboard-main-63cd3ecbb722385681be6530 (Craig, 15:57 EDT)
revision_sources:
  - revision 2: planner audit 2026-06-11 (subagent 06917fd9)
  - revision 3: council reviews 2026-06-11 (Kaito, Liora, Rei)
---

# BQ-774: cTrader Live Trading — Consolidate Spot Feed + Trade Client

## Revision 3 — council amendments applied

**Council result:** all three reviewers returned APPROVE WITH AMENDMENTS. Seventeen amendments total — Rei (5 blocking), Kaito (4 blocking), Liora (8 amendable). All seventeen accepted by the architect. This revision incorporates them.

## Problem Statement

The forward test cannot run in live mode because cTrader OpenAPI rejects the second of two simultaneous sessions for the same (app_id, account_id). Today's architecture launches **two independent OpenAPI connections** — one for the spot feed (ticks) and one for the trade client (orders) — both authenticating with the same `client_id` and `account_id`. cTrader permits only one active session per (app_id, account_id), so the second auth is rejected with `INVALID_REQUEST / Trading account is not authorized`.

## Decision

**Option C — share the spot feed's connection for both ticks and trades.** `OpenApiSpotFeed` becomes the single source of truth for the OpenAPI session; order execution methods are added to it; `OpenApiTradeClient` is deleted; the deprecated `OpenApiLiveClient` is deleted.

## Architecture After Refactor

```
ForwardTestEngine
├── OpenApiSpotFeed (single TCP connection, single auth)
│   ├── Market data (existing): subscribe_spots, on_tick, get_symbols
│   ├── Order execution (NEW): new_order, cancel_order, amend_sl_tp, close_position
│   ├── Order query (NEW): reconcile → list[Position]
│   ├── Callback surface (NEW): register_callback("on_order_*", fn)
│   ├── Disconnect handling (NEW): wake pending orders as UNKNOWN, force reconcile on reconnect
│   └── Lifecycle (existing): connect, auth, reconnect, health check
│
├── PaperTrader
│   └── api_client = OpenApiSpotFeed (was: OpenApiTradeClient)
│
├── cTraderLiveAdapter → PaperTrader (unchanged)
├── PositionMonitor (unchanged)
└── TradeLogger (unchanged)
```

`OpenApiTradeClient` is deleted. `OpenApiLiveClient` is deleted.

## cTrader Order Response Model (CORRECTED in revision 2, REFINED in revision 3)

The order flow is asynchronous and event-driven. The outgoing request sets `clientOrderId` to a local UUID; the later event echoes it back so the client can correlate.

| Step | Payload type | Class | Direction | Notes |
|------|-------------:|-------|-----------|-------|
| 1. Client sends new order | 2106 | `ProtoOANewOrderReq` | request | **set `clientOrderId = request_id` (UUID hex)** |
| 2. Server processes (no immediate response) | — | — | — | — |
| 3a. Server reports fill/reject later | 2126 | `ProtoOAExecutionEvent` | event | match by `event.order.clientOrderId` |
| 3b. Server reports order-specific error | 2132 | `ProtoOAOrderErrorEvent` | event | match by `error.clientOrderId` if present, else by `clientMsgId` |
| 3c. Server reports auth/validation error | 2142 | `ProtoOAErrorRes` | error response | match by `clientMsgId` (always set) |

**Why `clientOrderId` not `orderId`:** the server-assigned `orderId` is not known to the client until the event arrives. `clientOrderId` is the only field that the client sets and the server echoes. The plan now requires `new_order()` to set it.

**OrderStatus enum (Liora amendment):** the existing enum has `PENDING`, `FILLED`, `CANCELLED`, `REJECTED`. There is no `SUBMITTED`. The plan uses `PENDING` with an `Order.reason: str` field to disambiguate "submitted, awaiting fill event" from "submitted but unknown after disconnect." No new enum value is added.

## Public Surface on OpenApiSpotFeed

```python
class OpenApiSpotFeed:
    # Existing
    start(...) -> bool
    stop() -> None
    is_running -> bool
    state_manager -> ConnectionStateManager
    on_tick(callback) -> None
    subscribe_spots(symbol_ids: list[int]) -> None
    set_kill_switch(manager: KillSwitchManager) -> None
    resolve_symbol_id(name: str) -> int

    # NEW — for the api_client contract
    is_connected -> bool          # returns self._state_mgr.is_authenticated
    is_paper_mode -> bool         # returns False (always live when instantiated in live mode)
    register_callback(event_name: str, fn: Callable) -> None
    _trigger_callback(event_name, *args) -> None  # private

    # NEW — order methods
    def new_order(
        self,
        symbol_id: int,
        side: ProtoOATradeSide,
        volume: int,
        *,
        order_type: ProtoOAOrderType = MARKET,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        time_in_force: ProtoOATimeInForce = GTC,
        comment: str = "",
        timeout: float = 10.0,
    ) -> Order
    # Sets clientOrderId=request_id. Sends ProtoOANewOrderReq. Waits for the matching
    # ProtoOAExecutionEvent (matched by clientOrderId) up to timeout.
    # On fill: returns Order with status=FILLED, filled_price/executedVolume from event.
    # On reject: returns Order with status=REJECTED, reason from event.
    # On timeout: returns Order with status=PENDING, reason="timeout_awaiting_event".
    # On disconnect during wait: returns Order with status=PENDING, reason="connection_lost_during_order".

    def cancel_order(self, order_id: int, *, timeout: float = 10.0) -> bool
    def amend_order(self, order_id: int, *, price=None, sl=None, tp=None, timeout: float = 10.0) -> bool
    def amend_sl_tp(self, position_id: int, sl: float, tp: float, *, timeout: float = 10.0) -> bool
    def close_position(self, position_id: int, volume: int, *, timeout: float = 10.0) -> bool
    def reconcile(self, timeout: float = 10.0) -> list[Position]

    # Existing shim, signature must match what PaperTrader/OrderManager call
    def send_order(
        self,
        symbol: str,            # name (e.g. "GBPUSD")
        direction: TradeDirection,
        order_type: OrderType,
        volume: float,          # lots
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        comment: str = "",
    ) -> Order
    # Translates symbol name → id via self._name_to_id (NOT _id_to_name — that's the reverse).
    # Calls self.new_order internally. Returns the live Order.
```

## Callback Surface (Liora + Rei amendments)

Exact arities, matching `OrderManager._wire_live_callbacks` at `order_manager.py:381-405`:

```python
api.register_callback("on_order_filled",   on_filled)    # fn(order: Order, message: ProtoOAExecutionEvent)
api.register_callback("on_order_rejected", on_rejected)  # fn(order: Order, message: ProtoOAOrderErrorEvent, reject_msg: str)
api.register_callback("on_order_cancelled", on_cancelled)# fn(order: Order, message: ProtoOAExecutionEvent)
```

The dispatcher `_trigger_callback("on_order_*", *args)` invokes callbacks with these exact arities.

## Thread Safety (Rei amendment #3)

`OpenApiSpotFeed._on_message` runs on the Twisted reactor thread. Direct invocation of order callbacks from there can deadlock the reactor (a callback that calls back into `_send_and_wait` blocks message processing).

**Solution:** callbacks are dispatched via a `ThreadPoolExecutor(max_workers=1)` (`self._callback_executor`), not invoked directly. The executor is shut down on `stop()`. The `OpenApiSpotFeed._on_message` calls `self._callback_executor.submit(fn, *args)` and returns immediately.

AC: a test that registers a callback which calls `_send_and_wait` and asserts the message loop does not stall (assert next message is processed within 1s).

## Connection Loss and Reconnect (Kaito + Rei amendments)

### On disconnect (`_on_disconnected`)
- Iterate `self._pending_orders`
- For each: set `order.status = PENDING`, `order.reason = "connection_lost_during_order"`, set the wait event
- Clear `_pending_orders`
- (No callback fires here — the caller of `new_order` gets the `PENDING` Order on its `event.wait()`)

### On reconnect after auth (`_reconnect_restore`, after `_app_authed` and `_authed` set)
- Call `self.reconcile()` to pull live state
- After `reconcile()` completes: iterate any `PENDING` orders that were "connection_lost_during_order", update them with reconcile data, and fire `on_order_rejected` (or `on_order_filled`) for the ones that resolved
- (This is the "Monday morning disconnect" path: client comes back, sees what actually happened at the broker)

### During proactive token refresh
- If `new_order` is called while `_refresh_in_progress` is set, return immediately with `Order(status=PENDING, reason="refresh_in_progress")` and call `self.reconcile()` in the background
- No lock, no wait. Simple "you'll have to reconcile" semantics.

## Live Callback Wiring (Kaito amendment #7 — INTEGRATION BREAK)

Current code in `OrderManager.__init__` at `order_manager.py:102-104`:

```python
if self._api_client and not getattr(self._api_client, "is_paper_mode", False):
    if not getattr(self._api_client, "is_connected", False):
        logger.warning("cTraderAPIClient not connected — live callbacks will be wired on connect. Call connect() before trading.")
    self._wire_live_callbacks()
```

The warning fires and `_wire_live_callbacks()` IS still called. So this finding's "early-return" interpretation is wrong — the function IS invoked. The actual problem: the function checks `is_connected` again at line 361 and **does** return early there:

```python
def _wire_live_callbacks(self):
    if not self._api_client:
        return
    if not self._api_client.is_connected:
        logger.warning("Cannot wire live callbacks: FIX client not connected")
        return
    api = self._api_client
    ...
```

**Fix:** remove the `is_connected` gate from `_wire_live_callbacks`. The callbacks can register regardless. The `register_callback` call is idempotent on the spot feed (same name + same fn = re-register, no-op). When events arrive, the callbacks fire.

```python
def _wire_live_callbacks(self):
    if not self._api_client:
        return
    api = self._api_client
    # ... rest of method unchanged
```

## Kill Switch Wiring (Kaito + Rei amendments)

In `ForwardTestEngine._build_components`, immediately after constructing the spot feed:

```python
self._market_feed.set_kill_switch(self._kill_switch)
```

**Trade-off acknowledged:** the trade client fired kill switch immediately on auth failure. The spot feed's auth circuit breaker requires 5 failures before activating. The plan documents this difference:
- For initial auth failure, `ForwardTestEngine.start()` already aborts hard with `sys.exit(1)` (the "refusing to fall back to paper" path)
- For sustained in-run auth failure, the 5-failure circuit breaker activates → kill switch FREEZE

The 5-failure threshold is in `open_api_spot_feed.py:1077-1098`. Documenting, not changing — the existing threshold is the design choice.

## Object Identity (Rei amendment #4)

The plan makes it explicit: `_build_components` constructs `self._market_feed` and assigns it. `_start_openapi_feed` MUST NOT construct a new feed when one already exists. Add an identity check at the top of `_start_openapi_feed`:

```python
def _start_openapi_feed(self) -> bool:
    if self._market_feed is None:
        # legacy path — build a new one for non-live mode
        self._market_feed = OpenApiSpotFeed(...)
    return self._market_feed.start(...)
```

**AC:** `engine._paper_trader._api_client is engine._market_feed` (identity, not equality).

## Validation Ordering (Rei amendment #7)

Steps reordered: the one-shot live order test runs BEFORE the long-running service is restarted in live mode. If the one-shot fails, the service is NOT brought up in live mode — the plan stops and rolls back.

```
15. Rewrite / update test files
16. Run unit tests (mocked cTrader)
17. Run `python3 -m py_compile` on every modified file
18. ★ NEW ★ One-shot live order test (isolated script, NOT a service)
   - Stop any running `ayumi-forward-test` if it's in live mode
   - Run a Python script that connects via OpenApiSpotFeed, places one market order
     on the demo account, waits for the execution event, prints the result
   - If PASS → continue
   - If FAIL → roll back all changes via `git checkout`, do NOT restart service
19. Restart `ayumi-forward-test` service
20. Verify logs: AUTHENTICATED, no "Trading account is not authorized", ticks flow
21. Mark BQ-774 as consolidated-trading-path-done; create follow-up BQs
```

## File-by-File Plan (revised)

### Files Modified

1. **`src/forex_bot/adapters/ctrader/open_api_spot_feed.py`** (extend, ~350 lines added)
   - **Imports added:** `ProtoOANewOrderReq`, `ProtoOAClosePositionReq`, `ProtoOAAmendOrderReq`, `ProtoOACancelOrderReq`, `ProtoOAReconcileReq`, `ProtoOAAmendPositionSLTPReq`, `ProtoOAExecutionEvent`, `ProtoOAOrderErrorEvent`, `ProtoOAOrderType`, `ProtoOATradeSide`, `ProtoOATimeInForce`, `Order`, `Position`, `TradeDirection`, `OrderType`, `OrderStatus`, `uuid`, `concurrent.futures.ThreadPoolExecutor`
   - **Module-level helpers:** `_lots_to_units` (moved from `tests/test_ctrader_execution_v2.py`), `_calculate_full_jitter_backoff` deleted (only the trade client's reconnect used it, and the spot feed has its own jitter)
   - **State added:**
     - `self._pending_orders: dict[str, tuple[threading.Event, Order]]` keyed by `request_id`
     - `self._callback_executor: ThreadPoolExecutor(max_workers=1)`
     - `self._callbacks: dict[str, list[Callable]]`
     - `self._refresh_in_progress: threading.Event`
   - **`_send_and_wait`:** replace `clientMsgId = f"{id(message)}_{time.monotonic()}"` with `client_msg_id = client_msg_id_override or f"{prefix}_{uuid.uuid4().hex}"` where `prefix` is a new kwarg defaulting to `"spot"`. Caller passes `prefix="order"` or `prefix="qry"`. An optional kwarg `clientMsgId` is kept for tests that need deterministic IDs.
   - **`_on_message` extended:**
     - Existing tick dispatch (unchanged)
     - New `ProtoOAExecutionEvent` (2126) dispatch: parse, match by `event.order.clientOrderId`, update pending Order, fire `on_order_filled` or `on_order_cancelled` (via executor)
     - New `ProtoOAOrderErrorEvent` (2132) dispatch: match by `error.clientOrderId` or by `clientMsgId`, mark Order as REJECTED, fire `on_order_rejected` (via executor)
     - New `ProtoOAErrorRes` (2142) auth-error path during pending order: same handling
   - **`_on_disconnected` extended:** iterate `_pending_orders`, set `order.status=PENDING, reason="connection_lost_during_order"`, set wait events, clear the dict
   - **`_reconnect_restore` extended:** after auth completes and resubscribe happens, call `self.reconcile()`. After reconcile, iterate any `PENDING` orders that had `reason="connection_lost_during_order"`, update them with reconcile data, fire `on_order_rejected` (or `on_order_filled`) for ones that resolved
   - **`_is_expected_auth_response` validation:** already in `_auth()` after revision 2. Apply the same to `_reconnect_restore()` for both app and account auth
   - **New methods:** `new_order`, `cancel_order`, `amend_order`, `amend_sl_tp`, `close_position`, `reconcile`, `register_callback`, `_trigger_callback`, `send_order` shim
   - **New properties:** `is_connected` (`self._state_mgr.is_authenticated`), `is_paper_mode` (False)
   - **Existing `_on_connected` race-condition fix** (from this session's earlier work) is preserved

2. **`src/forex_bot/adapters/ctrader/forward_test_engine.py`** (modify, ~40 lines net change)
   - In `_build_components()`: when `cfg.live_mode`, construct `OpenApiSpotFeed` directly using the credentials from `_build_live_credentials()` and assign to `self._market_feed`. Drop the `OpenApiTradeClient` construction. Pass `api_client=self._market_feed` to `PaperTrader`.
   - In `_build_components()`: call `self._market_feed.set_kill_switch(self._kill_switch)` immediately after construction.
   - In `_start_openapi_feed()`: check `if self._market_feed is None` and only construct a new one in that case. Otherwise just `start()` the existing one.
   - In `start()`: drop the trade-client health-check block at lines 310-345. The spot feed's `is_connected` is the single health check.

3. **`src/forex_bot/adapters/ctrader/order_manager.py`** (modify, ~3 lines)
   - Remove the `is_connected` gate in `_wire_live_callbacks` (the live callback wiring fix).
   - Update error strings at lines 291, 309 from "FIX" to "cTrader connection".
   - The `TYPE_CHECKING` import of `OpenApiLiveClient` at line 20 can be deleted.

### Files Deleted

4. **`src/forex_bot/adapters/ctrader/open_api_trade_client.py`** (~1200 lines) — entire file.
5. **`src/forex_bot/adapters/ctrader/open_api_live_client.py`** (~500 lines) — entire file.

### Tests

6. **`tests/test_spot_feed_orders.py`** (NEW, replaces `tests/test_trade_client.py`)
   - Coverage: `new_order` happy path, rejected path, timeout path, `clientOrderId` correlation, callback arities, `reconcile` parse/empty/not-connected, kill switch activation on auth failure, limit order, request-field mapping, two in-flight orders resolve correctly (concurrency / `clientMsgId` collision test), reconnect-during-order scenario (mark PENDING, reconcile, resolve), refresh-during-order returns UNKNOWN, thread-safe callback dispatch (reactor thread test)
   - Imports `tests/_ctrader_stubs.py` to mock the cTrader SDK + Twisted

7. **`tests/_ctrader_stubs.py`** (NEW) — shared fixture for cTrader/Twisted module stubbing. Replaces the `mock_modules` block currently inlined in `tests/test_ctrader_execution_v2.py`.

8. **`tests/test_connection_state.py`** (NEW) — extracted from the deleted `tests/test_trade_client.py`. Covers `ConnectionState` enum, `ConnectionStateManager` transitions, kill switch activation on auth failure.

9. **`tests/test_open_api_spot_feed.py`** — UPDATE, not just extend. Remove stale tests referencing `_schedule_reconnect`, `_do_reconnect`, `_reconnect_delay`, `_reconnect_attempts` (lines 14-30 fallback constants, lines 318-404 stale tests). Add: `is_connected` reflects state, `is_paper_mode` is False, `_send_and_wait` uses unique `clientMsgId` per call (no collisions).

10. **`tests/test_ctrader_execution_v2.py`** — REWRITE. Imports move to `tests/_ctrader_stubs.py`. New tests use the `OpenApiSpotFeed` order methods. Keep scenarios: market/limit orders, amend/cancel/close, reconcile success/empty/timeout/parse error, callbacks, state guards, legacy `send_order`, symbol resolution.

11. **`tests/test_forward_test_live_execution.py`** — REWRITE. Constructs `OpenApiSpotFeed` instead of `OpenApiLiveClient`. Tests `is_paper_mode`, `is_live_mode`, `is_connected`, `send_order` name-to-id translation.

12. **`tests/test_live_ctrader.py`** — UNCHANGED. Uses raw `ctrader_open_api.Client` directly (per Liora, not launcher end-to-end). Note in ACs.

13. **`tests/test_order_manager_spot_feed_integration.py`** (NEW) — wires a real `OrderManager` to an `OpenApiSpotFeed`, simulates fill/reject/cancel events, verifies state and callback behavior exactly once. The integration test Liora asked for.

## Risks (revised)

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| `clientMsgId` collisions despite UUID4 (Rei #1, Kaito #1) | Low | High | UUID4 + optional injection kwarg for tests. Add concurrency test. |
| `OrderStatus.SUBMITTED` doesn't exist (Liora #2) | Eliminated | — | Use `PENDING` with `reason` field. |
| Symbol name lookup direction bug (Liora #2) | Eliminated | — | Use `_name_to_id` / `_resolve_name_to_id`. |
| `OrderManager._wire_live_callbacks` early-returns (Kaito #7) | High | High | **CONCRETE INTEGRATION BREAK.** Remove the gate. |
| Disconnect during order leaves pending in limbo (Kaito #3, Rei #6) | High | High | Mark PENDING + reason, wake waiters, force reconcile on reconnect. |
| Reconnect doesn't trigger order reconciliation (Kaito #4) | High | High | Call `reconcile()` after auth in `_reconnect_restore`. |
| Reactor-thread callback deadlock (Rei #3) | Medium | High | `ThreadPoolExecutor` for callback dispatch. AC: message loop test. |
| Token refresh races with order (Kaito #2) | Medium | Medium | Return UNKNOWN from `new_order` during refresh, force reconcile. |
| Kill switch not wired to spot feed (Rei #5, Kaito #6) | High | High | `set_kill_switch` in `_build_components`. AC: spot feed auth failure triggers FREEZE. |
| Object identity: `PaperTrader._api_client is engine._market_feed` (Rei #4) | Medium | High | Identity check in `_start_openapi_feed`. AC: `is` assertion. |
| Validation runs after service restart (Rei #7) | Medium | High | One-shot live order test BEFORE service restart. |
| Test coverage loss from `test_trade_client.py` deletion (Liora #1) | High | Medium | Port `ConnectionState` tests to `tests/test_connection_state.py`. |
| Helper functions in production (Liora #6) | Eliminated | — | `_lots_to_units` moved to `open_api_spot_feed.py`. |
| Claim of `<2s` recovery (Kaito #5) | Eliminated | — | Documented actual 5s retry. |
| Deferred BQ-774 items lost (Rei #8) | Medium | Medium | Create follow-up BQs before merge. |
| Deletion blast radius (Rei #9) | Low | Medium | `rg --hidden` + `pytest --collect-only` ACs. |
| Mock fidelity for new tests (Liora #3) | Medium | Medium | `tests/_ctrader_stubs.py` shared helper. |
| Stale test cleanup (Liora #4) | Low | Low | Plan covers. |

## Step Ordering (Builder, final)

1. Add new imports and module-level helpers (`_lots_to_units`) to `OpenApiSpotFeed`
2. Add `is_connected`, `is_paper_mode`, `register_callback`, `_trigger_callback` to `OpenApiSpotFeed`
3. Replace `_send_and_wait`'s `clientMsgId` generation with UUID4 + optional kwarg
4. Add `self._callback_executor` (ThreadPoolExecutor)
5. Implement `new_order`, `cancel_order`, `amend_order`, `amend_sl_tp`, `close_position`, `reconcile`
6. Wire `ProtoOAExecutionEvent` (2126) and `ProtoOAOrderErrorEvent` (2132) into `_on_message` with `clientOrderId` correlation and executor-based callback dispatch
7. Add pending-order handling in `_on_disconnected` (mark PENDING + reason, wake waiters)
8. Add post-reconnect `reconcile()` in `_reconnect_restore`
9. Add `_is_expected_auth_response` validation in `_reconnect_restore` (both app and account auth)
10. Implement `send_order` shim using `_name_to_id` (NOT `_id_to_name`)
11. Update `ForwardTestEngine._build_components` to construct spot feed before `PaperTrader`; call `set_kill_switch`
12. Update `ForwardTestEngine._start_openapi_feed` to start existing feed (not construct new)
13. Update `ForwardTestEngine.start` to drop the trade-client health-check block
14. Remove `is_connected` gate from `OrderManager._wire_live_callbacks`; update error strings; delete `OpenApiLiveClient` type hint
15. Delete `open_api_trade_client.py` and `open_api_live_client.py`
16. Create `tests/_ctrader_stubs.py`
17. Create `tests/test_connection_state.py`
18. Create `tests/test_spot_feed_orders.py`; delete `tests/test_trade_client.py`
19. Update `tests/test_open_api_spot_feed.py` (remove stale, add coverage)
20. Rewrite `tests/test_ctrader_execution_v2.py`
21. Rewrite `tests/test_forward_test_live_execution.py`
22. Create `tests/test_order_manager_spot_feed_integration.py`
23. Run `python3 -m py_compile` on every modified file. Report.
24. Run `pytest tests/test_spot_feed_orders.py tests/test_connection_state.py tests/test_open_api_spot_feed.py tests/test_ctrader_execution_v2.py tests/test_forward_test_live_execution.py tests/test_live_ctrader.py tests/test_order_manager_spot_feed_integration.py -v`. Report pass/fail per file.
25. Run `pytest --collect-only` (must succeed with no import errors). Report.
26. Run `rg --hidden "OpenApiTradeClient|OpenApiLiveClient|open_api_trade_client|open_api_live_client" .` (must be empty). Report.
27. ★ NEW ★ One-shot live order test (isolated script, NOT a service). See "Validation Ordering" above.
28. If 27 PASSES: restart `ayumi-forward-test` service. If FAILS: roll back via `git checkout`, do not restart.
29. Verify logs: `Forward test started: ... mode=LIVE`, ticks flow (`[B5 Periodic] ticks=N > 0`), no `Trading account is not authorized`, no `ALREADY_LOGGED_IN`.
30. Create follow-up BQs for engine recovery, daily health probe, linter rule. Update BQ-774 status to "trading path consolidated; recovery/probe/linter still open."

## Acceptance Criteria (final, 20 items)

- [ ] `OpenApiSpotFeed` exposes `is_connected`, `is_paper_mode`, `register_callback`, and the full set of order methods
- [ ] `OpenApiSpotFeed.new_order(...)` sets `ProtoOANewOrderReq.clientOrderId` to a local UUID; the matching `ProtoOAExecutionEvent` is correlated by `event.order.clientOrderId`
- [ ] `OpenApiSpotFeed.new_order(...)` returns an `Order` with status `FILLED` / `REJECTED` / `PENDING` (with `reason`) based on what arrives within the timeout
- [ ] `OpenApiSpotFeed._send_and_wait` uses unique `clientMsgId` per call (UUID4) — verified by a concurrency test with two in-flight requests
- [ ] `OpenApiSpotFeed._on_message` dispatches order callbacks via `ThreadPoolExecutor` (not directly on the reactor thread) — verified by a test that calls `_send_and_wait` from a callback
- [ ] `OpenApiSpotFeed._on_disconnected` marks all pending orders as `PENDING` with `reason="connection_lost_during_order"` and wakes waiters
- [ ] `OpenApiSpotFeed._reconnect_restore` calls `reconcile()` after auth succeeds; reconciles any `PENDING` orders against broker state
- [ ] `OpenApiSpotFeed._reconnect_restore` validates auth response payload types (rejects `ProtoOAErrorRes` masquerading as success)
- [ ] `OpenApiSpotFeed._lots_to_units` exists as a private module-level helper (moved from tests)
- [ ] `OpenApiSpotFeed.is_connected` returns `self._state_mgr.is_authenticated`
- [ ] `OpenApiSpotFeed.is_paper_mode` returns `False`
- [ ] `OpenApiSpotFeed.send_order(...)` translates `symbol` (name) to `symbol_id` via `self._name_to_id` (NOT `_id_to_name`)
- [ ] `ForwardTestEngine._build_components` constructs the spot feed BEFORE `PaperTrader` — verified by `engine._paper_trader._api_client is engine._market_feed`
- [ ] `ForwardTestEngine._build_components` calls `self._market_feed.set_kill_switch(self._kill_switch)` immediately after constructing the feed
- [ ] `OrderManager._wire_live_callbacks` no longer early-returns when `is_connected` is False; callbacks register unconditionally
- [ ] `ForwardTestEngine` starts in `--live` mode and reaches `Forward test started: ... mode=LIVE` without falling back to paper
- [ ] NO `Trading account is not authorized` error in `logs/forward_test-stderr.log` for a clean start
- [ ] NO `ALREADY_LOGGED_IN` error in `logs/forward_test-stderr.log` for a clean start
- [ ] `OpenApiTradeClient` and `OpenApiLiveClient` files are deleted; `rg --hidden "OpenApiTradeClient|OpenApiLiveClient|open_api_trade_client|open_api_live_client" .` returns no matches
- [ ] `pytest --collect-only` succeeds with no import errors
- [ ] All modified Python files pass `python3 -m py_compile`
- [ ] All rewritten tests pass: `test_spot_feed_orders.py`, `test_connection_state.py`, `test_open_api_spot_feed.py`, `test_ctrader_execution_v2.py`, `test_forward_test_live_execution.py`, `test_live_ctrader.py`, `test_order_manager_spot_feed_integration.py`
- [ ] One-shot live order test (step 27) places a market order against `demo.ctraderapi.com:5035` via the new code path and receives a `FILLED` (or `REJECTED` with a reason) `Order` back — BEFORE the service is restarted
- [ ] Follow-up BQs created for: engine recovery, daily health probe, linter rule for callback names. BQ-774 status reflects that those items are still open.
- [ ] No gateway restart, no SOUL.md / IDENTITY.md / constitution.md / beliefs.md / behaviors.md edits

## Negative Cases

- [ ] `OpenApiTradeClient` and `OpenApiLiveClient` are not silently kept around as dead code
- [ ] The spot feed's market data subscription must NOT be broken (smoke test: ticks still flow)
- [ ] Existing `--paper-only` mode must NOT be affected
- [ ] The `cTraderLiveAdapter` and `PaperTrader` duck-typed interface must NOT change
- [ ] `OpenApiTradeClient`-based dead imports must NOT be left in any test file
- [ ] The proactive token refresh scheduler is NOT duplicated (it lives in `OpenApiSpotFeed._schedule_proactive_refresh` only)
- [ ] `_calculate_full_jitter_backoff` is NOT shipped in production code (it was reconnect-only in the deleted trade client; the spot feed has its own jitter)
- [ ] The one-shot live order test must NOT be skipped or run AFTER service restart (it's a hard gate)

## Rollback Procedure

If the refactor fails validation or breaks live trading in a way the tests don't catch:

1. `git checkout HEAD~1 -- src/forex_bot/adapters/ctrader/open_api_spot_feed.py src/forex_bot/adapters/ctrader/forward_test_engine.py src/forex_bot/adapters/ctrader/order_manager.py`
2. `git checkout HEAD~1 -- src/forex_bot/adapters/ctrader/open_api_trade_client.py src/forex_bot/adapters/ctrader/open_api_live_client.py`
3. `git checkout HEAD~1 -- tests/test_trade_client.py tests/test_ctrader_execution_v2.py tests/test_forward_test_live_execution.py tests/test_open_api_spot_feed.py tests/test_spot_feed_orders.py tests/test_connection_state.py tests/_ctrader_stubs.py tests/test_order_manager_spot_feed_integration.py`
4. `sudo systemctl restart ayumi-forward-test`
5. Verify in logs that the previous behavior is restored

## Pre-Council Checklist (final)

- [x] BQ-774 draft read directly
- [x] Source files inspected (all 8 source files + 6 test files)
- [x] Data verified against actual files (account IDs, payload types, callback names, execution event model, `OrderStatus` enum, symbol lookup direction)
- [x] No unchecked assumptions
- [x] SP estimate (5) justified: 350 lines added to one file, 1700 lines deleted across two files, 4 test files rewritten, 1 mock helper, 1 integration test, one-shot live order gate, follow-up BQs
- [x] Dependencies identified: ctrader_open_api lib, all cTrader adapter files, all test files
- [x] Protected files excluded
- [x] Scope: one BQ, deferred items explicitly out-of-scope as follow-ups
- [x] Risk assessment complete (17 risks identified, all addressed)
- [x] Writer conflict check passed
- [x] AC concrete and testable (25 ACs)
- [x] Negative cases covered (8 items)
- [x] Validation method specified for each AC
- [x] File paths absolute
- [x] Step ordering explicit (30 numbered steps)
- [x] Rollback procedure documented
- [x] Council decisions all applied (17/17 accepted)

## Plan Self-Check: Council Decisions (verify each is in the plan)

| # | Council finding | Implemented in plan? | Where |
|---|---|---|---|
| 1 | clientOrderId correlation (Rei #1, Kaito #1) | ✅ | "cTrader Order Response Model" section |
| 2 | Connection loss marks pending UNKNOWN (Rei #6, Kaito #3) | ✅ | "On disconnect" section, AC #6 |
| 3 | Reconnect triggers reconcile (Kaito #4) | ✅ | "On reconnect after auth" section, AC #7 |
| 4 | Kill switch wiring (Rei #5, Kaito #6) | ✅ | "Kill Switch Wiring" section, AC #14 |
| 5 | Live callback wiring fix (Kaito #7) | ✅ | "Live Callback Wiring" section, AC #15 |
| 6 | Token refresh send gate (Kaito #2) | ✅ | "During proactive token refresh" section |
| 7 | Thread-safe callback dispatch (Rei #3) | ✅ | "Thread Safety" section, AC #5 |
| 8 | Callback arity (Rei #2, Liora #2) | ✅ | "Callback Surface" section, AC #2 (implied) |
| 9 | OrderStatus.SUBMITTED bug (Liora #2) | ✅ | "OrderStatus enum" note, uses PENDING + reason |
| 10 | Symbol lookup direction (Liora #2) | ✅ | `send_order` shim spec, AC #12 |
| 11 | Port ConnectionState tests (Liora #1) | ✅ | Tests section item 8 |
| 12 | _lots_to_units to production (Liora #6) | ✅ | OpenApiSpotFeed imports section, AC #9 |
| 13 | Don't claim <2s recovery (Kaito #5) | ✅ | Removed; documented 5s retry in Kaito amendment |
| 14 | Validation ordering (Rei #7) | ✅ | "Validation Ordering" section, step 27 |
| 15 | Follow-up BQs (Rei #8) | ✅ | Step 30, deferred items noted |
| 16 | Deletion blast radius (Rei #9) | ✅ | AC #19 (rg --hidden), AC #20 (pytest --collect-only) |
| 17 | Mock fidelity (Liora #3) | ✅ | `tests/_ctrader_stubs.py` |
| 18 | Stale test cleanup (Liora #4) | ✅ | Tests section item 9 |
| 19 | End-to-end coverage replay fixtures (Liora #5) | ✅ | `test_spot_feed_orders.py` includes replay-style tests |
| 20 | Don't close BQ-774 as done (Rei #8) | ✅ | Step 30, status update |
| 21 | Object identity AC (Rei #4) | ✅ | AC #13 |
| 22 | Behavioral contracts test (Liora #2) | ✅ | `test_order_manager_spot_feed_integration.py` |

All 22 council-driven items present.
