# Forward Test Troubleshooting Runbook

## Symptom: Forward test starts but produces no output (no ticks, no signals)

### Layer 1: Twisted Reactor Conflict
- **Cause:** OpenAPI bar fetch starts Twisted reactor, which corrupts OpenSSL state for the FIX SSL connection
- **Check:** Look for "Twisted reactor stopped cleanly" in logs after bar fetch
- **Fix:** `_shutdown_twisted_reactor()` in launch_blend_forward_test.py stops reactor before FIX connection

### Layer 2: FIX Connection Not Establishing
- **Cause:** Multiple possible
- **Check:** Look for "MD feed logged in" and "Subscribed to" messages in logs
- **If missing:** Connection failing silently — check socket/SSL connectivity

### Layer 3: No Ticks Flowing (connection ok but no data)
- **Cause:** Order book not populated from snapshot, spread filter too strict
- **Check:** market_data_feed.py — snapshot handler must populate _order_book, spread filter uses epsilon
- **Fix:** _on_snapshot writes price-keyed entries, filter uses `best_bid > best_ask + 1e-7`

### Layer 4: Connection Drops After ~2 Minutes
- **Cause:** Sequence number race condition — heartbeat timer and main code both incrementing _next_outgoing_seq without lock
- **Check:** Look for "recv empty bytes" with running=False in disconnect log
- **Fix:** _send_message wraps seq increment in _send_lock (api_client.py)

### Layer 5: Connection Drops After ~45 Seconds
- **Cause:** No heartbeat timer — only checked on incoming messages
- **Check:** Look for heartbeat messages in logs (every 15s)
- **Fix:** _start_heartbeat_timer() runs daemon thread sending heartbeats at half HeartBtInt interval

### Layer 6: Auto-Restart Loop
- **Cause:** Health monitor detects stale ticks from disconnected feed, triggers reconnect
- **Fix:** Fixing the underlying connection issue stops the restart loop

## Key Files
- `src/forex_bot/adapters/ctrader/api_client.py` — FIX client (connection, heartbeat, send)
- `src/forex_bot/adapters/ctrader/market_data_feed.py` — market data (order book, spread filter)
- `scripts/launch_blend_forward_test.py` — entry point (reactor shutdown)
- `src/forex_bot/adapters/ctrader/forward_test_engine.py` — health monitor, reconnect

## Debugging Tips
- FIX client logger is NOT under "ayumi" hierarchy — use print() for quick debugging or configure root logger
- cTrader demo server: `demo-uk-eqx-01.p.c-trader.com:5211`
- HeartBtInt is negotiated at logon (tag 108) — default 30s
- GBPUSD typically has sub-pip spreads (0.1 pip) — spread filter must be epsilon-tolerant
