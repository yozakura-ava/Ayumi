# Ayumi Forward Test Runbook

**Last updated:** 2026-07-30 (card 64a235ea)

---

## Overview

The forward test runs the Ayumi forex_bot strategies against cTrader demo/live
accounts in real-time. This runbook covers operational procedures.

## Service Management

```bash
# Status
systemctl status ayumi-forward-test

# Restart
sudo systemctl restart ayumi-forward-test

# Reset after failure
sudo systemctl reset-failed ayumi-forward-test
sudo systemctl restart ayumi-forward-test

# View logs
journalctl -u ayumi-forward-test -f
journalctl -u ayumi-forward-test --since "1 hour ago"
```

## Token Refresh

cTrader access tokens expire every 30 days. **Auto-refresh is enabled** via
`TokenLifecycle` (`_refresh_disabled = False`).

- **Proactive:** Timer fires at 80% of token TTL → auto-refresh
- **Reactive:** `CH_ACCESS_TOKEN_INVALID` error → auto-refresh

If auto-refresh fails, see the full manual rotation procedure:

**→ [cTrader Token Rotation Runbook](ayumi-ctrader-token-rotation-runbook.md)**

## Common Issues

### Service keeps restarting (crash loop)

1. Check logs: `journalctl -u ayumi-forward-test --since "10 min ago"`
2. Common causes: token expiry, DB lock, config error
3. If token-related → see Token Rotation Runbook
4. Reset and restart: `sudo systemctl reset-failed ayumi-forward-test && sudo systemctl restart ayumi-forward-test`

### No ticks / stale data

1. Check cTrader connection state in logs
2. Verify market hours (forex is 24/5)
3. Check network connectivity to `openapi.ctrader.com`

### Auth errors in log

1. Look for `CH_ACCESS_TOKEN_INVALID` or `CH_OAUTH_TOKEN_EXPIRED`
2. Auto-refresh should handle these — if it doesn't, check `_refresh_disabled` flag
3. Escalate to manual rotation if auto-refresh fails
