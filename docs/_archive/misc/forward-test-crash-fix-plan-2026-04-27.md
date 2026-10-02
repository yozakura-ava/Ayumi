# Forward Test Crash-Loop Fix Plan — 2026-04-27

## Symptoms

- 57 restarts in one day
- Each run: fetch bars via OpenAPI → register strategies → log "STARTING MULTI-STRATEGY FORWARD TEST" → die
- `twisted._threads._ithreads.AlreadyQuit` errors in logs
- Engine never logs "Forward test started" or "Market data feed connected"
- "Engine failed to start" never logged to file

## Root Cause Analysis

### Primary Issue: FIX Market Data Connection Failure (silent)

The `ForwardTestEngine` uses `logging.getLogger(__name__)` → `adapters.ctrader.forward_test_engine`, which does NOT match the `ayumi.*` prefix configured in `logging_config.py`. All engine-level logs (connection errors, "Failed to start market data feed") go to **console only**, never to the log file. This made the actual failure invisible.

The FIX market data connection likely fails because:
1. **Sunday/Monday market hours**: Forex market closes Friday 21:55 UTC, opens Sunday 21:00 UTC. On Monday daytime EDT the market IS open, so this isn't the issue today.
2. **Quote credentials**: `CTRADER_QUOTE_SENDER_SUB_ID` is set to empty string in `.env`. The `_build_quote_credentials()` uses `os.environ.get("CTRADER_QUOTE_SENDER_SUB_ID", self._config.quote_sender_sub_id)` — since the env var IS set (to `""`), the default `"QUOTE"` is never used. Empty `sender_sub_id` may cause FIX logon rejection.

When `_start_market_feed()` returns `False`, `engine.start()` returns `False`, and the launcher calls `shutdown()` → `sys.exit(0)`.

### Secondary Issue: Twisted AlreadyQuit (cosmetic, not fatal)

The `CTraderOpenApiClient` starts a Twisted reactor thread for OpenAPI bar fetching. After fetching:
1. `client.disconnect()` calls `reactor.callFromThread(reactor.stop)` 
2. `_shutdown_twisted_reactor()` calls `reactor.stop()` again (double-stop)
3. The reactor's shutdown fires `_stopThreadPool` → `_team.quit()` → `AlreadyQuit`

This is **not** killing the process. Twisted's `Unhandled Error` is logged but not fatal by default. However, the double-stop is messy and should be cleaned up.

### Tertiary Issue: Balance Hardcoded to $100K

Two locations hardcode $100,000 instead of the correct $10,000:

| File | Line | Code |
|------|------|------|
| `scripts/launch_blend_forward_test.py` | ~340 | `"account_balance": 100_000.0` |
| `scripts/launch_blend_forward_test.py` | ~355 | `starting_balance=100_000.0` |

---

## Fix Plan

### Fix 1: Fix engine logging (critical — makes failures visible)

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`
**Line:** ~30 (top of module)

**Change:** Replace:
```python
logger = logging.getLogger(__name__)
```
With:
```python
logger = logging.getLogger("ayumi.forward_test")
```

This routes all engine logs through the `ayumi.*` namespace so they appear in the log file.

### Fix 2: Fix quote credentials env var handling

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`
**Line:** ~369-373 (inside `_build_quote_credentials`)

**Change:** Replace empty-string env vars with defaults:
```python
# BEFORE:
quote_sender_sub_id = os.environ.get(
    "CTRADER_QUOTE_SENDER_SUB_ID",
    self._config.quote_sender_sub_id,
)
quote_target_sub_id = os.environ.get(
    "CTRADER_QUOTE_TARGET_SUB_ID",
    self._config.quote_target_sub_id or quote_sender_sub_id,
)
```
```python
# AFTER:
quote_sender_sub_id = os.environ.get("CTRADER_QUOTE_SENDER_SUB_ID") or self._config.quote_sender_sub_id
_raw_target = os.environ.get("CTRADER_QUOTE_TARGET_SUB_ID")
quote_target_sub_id = _raw_target or self._config.quote_target_sub_id or quote_sender_sub_id
```

Using `or` instead of default arg means empty string falls through to the config default (`"QUOTE"`).

### Fix 3: Clean up Twisted double-stop

**File:** `scripts/launch_blend_forward_test.py`
**Line:** ~125 (inside `_shutdown_twisted_reactor`)

**Change:** Remove the redundant `reactor.stop()` call. `client.disconnect()` already calls `reactor.callFromThread(reactor.stop)`. Just wait for it:

```python
def _shutdown_twisted_reactor(timeout: float = 5.0):
    """Wait for the Twisted reactor thread to fully stop.
    
    CTraderOpenApiClient.disconnect() already calls reactor.stop() via callFromThread.
    We just need to wait for the thread to finish.
    """
    from twisted.internet import reactor
    
    # Don't call reactor.stop() here — disconnect() already did that.
    # Just wait for the reactor thread to exit.
    
    if reactor.threadpool is not None:
        try:
            reactor.threadpool.stop()
        except Exception:
            pass
    
    deadline = time.monotonic() + timeout
    while reactor.running and time.monotonic() < deadline:
        time.sleep(0.1)
    
    if reactor.running:
        logger.warning("Twisted reactor still running after %.1fs", timeout)
    else:
        logger.info("Twisted reactor stopped cleanly")
```

### Fix 4: Fix balance hardcodes

**File:** `scripts/launch_blend_forward_test.py`

**Line ~340** — in `build_blend_runner()`:
```python
# BEFORE:
"account_balance": 100_000.0,
# AFTER:
"account_balance": 10_000.0,
```

**Line ~355** — in `main()`, `ForwardTestConfig`:
```python
# BEFORE:
starting_balance=100_000.0,
# AFTER:
starting_balance=10_000.0,
```

### Fix 5: Add connection error detail to launcher

**File:** `scripts/launch_blend_forward_test.py`
**Line:** ~375 (after `engine.start()` call)

**Change:** Add explicit error logging when engine fails to start:
```python
started = engine.start()
if not started:
    logger.error("Engine failed to start — FIX connection likely rejected. Check console output for details.")
    logger.error("Verify: CTRADER_ACCOUNT, CTRADER_PASSWORD, CTRADER_HOST, CTRADER_READONLY_SSL_PORT in .env")
    blend_runner.stop()
    sys.exit(1)  # Use exit code 1, not 0
```

Also change `shutdown()` to use `sys.exit(1)` on failure instead of `sys.exit(0)`.

---

## Testing Steps

### Step 1: Verify logging fix
```bash
cd $AYUMI_ROOT
source .venv/bin/activate
python -c "
import logging, sys
sys.path.insert(0, 'src/forex_bot')
from common.logging_config import setup_logging
setup_logging('DEBUG')
logger = logging.getLogger('ayumi.forward_test')
logger.info('TEST: ayumi.forward_test logger works')
"
# Check logs/ayumi_*.log for the TEST message
```

### Step 2: Verify quote credentials
```bash
cd $AYUMI_ROOT
source .venv/bin/activate
python -c "
import os; from dotenv import load_dotenv; load_dotenv('.env')
ssid = os.environ.get('CTRADER_QUOTE_SENDER_SUB_ID') or 'QUOTE'
print(f'sender_sub_id will be: {repr(ssid)}')
"
# Should print 'QUOTE', not ''
```

### Step 3: Run the forward test
```bash
cd $AYUMI_ROOT
source .venv/bin/activate
python scripts/launch_blend_forward_test.py 2>&1 | tee /tmp/blend-test-debug.log
```

Expected after fixes:
- Engine logs "Market data feed connected" to the log file
- Engine logs "Forward test started: symbol=GBPUSD ..." to the log file  
- No `AlreadyQuit` errors
- Balance shows $10,000.00
- Process stays alive (Ctrl+C to stop)

### Step 4: Confirm >5 minute survival
Run the test and verify it stays alive. Check:
```bash
# In another terminal:
tail -f logs/ayumi_$(date +%Y-%m-%d).log | grep -E "Heartbeat|tick|signal|Forward test"
```

After 5+ minutes, you should see heartbeat logs and tick processing.

---

## Summary

| # | Issue | Severity | Fix |
|---|-------|----------|-----|
| 1 | Engine logs not going to log file | **Critical** | Change logger name to `ayumi.forward_test` |
| 2 | Empty quote sender_sub_id from env | **High** | Use `or` instead of `get()` default |
| 3 | Twisted double-stop | Low | Remove redundant `reactor.stop()` in cleanup |
| 4 | Balance $100K instead of $10K | Medium | Change two hardcoded values |
| 5 | Silent failure on engine start | Medium | Add explicit error logging, exit(1) |
