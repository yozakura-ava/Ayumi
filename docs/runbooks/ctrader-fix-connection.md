# cTrader FIX Connection Troubleshooting Runbook

**Created:** 2026-04-26  
**Context:** Forward test launch debugging session

---

## Architecture Overview

cTrader FIX requires **two separate sessions** with distinct credentials:

| Session | Port | SenderSubID | TargetSubID | Purpose |
|---------|------|-------------|-------------|---------|
| **Trade** | 5212 (SSL) | `TRADE` | `TRADE` | Order management |
| **Quote** | 5211 (SSL) | `QUOTE` | `QUOTE` | Market data (read-only) |

Both share: host, SenderCompID, TargetCompID, account, password.

---

## Issue 1: Missing FIX Credentials in Launcher

**File:** `scripts/launch_forward_test.py`  
**Symptom:** FIX logon fails — missing SenderCompID/TargetCompID/SenderSubID tags  
**Root cause:** `build_credentials()` omitted `sender_comp_id`, `target_comp_id`, `sender_sub_id`. The engine's internal `_build_quote_credentials()` had them, but the launcher bypassed it.  
**Fix:** Add all three fields to the launcher's `build_credentials()`.

**Quick check:** Verify launcher output dict contains `sender_comp_id`, `target_comp_id`, `sender_sub_id`.

---

## Issue 2: Market Data Feed Using Trade Credentials

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py` (~line 319)  
**Symptom:** Market data feed fails to connect or receives no ticks  
**Root cause:** `_start_market_feed()` used `self._credentials` (TRADE, port 5212) when passed externally from the launcher. Quote feed needs QUOTE credentials (port 5211).  
**Fix:** Changed to always call `self._build_quote_credentials()` regardless of passed credentials.

**Quick check:** Log market feed connection — should show port 5211 and SenderSubID=QUOTE.

---

## Issue 3: Account Disabled (RET_ACCOUNT_DISABLED)

**Symptom:** FIX logon response returns `RET_ACCOUNT_DISABLED`  
**Cause:** FTMO or demo account expired/disabled by broker  
**Resolution:** Switch to an active account. Update credentials in config.

**Current active account:** cTrader demo 5795523  
**Host:** `demo-uk-eqx-01.p.c-trader.com`

---

## Issue 4: Symbol Discovery Failures

**File:** `src/forex_bot/adapters/ctrader/symbol_discovery.py`  
**Symptom:** SecurityListRequest rejected — missing tag 559, Invalid MsgType  
**Workaround:** Hardcoded `DEFAULT_SYMBOLS` in `market_data_feed.py` (GBP/USD=2)  
**TODO:** Fix SecurityListRequest to include required tag 559 (SecurityReqID) and correct MsgType.

---

## Diagnostic Steps

### 1. Test FIX Logon
```python
# Create FIXClient with TRADE credentials, connect to port 5212
# Check logon response for errors (especially RET_ACCOUNT_DISABLED)
```

### 2. Test Quote Feed
```python
# Create LiveMarketDataFeed with QUOTE credentials (port 5211)
# Subscribe to GBP/USD, wait for ticks
```

### 3. Check Account Status
Logon response text tells you directly — `RET_ACCOUNT_DISABLED` means expired.

---

## Common Failure Modes

| Symptom | Likely Cause | Check |
|---------|-------------|-------|
| Logon rejected immediately | Missing credentials fields | Issue 1 |
| No market data ticks | Wrong port/SubID for quote feed | Issue 2 |
| RET_ACCOUNT_DISABLED | Expired/disabled account | Issue 3 |
| No symbols found | SecurityListRequest malformed | Issue 4 |
| SSL handshake failure | Wrong host or port | Verify host matches account type (demo vs live) |
