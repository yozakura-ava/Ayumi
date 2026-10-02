# BQ-345: Signal Engine Bar-Close Audit

**Status:** AUDIT COMPLETE — No code changes required  
**Priority:** Medium  
**SP Estimate:** 1.5 → **Revised: 0.5** (audit + defensive hardening only)  
**Date:** 2025-06-12  

---

## Executive Summary

The audit finds that the signal engine **is already correctly gated on bar close**. The evaluation pipeline does NOT generate signals on forming (incomplete) bars. No functional bug exists.

However, there are **two defensive hardening opportunities** and one **documentation gap** worth addressing to prevent future regressions.

---

## Audit Findings

### F1: Bar-Close Gate — ✅ PASS

**Data flow (verified in source):**

```
Tick arrives
  → _on_tick() [forward_test_engine.py:665]
    → _update_current_bar() builds/updates forming bar in self._current_bar
      → If new bar period: _finalize_and_store_bar()
        → Stores finalized bar in self._bars
        → Sets self._bar_completed[key] = True
    → Evaluation gate check: has_new_bar?
      → Only True when self._bar_completed[key] is True
    → _evaluate_strategies() [line 725]
      → Builds tf_bars from self._bars ONLY (not self._current_bar)
      → Passes MarketState(bars=finalized_bars) to strategies
        → Strategy sees only completed bars
        → state.latest_bar = last CLOSED bar
```

**Key code evidence:**

1. **Evaluation trigger** (`forward_test_engine.py:711-716`): Evaluation only fires when `self._bar_completed.get(key, False)` is True, which is only set in `_finalize_and_store_bar()`.

2. **Bar data passed to strategies** (`forward_test_engine.py:748-751`):
   ```python
   bars = list(self._bars.get(key, []))  # self._bars = finalized only
   tf_bars[tf] = bars
   ```
   The forming bar (`self._current_bar`) is **never included** in the evaluation path.

3. **Completion flag consumption** (`forward_test_engine.py:720-724`): Flags are consumed (set to False) after detection, preventing duplicate evaluations.

### F2: Forming Bar Exposure — ⚠️ Minor Gap

`get_bars_for_timeframe()` (line 1254) includes the forming bar:
```python
def get_bars_for_timeframe(self, symbol, period_minutes):
    bars = list(self._bars.get(key, []))
    current = self._current_bar.get(key)
    if current is not None:
        bars.append(current)  # <-- includes forming bar
    return bars
```

This method is **not currently called in the evaluation pipeline** (grep confirms zero callers). However, it's a public API that could be mistakenly used in future code.

**Risk:** Low (no current callers), but the method signature doesn't communicate the forming-bar inclusion.

### F3: Total Bar Count Uses Forming Bar — ⚠️ Informational

Line 703-704 counts the forming bar for the min-bars threshold:
```python
current_bar = self._current_bar.get(primary_key)
total_bars = bar_count + (1 if current_bar else 0)
```

This is **correct behavior** — it's a startup gate to ensure enough data exists before evaluating. It does NOT feed the forming bar into strategy evaluation.

---

## Scope of Changes

Since no functional bug exists, the scope is **defensive hardening only**:

### Change 1: Document the bar-close invariant (0 SP)

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`  
**What:** Add a module-level or class docstring block documenting the bar-close invariant:
```
# INVARIANT: Strategy evaluation only occurs on CLOSED bars.
# - self._bars contains finalized bars only
# - self._current_bar contains the FORMING bar (excluded from evaluation)
# - Evaluation triggers ONLY when _bar_completed flag is set (bar just finalized)
# - Never pass self._current_bar into MarketState or strategy evaluation
```

### Change 2: Defensive assertion in `_evaluate_strategies` (0.25 SP)

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`  
**Function:** `_evaluate_strategies()`  
**What:** Add an assertion that `self._current_bar` for any evaluated timeframe is not included in the bar list passed to strategies. This catches future regressions:

```python
# Defensive: verify no forming bars leaked into evaluation
for tf in self._required_timeframes:
    key = self._bar_key(symbol, tf)
    forming = self._current_bar.get(key)
    if forming is not None:
        assert forming.time != tf_bars[tf][-1].time, (
            f"Bar-close invariant violated: forming bar leaked into evaluation for {key}"
        )
```

### Change 3: Rename/annotate `get_bars_for_timeframe` (0.25 SP)

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`  
**What:** Add docstring warning and/or rename to `get_bars_including_forming()`:

```python
def get_bars_for_timeframe(self, symbol: str, period_minutes: int) -> list[Bar]:
    """Get bars for a symbol+timeframe.

    WARNING: Includes the current FORMING bar (self._current_bar) as the last
    element if one exists. Do NOT use this for strategy evaluation — use
    self._bars directly instead.
    """
```

---

## Acceptance Criteria

| # | Criterion | Testable? |
|---|-----------|-----------|
| AC1 | Bar-close invariant documented in forward_test_engine.py | ✅ Grep for "INVARIANT" or "bar-close" |
| AC2 | Defensive assertion fires if forming bar leaks into evaluation | ✅ Unit test: inject forming bar, assert raises |
| AC3 | `get_bars_for_timeframe` has warning docstring about forming bar | ✅ Grep for docstring |
| AC4 | Existing test suite passes after changes | ✅ `pytest tests/ -q` green |
| AC5 | No strategy evaluation occurs without bar completion event | ✅ Integration test: verify evaluation only on `_bar_completed` |

---

## SP Estimate Justification

Original estimate was 1.5 SP based on assumption of a functional bug (signals on forming bars). The audit revealed no bug exists:

| Task | SP |
|------|----|
| Documentation (invariant comment) | 0 |
| Defensive assertion + test | 0.25 |
| Docstring annotation on public method | 0.25 |
| **Total** | **0.5** |

If the team wants the assertion test to be more thorough (mock tick pipeline, verify no evaluation until bar close), that's an additional 0.5 SP for a total of 1.0 SP.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Defensive assertion has false positive on edge case | Low | Low (just an assert) | Only triggers in dev, not production (asserts can be disabled with -O) |
| Future code breaks the invariant | Medium | High (false signals) | The assertion catches it immediately |
| Someone uses `get_bars_for_timeframe` in new strategy code | Medium | High | Docstring warns against it; assertion would catch it |

---

## Pre-Council Checklist

- [x] Read all relevant source code (forward_test_engine.py, signal_adapter.py, signal_engine/*)
- [x] Traced complete data flow from tick → bar → strategy → signal
- [x] Verified evaluation trigger mechanism (_bar_completed flags)
- [x] Verified bar data source in evaluation (self._bars only, not self._current_bar)
- [x] Checked for public API exposure of forming bar (get_bars_for_timeframe)
- [x] Confirmed no backtest_bridge usage in live evaluation path
- [x] Identified all files that would change
- [x] Acceptance criteria are specific and testable
- [x] SP estimate justified with breakdown
- [x] Risks identified with mitigations

---

## Files Examined

| File | Purpose |
|------|---------|
| `src/forex_bot/adapters/ctrader/forward_test_engine.py` | Bar building, evaluation trigger, strategy dispatch |
| `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` | Tick streaming, spot event handling |
| `src/forex_bot/adapters/ctrader/signal_adapter.py` | Strategy evaluation and signal adaptation |
| `src/forex_bot/signal_engine/__init__.py` | Signal engine exports |
| `src/forex_bot/signal_engine/data_types.py` | Signal, Level, Swing dataclasses |
| `src/forex_bot/signal_engine/gate_validator.py` | Gate validation logic |
| `src/forex_bot/signal_engine/confluence_scorer.py` | Confluence scoring |
| `src/forex_bot/signal_engine/backtest_bridge.py` | Batch signal processing bridge |
| `src/forex_bot/strategies/srmr_plus.py` | Sample strategy (evaluate method) |
| `src/forex_bot/backtest/types.py` | MarketState, Bar, StrategySignal definitions |

---

## Recommendation

**Close BQ-345 as "No Bug Found — Defensive Hardening Only."**

The bar-close gate is correctly implemented. The 0.5 SP of defensive hardening (assertion + documentation) should be tracked as a separate, lower-priority item or bundled into the next forward test maintenance sprint.
