# Ayumi Forward Test Remediation Plan — 2026-06-30

**Prepared by:** Planner subagent  
**Repo:** `$AYUMI_ROOT/`  
**Output:** `$AYUMI_ROOT/docs/plans/ayumi-forward-test-remediation-2026-06-30.md`  
**Run window:** 2026-06-30 00:07 → 08:29 EDT (sigterm)  
**Pipeline route:** Planner → Council → Craig → Builder → Validate

---

## 1. Scope Summary

Craig ran the blended forward-test against the cTrader demo account. Four systemic failures prevented the test from behaving safely:

1. **Canary disable failed** — `TestCanaryStrategy` was still firing every bar despite the "disable" commit, producing 72 canary signals while all production strategies stayed at `no_signal=72`.
2. **USDJPY SL/TP attach fails with `TRADING_BAD_STOPS`** — signals carry USDJPY prices ~100× the cTrader decimal scale (e.g. `16232.45` vs. broker ASK `162.33`). `amend_sl_tp()` rounds to the symbol's digits but never re-scales, so every late SL/TP amendment for USDJPY is rejected.
3. **Local risk state lies** — `risk_state_blend.json` shows `open_risk: 0.0` while 71 naked positions exist on the broker. `cancel_position()` is called on the late-fill rejection path for the *same risk amount* that was just registered as an open position, so the budget is freed the instant it is reserved.
4. **No pre-trade broker cross-check** — the launcher and engine never fetch live cTrader positions to reconcile local risk. Only the broker's `NOT_ENOUGH_MONEY` server-side guard protected margin at 08:15.

The plan below defines six concrete remediation tasks. **No code changes are made by this document**; it is input to the autobuild pipeline.

---

## 2. Evidence Inventory (read directly, not from Craig's summary)

| Item | Location | What it proves |
|---|---|---|
| `test_canary` signals | `logs/forward_test.log` lines 309, 613, 1295, 1765, 1848 | Canary fired on every 15m bar (`evals=72 no_signal=0`) |
| Canary instantiation | `scripts/launch_blend_forward_test.py:702` | `TestCanaryStrategy(tp_sl_pct=0.005)` bypasses the `enabled` property |
| USDJPY price mismatch | `logs/forward_test.log` lines 1297, 1765 | `entry=16213.40000` vs. broker ASK `162.33` |
| `TRADING_BAD_STOPS` | `logs/forward_test.log` lines 2952, 3845, 4128, 4406, 4691, 5251, 5837, 6399, 6960, 7243, 7536, 8104, 8383, 8665, 10360 | Late USDJPY SL/TP amendments rejected because TP is 100× too large |
| Risk cancellation loop | `logs/forward_test.log` lines 641, 673, 688-699, etc. | `Risk cancelled: $X freed, daily remaining=$500.00` at the same timestamp as fills |
| Risk state | `data/risk_state_blend.json` | `open_risk: 0.0`, `daily_risk_used: 0.0` while broker held 71 positions |
| Rejections at margin cap | `logs/forward_test.log` lines 10343, 10351 | `NOT_ENOUGH_MONEY` only server-side defense against over-trading |
| No reconcile usage | `src/forex_bot/adapters/ctrader/forward_test_engine.py` | `reconcile()` exists in `OpenApiSpotFeed` but is never called by `blend_runner.py` or the engine for risk checks |

---

## 3. Task Definitions

### Task A — Disable Test Canary at the Launcher Level

**Root-cause analysis**

Commit `fa28060` added an `enabled` property to `TestCanaryStrategy` that returns `self.tp_sl_pct > 0` and changed the class default to `tp_sl_pct=0.0`. However, the launcher in `scripts/launch_blend_forward_test.py` still constructs the strategy with an explicit `tp_sl_pct=0.005`:

```python
TestCanaryStrategy(tp_sl_pct=0.005),  # 0.5% SL/TP — demo execution validation
```

Because `0.005 > 0`, `enabled` evaluates to `True`, so the strategy continues to fire. The docstring in `test_canary.py` even states: *"0.0 = disabled (no signals)"*. The intent of the previous fix was correct but the call site was not updated. The strategy generated 72 signals overnight (health log: `Test Canary: evals=72 no_signal=0`), while production strategies produced `no_signal=72` — i.e. the only active signal source was the canary.

**Files to change**

1. `scripts/launch_blend_forward_test.py` — remove the canary from the production strategies list **or** instantiate it with `tp_sl_pct=0.0`.
2. (Optional hardening) `src/forex_bot/strategies/test_canary.py` — add a loud warning log when `enabled` is `True` so future accidental re-enable is visible.

**Acceptance criteria**

- [ ] `grep -n "TestCanaryStrategy" scripts/launch_blend_forward_test.py` shows no non-zero `tp_sl_pct`.
- [ ] In a dry-run/short forward test, `Test Canary` reports `no_signal=N` equal to its eval count.
- [ ] No `canary_<N>` rationale appears in order logs.

**SP estimate:** 1

**Dependencies:** None. Can be done in parallel with all other tasks.

---

### Task B — Fix USDJPY Price Scaling in Signal-to-Execution Path

**Root-cause analysis**

Two separate but related scaling problems exist.

**Problem B1: signal prices are ~100× too large for JPY pairs.**

The cTrader `ProtoOASpotEvent` sends prices as integer ticks. The `OpenApiSpotFeed._handle_spot_event()` divides by `10 ** digits`. For USDJPY the symbol metadata has `digits=3`, so the divisor is `1000`; a raw cTrader price of `162132` becomes `162.132`. That is the correct market price.

But the strategy layer emits USDJPY prices such as `entry=16213.40000` and `sl=16213.70000` (see log lines 1297, 1738, 1832, 2139). These are exactly 100× the tick-decoded market price. The same strategies produce correct EURUSD/GBPUSD prices (~1.14/1.32). The 100× inflation is therefore specific to how JPY-pair prices enter the *strategy's* market state, not the spot-feed tick decode.

The two most likely sources are:

- Historical bars from `fetch_trendbars()` are rounded to 5 decimal places (line 821 in `open_api_spot_feed.py`) regardless of symbol. For USDJPY this loses no precision, but if the raw trendbar delta values are interpreted with the wrong divisor, prices could be inflated.
- The strategy helper `_pip_size()` in `session_breakout.py` and `_pip_value_for_price()` in `session_range_mean_reversion.py` choose `_JPY_PIP = 0.01` when `price >= 50`. Those functions are used to compute SL/TP offsets in *pips*, not to scale prices, so they are probably not the root cause — but they show the codebase is aware of the JPY/non-JPY distinction.

The concrete evidence is that every USDJPY SL/TP amend is rejected with `TRADING_BAD_STOPS` because the TP (e.g. `16232.45`) is 100× the current ASK (`162.33`). EURUSD/GBPUSD amends succeed because their signal prices are already in the same scale as the broker.

**Problem B2: the late-fill SL/TP attach path passes raw signal prices.**

The engine's `_register_late_fill_callbacks()` at lines 1385-1425 calls:

```python
amended = self._market_feed.amend_sl_tp(
    ctrader_position_id, signal.stop_loss, signal.take_profit_1,
    symbol_id=symbol_id,
)
```

`amend_sl_tp()` then calls `self._round_price(symbol_id, sl/tp)` which rounds to `digits` decimal places (3 for USDJPY). Because the input price is already 100× too large, rounding to 3 digits does not fix the scale; it only trims noise.

Commit `2acdc46` added rounding but did not detect that the input itself was in the wrong unit for JPY pairs.

**Files to change**

1. `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`
   - `fetch_trendbars()` — verify the `d = 10 ** self._symbol_digits.get(...)` divisor is being applied to all four OHLC deltas consistently and that the returned `Bar` close for USDJPY matches the live tick scale.
   - `amend_sl_tp()` and `_round_price()` — add a normalization step that converts strategy-layer prices to broker scale using the symbol's `pip_size` / `digits`. The fix should be **symbol-aware**, not USDJPY-specific, so future JPY-crosses work automatically.
2. `src/forex_bot/adapters/ctrader/forward_test_engine.py`
   - The synchronous SL/TP attach path at line 1134 (`_execute_live_order`) and the late-fill callback path at lines 1385-1425 must both pass prices through a single broker-scale normalization helper before calling `amend_sl_tp()`.
3. `src/forex_bot/strategies/session_breakout.py`
   - Audit `_pip_size()` usage at lines 37, 131, 206 to confirm it is only used for pip-distance math, not price scaling. If it is accidentally used to re-scale prices, fix it.
4. `src/forex_bot/strategies/session_range_mean_reversion.py`
   - Same audit for `_pip_value_for_price()`.

**Acceptance criteria**

- [ ] Unit test: given a USDJPY signal price `16232.45` and a `ProtoOASymbol` with `digits=3`, the helper normalizes it to `162.325` (or equivalent broker scale) before `amend_sl_tp()`.
- [ ] Unit test: EURUSD/GBPUSD prices pass through unchanged at their 5-digit scale.
- [ ] Dry-run forward test: no `TRADING_BAD_STOPS` rejections for USDJPY; log shows `Late SL/TP attached to position ... sl=... tp=...` with USDJPY prices ~162.x.
- [ ] Add a regression assertion that USDJPY SL/TP values are within `[50, 250]` before sending.

**SP estimate:** 3

**Dependencies:**
- Task F (broker position reconciliation) is not strictly a dependency, but Task B should be validated **after** Task F so that a USDJPY position can actually be opened without hitting the broker margin cap.

---

### Task C — Stop Cancelling Risk on Fill / Persist Open Risk Correctly

**Root-cause analysis**

`BlendForwardTestRunner.on_signal()` registers open risk:

```python
self._sizer.register_open_position(order.risk_amount)
```

`SLPositionSizer.register_open_position()` increments `_open_risk`. That part is correct.

The bug appears because `cancel_position()` is invoked from the engine's late-fill callback whenever the outcome is **not** `FILLED` (lines 1465-1470 in `forward_test_engine.py`):

```python
if blend_runner is not None and rv_status != LiveExecutionStatus.FILLED:
    blend_runner.cancel_risk(self._config.starting_balance * 0.01)
```

But the engine also treats `TIMEOUT` and `SENT` as transient states that may later resolve to a fill. The log shows `Live order TIMEOUT` followed milliseconds later by a real execution event that confirms the fill. During that gap the late-fill callback already called `cancel_risk()`, freeing the budget. When the real fill event arrives, `on_fill()` calls `close_position()` which subtracts the risk amount again — often driving `_open_risk` negative and clamping it to zero (the `max(0.0, ...)` logic in `cancel_position` and `close_position`).

The result is that `daily_risk_used` never accumulates (`daily remaining=$500.00` all night) and `open_risk` is reported as `0.0` in the persisted state even while 71 broker positions are live.

The semantic problem: `cancel_risk()` should only run on **permanent** rejection/cancellation, not on `TIMEOUT` or `SENT`. A timeout is a local event-timeout, not a broker rejection. Risk should remain reserved until either:
- a real broker rejection/cancellation event arrives, or
- the fill is confirmed and `close_position()` recycles the budget.

**Files to change**

1. `src/forex_bot/adapters/ctrader/forward_test_engine.py`
   - In `_register_late_fill_callbacks()`, change the risk-release branch so it only calls `cancel_risk()` for `REJECTED`, `CANCELLED`, or `NOT_CONNECTED` — **not** for `TIMEOUT` or `SENT`.
   - Pass the actual `risk_amount` to `cancel_risk()` instead of the hard-coded `starting_balance * 0.01` (which is also wrong for mixed lot sizes).
2. `src/forex_bot/forward_test/blend_runner.py`
   - Change `cancel_risk()` to require a `signal_id`/`order_id` so it can verify the position is still in `_open_positions` before freeing budget (defense against double-cancel).
   - Add an overshoot log if `cancel_position()` clamps (it already logs, but make it an explicit warning).
3. `src/forex_bot/risk/sl_position_sizer.py`
   - Add a guard so `cancel_position()` and `close_position()` do not drive `_open_risk` below zero silently; log the condition.
   - Add `record_open_position()` (or rename `register_open_position` for symmetry) if needed for clearer accounting.

**Acceptance criteria**

- [ ] In a dry-run, a `TIMEOUT` that resolves to a late fill does **not** emit `Risk cancelled` before the fill.
- [ ] After a fill, `open_risk` reflects the live exposure until `close_position` is called on exit.
- [ ] `risk_state_blend.json` shows `open_risk > 0` while positions are open on the broker.
- [ ] `daily_risk_used` increases monotonically as new trades are accepted and only resets at day boundary.

**SP estimate:** 3

**Dependencies:** Task F (broker reconcile) makes this easier to validate with real position counts.

---

### Task D — Add Broker Position Reconciliation at Startup and Per-Tick Refresh

**Root-cause analysis**

`OpenApiSpotFeed.reconcile()` already exists (lines ~980+) and returns a list of `Position` objects from `ProtoOAReconcileReq`. No caller in `forward_test_engine.py` or `blend_runner.py` invokes it for risk accounting. Consequently:

- The launcher starts with no knowledge of pre-existing cTrader positions.
- The forward-test ran into the 71 naked positions from earlier activity and never knew about them.
- Local risk state diverged from broker reality immediately.

We need two controls:

1. **Startup reconciliation**: before accepting any new signal, fetch cTrader positions and seed `_sizer._open_risk` (and `_open_positions` tracking) with the broker's actual exposure.
2. **Per-tick or per-evaluation cap refresh**: periodically re-fetch positions (or use execution-event deltas) to keep the local risk cap aligned, preventing over-trading when late events or connection hiccups desynchronize state.

**Files to change**

1. `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`
   - Ensure `reconcile()` returns enough data (positionId, symbol, volume, open SL/TP) for the runner to map each position to a risk amount.
2. `src/forex_bot/forward_test/blend_runner.py`
   - Add `reconcile_open_positions(positions: list[Position])` that computes risk per position from entry/SL and registers it in the sizer.
3. `src/forex_bot/adapters/ctrader/forward_test_engine.py`
   - Call `reconcile()` once after the feed becomes operational, before the first strategy evaluation.
   - Schedule a periodic reconcile (e.g. every 60s or after every N fills) to refresh the local cap.
   - Wire execution events (fill/close) to update the blend runner's `_open_positions` directly so the refresh is incremental.

**Acceptance criteria**

- [ ] Startup log shows a reconcile result: e.g. `Reconciled 12 cTrader positions, seeding open_risk=$600.00`.
- [ ] If cTrader has pre-existing positions, the first new signal is rejected if it would exceed the daily risk cap.
- [ ] `risk_state_blend.json` `open_risk` is within 10% of the broker's theoretical exposure after a reconcile cycle.
- [ ] A unit test simulates 5 pre-existing positions and verifies the sizer refuses a 6th trade that would breach cap.

**SP estimate:** 3

**Dependencies:** None, but should land before or simultaneously with Task C so the local budget is actually used.

---

### Task E — Gate `amend_sl_tp()` Under Execution Permission Policy (Awareness / Future-Proofing)

**Root-cause analysis**

`execution_permission.py` explicitly documents that P5A covers `new_order()` only. `amend_sl_tp()`, `close_position()`, and `cancel_order()` are **not** gated. During the forward test, the engine sent many fire-and-forget SL/TP amendments even after the forward test was effectively misconfigured (canary active, USDJPY scaling broken, risk state lying). A kill switch would have stopped new market orders but not the amend storm.

The constraint says: *"`amend_sl_tp()` on OpenApiSpotFeed is NOT kill-switch-gated in P5A — keep awareness"*. This task is therefore **not** to change the protocol or the permission policy scope, but to add defensive awareness and a narrow gate so a future kill switch can also stop amendments.

**Files to change**

1. `src/forex_bot/adapters/ctrader/execution_permission.py`
   - Add an optional `can_amend_position()` check that mirrors `can_send_order()` logic (default allow for backward compatibility). Document that it is Phase-6 scope.
2. `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`
   - In `amend_sl_tp()`, log a clear warning if no permission policy is present.
   - If a policy is present and exposes `can_amend_position()`, consult it before sending.

**Acceptance criteria**

- [ ] `amend_sl_tp()` logs its policy status at DEBUG/INFO level.
- [ ] With kill switch active, `amend_sl_tp()` returns `False` without sending when a policy is configured (does not affect default demo-only runs).
- [ ] No regression: `new_order()` remains the only mandatory-gated path.

**SP estimate:** 2

**Dependencies:** None. Can be done in parallel.

---

### Task F — Fix Late-Fill Callback Position-ID Type Error

**Root-cause analysis**

The log shows repeated errors:

```
Late SL/TP amend error for position ccf76a6b15744b5195ec8945b72a60c6: 'str' object cannot be interpreted as an integer
```

In `_register_late_fill_callbacks()`, the code extracts `ctrader_position_id` from the execution event and then calls `int(pid)`:

```python
for source in (order_payload, position_payload, deal_payload):
    pid = getattr(source, "positionId", None)
    if pid is not None and int(pid) != 0:
        ctrader_position_id = int(pid)
        break
```

The UUID-like strings in the error message (`ccf76a6b...`) are **client-generated** order IDs, not cTrader `positionId`s. They are being passed to `amend_sl_tp()` because the fallback path is grabbing the wrong field. `ProtoOAAmendPositionSLTPReq.positionId` expects an integer (cTrader's native position ID). The fix is to ensure we only use a real integer `positionId` from the execution event; if none is available, log a warning and skip the amend rather than passing a string.

This is a follow-up to commit `4e43dd6` which attempted to use real cTrader `positionId`s but left a fallback that still accepts invalid values.

**Files to change**

1. `src/forex_bot/adapters/ctrader/forward_test_engine.py`
   - In `_register_late_fill_callbacks()`, validate that `ctrader_position_id` is an `int` (and not zero) before calling `amend_sl_tp()`.
   - If the event has no integer positionId, log: `Late fill for order %s but no cTrader positionId available — SL/TP not attached` (the warning already exists but may not be reached due to the string fallback).

**Acceptance criteria**

- [ ] No more `Late SL/TP amend error ... 'str' object cannot be interpreted as an integer` in logs.
- [ ] Unit test with a synthetic execution event containing only a string clientOrderId verifies the amend is skipped and a warning is logged.

**SP estimate:** 1

**Dependencies:** Task B (USDJPY scaling) — without correct prices, the amend would still be rejected even with a valid positionId.

---

## 4. Risks and Unknowns

1. **USDJPY scaling root location.** The 100× inflation could be in historical-bar preloading, in the strategy helper functions, or in how `MarketState` is assembled. If the fix is applied only in `amend_sl_tp()`, strategy-level risk/sizing (which uses the inflated price) will still compute wrong lot sizes. We must find and fix the *source* of the inflated price, not only the amend normalization.
2. **Race between `TIMEOUT` and late fill.** Removing `cancel_risk()` from the `TIMEOUT` branch means risk stays reserved longer. If a real rejection never arrives, the budget could be stranded until a manual reconcile or restart. We need a TTL/reaper for stale reserved risk.
3. **Demo/live mode assumptions.** The forward test runs demo-only, but the code paths (`OpenApiSpotFeed`, `ForwardTestEngine`) are also used for live trading. Any symbol-scale normalization must not break EURUSD/GBPUSD live behavior. We should add per-symbol unit tests before any live re-enable.
4. **cTrader `ProtoOAReconcileReq` granularity.** Reconcile returns positions but may not include entry prices/SL/TP, making risk computation approximate. We may need to fetch deal history or accept a conservative estimate.
5. **Can `TestCanaryStrategy` be deleted?** It is currently the only in-system execution-path validator. Removing it entirely loses that signal. The safer path is to keep it but default `tp_sl_pct=0.0` and require an explicit env var or launch flag to enable it.

---

## 5. Verification Approach

### 5.1 Unit / Regression Tests

| Task | Test |
|---|---|
| A | `TestCanaryStrategy(tp_sl_pct=0.0).enabled is False`; `evaluate()` returns `None` |
| B | USDJPY price normalizer: `16232.45` → `162.325`; EURUSD `1.138445` unchanged |
| C | Simulated `TIMEOUT`→fill sequence does not emit `Risk cancelled` before fill |
| D | Mock `reconcile()` with 3 positions seeds `open_risk` and rejects a 4th over-cap trade |
| E | `amend_sl_tp()` returns `False` when kill-switch policy is active |
| F | Execution event with string `positionId` triggers warning, no exception |

### 5.2 Forward-Test Dry Runs

1. **Canary-only dry run:** Start with `tp_sl_pct=0.0`; confirm zero canary signals in 30 minutes.
2. **USDJPY-only dry run:** Trade only `Session-Range Mean Reversion` on USDJPY with small size; confirm no `TRADING_BAD_STOPS` and SL/TP attach logs show prices ~162.x.
3. **Risk-state dry run:** Start with a few manually placed cTrader positions; confirm reconcile seeds `open_risk` and new signals are capped.
4. **Full blend dry run:** Run all production strategies for 2-4 hours; confirm `daily_risk_used` increases monotonically and `open_risk` matches broker position count × avg risk.

### 5.3 Code Review Gates

- Council review on the USDJPY scaling fix (must not be a USDJPY-only hack).
- Craig approval before any live-mode path touches.
- Validate subagent runs the full dry-run suite and reports pass/fail per acceptance criterion.

---

## 6. Suggested Dispatch Order

1. **Parallel wave 1 (no dependencies):**
   - Task A — disable canary (1 SP)
   - Task E — amend policy awareness (2 SP)

2. **Wave 2 (depends on nothing, but highest risk):**
   - Task F — fix late-fill positionId type (1 SP)
   - Task B — USDJPY scaling (3 SP)
   - Task C — risk cancellation logic (3 SP)

3. **Wave 3 (builds on B/C):**
   - Task D — broker reconcile (3 SP)

**Total:** 6 tasks, **13 SP**

Recommended Builder order: **A → F → E → B → C → D**.

Rationale: A gives immediate safety (no more canary noise). F and E are narrow, safe fixes. B and C are the two highest-impact correctness changes; they should be reviewed together because C's risk accounting must be validated against the real prices produced by B. D is last because it requires the local risk model to be correct before reconciling it against the broker.

---

## 7. Open Questions for Craig

1. **Canary fate:** Should `TestCanaryStrategy` be removed from the production launcher entirely, kept with `tp_sl_pct=0.0`, or gated behind an explicit env var (`AYUMI_ENABLE_CANARY=1`)?
2. **Reconcile frequency:** Is a 60-second periodic reconcile acceptable, or do you want event-driven updates only (every fill/close) to minimize API load?
3. **Stale-risk TTL:** If a `TIMEOUT` never resolves, after how many seconds/minutes should reserved risk be auto-released? (Suggested: 60s, configurable.)
4. **Live-mode guard:** Should we block all live-mode launches until this remediation is validated, or is demo-only sufficient for now?
5. **USDJPY scaling source:** Do you want a separate spike task to trace the exact 100× multiplication in `MarketState`/bar construction, or should Task B include that investigation?
