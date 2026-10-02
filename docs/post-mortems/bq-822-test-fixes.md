# BQ-822 Test Fixes — Post-Mortem

**Date:** 2026-07-03
**Author:** Tsukasa
**SP:** 2.0
**Status:** RESOLVED

## Problem

Three tests in `tests/integration/test_open_api_spot_feed.py` were failing:

1. `TestTokenRefresh::test_refresh_success_updates_tokens`
2. `TestExecutionEventClientMsgIdFallback::test_empty_client_order_id_falls_back_to_client_msg_id`
3. `TestExecutionEventClientMsgIdFallback::test_direct_client_order_id_match_still_works`

## Root Cause Analysis

### Failure 1: TestTokenRefresh

**Symptom:** `assert feed._access_token == "new-access"` failed; token remained `"test-access-token"`.

**Root Cause:** `getattr(mock_lifecycle, '_refresh_disabled', False)` returned a truthy `MagicMock` instance instead of `False`. MagicMock auto-creates attributes on access, so `getattr` never falls through to the default. This caused `_refresh_token_and_reauth()` to return early believing refresh was disabled.

**Fix:** Explicitly set `mock_lifecycle._refresh_disabled = False` in the test to prevent the MagicMock auto-attribute from triggering the guard.

**Lesson:** When using `getattr(obj, attr, default)` with a MagicMock, always explicitly set the attribute on the mock rather than relying on the default parameter.

### Failures 2 & 3: TestExecutionEventClientMsgIdFallback

**Symptom:** `ValueError: Unknown symbol_id: <MagicMock name='mock.order.symbolId'>`

**Root Cause:** The `_make_exec_event()` helper created a `MagicMock` for `order_payload` but never set `symbolId` to a concrete int. When `_handle_execution_event` processed the filled order, it called `VolumeCalculator.volume_to_lots(symbolId, executedVolume)` with a MagicMock key, which wasn't in `self._symbols` dict.

Additionally, the test's `setup` fixture didn't seed `self.feed._symbols` with any `SymbolInfo` entries, so even an int `symbolId` would have failed.

**Fix (test):**
1. Set `order_payload.symbolId = 1` in `_make_exec_event()`
2. Seed `self.feed._symbols[1]` with a `SymbolInfo` in the `setup` fixture

**Fix (production — defensive):** Changed `_handle_execution_event` to:
- Fall back to name-based lookup when `symbolId` is not in `self._symbols` (not just when it's `None`)
- Catch `ValueError` from `volume_to_lots()` gracefully instead of crashing the execution event handler

This makes the production code robust against execution events with unexpected or stale `symbolId` values.

## Files Modified

1. `tests/integration/test_open_api_spot_feed.py` — 3 changes (mock setup fixes)
2. `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` — defensive symbol_id handling in exec event

## Validation

```
46 passed, 7 warnings in 2.18s
```

All previously failing tests now pass. No regressions.

## Action Items

- Consider adding a conftest.py fixture that automatically sets `_refresh_disabled = False` on all MagicMock lifecycle objects to prevent this class of issue.
- Consider adding a `MagicMock` best-practices note to the test style guide.
