# BQ-369: Wire Kelly Criterion into MultiStrategyBacktestEngine

## Status: PLANNING
## SP Estimate: 1 (confirmed — wiring task, function exists)

---

## Current State

- `kelly_criterion(win_rate, avg_win, avg_loss)` exists in `src/forex_bot/quant/position_sizing.py`
  - Returns Half-Kelly, capped at 50% of bankroll
  - Returns 0.0 if inputs are invalid or Kelly is negative
- `MultiStrategyBacktestEngine` uses `ConfidencePositionSizer` exclusively (confidence-tier → fixed risk %)
- `ConfidencePositionSizer` is defined in `src/forex_bot/signal_engine/risk_sizer.py`
- The engine tracks closed trades per run and computes `win_rate`, `avg_win`, `avg_loss` in `_calculate_metrics()` — **but only after the run completes**
- During a run, `_open_trade()` calls `self.risk_sizer.get_risk_amount(signal.confidence)` for position sizing

## Integration Design

### Approach: Per-strategy, rolling-window Kelly overlay

Kelly should be applied **per-strategy** during `_run_single_strategy()`, using a rolling window of recent closed trades to compute win_rate/avg_win/avg_loss. This feeds into an additive multiplier on top of the confidence-based sizing.

**Why not portfolio-level Kelly:** In `run_combined_strategies()`, the combined trade stream is a mix of strategies with different edge profiles. Per-strategy Kelly is more accurate and aligns with how `run_all_strategies()` isolates strategy results.

**Why overlay, not replace:** Confidence tiers encode signal quality. Kelly encodes historical edge. Both are informative. The overlay approach multiplies the confidence-based size by the Kelly fraction.

### Data Flow

```
Closed trades (rolling window, last N trades)
  → extract win_rate, avg_win, avg_loss
  → kelly_criterion(win_rate, avg_win, avg_loss)
  → returns fraction f (0.0 to 0.5)
  → lot_size *= f / 0.5  (normalize: full-Half-Kelly = 1.0x, zero = 0.0x)
```

**Rolling window:** Default 50 trades. Fewer than 10 trades → no Kelly adjustment (use confidence-only sizing). This avoids noisy Kelly from small samples.

### Warm-up Period

During the first 10 trades of a strategy run, there's insufficient data for Kelly. The engine falls back to pure confidence-based sizing (current behavior). This is the safe default.

---

## Scope

### Files to Change

| File | Change |
|------|--------|
| `src/forex_bot/backtest/multi_strategy_engine.py` | Add `KellyConfig` dataclass, rolling trade tracker, integrate Kelly into `_open_trade()` |
| `src/forex_bot/backtest/engine.py` | No changes (types re-exported, no modifications needed) |
| `src/forex_bot/quant/position_sizing.py` | No changes (function already correct) |
| `tests/` | New test file `tests/test_kelly_backtest_integration.py` |

### Exact Changes in `multi_strategy_engine.py`

1. **Add `KellyConfig` dataclass** (near top, after imports):
   ```python
   @dataclass
   class KellyConfig:
       enabled: bool = True
       min_trades: int = 10        # minimum trades before Kelly activates
       rolling_window: int = 50    # number of recent trades to compute stats
       max_multiplier: float = 1.5 # cap on Kelly size multiplier
   ```

2. **Add `__init__` parameter**: `kelly_config: KellyConfig | None = None`

3. **Add rolling trade history tracking**: A `list[SimulatedTrade]` field for per-strategy closed trades, populated in `_check_open_trades()` when trades close.

4. **Add `_compute_kelly_multiplier()` method**: Takes the rolling trade list, computes win_rate/avg_win/avg_loss, calls `kelly_criterion()`, returns multiplier (0.0–1.5).

5. **Modify `_open_trade()`**: After computing `lot_size` via confidence sizer, multiply by `self._compute_kelly_multiplier()` when `kelly_config.enabled` and sufficient trades exist.

6. **Wire import**: `from quant.position_sizing import kelly_criterion`

### What `run_combined_strategies()` Gets

`run_combined_strategies()` also calls `_open_trade()` and `_check_open_trades()`, so it automatically gets Kelly sizing using the combined trade stream. This is acceptable — the rolling window adapts to whichever trades flow through.

---

## Acceptance Criteria

1. **AC-1: Kelly sizing activates after warm-up.** Given 10+ closed trades, `_open_trade()` applies a Kelly multiplier to lot size. Test: run backtest with 50+ trades, assert lot sizes change from first-10 vs subsequent trades.

2. **AC-2: Kelly degrades gracefully with few trades.** With < 10 closed trades, lot size is identical to confidence-only sizing. Test: compare lot sizes with `kelly_config.enabled=True` vs `False` for first 10 trades — must match.

3. **AC-3: Kelly returns 0 → no trade.** If `kelly_criterion()` returns 0 (negative edge), the trade is skipped (lot_size will be 0, existing guard returns `None`). Test: feed a strategy with 90% loss rate, verify trades stop opening after warm-up.

4. **AC-4: Kelly multiplier is capped.** `max_multiplier` prevents oversized positions even with strong edge. Test: construct trade history with 95% win rate, verify multiplier ≤ 1.5.

5. **AC-5: Existing tests pass unchanged.** No regressions in existing backtest engine tests.

6. **AC-6: Kelly disabled by config.** `KellyConfig(enabled=False)` produces identical results to current behavior. Test: run same bars both ways, assert metrics match.

---

## Risks

| Risk | Mitigation |
|------|------------|
| **No trade history for Kelly** (start of run) | Warm-up period: min 10 trades before activating |
| **Kelly amplifies bad data** (small sample, regime change) | Rolling window (50 trades) + cap (1.5x) + min_trades gate |
| **Kelly says 0 → engine stops trading** | This is correct behavior (negative edge detected). Document as feature, not bug. |
| **Double-counting: confidence + Kelly both measure "edge"** | Overlay approach is additive, not multiplicative in the statistical sense. Confidence = signal quality, Kelly = historical performance. Different signals. |
| **Per-strategy vs combined strategy mismatch** | Both paths use `_open_trade()` which is unified. Rolling window adapts naturally. |

---

## Pre-Council Checklist

- [x] Source code read and understood
- [x] Integration point identified (`_open_trade()` in `multi_strategy_engine.py`)
- [x] Data flow mapped (closed trades → rolling stats → Kelly → multiplier)
- [x] Warm-up strategy defined (10-trade minimum)
- [x] Acceptance criteria are testable
- [x] SP estimate justified (1 SP — single file change, function exists, no new concepts)
- [x] No external dependencies needed
- [x] No schema/API changes

---

## Implementation Notes for Builder

- Import path: `from quant.position_sizing import kelly_criterion`
- The `kelly_criterion()` function already does Half-Kelly + 50% cap — don't double-cap
- Add `KellyConfig` to `__init__` with `None` default for backward compatibility
- Track closed trades in a simple list; no need for circular buffer at this scale
- Tests should use mock strategies that produce known win/loss patterns
