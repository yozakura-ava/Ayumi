# Regime Cache & Precompute Window

## Problem Class

The `RegimeDetector` (in `src/forex_bot/regime/detector.py`) requires a minimum
number of bars to produce valid regime labels:

| Parameter         | Default | Purpose                        |
|-------------------|---------|--------------------------------|
| `atr_lookback`    | 50      | ATR rolling window length      |
| `adx_period`      | 14      | ADX indicator period           |
| **Minimum total** | **64**  | Sum of warmup requirements     |

In practice, we require **≥100 bars** to ensure both ATR and ADX have sufficient
warmup data beyond their bare minimums.

## The 60-Bar Bug

Several scripts historically used a 60-bar rolling window for precomputing
regime labels:

```python
# BUGGY — window too small for RegimeDetector
if i >= 60:
    w = bars[max(0, i-60):i+1]
    regime = detector.detect_current(highs, lows, closes)
```

With only 60 bars:
- ATR (lookback=50) has barely enough data, producing noisy values
- ADX (period=14) may return `NaN` or unreliable readings
- Early bars (i < 60) get `None` regime labels — silently missing
  `QUIET`/`VOLATILE` classifications

## Correct Pattern

Use a 100-bar window (or a named constant):

```python
# CORRECT — enough bars for both ATR and ADX warmup
WINDOW = 100

if i >= WINDOW:
    w = bars[max(0, i - WINDOW):i + 1]
    regime = detector.detect_current(highs, lows, closes)
```

Or using slice syntax for live detection:

```python
# CORRECT — use last 100 bars
window_bars = bars[-100:]
regime = detector.detect_current(highs, lows, closes)
```

## Affected Files (2026-07-23 Audit)

| File                                | Status     | Pattern              |
|-------------------------------------|------------|----------------------|
| `scripts/gate_loosening_study.py`   | Fixed Jul 22 | Used `WINDOW = 100` |
| `scripts/run_blend_5strat.py`       | Fixed Jul 23 | `max(0, i-60)` → `max(0, i-100)` |
| `scripts/test_lbo_gated.py`         | Fixed Jul 23 | `max(0,i-60)` → `max(0,i-100)` |
| `scripts/launch_blend_forward_test.py` | Known debt | `bars[-60:]` — filed as DEBT card |

## Regression Test

`tests/test_regime_precompute_window.py` scans all `src/` and `scripts/` Python
files for 60-bar precompute patterns and fails if any are found. This catches
reintroduction of the bug at CI time.

## Cache Invalidation

When the window size or `RegimeConfig` defaults change, all cached regime
labels (`.pkl` files under `data/cache/`) must be regenerated. The cache
filename includes a hash of `(symbol, tf, len(bars), start_time, end_time)`
but NOT the window size — so manual cache clearing is required after window
changes:

```bash
rm -f data/cache/labels_*.pkl
```
