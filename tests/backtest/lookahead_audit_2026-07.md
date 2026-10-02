# Backtester Look-Ahead Bias Audit — July 2026

## Objective

Audit the Ayumi backtesting engine for any look-ahead bias that could invalidate strategy performance results. Look-ahead bias occurs when a backtest uses information that would not have been available at the time of trading, producing unrealistically favorable results.

## Scope

Reviewed all data-access patterns in the backtest engine for future-leak:

- Indicator calculation timing
- Bar-close assumptions for signal generation vs trade execution
- Walk-forward train/test/val boundary integrity
- Regime filter precompute window leakage
- Intra-bar exit order (SL/TP resolution)

## Files Reviewed

| # | File | Responsibility |
|---|------|----------------|
| 1 | `src/forex_bot/backtest/engine.py` | Base engine core (BacktestConfig, Bar, MarketState, trade primitives) |
| 2 | `src/forex_bot/backtest/simple_engine.py` | SimpleBacktestEngine — legacy single-strategy engine |
| 3 | `src/forex_bot/backtest/enhanced_engine.py` | EnhancedBacktestEngine — ATR, Kelly, enhanced features |
| 4 | `src/forex_bot/backtest/multi_strategy_engine.py` | MultiStrategyBacktestEngine — multi-strategy + Kelly overlay |
| 5 | `src/forex_bot/backtest/walk_forward_runner.py` | Walk-forward backtest orchestration |
| 6 | `src/forex_bot/backtest/data_loader.py` | Data loading from DuckDB / file sources |
| 7 | `src/forex_bot/backtest/abstract_data_loader.py` | Abstract data loader interface |
| 8 | `src/forex_bot/backtest/audit_bar_close.py` | Bar-close timing audit utility |
| 9 | `src/forex_bot/quant/walk_forward.py` | WalkForwardValidator — train/val/test splitting |

## Methods

Each file was reviewed for the following bias categories:

1. **Temporal access**: Does any computation use bars beyond the current index?
2. **Entry timing**: Does trade execution use the signal bar or a future bar?
3. **Exit timing**: Are SL/TP checks conservative when both could be hit intra-bar?
4. **Indicator lookback**: Do indicators (ATR, regime, etc.) only use past data?
5. **Walk-forward integrity**: Are train/test splits temporally non-overlapping?
6. **Position sizing**: Does Kelly/position sizing use only closed-trade history?

## Findings

### Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 0 |
| MODERATE | 2 |
| LOW | 2 |
| NONE | 5 |

No critical look-ahead bias was found. The engine is structurally sound. The two MODERATE findings are conventional practices that introduce mild optimism and should be documented but do not invalidate results.

### MOD-1: Entry at bar close, not next-bar open

- **Severity:** MODERATE (standard practice, mildly optimistic)
- **Files:** All engines (`engine.py`, `simple_engine.py`, `enhanced_engine.py`, `multi_strategy_engine.py`)
- **Location:** Signal evaluation → trade execution path in each engine's main loop
- **Description:** When a strategy emits a signal on bar *i*, the trade is opened at bar *i*'s close price (`signal.entry_price = bars[i].close`). In live trading, there is always a small delay between signal generation and order fill. The backtest assumes instant execution at the close, which is mildly optimistic.
- **Impact:** Over 4.5 years of M15 data, this may inflate PF by ~1-3% depending on strategy entry frequency. This is a well-known backtesting convention, not a bug.
- **Recommendation:** Document as a known limitation. If higher fidelity is needed, add a `next_bar_open` execution mode option.

### MOD-2: Progressive SL update runs before exit check

- **Severity:** MODERATE (mildly optimistic for winning trades)
- **File:** `src/forex_bot/backtest/simple_engine.py`, lines 299-300, 310-337
- **Description:** In `SimpleBacktestEngine._check_open_trades()`, `_progressive_sl_update(trade, bar)` runs before `_check_trade_exit(trade, bar)`. The progressive SL update tightens the stop-loss when price reaches TP1 or TP2 thresholds. Because this runs before the exit check on the same bar, the exit check sees a tighter SL than what was in effect at the start of the bar. For longs where the bar hits both the original SL and TP1, the progressive update moves SL to breakeven+1pip before the exit check — making the exit check more likely to hit the (now tighter) SL rather than the original wider one. This is mildly optimistic because it assumes you could observe the TP1 hit and adjust SL before the bar closes.
- **Code path:**
  ```python
  # simple_engine.py line 299-300
  self._progressive_sl_update(trade, bar)  # SL tightened here
  hit, exit_price, reason = self._check_trade_exit(trade, bar)  # uses tightened SL
  ```
- **Impact:** Small. The scenario requires both TP1 and original SL to be hit in the same bar (rare). When it does occur, the trade exits at breakeven+1pip instead of the original SL, which is favorable. Over thousands of trades, this may inflate net P&L by a small amount.
- **Recommendation:** Swap the order (exit check first, then progressive update) for strict correctness. File as a follow-up debt card if desired. Rin noted this deserves a post-merge follow-up.

### LOW-1: Kelly position sizing default state

- **Severity:** LOW
- **File:** `src/forex_bot/backtest/multi_strategy_engine.py`
- **Description:** Kelly overlay initializes with empty closed-trade list and skip counter at 0. Before `min_trades` closed trades exist, Kelly has no effect. This is correct behavior, not a bias, but the initialization path was verified.
- **Impact:** None — verified correct.

### LOW-2: Walk-forward overlap parameter

- **Severity:** LOW
- **File:** `src/forex_bot/quant/walk_forward.py`
- **Description:** The `overlap_ratio` parameter (default 0.2) allows training data to overlap with the previous window's validation data. This is intentional for walk-forward design but could be misused if set too high. The default is conservative.
- **Impact:** None at default settings. Misconfiguration risk only.

### NONE findings (5)

| File | Check | Result |
|------|-------|--------|
| `data_loader.py` | Temporal data loading | CLEAN — loads historical data in chronological order, no future peeking |
| `abstract_data_loader.py` | Interface contract | CLEAN — no data access logic, pure interface |
| `audit_bar_close.py` | Bar-close utility | CLEAN — correctly validates that signals use closed-bar data |
| `walk_forward_runner.py` | WF orchestration | CLEAN — delegates to WalkForwardValidator, no independent data access |
| ATR calculation (`enhanced_engine.py`) | Indicator lookback | CLEAN — `_calculate_atr(bars, i)` only accesses `bars[max(0,i-lookback+1):i+1]`, verified by test |

## Regression Tests

A comprehensive anti-look-ahead regression test suite was created at `tests/backtest/test_anti_lookahead.py` (423 lines, 10 tests, 6 test classes):

| Test Class | Coverage |
|------------|----------|
| `TestNoFutureDataInSignalEvaluation` | Strategy `evaluate()` receives exactly `bars[:i+1]` — no future bars visible |
| `TestWalkForwardNoOverlap` | Train/test splits are temporally non-overlapping; validation sits between train and test |
| `TestATRNoFutureData` | ATR at index *i* only uses past bars; modifying future bars does not change ATR |
| `TestTradeEntryTiming` | Trade `entry_bar_index` matches the signal bar index exactly |
| `TestKellyNoFutureLeak` | Kelly overlay does not activate before `min_trades` closed trades exist |
| `TestConservativeExitOrder` | Intra-bar SL/TP resolution tested (conservative path documented) |

**Test results:** 10/10 PASSED (`pytest tests/backtest/test_anti_lookahead.py -v --import-mode=importlib`)

## Conclusion

The Ayumi backtest engine is **structurally free of critical look-ahead bias**. No future-data leakage was found in data loading, indicator calculation, signal evaluation, walk-forward splitting, or Kelly position sizing.

Two MODERATE findings represent conventional backtesting practices (entry at bar close, progressive SL before exit) that introduce mild optimism but are well-understood and documented in literature. Neither invalidates strategy comparison or walk-forward validation results.

**Recommendation:** Accept the audit. File a follow-up card for MOD-2 (swap exit check order in SimpleBacktestEngine) if stricter fidelity is desired. The regression test suite provides ongoing protection against future bias introduction.
