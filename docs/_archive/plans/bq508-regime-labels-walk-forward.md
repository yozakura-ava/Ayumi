# BQ-508: Regime Labels on Walk-Forward Windows

**Status:** Planned
**SP Estimate:** 2 (confirmed)
**Date:** 2026-06-12

---

## Problem

Walk-forward windows (`WindowMetrics`) track performance stats (win rate, profit factor, drawdown, etc.) but carry **zero context about the market regime** active during each window. Without regime labels, we cannot:

- Determine whether a strategy only passes in trending or low-vol regimes
- Filter or weight windows by regime confidence
- Produce regime-aware go/no-go decisions
- Feed regime-tagged data into ML feature pipelines

The regime detection code (`quant/regime.py`) is fully built and production-ready. It just isn't wired into the walk-forward pipeline.

---

## Scope

### Files to Change

| File | Change |
|------|--------|
| `src/forex_bot/quant/walk_forward.py` | Add regime fields to `WindowMetrics`; compute regime in `_compute_metrics`; add helper to extract OHLC series from bars |
| `src/forex_bot/backtest/walk_forward_runner.py` | Pass bar data to `_compute_metrics` calls in `run_strategy_walk_forward` and `run_multi_strategy_walk_forward` so regime can be computed |
| `tests/` | New test file for regime label integration |

### Files NOT Changed

| File | Reason |
|------|--------|
| `quant/regime.py` | Already complete — no changes needed |
| `backtest/engine.py` / `backtest/types.py` | `Bar` dataclass is sufficient (has OHLC + time) |

---

## Data Flow

```
Per walk-forward window:
  1. Split produces (train_bars, val_bars, test_bars)
  2. Strategy runs on test_bars → trades list
  3. test_bars → extract high[], low[], close[], time[] series
  4. Call regime detection on test_bars:
     a. volatility_regime() from ATR series (compute ATR from bars)
     b. trend_regime() from high/low/close
     c. session_regime() from bar timestamps
     d. combined_regime() → CombinedRegime
  5. Store regime fields on WindowMetrics
```

---

## Exact Fields and Types

### New Fields on `WindowMetrics`

```python
@dataclass(frozen=True)
class WindowMetrics:
    # ... existing fields unchanged ...
    window_index: int
    win_rate: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: float
    trade_count: int
    total_pnl: float
    passed_go_nogo: bool

    # NEW regime fields
    regime_volatility: str        # VolatilityRegime.value: "low"|"normal"|"high"|"extreme"
    regime_trend: str             # TrendDirection.value: "trending"|"ranging"|"neutral"
    regime_session: str           # SessionName.value: "asia"|"london"|"new_york"|"close"
    regime_volatility_percentile: float  # ATR percentile in window
    regime_confidence: float      # Combined confidence score [0.0, 1.0]
```

**Why strings, not enums?** `WindowMetrics` is frozen and serialized in reports. Using `.value` strings avoids enum import coupling across modules while keeping values human-readable and JSON-safe. Consumers can map back to enums if needed.

**Why dominant session?** Each window spans many bars across multiple sessions. The dominant session is the one with the most bars in the window. This is the representative session label.

---

## Implementation Details

### 1. ATR Computation Helper (in `walk_forward.py`)

Add a private helper to compute ATR series from bars:

```python
def _compute_atr_series(bars: list[Bar], period: int = 14) -> list[float]:
    """Compute rolling ATR values from bars."""
```

Uses the same Wilder smoothing already implemented in `trend_regime()`. Returns a list of ATR values, one per bar (padded with zeros for the initial warmup).

### 2. Dominant Session Extraction

```python
def _dominant_session(bars: list[Bar]) -> tuple[str, float]:
    """Return (session_name, vol_multiplier) for the most frequent session in bars."""
```

Counts bars per session using `session_regime(bar.time.hour, bar.time.weekday())`, returns the mode. This gives us a single representative session label per window.

### 3. Modify `_compute_metrics`

Signature change:
```python
def _compute_metrics(
    window_index: int,
    trades: list[dict[str, Any]],
    initial_balance: float = 10000.0,
    test_bars: list[Bar] | None = None,  # NEW
) -> WindowMetrics:
```

When `test_bars` is provided and non-empty:
- Extract high/low/close arrays
- Compute ATR series → `volatility_regime(atr_series)`
- Call `trend_regime(highs, lows, closes)`
- Get dominant session via `_dominant_session(test_bars)`
- Call `combined_regime(vol, trend, session)`
- Populate the 5 new fields

When `test_bars` is `None` or empty (backward compat):
- Default values: `"normal"`, `"neutral"`, `"close"`, `50.0`, `0.0`

### 4. Update Callers in `walk_forward_runner.py`

Both `run_strategy_walk_forward` and `run_multi_strategy_walk_forward`:
- Pass `test_bars=test_bars` to every `_compute_metrics()` call

Three call sites total:
1. `run_strategy_walk_forward` — early-return empty window (line ~67)
2. `run_strategy_walk_forward` — normal path (line ~103)
3. `run_multi_strategy_walk_forward` — two similar call sites (~166, ~193)

### 5. Update Callers in `walk_forward.py`

`run_strategy()` also calls `_compute_metrics` directly:
- Pass `test_bars=test` in the main loop
- Pass `test_bars=None` for the early-return empty window case

---

## Backward Compatibility

- All new fields have defaults in `_compute_metrics` when `test_bars` is `None`
- Existing callers that don't pass `test_bars` get safe defaults
- `WindowMetrics` is `frozen=True`, so all construction sites must be updated
- The `comparison_report()` function in `walk_forward.py` does NOT need changes (it doesn't display regime fields)
- Any code unpacking `WindowMetrics` positionally will break — but inspection shows all access is by attribute name, so this is safe

---

## Acceptance Criteria

| # | Criterion | Testable |
|---|-----------|----------|
| AC-1 | `WindowMetrics` has 5 new fields with correct types | `assert hasattr(m, 'regime_volatility')` etc. |
| AC-2 | `_compute_metrics(idx, [], test_bars=bars)` returns populated regime fields | Unit test with known bar data |
| AC-3 | `_compute_metrics(idx, [])` without `test_bars` returns defaults and does not raise | Unit test |
| AC-4 | `run_strategy_walk_forward` result has regime labels on every `per_window` entry | Integration test with fixture bars |
| AC-5 | Regime values are consistent with direct `regime.py` calls on same data | Cross-validation test |
| AC-6 | All existing tests pass unchanged | `pytest tests/ -q` green |
| AC-7 | No import cycles introduced | `python -c "from quant.walk_forward import WindowMetrics"` works |

---

## SP Justification

**2 SP** — this is a wiring/integration task:

- The hard algorithmic work (regime detection) is done (~0 SP)
- Changes are mechanical: add fields, compute from existing functions, pass bars through
- ~4 files touched, all changes are additive
- Test file is new but straightforward
- No design ambiguity — the plan specifies exact fields and call sites

Risks are low: frozen dataclass extension with backward-compatible defaults.

---

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| ATR warmup period insufficient for short test windows | Medium | Cap at available bars; default to `"normal"` if <14 bars |
| Dominant session is misleading for windows split across sessions | Low | Document that it's the mode, not the only session; future enhancement could add `session_distribution: dict` |
| Performance regression from regime computation per window | Low | Regime functions are O(n) on bar count; test windows are typically 500-2000 bars |
| `WindowMetrics` frozen=True requires all construction sites updated | Medium | Grep for all `WindowMetrics(` call sites — identified 6 in this plan |

---

## Out of Scope (Future Work)

- Regime-aware go/no-go weighting (use confidence to weight window importance)
- Regime-stratified performance breakdown in `comparison_report`
- ML feature pipeline integration (regime as input features)
- Per-session breakdown within a single window
- Regime stability metric (how much regime shifts within a window)
