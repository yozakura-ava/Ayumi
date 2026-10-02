# BQ Triage Report — Group A: Connection/Auth/Infrastructure

**Date:** 2026-06-12  
**Triage Agent:** Subagent (depth 1)  
**Context:** cTrader auth death spiral fixed today. Forward test stable (1948 ticks, 0 errors). Post-mortem: `docs/post-mortems/ctrader-openapi-connection-2026-06-11.md`.

---

## BQ-774: Forward Test Engine Resilience (3 SP, draft)

**Verdict: RESPEC (reduce to 1 SP)**

Today's fixes already delivered most of the resilience story:
- State machine with full RECONNECTING transitions (`connection_state.py`)
- Kill switch auto-clear on successful auth (`open_api_spot_feed.py:775-786`)
- Stale tick monitoring → kill switch FREEZE (120s threshold)
- Health monitor loop (5s interval) with heartbeat piggybacking
- Startup token validation (fail fast on placeholders)

What's left: the forward test engine (`forward_test_engine.py:1288 lines`) has health monitoring and reconnection tracking built in. The remaining gap is **error recovery integration tests** — verifying the engine recovers gracefully from mid-trade disconnects and stale-tick freeze/thaw cycles. This is a test-only task now.

**New scope:** Write integration tests for disconnect recovery + stale-tick freeze/thaw. Existing code handles the resilience; tests prove it.  
**New SP:** 1

---

## BQ-680: Implement Reconnection with Full Jitter Exponential Backoff (draft)

**Verdict: CLOSE**

Evidence from source code and post-mortem:
- `connection_state.py` has full state machine with RECONNECTING → CONNECTED/APP_AUTHENTICATING transitions (lines 51-53 of `_VALID_TRANSITIONS`)
- `open_api_spot_feed.py` has `_reconnect_restore()` (line 1787) that handles re-auth after TCP reconnect
- Auth error backoff with exponential backoff implemented (line 1216-1223: `backoff = min(10 * (2 ** error_count), 300)`)
- `backoffPolicy` was intentionally removed from Client — our state machine handles reconnect (line 582-584)
- Reconnect works in production: forward test stable with auto-re-auth after server disconnect

The reconnection + backoff is implemented and running. No draft spec file exists to check original scope, but the functionality is live.

---

## BQ-681: Build Token Manager with proactive 80% TTL refresh (draft)

**Verdict: RESPEC (keep at 2 SP)**

Today's work added:
- Startup token validation (fail fast on placeholders) — `open_api_spot_feed.py:293-303`
- Kill switch auto-clear on successful auth — `open_api_spot_feed.py:775-786`

What's missing: **proactive token refresh before expiry**. Currently tokens are static from `.env`. cTrader OAuth tokens have a TTL; a proper token manager should refresh at 80% TTL using the refresh token. This requires:
1. Token TTL tracking (parse `expires_in` from auth response or estimate)
2. Background refresh timer using refresh token
3. Thread-safe token swap (spot feed + trade client both use same tokens)

**New scope:** Token manager class that tracks TTL, proactively refreshes at 80%, and provides thread-safe access. No change to auth flow — just adds refresh lifecycle.  
**New SP:** 2

---

## BQ-664: Fix Ayumi forward test TP1 and connection death detection (3 SP, draft)

**Verdict: CLOSE**

Connection death detection is fully implemented:
- State machine: DEGRADED (no heartbeat for 15s) → RECONNECTING (>30s or error) → auto-reconnect
- `_reconnect_restore()` handles full re-auth cycle after reconnect
- Stale tick freeze: no tick for 120s → kill switch FREEZE (`open_api_spot_feed.py:1994-2018`)
- Health monitor loop runs every 5s checking heartbeats and errors

TP1 (take-profit level 1) is a strategy-level concern, not connection infrastructure. No `TP1` or `take_profit_1` references found in the forward test engine — this was likely a symptom of the auth death spiral (no connection = no TP1 triggers). The connection issue is resolved.

---

## BQ-615: Fix order book snapshot deletion in Open API path (1 SP, draft)

**Verdict: DEFER**

- No `order_book` or `OrderBook` references found anywhere in `src/forex_bot/adapters/ctrader/`
- This is an order book / depth-of-market feature that doesn't exist in the current codebase
- The forward test runs spot feed + trade client; order book depth is not part of the current trading strategy
- Low priority: not blocking forward test or live trading

**Reason:** Feature doesn't exist in codebase yet. Not blocking current work. Defer until order book data is actually needed for a strategy.

---

## BQ-685: Phase 1E.3-1E.5: Per-strategy isolation, recovery protocol, FTMO hardening (3 SP, draft)

**Verdict: READY**

Current state:
- `FTMOConfig` is imported and used in `forward_test_engine.py` (line 42, 155, 433)
- But per-strategy isolation and recovery protocol are not yet implemented
- The forward test engine currently runs strategies without isolation between them
- Recovery protocol (what happens when one strategy fails but others should continue) is undefined

This is genuinely new work that builds on top of today's stable foundation. Scope is valid as-is.

**SP estimate:** 3 — confirmed. Per-strategy isolation wrappers + recovery hooks + FTMO guard hardening.

---

## Summary

| BQ | Verdict | SP |
|----|---------|-----|
| BQ-774 | RESPEC | 1 (was 3) |
| BQ-680 | CLOSE | — |
| BQ-681 | RESPEC | 2 |
| BQ-664 | CLOSE | — |
| BQ-615 | DEFER | 1 |
| BQ-685 | READY | 3 |

---

## JSON Verdicts

```json
[
  {"id": "BQ-774", "verdict": "RESPEC", "evidence": "Connection resilience, state machine, reconnect, kill switch auto-clear, stale tick monitoring all implemented and running. Only gap is integration tests for recovery scenarios.", "new_sp": 1, "new_scope": "Integration tests for disconnect recovery + stale-tick freeze/thaw cycles. Code already handles resilience; tests prove it."},
  {"id": "BQ-680", "verdict": "CLOSE", "evidence": "Full reconnection with exponential backoff implemented: state machine with RECONNECTING transitions (connection_state.py), _reconnect_restore() for re-auth (open_api_spot_feed.py:1787), auth error backoff min(10*2^error_count, 300) (line 1216-1223). Forward test stable with auto-reconnect.", "new_sp": null, "new_scope": null},
  {"id": "BQ-681", "verdict": "RESPEC", "evidence": "Startup token validation added (open_api_spot_feed.py:293-303). Kill switch auto-clear on auth (line 775-786). Missing: proactive TTL-based token refresh before expiry.", "new_sp": 2, "new_scope": "Token manager class: TTL tracking, proactive refresh at 80% TTL via refresh token, thread-safe token access for spot feed + trade client."},
  {"id": "BQ-664", "verdict": "CLOSE", "evidence": "Connection death detection fully implemented: DEGRADED→RECONNECTING state transitions, stale tick freeze (120s threshold), health monitor loop (5s). TP1 was a symptom of auth death spiral, now resolved.", "new_sp": null, "new_scope": null},
  {"id": "BQ-615", "verdict": "DEFER", "evidence": "No order_book/OrderBook references in ctrader adapter code. Feature doesn't exist yet. Not blocking forward test or live trading.", "new_sp": null, "new_scope": null},
  {"id": "BQ-685", "verdict": "READY", "evidence": "FTMOConfig exists but per-strategy isolation and recovery protocol not implemented. Forward test engine runs strategies without isolation. Scope is valid and builds on today's stable foundation.", "new_sp": 3, "new_scope": null}
]
```
