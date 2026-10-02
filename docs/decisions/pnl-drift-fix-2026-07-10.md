# P&L Drift Fix — Root Cause Analysis

**Issue:** Ayumi forward-test daily P&L drift between engine state and cTrader broker state.
**Date:** 2026-07-10
**Branch:** `senior-dev/pnl-drift-fix` (merged as `c0c1f4f`)
**Primary fix commit:** `7a3209b` — fix(ayumi): remove P&L double-counting in RiskGuard/PaperTrader + fix daily_start_balance sync
**Status:** implemented, verified by regression tests

## Problem

Forward-test P&L was drifting away from the broker-reported balance over multi-day runs.
The drift was always positive (engine state > broker state) — a classic sign of double-counting
realised P&L on top of balance updates that already accounted for it. On mid-day restarts the
stale `daily_start_balance` was loaded into the running session, freezing the FTMO daily loss
trigger until the next calendar rollover — masking the bug for hours.

The P&L drift branch (senior-dev/pnl-drift-fix, commit `336b1eb`, merged as `c0c1f4f`) contained
code + tests; this document captures the three root causes and the reasoning behind each fix so
future maintainers (and a future regulator doing a post-mortem) can follow the audit trail
without re-deriving the analysis.

## Root A — `RiskGuard.record_trade()` double-counted realised P&L

**File:** `src/forex_bot/adapters/ctrader/risk_guard.py`

**Symptom:** Every close of a position added `pnl` a second time to `_current_balance`.

**Old logic:**
```python
def record_trade(self, pnl: float) -> None:
    self._current_balance += pnl  # WRONG — double-count
```

**Why it was wrong:** `update_balance()` / `sync_live_balance()` already account for realised P&L
when the broker reports it. Adding `pnl` again on `record_trade()` counted every closed trade
twice.

**Fix:** Replace `+=` with `=`. The authoritative balance is whichever update path last
succeeded; the broker sync wins.

```python
def record_trade(self, pnl: float) -> None:
    self._realized_pnl += pnl  # record realised
    # _current_balance is owned by update_balance() / sync_live_balance()
```

## Root B — `PaperTrader.close_position()` double-counted on close

**File:** `src/forex_bot/adapters/ctrader/paper_trader.py`

**Symptom:** On every position close, `closed_pnl` was added to `_current_balance`, stacking on
top of the value that `update_market_prices()` had just written.

**Old logic:**
```python
def close_position(self, closed_pnl: float) -> None:
    self._current_balance += closed_pnl  # WRONG — double-count
```

**Why it was wrong:** `update_market_prices()` writes
`starting_balance + realized_pnl + total_unrealized_pnl`, which already includes the position's
unrealised contribution. After the position is closed, the next `update_market_prices()` tick
recomputes from `starting_balance + realized_pnl + reduced total_unrealized` — the correct value.
Adding `closed_pnl` at close time added it AGAIN between these two writes.

**Fix:** Drop the `+=` line. `close_position()` updates realised P&L tracking but does not touch
`_current_balance`; the next `update_market_prices()` re-derives it correctly.

```python
def close_position(self, closed_pnl: float) -> None:
    self._realized_pnl += closed_pnl
    # _current_balance unchanged here; update_market_prices() recomputes next tick
```

## Root C — `_sync_live_balance()` rollover detection only fired once ever

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`

**Symptom:** Mid-day engine restarts loaded a stale `daily_start_balance` from persisted state.
The FTMO daily-loss trigger used that stale value as the baseline, suppressing alerts until the
next UTC midnight rollover — often 8+ hours later.

**Old condition:**
```python
if self._daily_trade_count == 0 and self._total_trades == 0:
    self._daily_start_balance = self._current_balance  # only fires on first ever run
```

**Why it was wrong:** Both counters are zero only on the very first invocation of the engine
in a process lifetime. Every subsequent call — including every daily restart — skipped the
rollover branch, leaving `daily_start_balance` frozen at whatever value had been persisted.

**Fix:** Check for actual day rollover, not zero trade count.

```python
if self._current_day != self._current_trading_day():
    self._daily_start_balance = self._current_balance
    self._current_day = self._current_trading_day()
```

This fires on every calendar-day boundary (and on every restart where the persisted
`_current_day` is stale), regardless of trade activity.

## Regression Tests

`tests/unit/forward_test/test_pnl_drift.py` — 8 tests added in commit `7a3209b`:

**`TestRootA_RiskGuardNoDoubleCount`** (3 tests):
1. `test_record_trade_does_not_add_pnl` — verifies balance is not incremented on trade close.
2. `test_record_trade_still_increments_counters` — verifies trade counters still move
   (realised P&L tracking is preserved; only the balance double-count is removed).
3. `test_multiple_trades_no_compounding_drift` — multi-trade scenario: 5 closes, asserts
   no compounding drift across the sequence.

**`TestRootB_PaperTraderNoDoubleCount`** (1 test):
4. `test_close_position_balance_matches_recompute` — verifies `close_position()` does not
   touch `_current_balance` and the next recompute produces the correct total without
   manual intervention.

**`TestRootC_DailyStartBalanceSync`** (3 tests):
5. `test_daily_start_resets_on_new_day` — advances the clock, asserts rollover.
6. `test_daily_start_preserved_same_day` — same day, no reset.
7. `test_first_ever_sync_sets_daily_start` — initial-sync path is preserved.

**`TestIntegration_ThreeTradeScenario`** (1 test):
8. `test_three_trade_no_drift` — 3 trades opened + closed, asserts the engine balance
   matches a hand-traced expected value across the full lifecycle. This test would have
   caught all three bugs simultaneously if any single root cause had been left unfixed.

The "3-trade scenario" test is the most important — it would have caught all three bugs
simultaneously if any single root cause had been left unfixed.

## Acceptance Criteria (this doc)

- [x] Root A documented with file path, old code, fix rationale
- [x] Root B documented with file path, old code, fix rationale
- [x] Root C documented with file path, old code, fix rationale
- [x] All 3 fix commits and 8 regression tests referenced

## Related Work

- Commit `336b1eb` (senior-dev/pnl-drift-fix branch tip) — original fix commits
- Commit `7a3209b` — primary fix (Tsukasa-authored) + test file
- Commit `c0c1f4f` — merge of pnl-drift-fix into main

## Postscript

The bug trio is a textbook example of why **state ownership must be unambiguous**. The three
layers (broker sync, paper-trader close, market-price update) each wrote to `_current_balance`
in ways that were individually plausible but collectively double-counted. The fix formalises
the invariant: `_current_balance` is the broker-reported value, period; realised P&L flows
through parallel counters (`_realized_pnl`, `_daily_pnl`) that are aggregated separately.
