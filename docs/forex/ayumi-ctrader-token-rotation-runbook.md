# cTrader Token Rotation Runbook

**Last updated:** 2026-07-30 (card 64a235ea)
**Owner:** Ops (Craig/Ava)
**Automation:** TokenLifecycle auto-refresh is ENABLED (`_refresh_disabled = False`)

---

## Overview

cTrader OpenAPI access tokens expire every 30 days. The `TokenLifecycle` module
handles proactive and reactive refresh automatically. This runbook covers
detection, manual rotation (fallback), and escalation.

## Architecture (how auto-refresh works)

```
TokenLifecycle._refresh_disabled = False  (DEFAULT — refresh enabled)
    │
    ├── Proactive: OpenApiSpotFeed schedules a timer at 80% of expires_in
    │   └── Calls force_refresh() → OAuth refresh → .env update → re-auth
    │
    └── Reactive: cTrader sends CH_ACCESS_TOKEN_INVALID
        └── Classified as REFRESHABLE_TOKEN_FAULT in auth_error_types.py
        └── policy.can_refresh = True
        └── _refresh_token_and_reauth() → force_refresh() → OAuth refresh
```

### Key files

| File | Role |
|---|---|
| `src/forex_bot/adapters/ctrader/token_lifecycle.py` | OAuth refresh logic, kill switch |
| `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` | Wires TokenLifecycle, triggers reactive refresh |
| `src/forex_bot/adapters/ctrader/credential_store.py` | Reads/writes tokens to `.env` |
| `src/forex_bot/adapters/ctrader/auth_error_types.py` | Classifies cTrader error codes |

---

## Detection

### Automatic detection (auto-refresh handles this)

- **Proactive timer:** Refreshes at 80% of `expires_in` (typically ~24 days).
- **Reactive refresh:** On `CH_ACCESS_TOKEN_INVALID`, `CH_OAUTH_TOKEN_EXPIRED`,
  `SESSION_EXPIRED`, or `CH_INVALID_TOKEN` error codes.

### Manual detection

```bash
# Check current token expiry
grep CTRADER_OPENAPI_TOKEN_EXPIRES_AT $AYUMI_ROOT/.env

# Check service logs for refresh activity
journalctl -u ayumi-forward-test --since "1 hour ago" | grep -i "token\|refresh"

# Check for auth errors
journalctl -u ayumi-forward-test --since "1 hour ago" | grep -i "AUTH\|INVALID_TOKEN"
```

---

## Automatic Recovery (should handle 99% of cases)

When `_refresh_disabled = False` (current default):

1. Token approaches expiry → proactive timer fires → OAuth refresh
2. Or: cTrader rejects token → `CH_ACCESS_TOKEN_INVALID` → reactive refresh
3. `force_refresh()` calls cTrader OAuth endpoint with `refresh_token`
4. New token validated against `openapi.ctrader.com/apps/metadata/account-list`
5. On success: `.env` updated atomically, service re-auths via reactor
6. On failure: structured error logged, auth circuit breaker engages after 5 failures

**No manual intervention needed unless both proactive and reactive refresh fail.**

---

## Manual Rotation (fallback when auto-refresh fails)

Use this when auto-refresh is disabled, the refresh token itself is expired,
or OAuth returns HTTP 400 (invalid grant).

### Step 1: Generate new tokens via OAuth flow

```bash
cd $AYUMI_ROOT
# Open the cTrader OAuth URL in a browser:
# https://openapi.ctrader.com/apps/auth?client_id=<CLIENT_ID>&redirect_uri=http://localhost:8080/callback&scope=trade&grant_type=authorization_code
# After consent, capture the authorization code from the redirect URL.
```

### Step 2: Exchange auth code for tokens

```bash
curl -X POST https://openapi.ctrader.com/apps/token \
  -d "grant_type=authorization_code" \
  -d "code=<AUTH_CODE>" \
  -d "client_id=<CLIENT_ID>" \
  -d "client_secret=<CLIENT_SECRET>" \
  -d "redirect_uri=http://localhost:8080/callback"
```

### Step 3: Update `.env`

```bash
# Backup current .env
cp $AYUMI_ROOT/.env $AYUMI_ROOT/.env.backup.$(date +%Y%m%d%H%M%S)

# Update token lines
sed -i "s|^CTRADER_OPENAPI_ACCESS_TOKEN=.*|CTRADER_OPENAPI_ACCESS_TOKEN=<NEW_ACCESS_TOKEN>|" $AYUMI_ROOT/.env
sed -i "s|^CTRADER_OPENAPI_REFRESH_TOKEN=.*|CTRADER_OPENAPI_REFRESH_TOKEN=<NEW_REFRESH_TOKEN>|" $AYUMI_ROOT/.env
sed -i "s|^CTRADER_OPENAPI_TOKEN_EXPIRES_AT=.*|CTRADER_OPENAPI_TOKEN_EXPIRES_AT=$(date -u -d '+30 days' +%Y-%m-%dT%H:%M:%S+00:00)|" $AYUMI_ROOT/.env
```

### Step 4: Restart the service

```bash
sudo systemctl reset-failed ayumi-forward-test
sudo systemctl restart ayumi-forward-test
```

### Step 5: Verify

```bash
# Service is running
systemctl is-active ayumi-forward-test

# No auth errors in logs
journalctl -u ayumi-forward-test --since "2 min ago" | grep -i "error\|auth\|INVALID"

# Token expiry updated
grep CTRADER_OPENAPI_TOKEN_EXPIRES_AT $AYUMI_ROOT/.env
```

---

## Escalation

| Condition | Action |
|---|---|
| Auto-refresh fails 3+ times in 24h | Check refresh token validity; may need full OAuth re-consent |
| Refresh token expired (HTTP 400 invalid_grant) | Manual rotation (Steps 1-5 above) |
| Service won't start after token update | Check `.env` format; verify `client_id`/`client_secret` are correct |
| cTrader OpenAPI downtime | Wait for recovery; service auto-retries with backoff |

---

## Incident History

| Date | Duration | Cause |
|---|---|---|
| 2026-07-05 | ~2h | Token expired, no auto-refresh |
| 2026-07-10 | ~3h | Token expired, no auto-refresh |
| 2026-07-22 | ~6h | Token expired, no auto-refresh |
| 2026-07-28 | 15.24h | Token expired, no auto-refresh |
| **Total Jul 2026** | **~26.24h** | Root cause: `_refresh_disabled=True` kill switch |

Auto-refresh was re-enabled on 2026-07-30 (card 64a235ea). Future occurrences
should be caught by the proactive timer or reactive refresh path.

---

## Kill Switch (emergency disable)

If auto-refresh causes problems (e.g., race condition, credential corruption):

```python
# In token_lifecycle.py, line 85:
_refresh_disabled: bool = True  # ← flip to disable ALL refresh
```

This is a class-level flag. All TokenLifecycle instances will skip refresh.
`OpenApiSpotFeed` will also detect this via `getattr()` and skip its
proactive/reactive refresh paths.

**Rolling back:** Set back to `False` and restart the service.
