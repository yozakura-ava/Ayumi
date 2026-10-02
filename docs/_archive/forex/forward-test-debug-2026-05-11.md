# Forward Test Debug Findings — 2026-05-11

**Status:** Partial fix applied, deeper issues remain
**Date:** 2026-05-11
**Priority:** P0 — forward test still non-functional

---

## Summary

The forward test connects to cTrader demo successfully, subscribes to spot events for GBPUSD and USDJPY, loads 5 strategies, but produces **zero signals and zero trades**.

---

## Bug #1: Missing Bar Preload (FIXED)

**Root cause:** `scripts/launch_blend_forward_test.py` fetched 100 H1 and 200 M15 historical bars from the cTrader API but never called `engine.preload_bars()` to load them into the engine. The engine started with `_bars = {}` — empty — and needed 50 completed H1 bars from live ticks before evaluation could happen.

**Fix applied:** Added preload loop between bar fetching and `engine.start()`:
```python
for sym, timeframes in symbol_bars.items():
    for period_minutes, bars in timeframes.items():
        engine.preload_bars(sym, period_minutes, bars)
        logger.info("Preloaded %d %dmin bars for %s into engine", len(bars), period_minutes, sym)
```

**Status:** Confirmed working — logs show `Preloaded 99 60min bars for GBPUSD into engine` etc.

---

## Bug #2: Evaluation Still Not Triggering (UNRESOLVED)

After the preload fix, the engine has 99 H1 bars per symbol (well above the 50-bar threshold) but still produces zero EVAL lines and zero signals.

**Symptoms:**
- Engine running at 0.9% CPU — mostly idle
- No EVAL log lines in 30+ minutes of running
- No signal, trade, or strategy evaluation output
- OpenAPI spot feed connects and subscribes successfully
- No error/exception logs

**Suspected causes (unconfirmed):**
1. **Ticks not arriving:** Despite subscription appearing successful, `_on_tick` may not be receiving callbacks. The `_on_tick` method has no INFO-level logging, so we can't confirm tick arrival without debug logging.
2. **Symbol resolution failing:** `_resolve_symbol_name()` may return `None` for all ticks if the symbol name format doesn't match. The engine subscribes by symbol_id (2 for GBPUSD, 4 for USDJPY) but resolves by name.
3. **Strategy evaluation crashing silently:** The `_evaluate_strategies` override in `BlendForwardTestEngine` may be throwing an exception that's caught and suppressed.
4. **Evaluation not being called at all:** The `_on_tick` → `_evaluate_strategies` path may have a logic issue.

**What's needed:**
- Add temporary DEBUG/INFO logging to `_on_tick` to confirm tick arrival
- Add logging at the start of `_evaluate_strategies` to confirm it's being called
- Check if `_resolve_symbol_name` is working correctly with the OpenAPI feed's symbol format
- Check if the `_live_adapter` (cTraderLiveAdapter) is properly initialized and returning results

---

## Bug #3: Duplicate Instances

**Issue:** Multiple forward test instances run simultaneously (one as root, one as $USER user). They fight for the same cTrader connection and can interfere with each other.

**Fix needed:** Single instance managed by systemd service. Script created during debug but not deployed — needs proper systemd setup.

---

## Bug #4: get_symbols AttributeError

**Log:** `'CTraderOpenApiClient' object has no attribute 'get_symbols'`

**Impact:** Non-critical — falls back to static symbol mapping. But indicates the OpenAPI client is missing a method.

---

## Bug #5: FIX Credential Warning

**Log:** `Credential validation: sender_comp_id is empty — may cause FIX logon failure`

**Impact:** Non-critical for paper trading (OpenAPI handles market data). Would be critical for live trading.

---

## Files Modified

| File | Change |
|------|--------|
| `scripts/launch_blend_forward_test.py` | Added `engine.preload_bars()` calls before `engine.start()` |

## Files NOT Modified (needs work)

| File | What needs investigation |
|------|-------------------------|
| `src/forex_bot/adapters/ctrader/forward_test_engine.py` | `_on_tick` needs debug logging |
| `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` | Verify tick callback is firing |
| `src/forex_bot/adapters/ctrader/forward_test_engine.py` | `_resolve_symbol_name` needs verification |
| `src/forex_bot/forward_test/blend_runner.py` | Verify signal routing |

---

## Historical Context

- AGENTS.md lists forward test as "BROKEN — position spam, zero signals. Kai assigned"
- Previous fix attempt (commit `17e0669` by Sage) fixed wire format bug in FIX MarketDataRequest but was never merged to main
- Commit `4bc3216` by Kai addressed cold start and heartbeat reliability but didn't fix the core signal issue
- Signal quality audit (2026-05-05) found forward test was producing EVAL lines at one point but signal quality was poor

---

## Next Steps

1. Add debug logging to `_on_tick` and `_evaluate_strategies`
2. Verify tick arrival from OpenAPI spot feed
3. Verify symbol resolution with actual tick data
4. Check if `cTraderLiveAdapter.evaluate_all_strategies()` is working
5. Set up systemd service for continuous operation
6. Full revisit and refactor needed
