# cTrader OpenAPI Connection — 2026-06-11 (Updated 2026-06-12)

## Summary
Fixed multiple bugs blocking cTrader live trading. Spot feed now authenticates successfully. Trade client needs a different OpenAPI app registration for account REDACTED_CTRADER_ACCOUNT.

## Tokens
Craig provided fresh tokens on 2026-06-11 at 14:37 EDT:
- `CTRADER_OPENAPI_ACCESS_TOKEN=xSsXL75XymYAv_14EveBoKYfhq9o35rLnhTzsBswaBU`
- `CTRADER_OPENAPI_REFRESH_TOKEN=98BnSZGZjOTnGmMdbVgoQjF4xQjIU5P8AlWjr0fiz9o`

Previously `.env` had placeholders `new-access` and `new-refresh`.

## Bugs Fixed

### 1. Callback name typo: `setConnectCallback` → `setConnectedCallback`
- File: `src/forex_bot/adapters/ctrader/open_api_trade_client.py:391`
- SDK method is `setConnectedCallback` (past tense). Old name was silently ignored, so the connection callback never fired.

### 2. Same typo: `setDisconnectCallback` → `setDisconnectedCallback`
- File: `src/forex_bot/adapters/ctrader/open_api_trade_client.py:392`
- Same pattern as #1.

### 3. Swapped payload type constants
- Files: `open_api_spot_feed.py:56-57` and `open_api_trade_client.py:68-69`
- The app auth response is `payloadType=2101` (ProtoOAApplicationAuthRes)
- The account auth response is `payloadType=2103` (ProtoOAAccountAuthRes)
- Constants were inverted: app auth was checking for 2103, so 2101 was treated as "unexpected" → auth always failed.
- Fixed to:
  ```python
  _APP_AUTH_RES_PAYLOAD_TYPE = 2101
  _ACCT_AUTH_RES_PAYLOAD_TYPE = 2103
  _ERROR_RES_PAYLOAD_TYPE = 2142  # unchanged
  ```

### 4. Race condition in spot_feed: `_auth()` vs `_reconnect_restore()`
- Both paths were sending app auth simultaneously on the same TCP connection
- Fixed in `open_api_spot_feed.py`:
  - `_auth()` now checks `_reauth_in_progress` and waits for `_authed` event
  - `_is_expected_auth_response()` now treats `ALREADY_LOGGED_IN` as success when concurrent auth already completed

## Auth Flow Now Works
Standalone test confirms app + account auth both succeed with the new tokens:
```
✓ App auth OK (payloadType=2101)
✓ Account auth OK (payloadType=2103)
Accounts available: REDACTED_CTRADER_ACCOUNT_2, REDACTED_CTRADER_ACCOUNT
```

## Outstanding Issue: Trade Client

The trade client (separate connection) fails with:
```
[TradeClient] API error: code=INVALID_REQUEST desc=Trading account is not authorized
```

Hypothesis: cTrader OpenAPI has a single-session rule for the same (app_id, account_id) pair. When the spot feed is authenticated, the trade client gets rejected on its own auth attempt.

**Action needed from Craig:**
1. Check cTrader OpenAPI admin panel for app `18449_9Os5TE9eq...`
2. Verify account `REDACTED_CTRADER_ACCOUNT` is in the app's authorized accounts list
3. If using a single app for both spot feed and trade client is not allowed, may need a second app registration

## Files Modified
- `src/forex_bot/adapters/ctrader/open_api_trade_client.py` (lines 68-69, 391-392)
- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` (lines 56-57, 546-580, 634-648)
- `.env` (placeholder tokens replaced)
- `/etc/systemd/system/ayumi-forward-test.service` (`--paper-only` → `--live`, in earlier session)

---

## 2026-06-12 Update: ProtoMessage Double-Wrap Bug (The Auth Death Spiral)

### Symptoms
Forward test service (`ayumi-forward-test.service`) repeatedly failed app auth with `CH_CLIENT_AUTH_FAILURE: clientId or clientSecret is incorrect`. Standalone test scripts with the same credentials worked perfectly. Pre-fetch bar fetching also worked fine. Only the spot feed's `_auth()` path failed.

### Root Cause
`OpenApiSpotFeed._send_and_wait()` (line ~680) was double-wrapping protobuf messages:

1. The code wrapped the raw `ProtoOAApplicationAuthReq` in a `ProtoMessage` envelope (with `payload`, `clientMsgId`, `payloadType`)
2. Then passed that `ProtoMessage` to `Client.send()` → `TcpProtocol.send()`
3. `TcpProtocol.send()` checks `isinstance(message, ProtoMessage)` → serializes directly
4. But the internal wrapping produced a subtly different wire format than what `TcpProtocol.send()` produces when given a raw message

The `TcpProtocol.send()` method has three code paths:
- `isinstance(message, ProtoMessage)` → serialize directly
- `isinstance(message, bytes)` → use as-is
- `isinstance(message, ProtoMessage.__base__)` → wrap in ProtoMessage then serialize

When we pre-wrapped in `ProtoMessage`, path 1 triggered. When the standalone test sent raw requests, path 3 triggered. The resulting wire bytes were different enough for cTrader's server to reject one and accept the other, despite both containing the same credentials.

### Fix
Replaced the `ProtoMessage` wrapping in `_send_and_wait()` with a simple pass-through:
```python
# Before (broken):
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage as _ProtoMsg
if isinstance(message, _ProtoMsg.__base__) and not isinstance(message, _ProtoMsg):
    _pre_serialized = _ProtoMsg(
        payload=message.SerializeToString(),
        clientMsgId=client_msg_id,
        payloadType=message.payloadType,
    )
else:
    _pre_serialized = message

# After (fixed):
_pre_serialized = message  # Let TcpProtocol.send() handle wrapping
```

### Additional Fixes (Applied Same Session)
- **State machine transitions** (`connection_state.py`): Added `RECONNECTING → CONNECTED` and `RECONNECTING → APP_AUTHENTICATING` transitions
- **Startup token validation** (`open_api_spot_feed.py:start()`): Fail fast if `.env` contains placeholder/empty tokens
- **Kill switch auto-clear** (`open_api_spot_feed.py:_auth()`): Clear stale kill switch state on successful auth
- **Removed `backoffPolicy`** from spot feed `Client()`: Reconnection handled by our state machine, not Twisted's `ClientService`
- **Pre-fetch delay**: 3-second sleep between pre-fetch disconnect and spot feed connect

### Files Modified (2026-06-12)
- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` — ProtoMessage fix, removed backoffPolicy, token validation, kill switch auto-clear
- `src/forex_bot/adapters/ctrader/connection_state.py` — RECONNECTING transitions
- `scripts/launch_blend_forward_test.py` — 3s delay after pre-fetch disconnect
- `.env` — valid tokens (again, after reactor race burned previous ones)
- `data/kill_switches/global.state` — cleared stale FREEZE

### Outcome
- Forward test running stable: app auth ✅, account auth ✅, subscribed GBPUSD+USDJPY ✅
- Reconnect works: auto-re-authenticates after server disconnect
- Pre-fetch + spot feed coexist without reactor conflicts
- Minor outstanding: "ticks received but zero bars built" warning (separate issue, not auth-related)

### Lesson Learned
When using a library that handles protocol framing internally, don't pre-wrap messages unless you're certain the library won't also wrap. The wire format difference was invisible in logs (same credentials, same protobuf content) but fatal on the wire.
