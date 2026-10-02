# P5A Static Order-Path Proof

Date: 2026-06-26
Branch: `senior-dev/p5a-cleanup-reconciliation`
Baseline: `183a996` on `recovery/ayumi-mvp-rebuild`

## Purpose

Document every new-order path in the cTrader adapter layer and confirm
each is guarded by `ExecutionPermissionPolicy` or is out of scope for P5A.

## Grep Methodology

```bash
grep -rn 'new_order\|send_order\|ProtoOANewOrderReq\|close_position\|cancel_order\|amend_sl_tp' \
  src/forex_bot/adapters/ctrader/ --include='*.py'
```

## Active Guarded Paths (P5A protected)

### 1. `ForwardTestEngine._execute_signal_live()` → policy gate

- **File:** `forward_test_engine.py:1051`
- **Gate:** `policy.can_send_order()` pre-flight check before dispatch
- **Behavior:** Returns `(False, reason)` if kill switch active; never calls `_market_feed.new_order()` when blocked

### 2. `OpenApiSpotFeed.new_order()` → defense-in-depth policy gate

- **File:** `open_api_spot_feed.py:775`
- **Gate:** Policy check before `ProtoOANewOrderReq` construction
- **Behavior:** Raises or returns denial if policy blocks; never sends the broker request

### 3. `OpenApiSpotFeed.send_order()` → delegates to `new_order()`

- **File:** `open_api_spot_feed.py` — `send_order()` calls `new_order()`
- **Coverage:** Inherits the `new_order()` policy gate

## Dead + Dangerous Paths

### 4. `OrderManager.place_order()` → `_api_client.send_order()`

- **File:** `order_manager.py:294`
- **Status:** **DEAD + DANGEROUS.** Zero production callers (`grep -rn 'place_order' src/forex_bot/ --include='*.py'` returns zero hits outside tests). AND if called, routes through `_api_client` (a `cTraderAPIClient` instance) that does NOT have `set_permission_policy()` called on it — see dual-instance issue below.
- **Risk:** If `place_order()` is ever called, it bypasses the defense-in-depth policy gate entirely. The `_api_client` instance's `new_order()` guard evaluates `self._permission_policy` as `None`, so the check is skipped.
- **Mitigation:** Phase 6 must either (a) wire policy into `_api_client` at construction in `_start_live_mode()`, or (b) remove `place_order()` as dead code.

### 5. `OrderGateway._create_request()` → `ProtoOANewOrderReq`

- **File:** `order_gateway.py:96`
- **Status:** `OrderGateway` class is defined but **never instantiated** anywhere in production code (`grep -rn 'OrderGateway(' src/forex_bot/ --include='*.py'` returns zero hits outside tests).
- **Risk:** None currently. If instantiated in future, it bypasses the policy.
- **Mitigation:** Phase 6: either wire `OrderGateway` through the policy or remove it.

### Dual-Instance Construction Blind Spot

In `ForwardTestEngine._start_live_mode()` (line 624-632):
- `_market_feed` = `OpenApiSpotFeed(...)` → gets `set_permission_policy(policy)` ✅
- `_api_client` = `cTraderAPIClient(...)` → does NOT get `set_permission_policy()` ❌

Since `cTraderAPIClient` inherits from `OpenApiSpotFeed`, it has the `new_order()` guard, but its `_permission_policy` is `None`, so the guard is a no-op. The live order path (`_execute_signal_live()`) uses `_market_feed` directly, so this is not exploitable today. But any future code routing through `_api_client` would be unguarded.

**Phase 6 fix:** Inject policy into `_api_client` at construction, or pass `_market_feed` as the API client instead of creating a separate instance.

## Phase 6 Scope-Outs (broker-mutating, not new-order)

Per Rei's review, these methods are broker-mutating but outside P5A's new-order scope.
They exist on BOTH the dead `OrderGateway` class AND the active `OpenApiSpotFeed` class:

| Method | OrderGateway (dead) | OpenApiSpotFeed (active) | Risk |
|--------|------|------|------|
| `close_position()` | `order_gateway.py:255` | `open_api_spot_feed.py:894` | Can close real positions without policy gate |
| `cancel_order()` | `order_gateway.py:192` | `open_api_spot_feed.py:871` | Can cancel working orders without policy gate |
| `amend_order()` | — | `open_api_spot_feed.py:877` | Can amend working orders without policy gate |
| `amend_sl_tp()` | — | `open_api_spot_feed.py:886` | Can amend SL/TP without policy gate |

**Note:** The `OpenApiSpotFeed` methods are live surface area on an instantiated class. They have zero callers today, but are reachable and ungated.

**Recommendation:** Phase 6 should extend `ExecutionPermissionPolicy` to cover all broker-mutating operations on `OpenApiSpotFeed`, not just new orders.

## Residual TOCTOU Risk

Per Rei's original review: there is a theoretical time-of-check-to-time-of-use window between `policy.can_send_order()` returning `True` and the broker request completing. This is accepted for P5A because:

1. The kill switch is the primary safety mechanism, not the policy gate
2. The policy gate is defense-in-depth, not the sole barrier
3. Closing this gap requires broker-side order confirmation hooks (Phase 6+)

## Verification Command

```bash
grep -rn 'new_order\|send_order\|ProtoOANewOrderReq\|close_position\|cancel_order\|amend_sl_tp' \
  src/forex_bot/adapters/ctrader/ --include='*.py'
```

## Conclusion

All active new-order paths are guarded. One delegated path (`OrderManager`) is covered
through current wiring but should be hardened in Phase 6. One dead path (`OrderGateway`)
is uninstantiated and poses no current risk. Broker-mutating methods beyond new orders
are explicitly scoped out to Phase 6.
