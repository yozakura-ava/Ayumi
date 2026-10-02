# Forward Test Reconnect Fix — Root Cause Analysis & Resolution

**Date:** 2026-05-08  
**Author:** Subagent (Ava Daigo)  
**Status:** Root cause identified; fix implemented pending token regeneration

---

## Executive Summary

The `ayumi-forward-test.service` enters a reconnect death spiral because the cTrader Open API **access token is expired**. The demo account's OAuth token has a 30-day TTL. After the token expires, the server responds with `ALREADY_LOGGED_IN` (a server-side session conflict) on reconnect attempts. The service keeps trying, auth fails, connection closes, repeat.

**The fix requires regenerating the OAuth tokens** — either via the cTrader web portal (if available) or by re-running the authorization flow.

---

## Root Cause

### The Error Chain

```
1. Token expires (TTL: ~30 days)
2. On next reconnect attempt:
   a. TCP connects fine (demo.ctraderapi.com:5035)
   b. Application auth succeeds
   c. Account auth attempt → server closes connection cleanly (ConnectionDone)
   d. ForwardTestEngine health monitor sees: connected=False, last_tick_ago=high
   e. Health monitor triggers reconnection
3. Reconnect attempt:
   a. TCP connects
   b. Application auth → ALREADY_LOGGED_IN error
   c. Timeout waiting for account auth response
   d. Connection drops
4. Dead loop: steps 2-3 repeat forever
```

### Why `ALREADY_LOGGED_IN`?

When the access token is expired/invalid, the cTrader Open API server:
1. Accepts the TCP connection (no TLS cert check on token)
2. Accepts the first message (application auth — uses `client_id`/`client_secret`, not the access token)
3. Rejects the second message (account auth — requires valid access token) with `ALREADY_LOGGED_IN` as a session-state error code
4. Closes the connection

The `_handle_error` method in `open_api_spot_feed.py` only triggers token refresh for `CH_OAUTH_TOKEN_EXPIRED` and `CH_INVALID_TOKEN`. `ALREADY_LOGGED_IN` is a **different error code** and falls through silently — the token refresh path is never triggered.

### Why No Automatic Refresh?

`OpenApiSpotFeed.__init__` only accepts `access_token` (not `refresh_token`). The `_refresh_token` attribute is never initialized, so `_refresh_token_and_reauth()` reads `self._refresh_token` which is `None` → the refresh request fails immediately with no logged error.

```python
# open_api_spot_feed.py, line ~582
"refresh_token": self._refresh_token,  # ← None! Never initialized in __init__
```

---

## What Was Changed

### Fix 1: Accept and store refresh token (open_api_spot_feed.py)

**Before:**
```python
def __init__(
    self,
    ctid_account_id: int,
    client_id: str,
    client_secret: str,
    access_token: str,
    host: str = "live.ctraderapi.com",
    port: int = 5035,
):
    ...
    self._access_token = access_token
```

**After:**
```python
def __init__(
    self,
    ctid_account_id: int,
    client_id: str,
    client_secret: str,
    access_token: str,
    refresh_token: str | None = None,
    host: str = "live.ctraderapi.com",
    port: int = 5035,
):
    ...
    self._access_token = access_token
    self._refresh_token = refresh_token  # NEW
```

**Files changed:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`

### Fix 2: Handle `ALREADY_LOGGED_IN` as a trigger for token refresh

**Before:**
```python
auth_errors = {"CH_OAUTH_TOKEN_EXPIRED", "CH_INVALID_TOKEN"}
if error_code in auth_errors:
    logger.info("Auth failure detected, attempting token refresh")
    self._refresh_token_and_reauth()
```

**After:**
```python
auth_errors = {
    "CH_OAUTH_TOKEN_EXPIRED",
    "CH_INVALID_TOKEN",
    "ALREADY_LOGGED_IN",       # ← NEW: token expired = session conflict
    "SESSION_EXPIRED",          # ← NEW: explicit session expiry
}
if error_code in auth_errors:
    logger.info("Auth failure detected (error=%s), attempting token refresh", error_code)
    self._refresh_token_and_reauth()
```

**Files changed:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`

### Fix 3: Pass refresh token through ForwardTestEngine → OpenApiSpotFeed

**Before** (forward_test_engine.py, `_start_openapi_feed`):
```python
self._market_feed = OpenApiSpotFeed(
    ctid_account_id=ctid_account_id,
    client_id=client_id,
    client_secret=client_secret,
    access_token=access_token,
    host=self._config.openapi_host,
    port=self._config.openapi_port,
)
```

**After:**
```python
refresh_token = os.environ.get("CTRADER_OPENAPI_REFRESH_TOKEN", "")
self._market_feed = OpenApiSpotFeed(
    ctid_account_id=ctid_account_id,
    client_id=client_id,
    client_secret=client_secret,
    access_token=access_token,
    refresh_token=refresh_token or None,
    host=self._config.openapi_host,
    port=self._config.openapi_port,
)
```

**Files changed:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`

### Fix 4: Add guard against None refresh_token in _refresh_token_and_reauth

**Before:**
```python
resp = requests.post(
    "https://openapi.ctrader.com/apps/token",
    data={
        "grant_type": "refresh_token",
        "refresh_token": self._refresh_token,  # ← None crashes or sends bad request
        ...
    },
    ...
)
```

**After:**
```python
if not self._refresh_token:
    logger.error("Cannot refresh token: refresh_token is not set (not stored in __init__)")
    return

resp = requests.post(
    "https://openapi.ctrader.com/apps/token",
    data={
        "grant_type": "refresh_token",
        "refresh_token": self._refresh_token,
        ...
    },
    ...
)
```

**Files changed:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`

---

## Token Regeneration (Required Before Service Will Work)

The code fix alone is **not sufficient** — the current refresh token in `.env` is also expired.

### Steps to Regenerate Tokens

1. **Via cTrader Web Portal** (if available):
   - Log into your cTrader account at `https://app.ctrader.com`
   - Navigate to Open API settings
   - Generate new tokens for your application

2. **Via the cTrader Open API Authorization Flow** (requires user interaction):
   - Construct authorization URL:
     ```
     https://openapi.ctrader.com/apps/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=YOUR_REDIRECT_URI&response_type=code&scope=trading
     ```
   - Complete authorization, exchange the code for tokens

3. **If tokens cannot be regenerated**: The service will remain in a reconnect loop. Craig should be notified that the cTrader demo account tokens have expired and need to be regenerated.

### After New Tokens Are Obtained

Update `.env`:
```bash
CTRADER_OPENAPI_ACCESS_TOKEN=<new_access_token>
CTRADER_OPENAPI_REFRESH_TOKEN=<new_refresh_token>
```

Then restart the service:
```bash
export XDG_RUNTIME_DIR=/run/user/0
systemctl --user restart ayumi-forward-test.service
```

---

## How to Troubleshoot Next Time

### Key Log Patterns

| Pattern | Meaning |
|---------|---------|
| `Application authenticated` then immediate `ConnectionDone` | Access token likely expired |
| `ALREADY_LOGGED_IN` on reconnect | Token expired, server rejected old session |
| `ALREADY_LOGGED_IN` on first connect | Client ID conflict (same creds used elsewhere) |
| `Application auth failed` (timeout) | Network issue or wrong `client_id`/`client_secret` |
| `Send-and-wait timeout` after auth | Server closed connection (expired token scenario) |

### Quick Diagnostic

```bash
# Check current token validity
source $AYUMI_ROOT/.env
curl -s -X POST https://openapi.ctrader.com/apps/token \
  -d "grant_type=refresh_token&refresh_token=$CTRADER_OPENAPI_REFRESH_TOKEN&client_id=$CTRADER_OPENAPI_CLIENT_ID&client_secret=$CTRADER_OPENAPI_CLIENT_SECRET"

# If response contains "errorCode": "ACCESS_DENIED" → token is expired, needs regeneration
```

### Health Monitor Behavior

The ForwardTestEngine health monitor runs every 10 seconds. If no ticks are received for >900 seconds (stale tick threshold), it triggers reconnection. The reconnect circuit-breaker trips after 20 consecutive failures, which would stop the engine — but before that, the OpenApiSpotFeed circuit-breaker trips at 20 attempts and sets `_running = False`.

---

## Files Changed

| File | Change |
|------|--------|
| `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` | Accept `refresh_token` in `__init__`, handle `ALREADY_LOGGED_IN` error, guard against `None` refresh token |
| `src/forex_bot/adapters/ctrader/forward_test_engine.py` | Pass `refresh_token` env var to `OpenApiSpotFeed` |

---

## Verification

After tokens are regenerated and service is restarted:

1. **Service should stay connected** for >60 seconds without reconnect attempts
2. **Log should show** tick events flowing:
   ```
   2026-05-08 xx:xx:xx | INFO     | ayumi.openapi_spot_feed | SpotEvent: symbol_id=2 raw_bid=... raw_ask=...
   ```
3. **No `ALREADY_LOGGED_IN` errors** in the logs
4. **No `Application auth failed`** messages

---

## Residual Risk

- **Token expiry still a risk**: cTrader demo tokens expire every ~30 days. A more robust solution would cache tokens to disk and refresh proactively before expiry. This fix is a defensive improvement but doesn't fully solve the recurring expiry problem.
- **Refresh token rotation**: cTrader may rotate the refresh token on each refresh. After a successful refresh, `_refresh_token` is updated in memory but not persisted. On next service restart, the old (rotated) token would be used and fail. This requires token persistence to disk.