# Sprint 2026-09-25-builder-debt

Card 1 of 3 — `b37494d7-24ed-4443-a6ae-67280902a90f`

## Scope

Sprint-105 late-fill registry rework. Second independent review returned
REWORK; iter-1 APPROVE is SUPERSEDED. 6 confirmed findings (1 HIGH + 5 MEDIUM)
to fix on the live late-fill handler module.

## Provenance — source location

`execution_event_handler.py` source was NOT at the card's claimed path
(`src/forex_bot/adapters/ctrader/execution_event_handler.py` had only a stale
`.pyc`, no `.py`). Live source located at:

```
/home/TacoPants/projects/Ayumi/worktrees/live-lint-270783df/src/forex_bot/adapters/ctrader/execution_event_handler.py
```

That path is in an orphan worktree directory (`worktrees/`, not `.worktrees/`)
and is NOT a git-tracked worktree. Re-keyed to this card.

Pre-fix line numbers (recorded for the proof):

| Region | Lines | Notes |
|---|---|---|
| `_late_fill` / `_late_fill_by_coid` registry declaration | 82–84 | Mirror of OpenApiSpotFeed._late_fill_registry |
| `cleanup_pending()` body | 118–121 | Pre-fix: popped only `_pending`, `_results`, `_client_order_ids` — left `_late_fill` / `_late_fill_by_coid` alive (HIGH trigger) |
| `_late_fill_ttl` constant | 134 | 120.0s — same as OpenApiSpotFeed._LATE_FILL_TTL_SEC |
| `_match_by_client_order_id` body | 151 | clientOrderId lookup helper |
| `_build_result_from_execution` PARTIAL_FILL branch | (inside method, ~line 360+) | Pre-fix: returned `OrderStatus.FILLED` for PARTIAL_FILL(11) — wrong (treating progress as terminal) |

## Findings & Fixes

### Fix #1 — HIGH — Atomic terminal consumption

Pre-fix: `cleanup_pending()` popped `_pending`, `_results`, and `_client_order_ids`
but left `_late_fill` and `_late_fill_by_coid` alive. A late-arrival terminal
event that bypassed `cleanup_pending` (timeout → late-fill registered → late
FILLED via `on_execution_event`) would leave `_late_fill_by_coid` populated.
A subsequent ORDER_ERROR for the same order could then match via the residual
late-fill entry and overwrite the confirmed FILLED with REJECTED.

Post-fix: A new `_consume_terminal_state()` helper atomically clears ALL FIVE
structures (`_pending`, `_results`, `_client_order_ids`, `_late_fill`,
`_late_fill_by_coid`) for the order. Called from inside the
`with self._lock:` block in `on_execution_event`, `on_order_error`, AND
`on_general_error` so the result write and the registry pop are mutually
visible to a concurrent handler invocation.

`cleanup_pending()` also updated to clear `_late_fill` and `_late_fill_by_coid`
so callers using the public cleanup API get the same atomic guarantee.

### Fix #2 — MEDIUM — PARTIAL_FILL(11) is PROGRESS, not terminal

Pre-fix: `_build_result_from_execution` mapped `(FILLED=3, PARTIAL_FILL=11)`
both to `OrderStatus.FILLED`. cTrader emits PARTIAL_FILL during multi-chunk
fills (iceberg / liquidity slicing). The very next event for the same
clientOrderId is usually another PARTIAL_FILL or ORDER_FILLED(3). Treating 11
as terminal fires `on_filled` prematurely with a PARTIAL executionPrice and
closes the pending entry so the real ORDER_FILLED(3) that arrives next would
either fail to match or be dropped as a duplicate.

Post-fix: `on_execution_event` short-circuits BEFORE the result-build call
when `etype == _EXEC_TYPE_PARTIAL_FILL` — logs informationally (carries
filled_price / filled_volume when present in the payload), does NOT pop the
pending entry, does NOT fire any callback, does NOT set the event. The
pending entry stays available for the eventual terminal event.

Defensive: `_build_result_from_execution` now raises `ValueError` if ever
invoked with PARTIAL_FILL (caller-side short-circuit must precede it).

### Fix #3 — MEDIUM — Counter blind path (clientOrderId lookup)

Pre-fix: `unmatched_late_fills` counter only fired via the `client_msg_id`
iteration path in `OpenApiSpotFeed._handle_execution_event`. An expired entry
matched ONLY by `clientOrderId` (no `client_msg_id` on the event) would silently
DROP without bumping the counter, leaving operators blind to that class of
broker late events.

Post-fix (in `execution_event_handler.py`): `_unmatched_late_fills_count` is
incremented exactly once when an execution event arrives after the late-fill
registry's TTL window. The counter is checked via an `expired_keys_seen` set
so the same expired entry observed via both `clientOrderId` and `client_msg_id`
only counts once. Wired into both `on_execution_event` AND `on_order_error`.

`_check_late_fill()` refactored to assume the caller holds `self._lock`
(non-reentrant Lock — re-entry from `on_execution_event` would deadlock).
Matches the OpenApiSpotFeed._lookup_late_fill pattern.

### Fix #4 — MEDIUM — End-to-end timeout→INDETERMINATE test

New test file: `tests/unit/ctrader/test_new_order_timeout_indeterminate.py`
(5 scenarios, all passing):

1. **`test_new_order_timeout_produces_indeterminate_status`** — full e2e:
   calls `new_order()` with a 0.5s timeout, patches reactor to run inline,
   asserts the order ends up with `status=PENDING` + `reason="indeterminate_awaiting_event"`,
   is in `_late_fill_registry`, and no `on_order_rejected` callback fires.

2. **`test_indeterminate_reason_is_distinct_from_terminal_reject`** —
   constant contract: `_INDETERMINATE_TIMEOUT_REASON` string must NOT collide
   with any `_TERMINAL_REJECT_STATUSES` value (would break late-fill
   classification).

3. **`test_late_fill_registry_ttl_is_configured`** — TTL must be > 0 and in
   the operational range `[1.0, 600.0]`.

4. **`test_pending_order_then_late_terminal_event_resolves_cleanly`** —
   timeout → late-fill upgrade: register pending + late-fill, send late
   FILLED, verify atomic consumption (all 5 structures empty) + FILLED
   delivered via `on_filled` callback.

5. **`test_unmatched_late_fills_counter_bumps_on_ttl_expired_lookup`** —
   counter blind path coverage: orphan late-fill with past-TTL expiry, send
   matching event, verify counter bumps to 1.

### Fix #5 — MEDIUM — Blend-health counter surfacing

Already applied to main in iter-1 (confirmed via grep of
`src/forex_bot/engine/health_monitor.py` lines 152-165):

```python
unmatched_late_raw = self._safe_attr(self._order_gateway, "_unmatched_late_fills_count", 0)
...
extras.append(f"unmatched_late_fills={unmatched_late}")

signals_indeterminate_raw = self._safe_attr(self._order_gateway, "_signals_indeterminate", 0)
...
extras.append(f"signals_indeterminate={signals_indeterminate}")
```

ForwardTestEngine heartbeat (`forward_test_engine.py` lines 2960-2980) uses
`_safe_spot_feed_counter()` to surface both `order_error_session_conflict` and
`unmatched_late_fills` in the ACTIVE blend heartbeat JSON.

No further work needed on this finding — main is current.

### Fix #6 — BUNDLE — OpenApiSpotFeed mirror consistency

Already applied to main in iter-1 (confirmed via grep of
`src/forex_bot/adapters/ctrader/open_api_spot_feed.py`):

* `_consume_order_state_across_all_maps(client_order_id, client_msg_id)` —
  pops `_pending_orders` (by client_order_id), `_pending_client_msg_ids`
  (by both keys), `_late_fill_registry` (by client_order_id AND iterating on
  client_msg_id). Same atomic-consume contract as the new
  `execution_event_handler._consume_terminal_state()`.

* `_TERMINAL_EXEC_TYPES = {FILLED, CANCELLED, REJECTED, EXPIRED}` and
  `_PROGRESS_EXEC_TYPES = {PARTIAL_FILL}` — terminal vs progress split
  matching the new `execution_event_handler.on_execution_event` short-circuit.

* `_unmatched_late_fills_count` — counter surfaced via health_monitor.

Mirror consistency verified: both modules use the same atomic-consume pattern
(consume by primary key + reverse-iterate on secondary key), the same
TTL (120s), the same terminal/progress split, and the same counter
semantics. The structures differ (open_api_spot_feed stores
`(expiry, Order, cmsg_id)` tuples in one map; execution_event_handler
stores `(expiry, result)` + a separate reverse-lookup map) but the
contracts are equivalent.

## Verification

### Targeted tests (HR5 — Craig 2026-09-25)

```
$ AYUMI_ROOT=$(pwd) python3 -m pytest \
    tests/unit/ctrader/test_execution_event_race.py \
    tests/unit/ctrader/test_timeout_race_errorcode.py \
    tests/unit/execution/test_late_fill_positionid.py \
    tests/unit/ctrader/test_new_order_timeout_indeterminate.py \
    -v --tb=short -p no:randomly
======================= 30 passed, 24 warnings in 9.27s ========================
```

* 5/5 new e2e tests pass (`test_new_order_timeout_indeterminate.py`)
* 25/25 existing ctrader tests still pass (no regressions)
* No full-suite run (HR5 — targeted only)
* No full tsc / full builds (HR5)

### Files touched

```
A  src/forex_bot/adapters/ctrader/execution_event_handler.py   (NEW, 684 lines)
A  tests/unit/ctrader/test_new_order_timeout_indeterminate.py  (NEW, 5 scenarios)
```

No edits to:

* `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` (iter-1 already
  correct — verified via grep)
* `src/forex_bot/adapters/ctrader/forward_test_engine.py` (iter-1 already
  correct — verified via grep)
* `src/forex_bot/engine/health_monitor.py` (iter-1 already correct —
  verified via grep)

## Build metadata (BUILD-METADATA)

```
BUILD-ID: sprint-2026-09-25-builder-debt-card1-b37494d7
BUILD-TYPE: rework (iter-2)
BASE: main @ e83a761b
BRANCH: reina/latefill-rework-iter2
WORKTREE: /home/TacoPants/projects/Ayumi/.worktrees/reina-latefill-iter2
FINDINGS-FIXED: 6/6 (1 HIGH + 5 MEDIUM)
NEW-TESTS: 5 scenarios (test_new_order_timeout_indeterminate.py)
TARGETED-TESTS-PASS: 30/30 (5 new + 25 existing ctrader)
REGRESSIONS: 0
LIVE-PID: 4039203 (untouched, STAYS RUNNING per card constraint)
DEPLOY: OUT OF SCOPE (per card constraint)
```

## Live-process safety

PID 4039203 (XAUUSD SRMR+ live) STAYS RUNNING — no restart, no deploy from
this card. The adjudicated posture holds: current code strictly better than
pre-fix; HIGH trigger is narrow; regime-gate volatile-blocking anyway.

## Rin review

Rin review required before any deploy. Deploy is OUT of this card's scope —
next sprint / coordinated restart #2 is the deploy path.

## Open follow-ups

None for this card. Next card in sprint: `759ca217-66ef-4de9-8d4c-9cdf8c338f81`
(dreaming tests).
