# Crypto Strategy Validation — Baseline Methodology Report

**Card:** 27913f19-a0c1-40e4-9a84-379a2992095a (CRYPTO-P1-STRAT-A)
**Author:** Tsubaki (builder)
**Date:** 2026-07-28
**Scope:** Strategy selection, crypto-specific configuration, and baseline methodology for 7-day paper trading on BTC/ETH/SOL
**Parent:** CRYPTO-P1-TECH (c83ed19e) — completed via rescope (Craig direction: existing Bybit demo + Binance public data-feed)

---

## 1. Strategy Selection

Three forex pipeline strategies are selected for crypto adaptation based on transferability of their core mechanics to 24/7 crypto markets.

### 1.1 Donchian + ATR Trailing Trend v2 (`donchian_atr_trend_v2`)

**Forex type:** Trend-following
**Source:** `src/forex_bot/strategies/donchian_atr_trend_v2.py`
**Research ref:** strategy-optimization-research.md §B.1

**Why it transfers:**
- Donchian channel breakout is a universal momentum signal — works on any trending asset.
- ATR-based trailing stop adapts to crypto's high volatility natively (ATR expands with volatility).
- ADX gate filters out choppy/ranging conditions, which are common in crypto consolidation phases.
- EMA trend filter aligns with persistent crypto trends (BTC bull/bear regimes).

**Why it's suited to BTC/ETH/SOL:**
- Crypto trends run longer and harder than forex trends. Donchian breakouts on M15/H1 capture multi-day momentum runs.
- The volatility expansion filter (ATR > SMA(ATR)) is especially relevant — crypto breakouts are often preceded by volatility compression.
- Three R-multiple TPs (1R/2R/3R) scale well with crypto's large directional moves.

### 1.2 Dual-Timeframe Squeeze Pro (`dual_tf_squeeze_pro`)

**Forex type:** Breakout (squeeze release)
**Source:** `src/forex_bot/strategies/dual_tf_squeeze_pro.py`
**Research ref:** strategy-optimization-research.md §B.2

**Why it transfers:**
- Bollinger Band + Keltner Channel squeeze is volatility-structure detection — asset-class agnostic.
- H1 context gate with M15 entry trigger reduces false signals in crypto's noisy intraday action.
- ADX minimum threshold on H1 prevents trading in true chop (critical for crypto sideways periods).
- The squeeze → release pattern is one of the most reliable crypto setups: BTC often consolidates in tight ranges before explosive moves.

**Why it's suited to BTC/ETH/SOL:**
- Crypto has well-documented volatility compression → expansion cycles (funding rate convergence, on-chain accumulation ranges).
- The dual-TF approach naturally filters out M15 noise that would generate false breakouts on a single timeframe.
- RSI zone gate (40-60) prevents chasing extended moves — a common crypto trap.

### 1.3 Session Range Mean Reversion Plus (`srmr_plus`)

**Forex type:** Mean reversion
**Source:** `src/forex_bot/strategies/srmr_plus.py`
**Research ref:** strategy-optimization-research.md §A.5

**Why it transfers (with modifications):**
- Mean reversion to session range extremes works when price respects defined trading ranges.
- Crypto has intraday ranges even in 24/7 markets — the key is defining "sessions" that correspond to liquidity windows (more on this in §2).
- RSI + ADX combination (oversold/overbought in low-trend conditions) is valid for crypto range-bound periods.
- DXY overlay is forex-specific and MUST be disabled for crypto (no DXY equivalent — see §2.3).

**Why it's suited to BTC/ETH/SOL:**
- BTC/ETH/SOL frequently establish intraday ranges during Asian hours (low liquidity) and revert during EU/NY overlap.
- Tighter RSI levels (30/70) and ADX < 20 filter for true range conditions, avoiding counter-trend entries during crypto trends.
- The `min_bars_since_extreme_touch` parameter is valuable for crypto — confirms multi-bar reversal rather than single-wick spike.

### Strategies NOT selected (and why)

| Strategy | Type | Reason for exclusion |
|----------|------|---------------------|
| `london_breakout_retest` | Breakout | Depends on defined Asian session range → London open mechanic. No direct crypto session equivalent (24/7 market). Adaptable but lower confidence than dual_tf_squeeze_pro for the same breakout thesis. |
| `killzone_momentum` | Momentum | Optimized for forex killzone timing (London/NY opens). Session timing is less reliable in crypto. |
| `ttc_xauusd` | Momentum | XAUUSD-specific tuning. Parameters would need full retuning for crypto pairs. |
| `volatility_regime_breakout` | Breakout | Overlaps conceptually with donchian_atr_trend_v2. Lower confidence in original forex backtests. |
| `bb_rsi_reversion` | Mean reversion | Simpler subset of srmr_plus. Would be redundant. |
| `volatility_squeeze` | Breakout | Superseded by `dual_tf_squeeze_pro` which fixes the "no trend confirmation" bug. |

---

## 2. Crypto-Specific Configuration

### 2.1 Session Handling (24/7 Market)

Crypto trades 24/7 with no daily close. The concept of "sessions" must be redefined as **liquidity windows**:

| Window | UTC Hours | Character | Equivalent Forex Session |
|--------|-----------|-----------|------------------------|
| Asian | 00:00–08:00 | Lower volatility, range formation | Asian session |
| European | 08:00–16:00 | Volatility pickup, directional moves | London session |
| American | 13:00–21:00 | Highest volume, trend continuation/reversal | NY session |
| Overlap (EU+US) | 13:00–16:00 | Peak liquidity, strongest signals | London-NY overlap |
| Weekend | Sat 00:00–Mon 00:00 | Reduced liquidity, gap risk | N/A (no equivalent) |

**Config changes per strategy:**

- **donchian_atr_trend_v2:** Remove session filter (trade 24/7). Add weekend caution flag — optionally tighten position size or skip new entries on Saturday/Sunday.
- **dual_tf_squeeze_pro:** No session filter to remove (already session-agnostic). Add optional weekend volume filter: skip entries when rolling 4h volume < 50% of 7-day average.
- **srmr_plus:** Redefine "session range" as the Asian liquidity window (00:00–08:00 UTC). Mean reversion target shifts to European/American sessions. This directly maps to observed crypto behavior: BTC establishes Asian range, then breaks or reverts during Western hours.

### 2.2 Leverage and Position Sizing

| Parameter | Forex (current) | Crypto (proposed) | Rationale |
|-----------|-----------------|-------------------|-----------|
| Max leverage | 1:30 (FTMO retail) | 1:10–1:20 (Bybit testnet) | Crypto volatility is 3–5× forex; lower leverage compensates. Bybit demo supports up to 100x but we cap at 20x for risk sanity. |
| Per-trade risk | 0.50% ($50 on $10K) | 0.30% ($30 on $10K) | Wider stops in crypto mean same % risk = larger absolute stop distance. Reduce risk % to maintain comparable dollar risk. |
| Max simultaneous positions | 3 | 3 | Same portfolio cap. |
| Total open risk | ≤2% | ≤1.5% | Tighter aggregate risk for crypto's fat-tail exposure. |
| Stop-loss method | ATR-based, pip-denominated | ATR-based, percentage-denominated | Crypto has no "pip" concept. All distances in basis points or %. |

### 2.3 Indicator Parameter Adjustments

#### Donchian + ATR Trailing Trend v2

| Parameter | Forex (XAUUSD M15) | Crypto BTC/ETH/SOL M15 | Rationale |
|-----------|--------------------|-------------------------|-----------|
| `donchian_period` | 20 | 20 | Standard. 20-bar lookback works across asset classes. |
| `atr_period` | 14 | 14 | Standard Wilder period. |
| `atr_multiplier` (trailing stop) | 2.5 | 3.0 | Crypto noise is higher; wider trailing stop avoids premature exit. |
| `adx_min` | 20.0 | 25.0 | Higher ADX threshold filters crypto chop more aggressively. |
| `ema_trend_period` | 50 | 50 | Same — captures medium-term trend. |
| `hard_cap_sl_pips` | 25.0 (pips) | 3.0% | Percentage-denominated. 3% is roughly equivalent to 25 pips on XAUUSD scaled for BTC's range. |
| `tp1_rr / tp2_rr / tp3_rr` | 1.0 / 2.0 / 3.0 | 1.5 / 3.0 / 5.0 | Crypto trends extend further. Stretch R-multiples to capture outlier moves. |

Per-pair adjustments:
- **BTC:** `adx_min=22` (trends are cleaner on BTC than altcoins)
- **ETH:** `adx_min=25` (more chop than BTC, slightly higher bar)
- **SOL:** `adx_min=28`, `atr_multiplier=3.5` (highest volatility, most noise)

#### Dual-Timeframe Squeeze Pro

| Parameter | Forex (XAUUSD) | Crypto BTC/ETH/SOL | Rationale |
|-----------|-----------------|---------------------|-----------|
| `bb_period` | 20 | 20 | Standard. |
| `bb_std_dev` | 2.0 | 2.0 | Standard. |
| `kc_atr_multiplier` | 1.5 (H1) | 1.5 (H1) | Same squeeze detection logic. |
| `adx_min_h1` | 18.0 | 22.0 | Higher trend threshold for crypto. |
| `rsi_zone_min / max` | 40 / 60 | 40 / 60 | Same neutral zone. |
| `hard_cap_sl_pips` | 50.0 (pips) | 4.0% | Percentage-denominated. |
| `tp1_rr / tp2_rr / tp3_rr` | 1.0 / 2.0 / 3.0 | 1.5 / 3.0 / 5.0 | Stretched for crypto trend capture. |

Per-pair adjustments:
- **BTC:** Default crypto params (cleanest crypto asset).
- **ETH:** `adx_min_h1=24`, `bb_std_dev=2.1` (slightly wider BB to accommodate ETH noise).
- **SOL:** `adx_min_h1=26`, `bb_std_dev=2.2`, `kc_atr_multiplier=1.7` (widest params for highest-vol asset).

#### Session Range Mean Reversion Plus

| Parameter | Forex (EURUSD H1) | Crypto BTC/ETH/SOL H1 | Rationale |
|-----------|--------------------|------------------------|-----------|
| `atr_period` | 14 | 14 | Standard. |
| `rsi_long_level` | 30.0 | 28.0 | Slightly deeper oversold — crypto RSI can stay extended longer. |
| `rsi_short_level` | 70.0 | 72.0 | Same logic for overbought. |
| `adx_max_threshold` | 20.0 | 18.0 | Tighter: only trade in very low-trend conditions. Crypto trends are stronger; must be confident we're ranging. |
| `session_range_min_pips` | 10.0 | 0.8% | Percentage-denominated. |
| `entry_near_extreme_pips` | 8.0 | 0.5% | Percentage-denominated. |
| `hard_cap_sl_pips` | 18.0 | 2.5% | Percentage-denominated. |
| `tp1_rr / tp2_rr` | 1.5 / 1.5 | 1.5 / 2.0 | Slightly higher TP2 for crypto mean-reversion bounces. |
| `ema_trend_period` | 50 | 50 | Same. |
| `dxy_overlay` | False (optional True) | **False — DISABLED** | No DXY equivalent for crypto. This overlay must be off. |
| `min_bars_since_extreme_touch` | 0 | 3 | Enable: requires 3+ bars since range extreme touch. Confirms genuine reversal, not single-wick spike. |

Per-pair adjustments:
- **BTC:** Default crypto params.
- **ETH:** `rsi_long_level=26`, `rsi_short_level=74` (ETH mean-reverts less cleanly).
- **SOL:** Consider disabling — SOL may trend too hard for mean reversion. Include in test with tight ADX gate (16.0) and flag for potential removal.

### 2.4 Funding Rate Awareness

Crypto perpetual futures carry funding rates (typically every 8 hours). This affects holding cost and should be tracked as a metric, not an entry signal:

- **During 7-day run:** Record funding paid/received per position.
- **Impact threshold:** If cumulative funding cost exceeds 0.5% of position notional, flag for evaluation.
- **Direction bias:** Positive funding (longs pay shorts) in strong uptrends adds cost to long positions. Factor into net P/L.

### 2.5 What Does NOT Transfer (Confirmed)

- ❌ **Interest rate differential strategies** — crypto has no central bank rate mechanism.
- ❌ **Central bank event trading** — no Fed/ECB calendar events.
- ❌ **End-of-day range patterns** — no daily close in 24/7 markets.
- ❌ **DXY regime overlay** — no DXY equivalent. Must be disabled.
- ❌ **Pip-based distance metrics** — crypto uses percentage or basis-point distances.

---

## 3. Performance Metrics to Track

### 3.1 Core Metrics (per strategy × pair)

| Metric | Target | Kill Criterion |
|--------|--------|----------------|
| Win rate | ≥40% | <25% after 20 trades |
| Profit factor (gross profit / gross loss) | ≥1.3 | <0.8 |
| Average R-multiple per trade | ≥0.3R | <−0.3R |
| Max drawdown (% of starting capital) | ≤8% | >12% |
| Sharpe ratio (annualized, daily returns) | ≥1.0 | <0.0 |
| Sortino ratio | ≥1.5 | <0.5 |
| Max consecutive losses | ≤6 | >8 |
| Average holding period (hours) | Track (no target) | — |
| Funding rate impact (% P/L) | ≤0.5% per position | >2% per position |

### 3.2 Comparative Metrics

- **Strategy-vs-strategy correlation:** Should be <0.5 between the three strategies (want diversified signal sources).
- **Pair-vs-pair correlation:** BTC/ETH will likely correlate >0.7; SOL should be more independent. Track and report.
- **Session performance breakdown:** Asian vs European vs American vs Overlap vs Weekend.

---

## 4. Risk Framework

### 4.1 Account Configuration

- **Test account:** Bybit demo account (per Craig's 2026-07-28 rescope direction)
- **Starting capital:** $10,000 (virtual)
- **Data feed:** Binance public data (OHLCV) for backtest verification; Bybit demo for live paper execution
- **Leverage cap:** 20x (configured in Bybit demo)
- **Pairs:** BTC/USDT, ETH/USDT, SOL/USDT
- **Timeframe:** M15 (entry), H1 (context), per strategy config

### 4.2 Position Sizing Rules

- **Base risk per trade:** 0.30% of account equity ($30 on $10K)
- **Max simultaneous open positions:** 3 (across all strategies and pairs)
- **Max risk per pair:** 0.60% (2 concurrent positions on BTC, for example)
- **Weekend risk reduction:** Cut base risk to 0.15% for new entries opened during weekend hours
- **Drawdown throttle:** If account drawdown reaches −3%, cut risk to 0.15%. If −5%, stop opening new positions for 24h.

### 4.3 Stop-Loss Rules

- Every trade MUST have a hard stop-loss set at entry (no naked positions).
- Stop distance determined by strategy ATR logic (crypto-adapted params above).
- No stop widening after entry. Tightening (trailing) is permitted per strategy logic.

---

## 5. Kill Criteria for 7-Day Run

The 7-day paper trading run (card CRYPTO-P1-STRAT-B) will be terminated early if ANY of the following occur:

| Kill Condition | Threshold | Action |
|----------------|-----------|--------|
| Max account drawdown | >12% of starting capital ($1,200 loss) | Immediate halt all strategies |
| Single-strategy drawdown | >8% from one strategy | Disable that strategy, continue others |
| Data feed outage | >2 consecutive hours of missing data | Pause until feed restored |
| Strategy error rate | >10% of signals produce execution errors | Investigate, fix, or disable strategy |
| API/key failure | Bybit demo or Binance data feed inaccessible for >4h | Halt, report, await fix |
| Cumulative funding cost | >2% of account across all positions | Review, possibly close long-biased positions |

**Normal termination:** After 7 full days (168 hours) of operation, run analysis and generate performance report.

---

## 6. Recommendation: Strategies to Advance to Live Paper

**Pre-run recommendation (subject to 7-day validation):**

1. **donchian_atr_trend_v2** — Highest confidence for crypto adaptation. Trend-following is the most natural fit for crypto's persistent directional moves. The ATR-based trailing stop and R-multiple TP ladder are well-suited to crypto's volatility profile.

2. **dual_tf_squeeze_pro** — Strong second choice. Squeeze detection captures the compression → explosion pattern that is characteristic of crypto market structure. Dual-TF approach is robust against M15 noise.

3. **srmr_plus** — Lower confidence, included for diversification. Mean reversion in crypto is riskier than trend-following because crypto trends can persist beyond logical extremes. The tight ADX gate (≤18) and 3-bar reversal confirmation are critical safety mechanisms. **If SOL shows >3 consecutive losses in the first 3 days, drop SOL from srmr_plus and evaluate BTC/ETH-only.**

**Advancement criteria post-7-day run:**
- Profit factor ≥1.3 AND max drawdown ≤8% → Advance to live paper trading
- Profit factor 0.8–1.3 AND max drawdown ≤12% → Parameter tuning cycle, then re-test
- Profit factor <0.8 OR max drawdown >12% → Do not advance; archive results and reassess strategy selection

---

## 7. Data Source Architecture (Per Craig's Rescope)

Per Craig's 2026-07-28 direction (card c83ed19e rescope):

- **Execution:** Bybit demo account (existing account, no new KYC needed)
- **Market data:** Binance public data feed (OHLCV candles, no auth required for public endpoints)
- **No new testnet accounts:** Binance used for data only, not execution
- **Bitget:** Phase 2+ live execution target (not used for Phase 1 validation)

This means the 7-day run will execute paper trades on Bybit demo using Binance public data for signal generation. The slight venue difference (Binance data → Bybit execution) is acceptable for paper validation but should be noted as a potential source of slippage in live trading.

---

## Acceptance Criteria Checklist

| Criterion | Status |
|-----------|--------|
| 2-3 strategies selected with documented rationale | ✅ 3 strategies: donchian_atr_trend_v2, dual_tf_squeeze_pro, srmr_plus |
| Crypto-specific parameter changes documented per strategy × pair (BTC/ETH/SOL) | ✅ See §2.3 for per-strategy × per-pair configs |
| Baseline report covers: strategy selection, per-pair parameters, metrics, risk framework, kill criteria | ✅ §1 (selection), §2 (params), §3 (metrics), §4 (risk), §5 (kill criteria) |
| py_compile / markdown lint passes | ✅ N/A (markdown only — no Python in this card) |
| Recommendation: which strategies to advance to live paper | ✅ §6 |
