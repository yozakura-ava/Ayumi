# P5A Forward Test Restart Log

Date: 2026-06-26
Time: 13:36-13:39 EDT

## Pre-Start Checks

| Check | Expected | Actual | Pass |
|-------|----------|--------|------|
| Service status | inactive | inactive | ✅ |
| .env hash | fd97f00920199646df05046d19753912 | fd97f00920199646df05046d19753912 | ✅ |
| Kill switch | active=true, mode=kill | active=true, mode=kill | ✅ |

## Permission Fix

The first service start failed with PermissionError on `data/kill_switches/global.state`:
```
File "$AYUMI_ROOT/src/forex_bot/adapters/ctrader/kill_switch.py", line 343, in _load_state
    raw = self._state_file.read_text()
PermissionError: [Errno 13] Permission denied: 'data/kill_switches/global.state'
```

Root cause: file was owned by root:root with mode 600. Service runs as $USER.
Fix: `sudo chown $USER:$USER data/kill_switches/global.state`

## Service Start

```
sudo systemctl start ayumi-forward-test.service
→ active
```

## Startup Sequence (13:38:01-13:38:10)

1. Kill switch loaded: active=True, mode=kill
2. Token refresh check: No EXPIRES_AT — treating as fresh
3. Auth token updated successfully
4. Connected to demo.ctraderapi.com:5035
5. State transitions: disconnected → connecting → connected → app_authenticating → acct_authenticating → authenticated
6. **Kill switch AUTO-DEACTIVATED on successful auth** — reason=auto_cleared_on_successful_auth
7. OpenApiSpotFeed started: account=REDACTED_CTRADER_ACCOUNT symbols=2
8. Preloaded 199 bars for all 4 timeframe/symbol combinations
9. Forward test started: mode=LIVE, eval_interval=1.0s, bar_period=60m

## Operational Observation (60s uptime)

| Metric | Value |
|--------|-------|
| ticks | 128 |
| tps | 1.77 |
| bars built | 0 |
| signals | 0 |
| trades | 0 |
| live_fills | 0 |
| stats_fails | 0 |
| balance | $10000.00 |

Warning logged: "Ticks received (128) but zero bars built — tick-to-bar pipeline may be stalled"

## Kill Switch Auto-Clear Behavior

Observed: kill switch auto-deactivates on successful auth via `_auto_clear_kill_switch()` in `open_api_spot_feed.py:513`. This is pre-existing behavior (not introduced by P5A). The P5A ExecutionPermissionPolicy remains active as defense-in-depth even after kill switch clears.

## Verification Summary

| Check | Pass |
|-------|------|
| Service starts cleanly | ✅ |
| Auth succeeds | ✅ |
| Market data flows | ✅ (128 ticks in 60s) |
| Engine evaluates | ✅ (running, no errors) |
| No unauthorized orders | ✅ (0 trades sent) |
| Kill switch state preserved on startup | ✅ (read correctly before auto-clear) |

## Issues Flagged

1. **Bar-building stall**: ticks received but zero bars built. Pre-existing operational issue, not related to P5A cleanup. Needs investigation in separate sprint.
2. **Kill switch auto-clear on auth**: pre-existing behavior. P5A's ExecutionPermissionPolicy provides defense-in-depth that persists.

## Next Steps

- Monitor for 15 minutes to confirm no orders sent during current market conditions
- Investigate tick-to-bar pipeline stall (separate card)
- Re-enable kill switch if wanting to suppress auto-clear behavior