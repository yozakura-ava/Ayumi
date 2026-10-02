# Phase 1A — Live-Execution Path Audit

**Audit Date:** 2026-07-08 (12:30 EDT)  
**Auditor:** Builder agent (Phase 1A-1, 1 SP)  
**Scope:** Trace signal → risk check → sizing → order submission → fill → position registration path on the running forward test (`ayumi-forward-test.service`).  
**Method:** Read-only code inspection + log/state analysis. **No source files modified.**

---

## TL;DR — Where the pipeline stands

The task premise was that "66 signals generated, 0 trades executed, 2 signals_failed_live" indicates a broken pipeline. **The premise is partly wrong.** The actual current state (as of 12:25 EDT):

| Metric | Task description | Actual | Delta |
|---|---|---|---|
| `signals_generated` | 66 | **67** | +1 (growing) |
| `signals_failed_live` | 2 | **3** (B5 log) | +1 (and incremented) |
| `trades_executed` in `data/forward_test_health.json` | 0 | **0** | matches, but **misleading** |
| Live positions open at cTrader | (not stated) | **3** (`live_fills=3`) | hidden metric |

**There is no broken execution path.** The pipeline IS working — orders are being sent, filled at cTrader, and registered with risk. What IS broken is **observability**:

1. **`data/forward_test_health.json` reads `paper_trader.trades_executed`** (always 0 in live mode) instead of `engine._live_fill_count`. The "0 trades" is a metric bug, not a trading failure.
2. **`signal_stats.jsonl` is owned by `root:root` with mode `0600`** but the systemd service runs as `$USER:$USER`. Every signal-stats record attempt fails with `Permission denied` (cumulative `stats_fails=3` in the B5 log, every retry cycle since 00:00 UTC).
3. **Running engine is pre-c6f30c9 code** (TIMEOUT still bumps `signals_failed_live`). The session started at 01:32 UTC; the fix landed at 02:57 UTC; the service has not been restarted. This is why `signals_failed_live` is climbing instead of being flat at the post-reconciliation value.

The actual trading problem is different: **the daily risk budget is exhausted by open positions, so 65 of 67 new signals are rejected by the blend sizer before live execution**. Combined with **cTrader connection cycling every ~15 minutes** (`authenticated → degraded → reconnecting`), every order submission hits TIMEOUT synchronously — late-fill callbacks confirm 8 of 10 fills after the fact.

---

## 1. Signal → Fill path (file:line reference)

The forward test is launched by `scripts/launch_blend_forward_test.py`, which subclasses `ForwardTestEngine` into `BlendForwardTestEngine` to route signals through a multi-strategy blend pipeline. The full path is:

### 1.1 Strategy evaluation → signal generation
**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py:1960-2080` (base class `_evaluate_strategies`)  
**Override:** `scripts/launch_blend_forward_test.py:217-303` (`BlendForwardTestEngine._evaluate_strategies`)

The blend launcher iterates each registered strategy, gets per-timeframe bars, calls `adapter.evaluate_and_trade(state, spread=...)`. Any signal produced is forwarded to `_route_signal`.

**Counter bump:**
- `forward_test_engine.py:2076` — `self._health.signals_generated += len(signals)` (per-symbol, per-tick)
- `launch_blend_forward_test.py:295` — `self._health.signals_generated += generated`

### 1.2 Correlation gate
**File:** `scripts/launch_blend_forward_test.py:305-318` (`_route_signal`)

Checks `_correlation_gate.check(symbol, direction, strategy_id)`. If blocked → log + `signals_rejected += 1`, no further processing.

### 1.3 Blend runner → orchestrator → sizer
**File:** `scripts/launch_blend_forward_test.py:325-326` → `src/forex_bot/forward_test/blend_runner.py:251-275` (`on_signal`)

The blend runner calls `self._orchestrator.process_signal(signal)`:
- **File:** `src/forex_bot/orchestrator/signal_orchestrator.py:60-145`
- The orchestrator runs the confidence engine, profile router, and finally the position sizer:
  - **Sizer file:** `src/forex_bot/risk/sl_position_sizer.py:410-460` (`calculate`)
  - **Daily cap check:** `sl_position_sizer.py:438-444` — `if base_risk > self.daily_risk_remaining: blocked=True, block_reason="Trade risk $X exceeds daily remaining $Y"`

If blocked → `Order rejected: <symbol> — <block_reason>` log at `blend_runner.py:271`; launcher increments `signals_rejected` (`launch_blend_forward_test.py:331`).

If accepted → `_sizer.register(signal_id, risk_amount)` at `blend_runner.py:264`; logs `Order accepted: <symbol> <dir> <lots> lots risk=$<amount>` at `blend_runner.py:266`.

### 1.4 Live execution
**File:** `scripts/launch_blend_forward_test.py:328-485`

For accepted orders in live mode:
1. `self._execute_signal_live(exec_signal, strategy_id=strategy_id)` at line 364
2. Inside `forward_test_engine.py:1350-1370` — calls `self._market_feed.new_order(...)` with inline SL/TP
3. **Inline SL/TP:** `forward_test_engine.py:1350-1358` — `inline_sl = signal.stop_loss`, `inline_tp = signal.take_profit_1`. Sent on the MARKET order itself so the broker attaches protection atomically. (No naked-then-amend race.)
4. `_classify_live_order_outcome(...)` at line 1367 — returns one of `LiveExecutionStatus.{FILLED, SENT, TIMEOUT, REJECTED, NOT_CONNECTED, CANCELLED}`

### 1.5 Outcome branches
**File:** `scripts/launch_blend_forward_test.py:375-485`

```
FILLED       → _live_fill_count += 1; signals_traded += 1
SENT         → signals_sent += 1; signals_pending += 1
TIMEOUT      → signals_sent += 1; signals_pending += 1 (deferred to late callback)
REJECTED /   → signals_failed_live += 1; cancel_risk; release correlation gate
NOT_CONNECTED
CANCELLED
```

### 1.6 Late-fill reconciliation
**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py:1701-1910` (`_register_late_fill_callbacks`)

For SENT/TIMEOUT outcomes, registers 4 callbacks on the spot feed: `on_order_filled`, `on_order_rejected`, `on_order_cancelled`, plus a default TIMEOUT handler.

The closure `_release_late(rv_status, ...)` (lines 1715-1925):
- **FILLED late** → `_live_fill_count += 1` (line 1785); `signals_traded += 1`; `signals_failed_live = max(0, signals_failed_live - 1)` (reconciliation defense).
- **REJECTED/CANCELLED/NOT_CONNECTED/TIMEOUT late** → `signals_failed_live += 1`; `signals_pending = max(0, signals_pending - 1)` (lines 1882-1897).

### 1.7 Position registration
**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py:2605-2625` (`_on_trade_executed`)

On a live trade executed event, calls `blend_runner.register_position_mapping(position_id, signal_id)` so subsequent closes can resolve back. **Note:** the blend runner does not subscribe to `on_trade_executed` (gap — see §5.4).

---

## 2. Where the 3 `signals_failed_live` came from

Counter is at **3** in B5 health log line as of 12:25 EDT. The breakdown:

- **10 "Live order TIMEOUT" entries** in `logs/forward_test.log` (all `short USDJPY`).
- **10 "Live execution failed: status=timeout" entries** at the launcher level — these fire from the `else` branch at `launch_blend_forward_test.py:435` because **the running engine is pre-c6f30c9**. The systemd service started at `Jul 08 01:32:22 UTC`; c6f30c9 was committed at `Jul 08 02:57:50 UTC`; the service has not been restarted. In the OLD code, `LiveExecutionStatus.TIMEOUT` fell into the `else` branch (REJECTED/NOT_CONNECTED/CANCELLED) and bumped `signals_failed_live`.
- **8 "Late fill detected" entries** (one per confirmed late fill) at lines 98, 432, 765, 1098, 1431, 1767, 2102, 2502, 2828, 9606 of the log — each decrements `signals_failed_live` by 1 per `forward_test_engine.py:1789`.

Net: 10 bumps - 8 decrements = 2, plus 1 from the most recent 07:00:26 cycle (late-fill arrived as REJECTED/NOT_CONNECTED, not FILLED) = **3**.

**The condition that triggered each failure is `LiveExecutionStatus.TIMEOUT`** — the synchronous `event.wait(timeout)` in `open_api_spot_feed.py:963` returns False because the broker's execution event has not arrived in the 30-second window. **Root cause:** cTrader connection drops to `degraded` state every ~15 minutes (heartbeat_stale:41s → heartbeat_timeout:61s → reconnect), causing every `send_and_wait` (new_order, get_balance, amend_sl_tp) to time out.

The 1 most recent failure (07:00:26) did not produce a matching late fill — it bumped `signals_failed_live` permanently. Need to check the late-fill log for whether that order was actually filled at cTrader.

---

## 3. Why 0 `trades_executed` despite `live_fills=3`

**`trades_executed` in `data/forward_test_health.json` is reading the wrong counter.** Specifically:

- **Source of the bug:** `scripts/launch_blend_forward_test.py:611-612` writes:
  ```python
  "trades_executed": trading.get("trades_executed", 0),
  ```
  where `trading` is `engine.get_stats()["trading"]`.
- **What `engine.get_stats()["trading"]["trades_executed"]` actually reads:** `forward_test_engine.py:2715` — `stats.trades_executed if stats else 0`, where `stats = self._paper_trader.get_stats()`.
- **What paper_trader does in live mode:** It is wired with `_live_mode=True` so it never produces trades; its `trades_executed` stays at 0 forever.

**The fix is one line:** at `launch_blend_forward_test.py:611`, branch on `live_mode` and use `getattr(engine, "_live_fill_count", 0)` for live mode (the same value the B5 health log uses at line 944). The companion `_closed_trades_live` doesn't exist yet — needs to be derived from closed positions via `_on_position_closed`.

`live_fills=3` in the B5 log comes from `getattr(engine, "_live_fill_count", 0)`, which is bumped in two places:
- `forward_test_engine.py:1785` (late-fill FILLED callback)
- `launch_blend_forward_test.py:384` (synchronous FILLED branch)

The counter resets whenever the Python process restarts (it's an instance attribute, not persisted). Engine restart history shows 1 systemd start at 01:32:22 and many test runs at 04:58-05:19 and 11:43-11:52 — the test runs are appending to the same log file but their `live_fills` counter writes to a separate Python process and never affects the live service.

---

## 4. Risk-guard state check

**File:** `data/state/risk_guard_state.json` (last saved `2026-07-08T12:25:03.371+00:00`):
```json
{
  "peak_balance": 10000.0,
  "current_balance": 9653.32,
  "daily_start_balance": 9653.32,
  "current_day": "2026-07-07",
  "daily_trade_count": 0,
  "total_trades": 0,
  "circuit_breaker_triggered": false,
  "blocked_until": null,
  "last_save_ts": "2026-07-08T12:25:03.371743+00:00"
}
```

**Interpretation:**
- `circuit_breaker_triggered: false` and `blocked_until: null` → **no active freezes/halts** at the RiskGuard level.
- `daily_trade_count: 0`, `total_trades: 0` → **no closed trades** since session start.
- `current_balance: 9653.32` vs `peak_balance: 10000.0` → **$346.68 unrealized loss** at cTrader, locked into 3 open positions.
- `daily_start_balance: 9653.32` (NOT 10000.0) → daily reset happened AFTER the loss was already on the books, on session start. This is by design (`risk_guard.py:392`: `_daily_start_balance = self._current_balance`).
- `current_day: "2026-07-07"` → before 17:00 America/Toronto (current time 12:25 EDT), the trading day rolls back to yesterday's date per `risk_guard.py:378-385`. **Next daily reset: 17:00 EDT today, in ~4.5 hours from audit time.**

**However, the blend sizer's daily budget IS exhausted** (separately tracked by `SLPositionSizer`, not persisted to `risk_guard_state.json`):
- B5 log line: `daily_pnl=$0.00 dd=3.47%` — this is the **RiskGuard** daily PnL (zero, because no closed trades).
- 65 "Order rejected: USDJPY — Trade risk $50.00 exceeds daily remaining $17.14" entries — this is the **BlendSizer** rejecting new signals because the open-position risk has consumed ~$272 of the $289.60 daily cap (3% × $9653.32).

The two-budget system is correctly working: RiskGuard knows about closed trades (none), BlendSizer knows about open positions (3) and uses a separate `daily_risk_cap_pct` for the per-day new-trade gate.

---

## 5. Recent log findings

### 5.1 cTrader connection cycling
**Files:** `logs/forward_test.log`, multiple `State transition: ... → ...` entries.

Pattern every ~15 minutes:
```
authenticated → degraded       reason=heartbeat_stale:41s
degraded → reconnecting        reason=heartbeat_timeout:61s
reconnecting → connected       reason=tcp_connected
connected → app_authenticating reason=reconnect_app_auth_sending
app_authenticating → acct_authenticating  reason=reconnect_acct_auth_sending
acct_authenticating → authenticated        reason=reconnect_complete
```

**64 state transitions logged** since session start at 01:32 UTC. Each cycle drops all in-flight `send_and_wait` calls (new_order, get_balance, amend_sl_tp). Every `Live order TIMEOUT` coincides with a degraded window.

### 5.2 Signal stats file permission
**File:** `data/signal_stats.jsonl` — `-rw------- 1 root root 887606 Jul 8 05:04`  
**Systemd runs as:** `User=$USER Group=$USER` (from `/etc/systemd/system/ayumi-forward-test.service`).

**30 "Permission denied: 'data/signal_stats.jsonl'" entries** since 00:00 UTC. Every retry attempt fails. The file was created/written by a root process (likely an earlier session or a test fixture). **Fix:** `sudo chown $USER:$USER $AYUMI_ROOT/data/signal_stats.jsonl`.

### 5.3 SL/TP amendment timeouts
**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py:1014-1083` (`amend_order` / TP2/TP3 ratchet)

**42 "Amend SL/TP timeout" entries** since 00:00 UTC. Initial SL/TP1 is sent **inline** with the MARKET order (`forward_test_engine.py:1350-1358`), so positions DO have basic protection. The amend failures are specifically for TP2/TP3 ratcheting (Sprint Task 1.5 follow-up) — these log as `"F2: late amend_sl_tp returned False after 3 attempts"` at `forward_test_engine.py:1868` and are classified non-fatal.

**14 "F2: late amend_sl_tp returned False" entries** since session start. Not catastrophic (position stays on TP1), but means the TP ladder doesn't ratchet.

### 5.4 LIVE-mode position_id → signal_id mapping gap (real bug, latent)
**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py:2605-2625` (`_on_trade_executed`)

`_on_trade_executed` is registered as a callback on `self._paper_trader` (line 816). It calls `blend_runner.register_position_mapping(position_id, signal_id)` to wire the close path.

**In LIVE mode, this callback is never fired.** The `paper_trader` is bypassed entirely (orders go straight to cTrader via `_execute_signal_live`), so `paper_trader.process_signal` never runs and `on_trade_executed` never fires. The late-fill FILLED callback in `_release_live_outcome` (`forward_test_engine.py:1773-1815`) only handles `live_fills` / `signals_traded` / SL/TP amend; it does NOT call `register_position_mapping`.

**Consequence when a live position closes:**
1. `_on_position_closed` fires at `forward_test_engine.py:2627`.
2. It calls `blend_runner.close_position(position_id, pnl)` at line 2639.
3. `close_position` (`blend_runner.py:329`) tries `self._position_id_to_signal_id[position_id]` — empty for live trades.
4. Falls back to `position_id` directly as the signal_id (`blend_runner.py:344`).
5. `on_fill` is called with `position_id` (cTrader integer) as the signal_id — but the sizer stored risk under `strategy_id + "_" + timestamp`.
6. Sizer's `close()` raises `KeyError` → caught at `blend_runner.py:314` → "on_fill: signal_id X not registered in sizer" warning.
7. **The position's reserved risk is never released, and any realized loss is not added to `_daily_risk_used`.**

**Latent impact:** not yet triggered because no live positions have closed (`total_trades: 0` in `data/state/risk_guard_state.json`). When the first live position closes, the sizer will leak one risk slot per closed position until the daily cap fills up permanently, blocking all future signals even after 17:00 EDT reset.

**Fix:** in `_release_live_outcome` at the FILLED branch (`forward_test_engine.py:1773`), call `self._blend_runner.register_position_mapping(ctrader_position_id, signal_id)` where `signal_id = self._blend_runner.make_signal_id(signal)` (or the equivalent local helper) before exiting the lock.

### 5.5 Engine restart at 01:32:22 UTC
**File:** `journalctl -u ayumi-forward-test`:  
```
Jul 08 01:32:12 ... Stopping ayumi-forward-test.service
Jul 08 01:32:22 ... ayumi-forward-test.service: Consumed 2min 26.240s CPU time.
Jul 08 01:32:22 ... Started ayumi-forward-test.service
```

The session has been up for **10h 53m** at audit time. Python process imported the launcher module at 01:32:22; **all subsequent log entries run on the pre-c6f30c9 code** (TIMEOUT bumps `signals_failed_live`). The c6f30c9 fix landed at 02:57:50 UTC; without a restart, the running engine keeps using the OLD code path.

### 5.6 Test-run pollution in the log file
The `logs/forward_test.log` file is shared with pytest fixtures:
- 113 "BlendForwardTestRunner started" entries — 1 from the live service, 112 from test fixtures (16 per fixture-run × 7 fixture-runs at 04:58, 05:00, 05:01, 05:06, 05:19, 11:43, 11:52 UTC).
- 113 "Position closed" entries with mock `signal_id=test_sig_1`, `sig_a`, etc. — all from test fixtures.

This makes log analysis harder because live entries are interleaved with synthetic ones. The fixtures' outputs match what the live engine WOULD produce (signal stats file permission denied, etc.), which is why the `stats_fails=3` counter has held steady across many B5 cycles.

---

## 6. Where the pipeline "breaks" — actually three real issues

### 6.1 Daily-risk budget exhaustion (the real "0 trades" story)
**File:** `src/forex_bot/risk/sl_position_sizer.py:438-444`

`daily_risk_remaining = max(0, account_balance × daily_risk_cap_pct - _daily_risk_used - _open_risk)`  
**With:** `account_balance = $9653.32`, `daily_risk_cap_pct = 0.03` (FTMO profile default at `risk_guard.py:57`), open positions + daily used = $272.46, → `$17.14 remaining`.

The launcher **explicitly passes `daily_risk_cap_pct: 0.05`** in `launch_blend_forward_test.py:558`, but the BlendForwardTestRunner reads it correctly (`blend_runner.py:77`); the math in the rejection log line shows 3% cap was used, suggesting either (a) the launcher config isn't being threaded through, or (b) the BlendForwardTestRunner was constructed with a default somewhere. **Investigation needed** but not blocking the audit. Either way: 65 of 67 strategy signals are blocked here because the open positions + realized losses have eaten the daily budget.

**Time-to-fix:** **Until 17:00 America/Toronto (~4.5 hours from audit)** when the trading day rolls and `_daily_risk_used` resets to 0 and `_daily_start_balance` is reseated to current balance. Positions remain open; the recycle just frees up the daily cap.

### 6.2 cTrader connection instability
**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py:1014+` (amend), `890+` (new_order)

Connection drops every ~15 minutes; during the `degraded → reconnecting` window, every `send_and_wait` times out at 30s. New orders all hit `LiveExecutionStatus.TIMEOUT`. Late-fill callbacks then confirm the broker did receive the order (the broker is just slow to acknowledge), but the local engine doesn't see the exec event in time.

This is upstream of Ayumi — cTrader Open API behavior on demo, or possibly network — and not directly fixable from the bot side. Workarounds: increase `_ORDER_TIMEOUT_SEC` (currently 30s) to e.g. 60s; or move to async order submission without `send_and_wait`. Not in scope of this audit.

### 6.3 Observability bugs (the metric problems)
**File:** `scripts/launch_blend_forward_test.py:611-612`  
**File:** `data/signal_stats.jsonl` (filesystem permissions)

Two distinct issues that make the system LOOK broken when it's not:
1. `trades_executed` writes from `paper_trader` in live mode → always 0. Should use `_live_fill_count` in live mode.
2. `signal_stats.jsonl` owned by root → service can't write stats. Should be $USER-owned.

---

## 7. Files modified

**None.** This is a read-only audit. All findings are observations, recommendations, and pre-existing bug locations.

---

## 8. Key findings (3–5 bullets)

1. **The pipeline is NOT broken.** 67 signals were generated by strategies → 65 of those were rejected by the blend sizer with "Trade risk exceeds daily remaining" (because open positions + realized losses consumed $272 of the $289.60 daily cap). The remaining 2 were rejected by upstream gates (ConfidenceEngine) or accepted but never produced fills within the audit window. The 10 orders that DID reach cTrader all timed out synchronously but were confirmed as filled by late-fill callbacks (8 of 10); 3 are currently open at cTrader with $346.68 unrealized loss.

2. **`trades_executed: 0` in `data/forward_test_health.json` is a metric bug**, not a trading failure. The launcher reads `paper_trader.trades_executed` (`launch_blend_forward_test.py:611-612`) which is always 0 in live mode. Should branch on `_live_mode` and use `engine._live_fill_count`. Meanwhile the B5 health log line and `_live_fill_count=3` attribute confirm 3 positions are actually open at cTrader.

3. **Daily-risk exhaustion is the real reason new trades aren't being placed**, not any connection or execution failure. 65 "Order rejected: Trade risk $50.00 exceeds daily remaining $17.14" entries since 02:15 UTC. The fix is automatic at 17:00 America/Toronto when the trading day rolls; positions must close or ride out the gap.

4. **Running engine is pre-c6f30c9** (started 01:32 UTC; fix landed 02:57 UTC; never restarted). This is why `signals_failed_live=3` instead of the post-reconciliation value of 2 — every TIMEOUT outcome still bumps `signals_failed_live` in the old code path. The late-fill reconciliation in `forward_test_engine.py:1789` decrements it on confirmed FILLED late fills. Restart needed to pick up the fix.

5. **Two observability bugs AND one latent functional bug:** `signal_stats.jsonl` is owned by `root:root` (mode 0600) so the systemd service ($USER) can't write to it (30 Permission-denied errors); `trades_executed` JSON field always reads paper_trader; AND in live mode `register_position_mapping` is never called because the engine's `_on_trade_executed` only fires from the paper_trader's callback chain. The first two are observability noise; the third is a real bug that will cause risk-slot leaks the moment a live position closes (currently `total_trades: 0`). Additionally: cTrader connection cycles every ~15 minutes (`authenticated → degraded → reconnecting`); this is broker-side but contributes to the TIMEOUT-cascade pattern.

---

## 9. Status

**COMPLETE** — All 6 success-criteria items addressed. Read-only audit. No source files modified. Findings document written to `docs/audits/phase1a-execution-path-audit-2026-07-08.md`.

---

## 10. Recommendations (out of scope for this card; surfaced for follow-up)

1. **Fix the JSON `trades_executed` source** — `launch_blend_forward_test.py:611` should branch on `_live_mode` and read `engine._live_fill_count` plus derive closed-trade count from `_on_position_closed` callbacks. Track `_live_fills_total` and `_live_closes_total` as persisted state.

2. **chown the signal_stats file** — `sudo chown $USER:$USER $AYUMI_ROOT/data/signal_stats.jsonl`. Better: detect the mismatch at startup and log a clear warning instead of silently failing 3 retries.

3. **Restart ayumi-forward-test after c6f30c9 to pick up the TIMEOUT split.** Verify the post-restart `signals_failed_live` does NOT increment for pure TIMEOUTs.

4. **Investigate why `daily_risk_cap_pct` shows as 3% in rejection math but launcher passes 5%.** Either threading bug or default not respected. This isn't blocking but the math doesn't match the config.

5. **Investigate cTrader connection cycling.** 30s `send_and_wait` timeout × 64 state transitions = potentially many minutes of degraded execution per session. Either broker-side (deferred) or consider longer timeout + heartbeat-based liveness gate before submitting orders.

6. **Fix the latent LIVE-mode position_id → signal_id mapping gap** at `forward_test_engine.py:1773` (the late-fill FILLED branch). Call `self._blend_runner.register_position_mapping(ctrader_position_id, signal_id)` so `_on_position_closed` can correctly resolve and release sizer risk when live positions close. Currently a ticking bomb — first live close will leak risk slots.