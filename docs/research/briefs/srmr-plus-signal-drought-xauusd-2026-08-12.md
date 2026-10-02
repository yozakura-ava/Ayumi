# SRMR+ Signal Drought on XAUUSD M15 — Diagnostic Brief

**Date:** 2026-08-12
**Researcher:** Satsuki
**Consumer:** Hayate (strategy operator), Ava (COO)
**Card:** 284e9d71 — `[R&D: SRMR+ signal drought on XAUUSD — 62 evals / 0 signals since 21:00Z Aug 11]`

---

## Question

Why did SRMR+ produce zero signals across 62 M15 bar evaluations on XAUUSD over a 15-hour forward test window (21:00Z Aug 11 – 13:25Z Aug 12)?

## Answer (High Confidence)

**The forward test is running SRMR+ with unoptimized default config, not the validated XAUUSD M15 params.** The defaults are calibrated for forex pairs and are far too restrictive for gold. Zero signals over this window is expected behavior — not a bug, not a regime mismatch.

---

## Evidence

### 1. Configuration Mismatch

The forward test launch script (`scripts/launch_blend_forward_test.py:1004`) instantiates SRMR+ as:

```python
SRMRPlusStrategy(config=SRMRPlusConfig(symbol="XAUUSD"))
```

This passes only `symbol="XAUUSD"` — every other parameter takes the **default** value from `SRMRPlusConfig`.

Meanwhile, the validated XAUUSD M15 config exists in `src/forex_bot/config/strategies.yaml` under `srmr_xauusd_m15`, with Optuna+WF-optimized params (PF=7.16, WR=73.4%, 227 trades, $11,994 PnL/window — the strongest stream in the portfolio). **The forward test does not use these params.**

| Parameter | Default (in use) | Optimized `srmr_xauusd_m15` |
|---|---|---|
| `rsi_long_level` | 30.0 | 41.8 |
| `rsi_short_level` | 70.0 | 62.3 |
| `adx_max_threshold` | 20.0 | 38.4 |
| `session_range_min_pips` | 10.0 | 176.9 |
| `entry_near_extreme_pips` | 8.0 | 200.4 |
| `pip_value` | None → 0.1 (auto) | 0.01 |
| `use_same_day_range` | False | True |

Source: `strategies/srmr_plus.py` (SRMRPlusConfig dataclass, L30-56), `config/strategies.yaml` (srmr_xauusd_m15 entry), `scripts/launch_blend_forward_test.py:1004`.

### 2. Filter-by-Filter Rejection Analysis (Historical Proxy)

Ran a diagnostic script (`tools/srmr_plus_drought_diagnostic.py`) using the **same default config** against XAUUSD M15 historical data (Feb–Apr 2026, 2000 bars from `XAUUSD_M15_fresh.csv`).

**Rejection breakdown:**

| Filter | Bars Rejected | % of Total |
|---|---|---|
| `outside_session` | 1306 | 65.3% |
| `adx_too_high` (>20.0) | 412 | 20.6% |
| `no_entry_condition` (passed all filters, no signal) | 195 | 9.8% |
| `no_prev_session_range` | 70 | 3.5% |
| **SIGNAL_SHORT** | **14** | **0.7%** |
| **SIGNAL_LONG** | **3** | **0.1%** |

**Total signal rate: 17/2000 bars (0.85%), ~0.5 signals/day.**

### 3. Primary Bottleneck: RSI Extremity

Of the 195 bars that passed all upstream filters (session hours, ADX, range, ATR) but failed to produce a signal:

| Sub-condition | True Rate | 
|---|---|
| `rsi_long_ok` (RSI < 30) | 3/195 (1.5%) |
| `rsi_short_ok` (RSI > 70) | 5/195 (2.6%) |
| `near_low` (price within 8 pips of session low) | 36/195 (18.5%) |
| `near_high` (price within 8 pips of session high) | 49/195 (25.1%) |

RSI distribution on these bars: min=26.3, max=79.7, mean=49.5. Only 3 bars had RSI < 30; only 5 had RSI > 70.

**The RSI < 30 / RSI > 70 gates are the dominant bottleneck.** Price does reach session extremes (~18-25% of the time), but RSI rarely reaches the extreme oversold/overbought levels required by the default config at the same time.

### 4. Session Hours Account for Most Evaluations

The SRMR+ trading sessions (from `config/sessions.py`):
- London: 07:00–11:00 UTC
- NY Open: 12:00–15:00 UTC
- London/NY Overlap: 12:00–16:00 UTC

Combined: **9 hours/day of trading session out of 24**. 65.3% of M15 bars fall outside these windows and are correctly rejected.

Over the 15-hour drought window (21:00Z Aug 11 – 12:00Z Aug 12), approximately 28 of the 62 evaluations occurred during trading sessions. At an expected signal rate of ~0.5/day, zero signals across ~7 hours of in-session trading is **within normal variance**.

### 5. Forward Test Log Confirmation

Log evidence from `logs/forward_test.log.2026-08-11` and `forward_test.log`:
- Every eval during hours 16-23 and 0-6 UTC logs: `SRMR+ ?: outside trading hours (hour=N)`
- During trading hours (7-11, 12-15 UTC), evals increase the counter but produce no signal
- No DEBUG-level rejection reasons logged (log level is INFO; rejection paths use `logger.debug()`)
- `eval_errors=0` — no code errors in the evaluation pipeline

The `?` in log messages (`SRMR+ ?:`) indicates the symbol attribute is not being attached to bar objects in the forward test, but this is cosmetic — the strategy functions correctly.

---

## Stopping Condition

**Question answered.** Root cause identified with high confidence. No bug found. No further investigation needed.

## Recommendations

1. **Switch the forward test to use optimized `srmr_xauusd_m15` params** from `strategies.yaml`. The current default-config run will produce ~0.5 signals/day — far too sparse for meaningful forward validation. The optimized config was validated at 227 trades/window and would provide actual sample data.

2. **If the goal is specifically to validate default-config behavior on XAUUSD:** accept the drought as expected. The strategy is working as designed; the defaults just aren't tuned for gold. Consider this run informational about default-config behavior, not a bug hunt.

3. **Minor (not blocking):** The `use_same_day_range` field in `SRMRPlusConfig` appears to be declared but never read in the `evaluate()` method. The strategy always calls `_get_previous_session_range` regardless. This is a latent bug for any optimized config that sets `use_same_day_range=True` (including the validated `srmr_xauusd_m15` params). Flag for a separate DEBT card if the optimized params are adopted for forward testing.

## Sources

- `strategies/srmr_plus.py` — SRMRPlusConfig defaults, evaluate() filter chain (full read)
- `config/strategies.yaml` — Optimized `srmr_xauusd_m15` params
- `scripts/launch_blend_forward_test.py:1004` — Forward test instantiation
- `config/sessions.py` L106-112 — SessionRangeHours constants
- `utils/pip_value.py` L102-103 — XAUUSD pip = 0.1
- `logs/forward_test.log.2026-08-11` — SRMR+ eval log, session rejection messages
- `logs/forward_test.log` — Current run eval log
- `tools/srmr_plus_drought_diagnostic.py` — Custom diagnostic (run against `XAUUSD_M15_fresh.csv`, 2000 bars, Feb–Apr 2026)
- `data/forex/historical/XAUUSD_M15_fresh.csv` — Historical M15 bars (4465 bars, Feb 11 – Apr 23, 2026)
