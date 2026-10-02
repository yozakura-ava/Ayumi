# SRMR+ Parameter Audit — 2026-07-01

## Scope

Compare SRMR+ strategy parameters across three config sources for three pairs (GBPUSD, EURUSD, XAUUSD) and document all discrepancies.

## Sources

1. **Forward test config (`strategies.yaml`)** — `src/forex_bot/config/strategies.yaml`, H1 entries
2. **Paper MVP config (`paper_mvp.yaml`)** — `config/paper_mvp.yaml`, Optuna-optimized
3. **Walk-forward validated results** — `reports/srmr_plus/srmr_plus_multi_pair_M15_20260414_1856.json` (M15 only; no H1 WF report found)
4. **Strategy class defaults** — `src/forex_bot/strategies/srmr_plus.py`, `SRMRPlusConfig` dataclass

## Comparison Table

### GBPUSD H1

| Param                  | strategies.yaml | paper_mvp.yaml | WF Report | SRMRPlusConfig default |
|------------------------|-----------------|----------------|-----------|------------------------|
| `min_confidence`       | 0.40            | — (not in paper MVP for GBPUSD) | N/A | N/A (set by caller) |
| `session_range_min_pips` | 25.0          | —              | N/A       | 15.0                  |
| `entry_near_extreme_pips` | 15.0          | —              | N/A       | 15.0                  |
| `hard_cap_sl_pips`     | 25.0            | —              | N/A       | 25.0                  |
| `tp1_rr`               | 1.0             | —              | N/A       | 1.5                   |
| `tp2_rr`               | 1.5             | —              | N/A       | 1.5                   |
| `pip_value`            | (not set)       | —              | N/A       | None (auto-detect)    |
| `cooldown_sec`         | (not set)       | —              | N/A       | N/A (caller-managed)  |

### EURUSD H1

| Param                  | strategies.yaml | paper_mvp.yaml | WF Report | SRMRPlusConfig default |
|------------------------|-----------------|----------------|-----------|------------------------|
| `min_confidence`       | 0.40            | — (not in paper MVP for EURUSD) | N/A | N/A |
| `session_range_min_pips` | 15.0          | —              | N/A       | 15.0                  |
| `entry_near_extreme_pips` | 12.0          | —              | N/A       | 15.0                  |
| `hard_cap_sl_pips`     | 20.0            | —              | N/A       | 25.0                  |
| `tp1_rr`               | 1.0             | —              | N/A       | 1.5                   |
| `tp2_rr`               | 1.5             | —              | N/A       | 1.5                   |
| `pip_value`            | (not set)       | —              | N/A       | None                  |
| `cooldown_sec`         | (not set)       | —              | N/A       | N/A                   |

### XAUUSD H1

| Param                  | strategies.yaml | paper_mvp.yaml (M5 Optuna) | paper_mvp.yaml (H1 original) | WF Report (M15) | SRMRPlusConfig default |
|------------------------|-----------------|-----------------------------|------------------------------|-----------------|------------------------|
| `min_confidence`       | 0.40            | 0.55                        | 0.55                         | N/A             | N/A                    |
| `session_range_min_pips` | 200.0         | 23.0                        | 15.0 (default)               | N/A             | 15.0                   |
| `entry_near_extreme_pips` | 150.0         | 18.0                        | 15.0 (default)               | N/A             | 15.0                   |
| `hard_cap_sl_pips`     | 300.0           | 25.0                        | 25.0 (default)               | N/A             | 25.0                   |
| `tp1_rr`               | 1.0             | 0.9                         | 1.5 (default)                | N/A             | 1.5                    |
| `tp2_rr`               | 1.5             | 1.5                         | 1.5 (default)                | N/A             | 1.5                   |
| `pip_value`            | 0.01            | (not set)                   | (not set)                    | N/A             | None                   |
| `cooldown_sec`         | (not set)       | 60                          | 300                          | N/A             | N/A                    |

## Walk-Forward Report Coverage

**File found:** `reports/srmr_plus/srmr_plus_multi_pair_M15_20260414_1856.json`

| Pair    | Timeframe | Go/No-Go | Windows Passed |
|---------|-----------|----------|----------------|
| EURUSD  | M15       | ❌ FAIL  | 0/5            |
| USDJPY  | M15       | ❌ FAIL  | 1/5            |
| XAUUSD  | M15       | ✅ PASS  | 5/5            |

**Critical gap:** No H1 walk-forward validation report exists. The forward test uses H1 strategies, but WF validation was only done on M15. The WF report does not contain strategy parameters — only performance metrics (win rate, PF, drawdown, Sharpe). No parameter sets can be extracted from the WF report.

The `paper_mvp.yaml` comments claim "5/5 WF pass" for XAUUSD at M5, H1, and H4 timeframes, but no corresponding H1 or H4 WF report files were found in `reports/walk_forward/` or `reports/srmr_plus/`.

## DISCREPANCIES

### 1. GBPUSD H1 — strategies.yaml vs SRMRPlusConfig defaults

| Param                    | strategies.yaml | Default   | Delta |
|--------------------------|-----------------|-----------|-------|
| `session_range_min_pips` | 25.0            | 15.0      | +10.0 (strategies.yaml is tighter) |
| `tp1_rr`                 | 1.0             | 1.5       | -0.5  |

**Impact:** `tp1_rr=1.0` means TP1 is placed at 1:1 risk/reward, lower than the class default of 1.5. This affects exit strategy and overall profitability profile.

### 2. EURUSD H1 — strategies.yaml vs SRMRPlusConfig defaults

| Param                      | strategies.yaml | Default | Delta |
|----------------------------|-----------------|---------|-------|
| `entry_near_extreme_pips`  | 12.0            | 15.0    | -3.0 (strategies.yaml is looser) |
| `hard_cap_sl_pips`         | 20.0            | 25.0    | -5.0 (tighter SL) |
| `tp1_rr`                   | 1.0             | 1.5     | -0.5  |

**Impact:** EURUSD has both a tighter SL and lower TP1 than defaults. This creates a more aggressive risk profile with earlier stops and earlier profit-taking.

### 3. XAUUSD H1 — strategies.yaml vs paper_mvp.yaml (M5 Optuna)

| Param                      | strategies.yaml (H1) | paper_mvp (M5 Optuna) | Delta |
|----------------------------|----------------------|-----------------------|-------|
| `min_confidence`           | 0.40                 | 0.55                  | -0.15 |
| `session_range_min_pips`   | 200.0                | 23.0                  | +177.0 |
| `entry_near_extreme_pips`  | 150.0                | 18.0                  | +132.0 |
| `hard_cap_sl_pips`         | 300.0                | 25.0                  | +275.0 |
| `tp1_rr`                   | 1.0                  | 0.9                   | +0.1  |

**Impact:** The XAUUSD H1 params in strategies.yaml are dramatically wider than the Optuna-optimized M5 params (200 pips session range vs 23, 300 pip SL vs 25). However, this is expected — XAUUSD H1 bars have much larger ranges than M5 bars. The comparison is across timeframes so the delta is not necessarily a bug.

### 4. XAUUSD H1 — strategies.yaml vs paper_mvp.yaml (H1 original)

| Param                      | strategies.yaml (H1) | paper_mvp (H1, params: null → defaults) | Delta |
|----------------------------|----------------------|-----------------------------------------|-------|
| `min_confidence`           | 0.40                 | 0.55                                    | -0.15 |
| `session_range_min_pips`   | 200.0                | 15.0 (default)                          | +185.0 |
| `entry_near_extreme_pips`  | 150.0                | 15.0 (default)                          | +135.0 |
| `hard_cap_sl_pips`         | 300.0                | 25.0 (default)                          | +275.0 |
| `tp1_rr`                   | 1.0                  | 1.5 (default)                           | -0.5  |

**Impact:** MAJOR DISCREPANCY. The paper_mvp.yaml H1 config uses `params: null` (all class defaults), but strategies.yaml uses massively wider params for XAUUSD. The session_range_min_pips is 13× wider, entry_near_extreme is 10× wider, and hard_cap_sl is 12× wider. The forward test will behave fundamentally differently from the paper MVP for XAUUSD.

### 5. XAUUSD H1 — strategies.yaml min_confidence vs paper_mvp.yaml

| Source             | min_confidence |
|--------------------|----------------|
| strategies.yaml    | 0.40           |
| paper_mvp.yaml     | 0.55           |

**Impact:** Forward test uses a lower confidence threshold (0.40 vs 0.55). This means more signals will pass the filter, increasing trade frequency but potentially lowering quality.

### 6. No H1 walk-forward validation exists

The forward test runs H1 strategies, but the only WF report found is M15. The paper_mvp.yaml comments claim H1 and H4 WF passes, but no supporting report files exist. This means the H1 parameters in strategies.yaml have NOT been walk-forward validated.

### 7. `tp1_rr` systematically lower in strategies.yaml

Both GBPUSD and EURUSD use `tp1_rr: 1.0` in strategies.yaml, while the SRMRPlusConfig default is `1.5`. The comment in the source code says `tp1_rr` "was 1.0; raised to 1.5 to pass min_risk_reward=1.5 gate" — meaning the default was intentionally raised, but strategies.yaml still uses the old 1.0 value.

### 8. Missing params in strategies.yaml

strategies.yaml does not set `cooldown_sec`, `atr_period`, `rsi_period`, `rsi_long_level`, `rsi_short_level`, `adx_max_threshold`, or `ema_trend_period`. These all fall back to class defaults. paper_mvp.yaml explicitly sets some of these (e.g., `atr_period: 15`, `rsi_period: 13` for the M5 Optuna config).

## Summary

| # | Discrepancy | Severity | Pairs Affected |
|---|-------------|----------|----------------|
| 1 | GBPUSD session_range 25 vs default 15 | Medium | GBPUSD |
| 2 | EURUSD tighter SL/lower TP vs defaults | Medium | EURUSD |
| 3 | XAUUSD params 10-13× wider than paper_mvp defaults | **HIGH** | XAUUSD |
| 4 | XAUUSD min_confidence 0.40 vs paper_mvp 0.55 | Medium | XAUUSD |
| 5 | No H1 walk-forward validation report | **HIGH** | All H1 pairs |
| 6 | tp1_rr=1.0 in strategies.yaml vs raised default 1.5 | Medium | GBPUSD, EURUSD |
| 7 | Missing indicator params in strategies.yaml | Low | All pairs |

**Recommendation:** Ava should decide whether strategies.yaml H1 params need to be re-aligned with either the class defaults or the Optuna-optimized values. The lack of H1 WF validation is the most significant finding — the forward test is running unvalidated parameters.
