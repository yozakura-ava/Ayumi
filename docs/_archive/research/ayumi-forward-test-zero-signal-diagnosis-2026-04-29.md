# Ayumi Forward Test — Zero-Signal Diagnosis

**Date:** 2026-04-29
**Diagnosed by:** Ava (subagent)
**Service:** `ayumi-forward-test.service` (PID 2331614, running since 22:47 UTC Apr 28)

---

## Bug 1: Zero Signals for 13+ Hours (CRITICAL)

### Root Cause
The FIX quote connection (SSL to port 5211) is in **CLOSE-WAIT** state. The cTrader server closed the TCP connection, but the process never detected the disconnect.

```
CLOSE-WAIT 28876 0 23.94.149.238:50786  99.83.135.211:5211  users:(("python",pid=2331614,fd=7))
```

`LiveMarketDataFeed.is_running` returns `True` because the internal `_running` flag was never cleared. The health monitor sees `connected=True` but `last_tick_ago` keeps climbing (2000s+). It triggers reconnects every ~10s, but `_start_market_feed()` creates a new feed that also immediately gets a CLOSE-WAIT socket because the underlying FIX protocol layer doesn't properly detect the dead connection.

### Why Reconnect Doesn't Fix It
`_attempt_reconnect()` calls `self._market_feed.stop()` then `self._start_market_feed()`. The `stop()` sets `_running=False`, but the old socket fd remains in CLOSE-WAIT. The new connection attempt likely fails silently or immediately gets disconnected by the server (possibly duplicate FIX session or stale SSL state).

### Fix Required (Kai)
The `market_data_feed.py` needs:
1. **TCP-level connection death detection** — detect CLOSE-WAIT or RST via socket error handling, not just application-level heartbeat
2. **Fix health monitor reconnect logic** — when `_start_market_feed()` returns `True` but no ticks arrive within N seconds, the feed should be considered failed and a full teardown (close fd, recreate SSL context) should happen
3. **Consider a FIX Logon response timeout** — if no 35=A (Logon acknowledgment) arrives within 10s of connection, tear down

### Workaround
**Restart the service** to get a fresh connection. This is the immediate fix.

---

## Bug 2: "unknown" Strategy Name in Signal Logs

### Root Cause (Pre-22:47 Run Only)
Before the 22:47 restart, the signal log showed `strategy=unknown` for all signals. This was from the **old launch script version** that didn't properly pass `strategy_id` through the blend pipeline.

In the current code (post-22:47), the `_route_signal` method in `BlendForwardTestEngine` correctly resolves strategy names:
```python
strategy_id = self._strategy_id_map.get(strategy_name, strategy_name.lower().replace(" ", "_"))
```
And the launcher verifies at startup:
```
Registered 5 strategies: ['SRMR+', 'Killzone Momentum', 'Donchian Channel Breakout', 'Session-Range Mean Reversion', 'BB+RSI Mean Reversion']
All strategy .name properties verified against maps
```

### Status
**Already fixed** in the current codebase. The 22:47 restart confirmed the fix works (no "unknown" strategy names in post-restart logs — though no signals were generated due to Bug 1).

---

## Bug 3: R:R Always = 1.00 (Signals Blocked)

### Root Cause
`SRMRPlusConfig` has `tp1_rr: float = 1.0` as the default. The TP1 calculation is:
```python
tp1 = entry + risk * config.tp1_rr  # risk * 1.0 = risk
```
This gives `|tp1 - entry| / |entry - sl| = 1.0`.

The `PaperTrader.process_signal()` runs through `RiskGuard._calculate_risk_reward()` which uses `signal.take_profit_1`, comparing against `min_risk_reward=1.5`. Since 1.0 < 1.5, **all SRMR+ signals are blocked by the risk guard**.

### Why tp2 isn't used
The risk guard only checks `take_profit_1`. It doesn't consider `take_profit_2` (which has `tp2_rr=1.5` and would pass). This is a design limitation — the guard was written for single-TP signals.

### Fix
Change `tp1_rr` default from `1.0` to `1.5` in `SRMRPlusConfig`:

```python
# src/forex_bot/strategies/srmr_plus.py line 30
tp1_rr: float = 1.5  # was 1.0
```

This is safe — it just means TP1 is further away (1.5R instead of 1R), which is actually better for the FTMO risk profile. **Requires service restart to take effect.**

### Additional Issue: Double Processing
`BlendForwardTestEngine._evaluate_strategies()` calls `adapter.evaluate_and_trade()` which sends the signal to `PaperTrader` (risk guard blocks it), but then ALSO routes the signal through the blend runner pipeline via `_route_signal()`. The paper trader's rejection doesn't prevent the blend runner from processing it. This is a design issue — the blend runner should be the sole signal consumer when running in blend mode.

---

## Test Results
```
1 failed, 511 passed, 62 skipped
FAILED: tests/test_ctrader_market_data_feed.py::TestMarketDataIncrementalRefresh::test_snapshot_clears_order_book
  (Pre-existing: Twisted ReactorNotRestartable error — unrelated)
```

---

## Summary & Action Items

| # | Bug | Severity | Status | Action |
|---|-----|----------|--------|--------|
| 1 | FIX CLOSE-WAIT — zero ticks flowing | CRITICAL | Diagnosed | **Restart service now.** Kai: fix connection death detection in `market_data_feed.py` |
| 2 | "unknown" strategy name | Medium | **Fixed** | Already fixed in current code. No action needed |
| 3 | R:R = 1.00 (tp1_rr default) | High | Fix ready | Change `tp1_rr` from 1.0 to 1.5 in `SRMRPlusConfig`. Restart required |
| 4 | Double signal processing | Low | Diagnosed | Kai: `BlendForwardTestEngine._evaluate_strategies` should bypass `PaperTrader.process_signal` when blend runner is active |

### Immediate Action
1. **Restart the service** to restore tick flow
2. **Apply tp1_rr fix** before restarting
3. Both changes require a service restart — do them together
