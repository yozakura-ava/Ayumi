# BQ-687 — Per-Strategy Isolation in MultiStrategyBacktestEngine

**SP Estimate:** 1.5 | **Scope:** Refactor + tests | **Owner:** Ava (planner)
**Status:** DEFERRED to next sprint (2026-06-12)

---

## Council Findings (2026-06-12 — Kaito + Liora)

**Verdict:** REJECT (Kaito) — plan premise partially stale after BQ-369 merge

### Key Findings
1. **`_reset()` already clears Kelly state** — BQ-369 added `_kelly_closed_trades = []` and `_kelly_skips = 0` to `_reset()` (lines 263-264). The plan's claim that "Kelly trade history is not reset" is incorrect for individual runs.
2. **Real remaining gap: `run_combined_strategies()` leak** — combined run state DOES leak into individual results because the combined loop runs first, then `_run_single_strategy()` for each strategy. This is still a bug.
3. **No error containment** — any `strategy.evaluate()` exception still kills the entire `run_all_strategies()` loop. No try/except wrapping.
4. **StrategyRunState is architectural overkill at this scope** — the individual-run isolation is already functional via `_reset()`. The dataclass refactor would be cleaner but isn't addressing a live bug for individual runs.

### Recommended Rescope for Next Sprint
- **Error containment:** wrap each `strategy.evaluate()` in try/except in both `run_all_strategies()` and `run_combined_strategies()` (~0.5 SP)
- **Combined-run leak fix:** reset engine state before each `_run_single_strategy()` call in `run_combined_strategies()` (~0.5 SP)
- **StrategyRunState refactor:** optional architectural improvement, not bug-fix critical (~0.5 SP)
- **Total rescoped estimate:** 1-1.5 SP with clear prioritization

---

## 1. Problem Statement

`MultiStrategyBacktestEngine` (`src/forex_bot/backtest/multi_strategy_engine.py`) runs multiple strategies in `run_all_strategies()` and `run_combined_strategies()`. Currently, all strategies **share mutable engine state** within a run:

- `self.balance`
- `self.peak_balance`, `self.max_drawdown`
- `self.current_day`, `self.daily_start_balance`, `self.max_daily_loss`
- `self.total_spread_cost`, `self.total_commission_cost`
- `self._kelly_closed_trades`, `self._kelly_skips`

If one strategy has a catastrophic loss or bug, it can corrupt the shared balance/drawdown state, producing incorrect metrics for subsequent strategies in the same `run_all_strategies()` loop. Additionally, the `run_combined_strategies()` method calls `_run_single_strategy()` for each strategy **after** the combined run, meaning the combined run's shared state leaks into individual strategy results.

---

## 2. Current State Analysis

### 2.1 Shared vs Isolated Today

| State | Shared? | Notes |
|-------|---------|-------|
| `balance` | **Shared** | Reset per strategy via `_reset()`, but combined run leaks |
| `peak_balance` | **Shared** | Same as above |
| `max_drawdown` | **Shared** | Same as above |
| `current_day` / `daily_start_balance` | **Shared** | Daily tracking resets per strategy |
| `max_daily_loss` | **Shared** | |
| `total_spread_cost` / `total_commission_cost` | **Shared** | Accumulated across all strategies |
| `_kelly_closed_trades` / `_kelly_skips` | **Shared** | Kelly edge estimation is global, not per-strategy |
| `trades` list | **Isolated** | Local to `_run_single_strategy()` |
| `equity_curve` | **Isolated** | Local to `_run_single_strategy()` |
| `open_trades` list | **Isolated** | Local to `_run_single_strategy()` |

### 2.2 Key Bug: `run_combined_strategies()` Leak

```python
def run_combined_strategies(self, strategies, bars):
    # Combined run mutates self.balance, self.peak_balance, etc.
    for i in range(len(bars)):
        ...
    # THEN individual runs happen — but shared state is NOT reset between them
    for strategy in strategies:
        individual[strategy.name] = self._run_single_strategy(strategy, bars)
```

The combined run drains `self.balance` to some ending value. Then `_run_single_strategy()` is called for each strategy, which calls `self._reset()`. **This is correct** — but if any code path skips `_reset()`, the combined state leaks. More importantly, the `_kelly_closed_trades` list is **not** reset per strategy, so Kelly multipliers from the combined run affect individual runs.

### 2.3 Error Containment Gap

If `strategy.evaluate()` raises an exception in `run_all_strategies()`:

```python
for strategy in self.strategies:
    result = self._run_single_strategy(strategy, bars)  # exception kills entire run
```

There is no try/except — one broken strategy aborts the whole backtest.

---

## 3. Design: Isolation Boundaries

### 3.1 What SHOULD Remain Shared

- **Nothing at the engine-instance level** during execution.
- The `BacktestConfig` (immutable) and `MultiStrategyConfig` are shared by reference — fine.
- `risk_sizer` instance is shared but stateless for our purposes.
- `KellyConfig` is shared by reference.

### 3.2 What MUST Be Isolated Per-Strategy

Introduce a `StrategyRunState` dataclass that encapsulates all per-strategy mutable state:

```python
@dataclass
class StrategyRunState:
    balance: float
    peak_balance: float
    max_drawdown: float
    current_day: date | None
    daily_start_balance: float
    max_daily_loss: float
    total_spread_cost: float
    total_commission_cost: float
    kelly_closed_trades: list[SimulatedTrade]
    kelly_skips: int
```

This replaces the current pattern of `self._reset()` mutating engine-level fields.

### 3.3 Refactor Strategy

1. **Extract `_run_single_strategy()` to accept an optional `StrategyRunState`** — or create one internally.
2. **Move all balance/drawdown/commission logic into `StrategyRunState`** methods.
3. **Pass `state` through `_check_open_trades`, `_close_trade`, `_open_trade`, `_update_daily_tracking`, etc.**
4. **Reset by creating a fresh `StrategyRunState`** instead of mutating `self.*`.
5. **Wrap each strategy call in `try/except`** — log the exception, return a `StrategyBacktestResult` with `metrics=None` or empty metrics.

### 3.4 Minimal Viable Isolation (1.5 SP)

To stay within 1.5 SP, we do **not**:
- Refactor `SimpleBacktestEngine` or `EnhancedBacktestEngine`
- Change the `BacktestMetrics` dataclass
- Introduce a full strategy-runner abstraction
- Parallelize execution

We **do**:
- Isolate `run_all_strategies()` loops per-strategy
- Fix the `run_combined_strategies()` leak
- Add error containment
- Add tests verifying isolation

---

## 4. Exact Changes

### 4.1 Add `StrategyRunState` to `multi_strategy_engine.py`

```python
from dataclasses import dataclass, field
from datetime import date

@dataclass
class StrategyRunState:
    config: BacktestConfig
    balance: float = field(init=False)
    peak_balance: float = field(init=False)
    max_drawdown: float = field(init=False)
    current_day: date | None = field(init=False)
    daily_start_balance: float = field(init=False)
    max_daily_loss: float = field(init=False)
    total_spread_cost: float = field(init=False)
    total_commission_cost: float = field(init=False)
    kelly_closed_trades: list[SimulatedTrade] = field(init=False)
    kelly_skips: int = field(init=False)

    def __post_init__(self):
        self.reset()

    def reset(self):
        self.balance = self.config.starting_balance
        self.peak_balance = self.config.starting_balance
        self.max_drawdown = 0.0
        self.current_day = None
        self.daily_start_balance = self.config.starting_balance
        self.max_daily_loss = 0.0
        self.total_spread_cost = 0.0
        self.total_commission_cost = 0.0
        self.kelly_closed_trades = []
        self.kelly_skips = 0
```

### 4.2 Update `MultiStrategyBacktestEngine.__init__`

Remove mutable per-run fields from `self`:

```python
# REMOVE these from __init__:
# self.balance = config.starting_balance
# self.peak_balance = config.starting_balance
# ... etc.

# KEEP on self (shared/config):
self.config = config
self.strategies = strategies
self.multi_config = multi_config or MultiStrategyConfig()
self.risk_sizer = risk_sizer or ConfidencePositionSizer(account_size=config.starting_balance)
self._kelly_config = kelly_config or KellyConfig()
```

### 4.3 Update `run_all_strategies()`

```python
def run_all_strategies(self, bars: list[Bar]) -> dict[str, StrategyBacktestResult]:
    results = {}
    for strategy in self.strategies:
        try:
            result = self._run_single_strategy(strategy, bars)
            results[strategy.name] = result
        except Exception as exc:
            logger.warning("Strategy %s failed: %s", strategy.name, exc)
            results[strategy.name] = StrategyBacktestResult(
                strategy_name=strategy.name,
                metrics=BacktestMetrics(
                    starting_balance=self.config.starting_balance,
                    ending_balance=self.config.starting_balance,
                    total_pnl=0.0,
                    total_pnl_pct=0.0,
                    win_rate=0.0,
                    total_trades=0,
                    winning_trades=0,
                    losing_trades=0,
                    breakeven_trades=0,
                    avg_win=0.0,
                    avg_loss=0.0,
                    largest_win=0.0,
                    largest_loss=0.0,
                    profit_factor=0.0,
                    max_drawdown_pct=0.0,
                    max_drawdown_dollar=0.0,
                    max_daily_loss_dollar=0.0,
                    sharpe_ratio=0.0,
                    avg_risk_reward=0.0,
                    expectancy=0.0,
                    avg_holding_bars=0.0,
                    equity_curve=[self.config.starting_balance],
                    trades=[],
                    total_spread_cost=0.0,
                    total_commission_cost=0.0,
                    rejected_signals=0,
                ),
                last_signal=None,
            )
    return results
```

### 4.4 Update `_run_single_strategy()`

```python
def _run_single_strategy(
    self, strategy: ISignalStrategy, bars: list[Bar]
) -> StrategyBacktestResult:
    if len(bars) < self.config.min_bars_before_signal:
        raise ValueError(f"Need at least {self.config.min_bars_before_signal} bars")

    state = StrategyRunState(self.config)  # Fresh isolation
    trades: list[SimulatedTrade] = []
    equity_curve = [state.balance]
    open_trades: list[SimulatedTrade] = []
    last_signal: StrategySignal | None = None

    for i in range(len(bars)):
        bar = bars[i]
        self._update_daily_tracking(state, bar.time)

        if state.balance <= 0:
            break
        if self._is_max_drawdown_breached(state):
            break
        if self._is_max_daily_loss_breached(state):
            continue

        self._check_open_trades(state, open_trades, bar, i, trades, equity_curve)

        if (
            len(open_trades) < self.config.max_open_trades
            and i >= self.config.min_bars_before_signal
        ):
            market_state = MarketState(
                bars=bars[: i + 1], current_session=determine_session(bars[i].time)
            )
            signal = strategy.evaluate(market_state)
            if signal is not None and self._passes_filters(signal):
                trade = self._open_trade(state, signal, bar, i)
                if trade is not None:
                    open_trades.append(trade)
                    last_signal = signal

        equity_curve.append(state.balance)

    trades.extend(
        self._close_all_open_trades(
            state, open_trades, len(bars) - 1, bars[-1].time, bars[-1].close
        )
    )
    metrics = self._calculate_metrics(state, trades, equity_curve, 0)

    return StrategyBacktestResult(
        strategy_name=strategy.name, metrics=metrics, last_signal=last_signal
    )
```

### 4.5 Update Helper Methods to Accept `StrategyRunState`

The following methods must gain a `state: StrategyRunState` first parameter:

- `_update_daily_tracking(state, bar_time)`
- `_is_max_drawdown_breached(state)`
- `_is_max_daily_loss_breached(state)`
- `_check_open_trades(state, open_trades, bar, bar_index, closed_trades, equity_curve)`
- `_close_trade(state, trade, bar_index, exit_time, exit_price, reason)`
- `_close_all_open_trades(state, open_trades, bar_index, exit_time, exit_price)`
- `_open_trade(state, signal, bar, bar_index)`
- `_calculate_metrics(state, trades, equity_curve, rejected_signals)`
- `_compute_kelly_multiplier(state)` (uses `state.kelly_closed_trades`)

### 4.6 Update `run_combined_strategies()`

The combined run needs **two levels** of isolation:

1. Combined run gets its own `StrategyRunState` — independent of individual runs.
2. Individual results are generated from fresh `StrategyRunState` instances.

```python
def run_combined_strategies(
    self, strategies: list[ISignalStrategy], bars: list[Bar]
) -> tuple[dict[str, StrategyBacktestResult], BacktestMetrics]:
    # --- Combined run (isolated) ---
    combined_state = StrategyRunState(self.config)
    combined_trades: list[SimulatedTrade] = []
    combined_equity = [combined_state.balance]
    combined_open: list[SimulatedTrade] = []

    for i in range(len(bars)):
        bar = bars[i]
        self._update_daily_tracking(combined_state, bar.time)

        if combined_state.balance <= 0:
            break
        if self._is_max_drawdown_breached(combined_state):
            break
        if self._is_max_daily_loss_breached(combined_state):
            continue

        self._check_open_trades(combined_state, combined_open, bar, i, combined_trades, combined_equity)

        if (
            len(combined_open) < self.config.max_open_trades
            and i >= self.config.min_bars_before_signal
        ):
            state = MarketState(
                bars=bars[: i + 1], current_session=determine_session(bars[i].time)
            )
            signals = [s.evaluate(state) for s in strategies if s.evaluate(state)]
            # ... combine signals, open trade ...

        combined_equity.append(combined_state.balance)

    # ... close combined_open ...
    combined_metrics = self._calculate_metrics(combined_state, combined_trades, combined_equity, 0)

    # --- Individual runs (each isolated) ---
    individual = {}
    for strategy in strategies:
        try:
            individual[strategy.name] = self._run_single_strategy(strategy, bars)
        except Exception as exc:
            # Same error containment as run_all_strategies
            ...

    return (individual, combined_metrics)
```

### 4.7 Update `VAPSBacktestEngine` (subclass)

`VAPSBacktestEngine` overrides `run_all_strategies` and `_open_trade`. After refactor:

- `run_all_strategies` calls `super().run_all_strategies(bars)` — no change needed.
- `_open_trade` signature changes to `_open_trade(self, state, signal, bar, bar_index)`. Update override accordingly.

```python
def _open_trade(self, state: StrategyRunState, signal, bar: Bar, bar_index: int):
    atr = _compute_atr(self._all_bars[: bar_index + 1], self._atr_period)
    self._atr_history.append(atr)

    trade = super()._open_trade(state, signal, bar, bar_index)
    if trade is None:
        return None
    # ... VAPS multiplier logic ...
    return trade
```

### 4.8 Remove `_reset()`

`self._reset()` is no longer needed — state isolation is via fresh `StrategyRunState` instances. Remove the method entirely.

---

## 5. Tests to Add

Create `tests/test_per_strategy_isolation.py`:

### 5.1 Test: Independent Balance Tracking

```python
def test_strategies_have_independent_balances():
    """Two strategies: one always wins, one always loses.
    Each should start with 10k and end based on its own trades."""
    # Assert winner.balance > 10k, loser.balance < 10k
    # Assert winner.metrics is not affected by loser's trades
```

### 5.2 Test: Error Containment

```python
def test_broken_strategy_does_not_kill_run():
    """A strategy that raises on evaluate() should not abort others."""
    # Broken strategy returns zero-metrics result
    # Good strategy returns normal result
```

### 5.3 Test: Combined Run Isolation

```python
def test_combined_run_does_not_leak_to_individual():
    """run_combined_strategies() individual results must be isolated
    from combined run state."""
    # Combined run drains balance to ~0
    # Individual results should still show their own independent metrics
```

### 5.4 Test: Kelly Isolation

```python
def test_kelly_closed_trades_isolated_per_strategy():
    """Kelly multiplier should be computed from each strategy's own trades."""
    # Strategy A: 100% wins → Kelly multiplier = 1.0
    # Strategy B: 0% wins → Kelly multiplier = 0.0 (skips)
    # Verify B's trades are skipped, A's are not affected
```

---

## 6. Files to Touch

| File | Change |
|------|--------|
| `src/forex_bot/backtest/multi_strategy_engine.py` | Major refactor: add `StrategyRunState`, update all method signatures, remove `_reset()`, add error containment |
| `src/forex_bot/backtest/vaps_engine.py` | Update `_open_trade` override signature |
| `tests/test_per_strategy_isolation.py` | New file: 4 test cases |

---

## 7. Acceptance Criteria

- [ ] `run_all_strategies()` produces independent `BacktestMetrics` for each strategy — no shared mutable state.
- [ ] `run_combined_strategies()` produces independent individual results that are NOT affected by combined run state.
- [ ] One strategy raising an exception does not abort the entire run — other strategies still execute.
- [ ] Kelly closed-trade history is per-strategy, not global.
- [ ] All existing tests pass without modification (backward compatibility).
- [ ] New isolation tests pass.
- [ ] `pytest tests/ -q` passes in full.

---

## 8. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Large refactor touches many lines → regressions | Keep method logic identical; only add `state` parameter. Do not change trade math. |
| `EnhancedBacktestEngine` and other subclasses | Only refactor `MultiStrategyBacktestEngine`. Others untouched. |
| Performance: creating `StrategyRunState` per strategy | Negligible — dataclass with 8 floats and 2 lists. |
| `VAPSBacktestEngine._open_trade` signature mismatch | Update override to match new signature; 2-line change. |
| Backward compatibility for callers | No public API changes — `run_all_strategies()` and `run_combined_strategies()` signatures unchanged. |

---

## 9. Key Questions Answered

### What state is currently shared vs isolated?

**Shared (bug):** `balance`, `peak_balance`, `max_drawdown`, `daily tracking`, `spread/commission costs`, `Kelly history`.  
**Isolated (correct):** `trades`, `equity_curve`, `open_trades` lists are local to `_run_single_strategy()`.

### What's the minimum viable isolation for safety without over-engineering?

Introduce `StrategyRunState` to hold the 8 shared fields. Pass it through internal methods. Create a fresh instance per strategy. Add try/except in the loop. That's it — no threading, no new abstractions, no changes to `SimpleBacktestEngine`.

### Should each strategy get its own `SimulatedTrade` list and metrics dict?

**Yes**, and it already does (local variables). The gap is that the **balance/drawdown/cost state** used to compute metrics is shared. `StrategyRunState` fixes this.

---

*Plan written by Ava (subagent) for BQ-687. Ready for council review or build assignment.*
