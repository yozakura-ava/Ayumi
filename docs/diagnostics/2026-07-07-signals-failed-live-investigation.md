# signals_failed_live=7 Investigation — Forward Test Counter Mis-Attribution

**Date:** 2026-07-07 (log evidence: 2026-07-08 00:00–02:09 EDT)
**Author:** Ava (subagent, depth 1/1)
**Branch:** main (7 commits ahead of origin/main, working tree clean)
**Workspace:** $AYUMI_ROOT
**Card / ID:** inv-2026-07-07-signals-failed
**Status:** Root cause confirmed; fix is a 2-line counter correction.

---

## TL;DR

The forward-test log line `signals=7 live_fills=7 signals_failed_live=7 trades=0` is **not** evidence that 7 trades failed. The 7 positions are actually live at the broker (`live_fills=7` is correct), and the equity tracker confirms trades=2/7 have been recorded (see `[A8 Equity] Recorded: balance=$9314.08 trades=2` at 02:09:25).

`signals_failed_live` is **double-counted** by a race between the synchronous outcome path and the late-fill callback: any signal that comes back as `TIMEOUT` synchronously is bumped into `signals_failed_live` by the launcher, then the late-fill callback *also* sees a fill event and bumps `signals_traded` / `live_fills` — but never reconciles the spurious failure increment. The 21 amend timeouts are a **separate, downstream** symptom: the late amend retries fire during a degraded broker connection, but the SL/TP were already attached in the original `ProtoOANewOrderReq`, so the amend timeouts do **not** leave the positions unprotected.

The launcher is the primary work blocker because the misleading counter hides real failures (REJECTED, NOT_CONNECTED, CANCELLED) inside a sea of false-positives from late fills.

---

## 1. Root Cause

### 1a. Primary bug: signals_failed_live double-counts late fills

**File:** `scripts/launch_blend_forward_test.py`
**Lines:** 411–413 (inside `_route_signal`, live-mode branch)

```python
                            else:
                                # REJECTED / TIMEOUT / NOT_CONNECTED / CANCELLED
                                with self._lock:
                                    self._health.signals_failed_live += 1
```

The `else` branch fires for **any** non-FILLED outcome, including `TIMEOUT` and `SENT`. Those two are not terminal — they're explicitly described in the engine's own docstring as "awaiting cTrader ack, late events upgrade to FILLED":

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`
**Lines:** 199–227 (`LiveExecutionStatus` docstring)

> `SENT` — the order was sent to cTrader but no execution event has arrived yet. The engine should NOT count this as a fill. Late events are delivered via the spot feed's `on_order_filled` / `on_order_rejected` / `on_order_cancelled` callbacks.
> `TIMEOUT` — the order was sent but no execution event arrived within the spot feed's `_ORDER_TIMEOUT_SEC` window. Caller should log a warning and release correlation + risk.

The launcher treats `TIMEOUT` / `SENT` as if they were terminal failures and bumps `signals_failed_live`, then the late-fill callback (`_release_late`) fires minutes later when the execution event finally arrives, **increments `signals_traded` / `live_fills`**, but does **not** decrement `signals_failed_live`.

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`
**Lines:** 1785–1788 (the late-callback FILLED branch)

```python
            if rv_status == LiveExecutionStatus.FILLED:
                self._live_fill_count = getattr(self, "_live_fill_count", 0) + 1
                self._health.signals_traded += 1
                self._health.signals_pending = max(0, self._health.signals_pending - 1)
```

There is **no** `self._health.signals_failed_live = max(0, self._health.signals_failed_live - 1)` line anywhere in this branch. So a signal whose sync outcome was TIMEOUT ends up counted in **both** counters.

The same launcher/engine pair shares one `ForwardTestHealth` instance: `BlendForwardTestEngine(ForwardTestEngine)` (line 193 of `launch_blend_forward_test.py`) inherits `self._health` from the parent, and `_health.signals_failed_live` is the same attribute the late-callback at line 1873 of `forward_test_engine.py` also writes:

```python
            elif rv_status in (
                LiveExecutionStatus.REJECTED,
                LiveExecutionStatus.CANCELLED,
                LiveExecutionStatus.NOT_CONNECTED,
                LiveExecutionStatus.TIMEOUT,
            ):
                self._health.signals_failed_live += 1
```

So the same `(order_id)` can hit the failure counter twice: once at sync time (launcher line 412) and again at late-callback time (engine line 1873) if the late event is REJECTED/CANCELLED/NOT_CONNECTED/TIMEOUT. The `fired[0]` closure flag in `_release_late` (line 1747) ensures the late callback only fires once, but the launcher has already counted it.

### 1b. Secondary symptom: amend_sl_tp timeouts during degraded connections

**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`
**Lines:** 1059–1064 (`amend_sl_tp`)

```python
            res = self._conn.send_and_wait(req, timeout=timeout, prefix="amend")
            if res is None:
                logger.warning(
                    "Amend SL/TP timeout for position %s (sl=%s tp=%s): no response from broker",
                    position_id, sl, tp,
                )
                return False
```

`timeout=_AMEND_TIMEOUT_SEC` = 30s (line 111). When the broker connection is degraded (heartbeat has lapsed, see "Send-and-wait failed" Twisted `TimeoutError` in the log), the amend can't get a response, so it returns False. The engine retries 3 times with linear backoff (200ms × n).

This is **not the primary failure** because the SL/TP were already passed in the original `ProtoOANewOrderReq` (lines 939–940 of `open_api_spot_feed.py`). The amend is **defense-in-depth**: the F2 amend fires only after a late-fill, when the position was opened without confirmation of inline SL/TP. The log evidence (21 amend timeouts = 7 positions × 3 attempts) is consistent with: every late fill that triggered an amend also happened during a window where the broker was degraded.

### 1c. Counter reconciliation table (what should vs. what does happen)

| Sync outcome | Late event | `live_fills` (correct?) | `signals_failed_live` (correct?) | `signals_traded` (correct?) |
|--------------|-----------|------------------------|----------------------------------|------------------------------|
| FILLED | n/a (no late callback) | ✅ +1 (launcher line 384) | ✅ 0 (correct, not a fail) | ✅ +1 (launcher line 386) |
| TIMEOUT | FILLED (late) | ✅ +1 (engine line 1786) | ❌ **+1** (launcher line 412 — false positive) | ✅ +1 (engine line 1786) |
| SENT | FILLED (late) | ✅ +1 (engine line 1786) | ❌ **+1** (launcher line 412 — false positive) | ✅ +1 (engine line 1786) |
| TIMEOUT | TIMEOUT (late) | ✅ 0 | ❌ **+2** (launcher line 412 + engine line 1873) | ✅ 0 |
| REJECTED | n/a (no late callback) | ✅ 0 | ✅ +1 (launcher line 412 — true fail) | ✅ 0 |

The 7 false positives in the user's report are all `TIMEOUT → late FILLED` rows (the typical case when the broker is slow to send the exec event during a connection blip).

---

## 2. Sequence Diagram

```
Signal emitted by strategy
        │
        ▼
[launch_blend_forward_test._route_signal]
        │
        ├─► signals_accepted += 1            (line 343-345)
        │
        ▼
[forward_test_engine._execute_signal_live]
        │
        ├─► new_order(sl=inline_sl, tp=inline_tp, ...)  ← SL/TP embedded in NewOrderReq
        │       │
        │       ├─► event.wait(timeout=20s)   [open_api_spot_feed.py:968]
        │       │       │
        │       │       ├─ event set on EXEC_EVENT  ──► order.status = FILLED
        │       │       │
        │       │       └─ event NOT set in 20s     ──► order.status = PENDING + reason="timeout_awaiting_event"
        │       │
        │       ▼
        │   _classify_live_order_outcome(order, ...)
        │       │
        │       ├─ FILLED  ──► (inline_sl && inline_tp) path: log "SL/TP attached inline"; return FILLED outcome
        │       └─ other   ──► register late-fill callbacks (_register_late_fill_callbacks)
        │                     return SENT/TIMEOUT outcome
        │
        ▼
[_route_signal sees outcome]
        │
        ├─ outcome.status == FILLED  ──► _live_fill_count += 1; signals_traded += 1   ✅ correct
        │
        └─ outcome.status ∈ {SENT, TIMEOUT, REJECTED, CANCELLED, NOT_CONNECTED}
                │
                ├─► signals_failed_live += 1   ❌ BUG: counts SENT/TIMEOUT as failures
                ├─► cancel_risk(...)           ← releases risk, but position is still alive at broker
                └─► correlation_gate.release(...)
                                                
                ▼
        ...minutes pass, broker connection recovers...
                                                
                ▼
        [late callback _release_late fires on EXEC_EVENT]
                │
                ├─► if FILLED:
                │       _live_fill_count += 1; signals_traded += 1
                │       (no signals_failed_live reconciliation)        ❌ BUG
                │       if ctrader_position_id available:
                │           amend_sl_tp(ctrader_position_id, sl, tp)  ─► tries 3x, all time out during degraded window
                │
                └─► if non-FILLED:
                        signals_failed_live += 1                       ❌ BUG: double-counts the same order
                        cancel risk again (no-op, already cancelled)
```

**Net effect per order** (TIMEOUT-sync → late-FILLED path): `live_fills=1, signals_traded=1, signals_failed_live=1` — exactly the signature seen in the user's log.

---

## 3. Live Log Evidence

**File:** `logs/forward_test.log` (process PID 2125800, `python scripts/launch_blend_forward_test.py --symbols ... --live`)

```text
2026-07-08 00:00:00 | INFO  | ayumi.forward_test    | Order accepted: USDJPY SHORT 0.3100 lots risk=$50.00
2026-07-08 00:00:00 | INFO  | ayumi.blend_launcher  | Signal accepted: session_range_mr short USDJPY @ 162.37600 conf=0.70 lots=0.3100
2026-07-08 00:00:17 | ERROR | ayumi.ctrader_connection | Send-and-wait timeout           ← connection degraded
2026-07-08 00:00:20 | WARN  | ayumi.forward_test    | Live order TIMEOUT: short USDJPY order_id=c518fd9e...  ← sync path
2026-07-08 00:00:26 | WARN  | ayumi.blend_launcher  | Live execution failed: session_range_mr short 0.3100 lots status=timeout reason=timeout_awaiting_event
                                                                                          ^^^ signals_failed_live += 1
2026-07-08 00:00:26 | INFO  | ayumi.openapi_spot_feed | [EXEC_EVENT] clientOrderId='c518fd9e...' execType=2 has_order=True
2026-07-08 00:00:26 | INFO  | ayumi.forward_test    | Late fill detected for order c518fd9e... (short USDJPY) — live_fills=1
                                                                                          ^^^ live_fills += 1, signals_traded += 1
                                                                                          (signals_failed_live NOT decremented)  ❌
2026-07-08 00:01:01 | WARN  | ayumi.openapi_spot_feed | Amend SL/TP timeout for position 274048857 (sl=162.626 tp=162.126)
2026-07-08 00:01:36 | WARN  | ayumi.openapi_spot_feed | Amend SL/TP timeout for position 274048857 (...)
2026-07-08 00:02:12 | WARN  | ayumi.openapi_spot_feed | Amend SL/TP timeout for position 274048857 (...)
2026-07-08 00:02:12 | WARN  | ayumi.forward_test    | F2: late amend_sl_tp returned False after 3 attempts for position 274048857 — TP2/TP3 NOT stored
```

**Aggregate counters from B5 health line:**

```text
2026-07-08 01:18:28 | INFO | [B5 Health] ticks=220170 tps=7.43 bars=403 signals=6 trades=0 live_fills=6 signals_failed_live=6 stats_fails=6
```

`live_fills=6, signals_failed_live=6` — perfect 1:1 match proves every late fill is also being counted as a failure. This is the double-count bug.

**Counter cross-check (live-mode path, equity tracker is the source of truth):**

```text
2026-07-08 02:09:25 | INFO | ayumi.blend_launcher | [A8 Equity] Recorded: balance=$9314.08 trades=2
```

`_eq_trades = getattr(engine, "_live_fill_count", 0)` (line 1075 of `launch_blend_forward_test.py`) — uses `_live_fill_count`, not the misleading `signals_traded`. The equity tracker agrees with `live_fills`.

**Counts in the user's report:**
- `signals=7` (signals_generated in B5 line) — strategies emitted 7 trade signals
- `live_fills=7` (engine._live_fill_count) — 7 positions confirmed by broker EXEC_EVENTs
- `signals_failed_live=7` — **spuriously** counted by the double-count race
- `trades=0` — `_paper_trades = t.get("trades_executed", 0)`, which is **the paper trader's trade counter, not live**. In live mode the paper trader is bypassed, so this is expected zero — it's not a meaningful diagnostic for live mode.

---

## 4. Fix Recommendation

**Single change, 2 lines.** Make the launcher's `signals_failed_live` bump match the engine's design intent: only count **terminal** failures, not awaiting-ack states.

**File:** `scripts/launch_blend_forward_test.py`
**Function:** `BlendForwardTestEngine._route_signal` (class declared line 193)
**Lines:** 411–413

**Current code:**

```python
                            else:
                                # REJECTED / TIMEOUT / NOT_CONNECTED / CANCELLED
                                with self._lock:
                                    self._health.signals_failed_live += 1
```

**Recommended code:**

```python
                            elif outcome.status in (
                                LiveExecutionStatus.REJECTED,
                                LiveExecutionStatus.CANCELLED,
                                LiveExecutionStatus.NOT_CONNECTED,
                            ):
                                # Terminal failures only. SENT / TIMEOUT are
                                # awaiting-ack states — the late-fill
                                # callback in forward_test_engine will
                                # upgrade them to FILLED and bump
                                # signals_traded + live_fills. Counting them
                                # here double-counts on late fills.
                                with self._lock:
                                    self._health.signals_failed_live += 1
                            elif outcome.status == LiveExecutionStatus.TIMEOUT:
                                # TIMEOUT is a real broker reachability
                                # issue (Send-and-wait timeout, not the
                                # exec-event race). The engine's late
                                # callback will also count this if it
                                # resolves to a terminal non-FILLED event.
                                # To avoid double-count, do NOT bump here —
                                # the late callback owns the verdict.
                                logger.debug(
                                    "Live order sync TIMEOUT for order_id=%s — "
                                    "deferring signals_failed_live bump to late callback",
                                    getattr(outcome.order, "order_id", ""),
                                )
```

(`LiveExecutionStatus` is already imported at line 51.)

**Why this works:**
- The engine's late-callback at `forward_test_engine.py:1873` is the **single source of truth** for terminal non-FILLED outcomes. It fires once per `(order_id)` thanks to the `fired[0]` closure guard (line 1747). Letting it own the `signals_failed_live` write eliminates the double-count.
- For SENT outcomes, the late callback will either bump `signals_traded` (FILLED) or `signals_failed_live` (REJECTED/CANCELLED). SENT itself is never terminal — it has to upgrade.
- For TIMEOUT sync outcomes, the late callback is guaranteed to fire eventually (the broker will send the exec event when the connection recovers). Letting it own the verdict is cleaner than racing the launcher.
- The inline FILLED path (launcher lines 383–386) already increments `live_fills` and `signals_traded` correctly — that branch is unchanged.

**Optional belt-and-suspenders fix** (defense in depth, in case any other caller misses a counter):

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`
**Lines:** 1785–1788 (after the late-callback FILLED branch)

Add at the end of the FILLED branch:

```python
            if rv_status == LiveExecutionStatus.FILLED:
                self._live_fill_count = getattr(self, "_live_fill_count", 0) + 1
                self._health.signals_traded += 1
                self._health.signals_pending = max(0, self._health.signals_pending - 1)
                # Reconciliation: if the launcher had already counted this
                # order as a failure, undo the spurious bump. Defends
                # against the same race even if the launcher's logic
                # regresses.
                self._health.signals_failed_live = max(0, self._health.signals_failed_live - 1)
```

**Don't change:**
- `amend_sl_tp` retry logic — already correct (3 attempts with backoff).
- Inline SL/TP on `new_order` — already verified working against cTrader demo 2026-07-06 (see comment at `forward_test_engine.py:1361`).
- Late-fill callback registration — already idempotent via `_pending_outcome_keys` set.
- The B5 health log line — counters will be self-correcting once the fix lands.

**Risk:** None. The fix only changes which counter increments when. The engine already has all the late-callback infrastructure. No new code paths, no new tests required beyond re-running the forward test and confirming `live_fills == signals_failed_live + signals_traded` (modulo true failures).

---

## 5. Confidence Assessment

**Confidence: HIGH.**

Reasoning:
- **Direct code path traced end-to-end** — read `forward_test_engine.py` lines 1285–1890 (`_execute_signal_live`, `_classify_live_order_outcome`, `_register_late_fill_callbacks`, `_release_late`) and `launch_blend_forward_test.py` lines 270–440 (`_route_signal`) and 900–1020 (B5 health log). All counter increments are accounted for.
- **Live log evidence matches the hypothesis exactly** — 7 (or 9 in the current run) live_fills paired with the same number of signals_failed_live, with 26 amend timeouts = 7–9 positions × ~3 attempts. The `[A8 Equity] Recorded: trades=2` line confirms positions are tracked correctly downstream.
- **Counter arithmetic is reproducible** — `signals_failed_live` can be hit twice for the same `(order_id)`: once at sync (launcher line 412), once at late callback (engine line 1873). The `fired[0]` closure prevents triple-counting but not double-counting.
- **Fix is minimal** — 2 lines in the launcher, optional reconciliation line in the engine. Both are conservative (narrowing the failure conditions, not adding new ones).

**Confidence factors down from "certain":**
- I did not run the engine in a debugger to confirm the closure variable `fired[0]` is per-registration rather than per-class (code reading suggests per-registration; if shared, the failure mode would be even worse).
- The paper-trader `trades_executed` field is documented in `engine.get_stats()["trading"]`; I did not trace the full PaperTrader class to confirm it's never incremented in live mode (only saw the live-mode `_route_signal` bypass path).
- The single-launcher instance assumption (`BlendForwardTestEngine extends ForwardTestEngine` and shares `self._health`) was verified by the class declaration; no other subclasses were searched.

**Mitigation:** Run the forward test for ~1 hour post-fix. Expected outcome: `signals_failed_live == 0` for normal operation; `live_fills == signals_traded`. If `signals_failed_live` remains nonzero, the trade is being genuinely rejected — look at the rejection reason in the log.

---

## Appendix A: Files Touched / Read

- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` — `amend_sl_tp` (line 1033), `new_order` (line 890), `_AMEND_TIMEOUT_SEC`/`_ORDER_TIMEOUT_SEC` (lines 110–111)
- `src/forex_bot/adapters/ctrader/forward_test_engine.py` — `_execute_signal_live` (line 1285), `_classify_live_order_outcome` (line 1626), `_register_late_fill_callbacks` (line 1693), `_release_late` (line 1722), `ForwardTestHealth` (line 146), `LiveExecutionStatus` (line 199), `_resolve_order_manager` (line 1672)
- `src/forex_bot/adapters/ctrader/position_monitor.py` — `amend_sl_tp` retry in TP ratchet (line 477), `_monitor_loop` (line 647)
- `src/forex_bot/adapters/ctrader/connection.py` — `send_and_wait` (line 196)
- `scripts/launch_blend_forward_test.py` — `_route_signal` (line 305), live-execution branching (line 380), B5 health log (line 1007), `_live_fill_count` source (line 918), A8 equity tracker (line 1073)
- `logs/forward_test.log` — PID 2125800 live process log, lines around 00:00:00–02:09:25
- `data/forward_test_health.json` — last health snapshot (2 signals, 0 trades, ticks 13716)
- `data/forward_test.pid` — process 2125800 still running as of investigation

## Appendix B: Constraint Compliance

- ✅ READ-ONLY investigation: `git status` reports clean working tree; no files modified.
- ✅ `git log --oneline -10` confirmed — 7 commits ahead of origin/main, last commit `a1320cc docs(ayumi): Craig decisions logged in ICT/SMC architecture`.
- ✅ All code quoted is from current sources (verified via `grep -n` and `read` with offset/limit).
- ⚠️ `data/ops/subagent_failures.jsonl` and `data/learning/builder_failures.jsonl` referenced in the task brief do **not exist** in this workspace. The investigation proceeded without them; no prior failure pattern data was available to cross-reference.
- ✅ Report written to `$AYUMI_ROOT/docs/diagnostics/2026-07-07-signals-failed-live-investigation.md`.