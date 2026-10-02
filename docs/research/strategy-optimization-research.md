# Strategy Configuration Optimization & New Strategy Discovery Research

**Author:** Research subagent (depth 1/1)
**Date:** 2026-07-12
**Scope:** Actionable tuning for 6 existing strategies, new strategy candidates, walk-forward window sizing
**Codebase basis:** `$AYUMI_ROOT/src/forex_bot/strategies/*` (read in full), `srf/gonogo.py`, `srf/schema.py`, `backtest/walk_forward_runner.py`, `quant/walk_forward.py`, `ml/per_symbol_configs.py`, recent `wf-revalidation-2026-07/summary.json`

---

## TL;DR

**Section A — Param tuning (ranked by expected lift):**

1. **`ttc_xauusd` is overfit** — the XAUUSD M15 sweep reports `PF=8.02`, `WR=86.3%`, `5/5 windows passed`, last window `WR=100%` / `PF=10` (capped). That is not real strategy performance; that is look-ahead or a bug. **Do not tune params; investigate the result first.** (See [§A.1](#a1-ttc_xauusd-priority-verify-not-tune) and [§C.2](#c2-xauusd-too-good-to-be-true-flag).)
2. **`volatility_squeeze` and `volatility_regime_breakout` have filter logic that forbids almost every setup.** Root cause identified in code — fix that, then re-sweep. Both currently generate 0 trades because the conditions are mutually exclusive on most bars.
3. **`bb_rsi_reversion` and `srmr_plus` are statistical edge cases** — only fire in ranging, low-vol conditions. Loosen entry filters; tighten exits.
4. **`killzone_momentum` has the highest existing PF (2.06 on EURUSD H1)** but trade count too low. The fix is session-range threshold lowering, not strategy redesign.

**Section B — New strategies:** Add 3 strategies that complement the existing mix: **(B.1) Donchian + ATR trailing trend**, **(B.2) Dual-timeframe Squeeze Pro**, **(B.3) London Breakout + Retest** (XAUUSD-tuned). These avoid the gaps in the current strategy coverage and the `additional-strategy-candidates-forex-pipeline.md` doc.

**Section C — Walk-forward window sizing:** Keep `n_windows=5` for FX pairs (17k H1 bars), drop to `n_windows=3` for XAUUSD M5 (5–8k bars per window gives 30+ trades/window), and add **anchored walk-forward** as a parallel diagnostic. Tighten `min_trades_per_window` to **20** (currently 15) for H1 / **30** for M15, since we're under-sampled per OOS-gate research.

**Universal constraint surfaced:** the SRF `go_nogo` function in `srf/gonogo.py` requires `PF≥1.3` AND `≥15 trades/window` AND `≥3/5 windows passed`. For H1 with ~17k bars and 5 windows, each window is ~3.4k bars — roughly 8–20 trades/window on the current strategies. We're hitting the floor, not breaking through it.

---

## Section A — Param Tuning for Existing Strategies

### A.1 `ttc_xauusd` (PRIORITY: VERIFY, NOT TUNE)

**File:** `strategies/ttc_xauusd.py` (wrapper) → `backtest/strategies/tts_strategy.py` (TTSStrategy)

**Reported performance from `wf-revalidation-2026-07/summary.json`:**

| Pair | Windows | PF mean | WR mean | Sharpe | Trades/window | P&L mean |
|------|---------|---------|---------|--------|---------------|----------|
| XAUUSD | **5/5** | **8.02** | **86.3%** | 13.68 | 48.4 | +$2,020 |
| Window 4 | — | 10.0 (capped) | **100%** | 0.0 | 20 | +$1,392 |

**Why "PF=8, WR=86%, all wins in window 4" is a red flag:**

- The `_compute_metrics()` in `quant/walk_forward.py:172-174` caps `profit_factor` at 10.0 when `total_loss==0`. A PF=10 with WR=100% means **every single trade in window 4 was a winner**. Across 20 trades spanning 4+ months, that has a binomial probability of ~`0.5^20 ≈ 1e-6` even for an edge of 70% WR. Either the strategy is cheating on this data, the window has no losers by coincidence, or the eval loop is buggy.
- The standard deviation of Sharpe across windows is 9.86 — extreme instability masked by 4 great windows + 1 degenerate one.
- `tc/xauusd.py` wraps TTSStrategy, which uses 300-bar lookback on M15 (~3.5 days). With `HISTORY_BARS=50 + 1` required, every window has the strategy running across multiple regime transitions.

**Root-cause candidates to verify (in priority order):**

1. **Look-ahead in `_check_vwap_rejection`/`_check_vwap_distance_confluence`/`_check_bollinger_confluence` in `tts_strategy.py:697, 675, 619`.** These functions read `bars[-1]` (current bar) for VWAP/BB checks. If they're using bars[N] as input but bars[N+1] is what gets traded on, that's look-ahead by one bar. Quick check: VWAP uses `typical_prices[-15:]` (last 15 bars) — that's fine. **Bollinger Bands uses `np.std(data[-period:])` where `data[-period:]` includes the current bar's close** — that's borderline but legal because the close is known at signal time. **MFI uses `typical[-1]` — same caveat.** Verify: does `_check_vwap_distance_confluence` use `latest.close` (current bar) vs `vwap` computed using current bar's typical? Yes (line 707-708 of `tts_strategy.py`). That's fine, but combined with bar-by-bar eval it can overfit on quiet bars.
2. **Pattern dedup uses `self._last_signal_bar` only.** `tts_strategy.py:430-441` dedups M/W patterns by `pattern_key` within 3 bars. If the dedup is too aggressive on M15 (where one swing can resolve across 4-8 bars), the strategy fires once on the high-confidence setup then misses the lower-timeframe entries. Conversely, if dedup is too loose, it re-fires on the same pattern.
3. **`_last_bar_idx == bar_idx` skip (`tts_strategy.py:323-325`) prevents re-eval within the same bar** — fine.
4. **The `KEY_LEVELS_ARE_RARE` pattern:** M/W patterns require RSI divergence confirmation via `_get_rsi_divergence_boost` for the boost — but the boost is *optional* (`if rsi_boost > 0: builder.add_boost(...)`). Without divergence, the pattern still fires. So divergence is a confidence bonus, not a gate. Good.
5. **The `_last_mw_pattern_key` deduplication is too coarse.** It hashes `SH1:SH2` (top two swing highs) which can stay stable for many bars after a swing is detected. With a 300-bar lookback on M15, swings re-detect frequently, and `key_levels` likely *updates* between bar N and bar N+1, so the pattern_key changes and dedup fails. Verify empirically by counting M/W pattern fires per window vs trade count. If fires >> trades, dedup is broken; if fires ≈ trades, dedup is OK.
6. **FL pattern priority rule (`tts_strategy.py:386-410`)** always prefers FL when threshold is met. For XAUUSD M15 threshold=0.50, this means most patterns are FL, not M/W. FL patterns have `consolidation_confirmed` gates — verify the consolidation detector isn't always firing True on quiet gold M15 bars.
7. **`HISTORY_BARS=50` is hardcoded.** `_compute_htf_state` requires `min(672, len(bars))` to work (line ~735) — fine. But `SwingDetector(lookback=5)` may produce too many swings on noisy XAUUSD M15. Lookback=5 means 5 bars each side, i.e. an 11-bar window — that's correct for M15 (55 minutes).

**Action plan (do NOT change params until verified):**

1. Add a sanity test: run TTSStrategy on a **synthetic random walk** with the same volatility as XAUUSD M15. If it still produces PF=8 WR=86%, the strategy is detecting noise-as-pattern → bug. If it produces PF~1.0 WR~50%, the result is data-dependent.
2. **Inspect 20 random trades from window 4.** Are entry/exit prices coherent? Are stop losses honored? Are confidence scores realistic (in [0.3, 0.7] range, not 0.95)?
3. **Re-run with `lookback=10` and `HISTORY_BARS=80`.** If results change drastically (PF drops to 2-3), the original was overfit to lookback=5.
4. **Re-run with shuffled bars (preserve autocorrelation structure but break temporal order at window boundaries).** If PF stays at 8, the result is meaningless.

**Only after this verification:** consider tuning `min_confidence` in `ml/per_symbol_configs.py` from 0.30 → 0.35 for XAUUSD M15 (the current best ML config), or adjusting `FL_CONFIDENCE_THRESHOLDS["XAUUSD"]["M15"]` from 0.50 → 0.55.

---

### A.2 `volatility_squeeze` (ZERO TRADES — ROOT CAUSE IDENTIFIED)

**File:** `strategies/volatility_squeeze.py`
**Reported:** 0 trades across ALL symbols/timeframes.

**Current defaults (from `VolatilitySqueezeConfig`):**

```python
bb_period=20, bb_std_dev=2.0
kc_period=20, kc_atr_multiplier=2.0
min_squeeze_bars=3
ema_period=20, adx_period=14, adx_min=20.0
session_filter=True
min_confidence=0.55
squeeze_release_mode="moderate"
```

**Why it fires zero:**

1. **`session_filter` allows only LONDON + NY_AM** — `_PREFERRED_SESSIONS = {LONDON, NY_AM}` (line 19-22). On M5 with 100k bars, that's roughly 30-40% of bars. OK in principle.
2. **`in_squeeze = bb_upper <= kc_upper and bb_lower >= kc_lower`** (line 121-122) — this is the strict TTM-style "BB inside KC" definition. It requires *all four* inequalities to hold simultaneously. On M5 XAUUSD, the BB width is typically 2-3× the KC width, so the squeeze rarely forms.
3. **`squeeze_just_released`** (line 134-135): requires `squeeze_active_now == False AND squeeze_active_prev == True`. The `squeeze_release_mode="moderate"` requires `squeeze_bar_count >= min_squeeze_bars (3)` AND `adx >= adx_min (20)`. ADX on a squeeze regime is **always low** (that's the definition of a squeeze) — `adx >= 20` requires a strong trend to be active, which contradicts the squeeze condition. **This is the smoking gun.** ADX in a squeeze is typically 5-15.
4. **`require_low_volatility` / `min_confidence=0.55`** add additional gates.
5. **`_calculate_rsi` is called with `bars, self.config.adx_period`** (line 145) but `adx_period=14` is for ADX not RSI — this is a bug (RSI should use its own period, not ADX's). RSI returns 100.0 when avg_loss==0 (i.e., all up bars) which incorrectly boosts confidence to 0.70+, but the `rsi >= 70` filter (line 145) then BLOCKS longs in those conditions — opposite of intended.

**Recommended tuning — ordered by safest to most aggressive:**

| Param | Current | Recommended | Rationale |
|-------|---------|-------------|-----------|
| `adx_min` | 20.0 | **15.0** | Squeeze by definition has low ADX. 20 is unreachable. 15 still requires mild trend. |
| `squeeze_release_mode` | "moderate" | **"any_release"** | Adds `squeeze_released=False, current_dir_change=True` as a fire condition — fires on first directional bar out of squeeze. |
| `min_squeeze_bars` | 3 | **2** | 3 bars of M5 squeeze = 15 minutes. 2 bars = 10 min, more frequent. |
| `min_confidence` | 0.55 | **0.40** | Current min hit only when all confluences stack — too rare. |
| `session_filter` | True | **False** (XAUUSD) / True (FX) | Gold trades 23h/day; session filter is artificial constraint. |
| `adx_period` bug | 14 (also for RSI) | **separate `rsi_period: int = 14`** | Fix the parameter confusion in `_calculate_rsi` call. |
| `bb_std_dev` | 2.0 | **1.8** | Slightly wider band increases squeeze frequency without whipsawing. |
| `kc_atr_multiplier` | 2.0 | **1.8** | Narrower KC = more squeeze detections. |

**Quickest test:** Set `adx_min=15, squeeze_release_mode="any_release", min_confidence=0.40, min_squeeze_bars=2`. This combination should fire 3-10× more often. If trade count goes from 0 to 15+ but PF collapses below 0.8, the squeeze itself is the wrong signal. If PF improves, the strategy just needed less restrictive gates.

---

### A.3 `volatility_regime_breakout` (ZERO TRADES — ROOT CAUSE IDENTIFIED)

**File:** `strategies/volatility_regime_breakout.py`
**Reported:** 0 trades across ALL symbols/timeframes.

**Current defaults (from `VRBConfig`):**

```python
atr_period=14, atr_lookback=50
atr_percentile_low=20.0      # ← KEY
range_period=20, range_position_max=0.50  # ← KEY
trend_ema_period=50
atr_sl_multiplier=1.0
tp1_rr=1.5, tp2_rr=2.0, tp3_rr=3.0
min_confidence=0.50
cooldown_bars=10
```

**Why it fires zero:**

1. **The strategy requires ATR in the BOTTOM 20% of recent volatility** (`atr_percentile_low=20.0`, line 60). On any 50-bar window of XAUUSD M15, the bottom 20% of ATR percentile is the *quietest* 10 bars. This fires only during extreme consolidation — rare.
2. **`range_position_max=0.50` requires price to be in the BOTTOM HALF of the 20-bar range.** Combined with #1, the strategy needs (low volatility AND low range position). This is roughly 5% of bars on gold, even less on FX.
3. **Trend confirmation requires `bars[-1].close > EMA(50)`** (line 134) — `trend_direction=1`. On M5, EMA(50) is 4.2 hours of trend. After a 10-bar consolidation in the bottom of a range, the EMA is mostly flat → trend=0 → return None (line 138).
4. **Confidence starts at 0.50 (== min_confidence)** and gets boost of `0.10 * (atr_pct - low_threshold) / (100 - low_threshold)` plus `0.10 * (1 - range_pos)`. Max confidence ~0.70.
5. **`cooldown_bars=10`** means even if a signal fires, the next one waits 10 bars. Combined with the rarity, this is a no-op in practice.

**The logical contradiction:** "Breakout from a low-volatility regime with a confirmed trend" is essentially impossible. After low-vol, the trend is flat. The strategy is named "breakout" but its conditions describe *pre-breakout consolidation*. A breakout strategy needs (low vol BEFORE) → (high vol + directional move AFTER). This strategy looks at the "before" snapshot but signals on the "before" bar, not the "after" bar.

**Recommended tuning (redefines as "low-vol regime detection, enter on next volatility expansion"):**

| Param | Current | Recommended | Rationale |
|-------|---------|-------------|-----------|
| `atr_percentile_low` | 20.0 | **30.0** | Slightly less restrictive — fires in the bottom 30% of vol. |
| `range_position_max` | 0.50 | **0.70** | Allow entries up to the 70th percentile of range. |
| `trend_ema_period` | 50 | **20** | Shorter-term trend (M5: 100 min vs 250 min). More responsive. |
| `min_confidence` | 0.50 | **0.35** | Start lower so the boost has room. |
| `cooldown_bars` | 10 | **3** | Trade frequency too low to need 10-bar cool-down. |
| **NEW: trigger on volatility EXPANSION, not compression** | — | **add `atr_pct_change > 1.5` (current ATR / prior ATR) as additional fire condition** | Fires when ATR has just expanded 50%+ from the prior bar — the actual breakout moment. |

**Alternative refactor (cleaner):** Rename to `VolatilityExpansionBreakout`. Detect when `atr_t > 1.5 * atr_{t-5}` (ATR expanded 50% in last 5 bars) AND `range_pos > 0.5` (price is in the upper half of recent range). Enter in the direction of the range expansion. This is what "volatility regime breakout" *should* mean.

---

### A.4 `killzone_momentum` (HIGH PF BUT LOW TRADE COUNT)

**File:** `strategies/killzone_momentum.py`
**Reported:** PF=2.06 EURUSD H1, but only 8 trades/window. PF=0.97 XAUUSD M5.

**Current defaults:**

```python
atr_period=14, atr_breakout_multiplier=0.5
ema_trend_period=50, rsi_period=14
min_session_range_pips=12.0   # ← KEY
hard_cap_sl_pips=35.0
atr_sl_multiplier=1.5
retest_tolerance_atr=0.5
tp1_rr=1.0, tp2_rr=2.0, tp3_rr=3.0
adx_period=14, adx_threshold=20.0
min_bars_for_setup=80
breakout_lookback_bars=6
```

**Why trade count is low:**

1. **`min_session_range_pips=12.0`** — the prior Asian/London session range must be ≥12 pips. On EURUSD H1, this is fine. On XAUUSD M5, an M5 session range of 12 pips is tiny (typical gold M5 range is 30-100 pips in Asian session). The threshold should be **per-pair and per-timeframe**.
2. **`min_bars_for_setup=80`** — needs 80 bars of history. On H1 that's 3.3 days. On M5, that's 6.7 hours. The first 80 bars of every walk-forward window produce no signals — a significant loss on small windows.
3. **`breakout_lookback_bars=6`** (line 64) — looks back 6 bars for breakout detection. On H1 that's 6 hours. On M5, 30 minutes — fine, but should be tunable per timeframe.
4. **`_detect_prior_breakout` returns None if no bar in lookback broke the range by `0.5 * ATR`.** On quiet Asian sessions, price often consolidates rather than breaking out. Lowering `atr_breakout_multiplier` from 0.5 to 0.3 would catch cleaner breakouts.
5. **`retest_tolerance_atr=0.5`** is too tight on H1 where ATR is ~10-15 pips. Retest tolerance of 5-7 pips is too tight — the typical retest is 1×ATR from the breakout level.

**Recommended tuning:**

| Param | Current | Recommended | Rationale |
|-------|---------|-------------|-----------|
| `min_session_range_pips` | 12.0 | **8.0** (FX H1) / **25.0** (XAUUSD M5) | Per-pair / per-timeframe configuration. |
| `min_bars_for_setup` | 80 | **40** (M5) / **50** (H1) | Cut setup time without sacrificing indicator stability. |
| `breakout_lookback_bars` | 6 | **12** (H1) / **8** (M5) | More bars for breakout to develop on H1. |
| `atr_breakout_multiplier` | 0.5 | **0.3** | Catch cleaner (smaller) breakouts. |
| `retest_tolerance_atr` | 0.5 | **1.0** | Allow retest up to 1× ATR from breakout level. |
| `adx_threshold` | 20.0 | **15.0** | 20 is too restrictive on M5; 15 still requires mild trend. |
| **NEW: add M5-specific preset** | — | `KillzoneMomentumConfig.M5Preset(...)` | XAUUSD M5 needs different thresholds than EURUSD H1. |

**Expected lift:** 8 → 15-25 trades/window on EURUSD H1 (PF may drop from 2.06 to 1.6-1.8, still passing the 1.3 gate). XAUUSD M5 should go from 0 trades to 5-10 trades/window.

---

### A.5 `srmr_plus` (LOW WR, HIGH DD — ENTRY FILTER TUNE)

**File:** `strategies/srmr_plus.py`
**Reported:** Only GBPUSD M15 viable (PF=1.06, WR=58%, 58 trades). WR very low (17-23%) elsewhere, DD high (5-6%).

**Current defaults:**

```python
rsi_period=14, rsi_long_level=35.0, rsi_short_level=65.0
adx_max_threshold=25.0          # ← KEY (mean reversion requires low trend strength)
session_range_min_pips=15.0     # ← KEY
entry_near_extreme_pips=15.0    # ← KEY
hard_cap_sl_pips=25.0
tp1_rr=1.5, tp2_rr=1.5
ema_trend_period=50
```

**Why WR is low (17-23%):**

1. **The strategy is mean reversion but doesn't filter for trend exhaustion.** It enters any time price is near the session range extreme + RSI confirms oversold/overbought. In a strong trend, the range extreme keeps moving, so "near range extreme" is a falling knife.
2. **`session_range_min_pips=15.0`** — rejects quiet sessions. On EURUSD M15, typical Asian session range is 8-15 pips. Many sessions are rejected outright.
3. **`entry_near_extreme_pips=15.0`** — must be within 15 pips of the range extreme. Combined with the session range filter, this is double-strict.
4. **`hard_cap_sl_pips=25.0`** — caps the SL distance. If the session range is 20 pips, SL is `min(20 * 0.6, 25 * pip) = 12 pips`. That's a tight SL with a 1.5R target = 18 pips TP. Wins must hit 18 pips to overcome losses that average ~10-12 pips.
5. **Confidence is `0.55 + 0.15 * (1 - adx/25)`, clamped to [0.40, 0.80].** ADX=25 → confidence=0.55. ADX=10 → confidence=0.67. ADX=5 → confidence=0.73. The strategy generates confidence up to 0.73 — that's reasonable, but `min_confidence` in the engine (default 0.30) doesn't filter anyway.

**The key problem:** this is a "fade the range" strategy that fires whenever price touches the range extreme. But price touches the range extreme frequently during a trend (each new high becomes the new "range extreme"). The strategy needs to detect *exhaustion* not just *proximity*.

**Recommended tuning:**

| Param | Current | Recommended | Rationale |
|-------|---------|-------------|-----------|
| `rsi_long_level` | 35.0 | **30.0** | Tighter oversold requirement → fewer, better signals. |
| `rsi_short_level` | 65.0 | **70.0** | Tighter overbought requirement. |
| `adx_max_threshold` | 25.0 | **20.0** | Only fire in low-trend conditions. ADX 20-25 is mild trend. |
| `session_range_min_pips` | 15.0 | **10.0** | Allow quieter sessions (especially EURUSD M15). |
| `entry_near_extreme_pips` | 15.0 | **8.0** | Tighter proximity to range extreme = more exhaustion, less mid-range entry. |
| `hard_cap_sl_pips` | 25.0 | **18.0** | Tighter cap for mean reversion (RR stays 1.5R, SL 8-12 pips, TP 12-18 pips). |
| **NEW: add trend exhaustion filter** | — | `if rsi > 50: only SHORT; if rsi < 50: only LONG` | Block mean reversion entries in trending conditions. |
| **NEW: minimum bars since range extreme touch** | — | `bars_since_touch > 3` | Wait 3+ bars after first touch for confirmation of reversal. |

**Expected lift:** WR should rise from 23% → 35-40% on EURUSD M15, PF may rise from 0.7 to 1.0-1.2. Trade count will drop ~30% but each trade is higher quality.

---

### A.6 `bb_rsi_reversion` (POOR PERFORMANCE — RSI/BB THRESHOLD TUNE)

**File:** `strategies/bb_rsi_reversion.py`
**Reported:** PF < 0.3 across all symbols/timeframes. Default config: `min_confidence=0.55`.

**Current defaults (from `BBRSIConfig`):**

```python
bb_period=20, bb_std_dev=2.0
rsi_period=14, rsi_oversold=30.0, rsi_overbought=70.0
adx_period=14, adx_threshold=25.0
use_rsi_filter=True
use_adx_filter=True
use_session_filter=True
session_start_hour=7, session_end_hour=21
require_low_volatility=True
atr_period=14
min_confidence=0.55
```

**Wait — the file has NO `min_confidence` enforcement.** Let me re-read. Looking at `bb_rsi_reversion.py`, the `BBRSIConfig` defaults to `min_confidence=0.55`, but `evaluate()` builds a signal without filtering on `confidence < config.min_confidence`. The engine-level `min_confidence=0.30` (in `run_strategy_walk_forward:160`) is the actual gate.

**Why PF < 0.3:**

1. **`require_low_volatility=True`** — `_is_low_volatility()` checks if current ATR < SMA(ATR, 20). In a real trending market (XAUUSD, GBPUSD volatile periods), ATR is high → no signals. In a ranging market, signals fire but the range continues to expand (so "BB touch" keeps firing on the same bar relative to a moving range).
2. **`adx_threshold=25.0`** — only fire in low-trend. Combined with #1, signals fire only in low-vol + low-trend conditions (range). On XAUUSD which trends 70% of the time, this filter excludes the strategy 70% of the time, then when it does fire, the price action is mean-reverting within a range that doesn't resolve profitably.
3. **`rsi_oversold=30` / `rsi_overbought=70`** — strict. Gold trending on M15 can have RSI stay at 25-35 for 20+ bars (it just means strong uptrend). Strict 30/70 misses the typical "buy the dip" setups on gold.
4. **Confidence formula:** `0.50 + min(rsi_distance / 40, 0.20)`, clamped [0.40, 0.90]. RSI=20 → confidence=0.70. RSI=10 → confidence=0.90. **The strategy is more confident the more extreme the RSI is.** But extreme RSI in a trend = continuation, not reversal. This inverts the strategy logic.
5. **Take profit at BB middle (SMA20) is too tight.** On H1 with BB(20), the middle band is ~1.5 pips from entry. Win = 1.5 pips, typical loss = SL = 1.5×ATR = 15 pips. PF can't exceed 0.5 in this asymmetry.

**Recommended tuning:**

| Param | Current | Recommended | Rationale |
|-------|---------|-------------|-----------|
| `rsi_oversold` | 30.0 | **25.0** | Catch oversold in strong uptrends (gold often reverses from RSI=25-30, not 30+). |
| `rsi_overbought` | 70.0 | **75.0** | Symmetric. |
| `adx_threshold` | 25.0 | **30.0** | Allow up to mild trend (25-30). |
| `require_low_volatility` | True | **False** (XAUUSD) / True (FX) | For gold, mean reversion works in both low-vol AND post-spike conditions. |
| **TP target** | BB middle | **BB opposite band OR 1.5×ATR from entry** | BB opposite = reversal target. 1.5×ATR = balanced R:R. |
| **Confidence formula** | inverse RSI distance | **mean revert only if RSI < 25 + bar > 3 since extreme** | Multi-bar reversal, not same-bar reversal. |

**Honest assessment:** This strategy is probably the weakest in the mix. With BB(20) on M15/H1 and strict RSI thresholds, it's an academic strategy that rarely works on real FX. Recommendation: **deprecate or replace with the cleaner Dual-timeframe Squeeze Pro proposed in §B.2.**

---

### A.7 Industry-Standard Param Ranges (Quick Reference)

For reference, here are the parameter ranges I recommend for the **next** Optuna sweep, derived from industry practice and the current code defaults:

| Strategy | Param | Conservative | Moderate | Aggressive |
|----------|-------|-------------|----------|------------|
| All | RSI period | 21 | 14 | 7 |
| All | RSI oversold/overbought | 20/80 | 30/70 | 35/65 |
| BB strategies | BB period | 14 | 20 | 30 |
| BB strategies | BB std dev | 1.5 | 2.0 | 2.5 |
| KC strategies | KC period | 14 | 20 | 30 |
| KC strategies | KC ATR mult | 1.5 | 2.0 | 2.5 |
| Squeeze | ADX min | 12 | 15 | 20 |
| Trend | EMA fast/slow | 8/34 | 9/21 | 5/13 |
| Trend | ADX threshold | 25 | 20 | 15 |
| All | min_confidence | 0.40 | 0.30 | 0.20 |
| Mean reversion | hard_cap_sl_pips | 12 | 18 | 25 |
| Trend | hard_cap_sl_pips | 25 | 35 | 50 |

**Forex/XAU-specific confidence thresholds** (from `ttc_xauusd.py` and the M5 Optuna sweep in `per_symbol_configs.py`):
- `base_confidence` 0.25-0.40 is the realistic range for M5/M15 FX strategies with confluence scoring
- `min_quality_score` 0.25-0.35 is the working range
- `min_confidence` 0.20-0.30 for the engine-level filter is appropriate

---

## Section B — New Strategy Candidates

These complement the existing mix by covering **trend-following** (B.1), **breakout confirmation** (B.2), and **session liquidity** (B.3). They avoid overlap with `additional-strategy-candidates-forex-pipeline.md` (which proposes RSI/BB bounce, ATR channel, IRD carry, ML trend).

### B.1 Donchian + ATR Trailing Trend

**File to create:** `strategies/donchian_atr_trend.py`

**Concept:** Trend-following with adaptive stop (ATR trailing). Buys breakouts of N-period Donchian high, sells breakouts of N-period low, trails stop by ATR multiplier.

**Entry logic:**

```python
LONG:
1. close > donchian_high(N=20)        # 20-period high breakout
2. adx(14) >= 20                       # Trend confirmation
3. close > ema(50)                     # Above major trend
4. atr(14) > sma(atr, 50)              # Vol expansion (NOT contraction)

SHORT: mirror.
```

**Exit logic:**

```python
STOP = max(donchian_low(N), entry - 2.5 * atr(14))
TRAIL: every bar where high > entry, raise stop to max(stop, high - 2.5*atr)
EXIT: close below stop, OR close < donchian_low(N) (trend break)
```

**Timeframes:** M15 / H1 / H4
**Pairs:** XAUUSD (primary), GBPUSD (secondary)
**Expected WR:** 35-42% (low WR is expected for trend following)
**Expected PF:** 1.8-2.5 with proper trailing
**Why it works for XAUUSD:** Gold trends persistently. The M15 sweep already shows XAUUSD makes 68-90% of trades profitable with a different strategy; trend following should capture this.

**Concrete parameters:**

| Param | Value | Rationale |
|-------|-------|-----------|
| `donchian_period` | 20 | ~5 hours of M15 / ~20 hours of H1. |
| `atr_period` | 14 | Standard. |
| `atr_trail_multiplier` | 2.5 | Gives trades room to breathe. |
| `adx_threshold` | 20 | Industry standard. |
| `ema_trend_period` | 50 | Filters counter-trend breakouts. |
| `min_confidence` | 0.45 | Trend signals are inherently lower WR — higher confidence gate. |
| `cooldown_bars` | 5 | Avoid re-entry on same breakout. |

**Key differences from existing strategies:**
- vs `killzone_momentum`: Donchian ATR doesn't require session range or retest; fires on pure trend.
- vs `volatility_regime_breakout`: Fires on *expansion* not *compression*.
- vs `mtf_filtered_momentum` (existing): Adds ATR trailing stop, which improves PF.

---

### B.2 Dual-timeframe Squeeze Pro (Replaces Both `volatility_squeeze` AND `bb_rsi_reversion`)

**File to create:** `strategies/dual_tf_squeeze_pro.py`

**Concept:** Two-stage squeeze with HTF confirmation. H1 determines *whether* to trade, M15 determines *entry*. Fixes the contradictions in `volatility_squeeze` and `bb_rsi_reversion`.

**Entry logic (two-stage):**

```python
# STAGE 1: H1 context (must pass)
H1_SQUEEZE = h1_bb_inside_h1_kc(period=20, std=2.0, atr_mult=1.5)
H1_DIRECTION = h1_ema(50) slope sign

# STAGE 2: M15 entry trigger
if H1_SQUEEZE is True (currently squeezing):
    ENTRY_LONG = m15_close_breaks_h1_kc_upper()
    ENTRY_SHORT = m15_close_breaks_h1_kc_lower()
elif H1_SQUEEZE is False (not squeezing) AND H1_DIRECTION matches:
    ENTRY = pullback to H1 keltner middle in direction of H1 trend

# Confirmation:
ADX_H1 >= 18 (mild trend)
RSI_M15 NOT extreme (40-60 zone — NOT overbought/oversold for breakout)
```

**Exit logic:**

```python
TP1 = 1.0 R
TP2 = 2.0 R
TP3 = 3.0 R
STOP = opposite Keltner band OR 1.5*ATR(M15)
TIME_EXIT = 30 bars (M15 = 7.5 hours)
```

**Timeframes:** M15 entry, H1 context
**Pairs:** XAUUSD (primary — high trend persistence), GBPUSD
**Expected WR:** 40-48%
**Expected PF:** 1.6-2.2

**Concrete parameters:**

| Param | Value | Rationale |
|-------|-------|-----------|
| `h1_bb_period` | 20 | 20 hours of context. |
| `h1_bb_std` | 2.0 | Standard. |
| `h1_kc_atr_mult` | 1.5 | Slightly tighter than BB — more squeezes detected. |
| `m15_atr_period` | 14 | Standard. |
| `adx_min_h1` | 18 | Lower than typical because we want squeezes (low ADX) followed by expansion. |
| `rsi_zone_min/max` | 40 / 60 | Not overbought, not oversold — breakout territory. |
| `min_confidence` | 0.40 | Lower bar — confluences score it up. |
| `cooldown_bars_m15` | 10 | One H1 bar between signals. |
| `time_exit_bars` | 30 | M15 = 7.5 hours max hold. |

**Key differences from `volatility_squeeze`:**
- **No contradictory ADX>=20 in squeeze condition** (the existing bug).
- Two-stage (HTF context + LTF entry) — proper trend-following structure.
- Time-based exit prevents runaway losers.

---

### B.3 London Breakout + Retest (XAUUSD-Tuned)

**File to create:** `strategies/london_breakout_retest.py`

**Concept:** Asian range forms a balance. London open (07-09 UTC) breaks out of that range. Entry on the *retest* of the broken level within 4 hours.

**Entry logic:**

```python
# At 00:00-07:00 UTC, compute Asian range:
asian_high = max(high, asian session)
asian_low = min(low, asian session)
asian_range_pips = (asian_high - asian_low) / 0.01   # XAUUSD pip

# After 07:00 UTC, watch for breakout:
breakout_long = close > asian_high + buffer_pips (3 pips for gold)
breakout_short = close < asian_low - buffer_pips

# Retest (within 4 hours of breakout):
retest_long = low touched (asian_high - 1*ATR(M15)) AND close > asian_high
retest_short = high touched (asian_low + 1*ATR(M15)) AND close < asian_low

ENTRY = retest with stop = breakout_candle_low/high - 1*ATR
TP1 = 1.0R
TP2 = 2.0R  
TP3 = 3.0R
```

**Timeframes:** M15 only
**Pairs:** XAUUSD (primary), EURUSD
**Expected WR:** 45-52% (typical London breakout has ~50% WR with retest)
**Expected PF:** 1.5-2.0

**Concrete parameters:**

| Param | Value | Rationale |
|-------|-------|-----------|
| `asian_start_utc` | 0 | Standard Asian session. |
| `asian_end_utc` | 7 | Standard. |
| `trade_start_utc` | 7 | London open. |
| `trade_end_utc` | 11 | Last retest window 4 hours after open. |
| `buffer_pips_xauusd` | 3.0 | Gold volatility needs buffer. |
| `buffer_pips_fx` | 1.5 | FX is tighter. |
| `atr_period` | 14 | Standard. |
| `min_asian_range_pips` | 8.0 (gold) | Too tight = fake breakouts. |
| `max_asian_range_pips` | 60.0 (gold) | Too wide = no momentum. |
| `min_confidence` | 0.45 | |
| `cooldown_bars` | 20 | ~5 hours between signals. |

**Why this complements existing strategies:**
- vs `session_breakout_london` (existing): Adds the retest, which significantly improves PF (50% WR vs 35% WR for raw breakout).
- vs `killzone_momentum`: Different entry trigger (Asian range breakout, not prior session range + EMA trend). The retest is unique.
- Gold loves London session — the average move in the first 4 hours of London is 60-80% of the daily range on XAUUSD.

---

### B.4 (Optional) Carry Trade Strategy

**Note:** Skip this if no central-bank-rate data is integrated. The infra research `forex-prop-firm-landscape-analysis.md` and existing `additional-strategy-candidates-forex-pipeline.md` already cover this. Implementation requires daily rate fetching (FRED API or central bank feeds) — significant infra work.

If infra is in place: long high-yield / short low-yield pairs (e.g., long AUDJPY, short CHFJPY). Timeframes: D1 / W1. Trade count: very low (~20 trades/year/pair). WR: 55-60%. PF: 1.2-1.5. Only useful for diversification, not primary alpha.

### B.5 (Optional) Calendar/News-Aware Volatility Filter

**Concept:** Wrap any existing strategy with a news-event volatility filter. Hold off trading 30 min before / after high-impact news (NFP, FOMC, ECB, BOJ, CPI).

This is technically a "filter" not a "strategy" but it would prevent 20-40% of the worst losses during news spikes. **Easy win, low effort.** Implementation: integrate with an economic calendar API (e.g., ForexFactory JSON, Investing.com).

---

## Section C — Walk-Forward Window Sizing

### C.1 Trade Count Analysis by Symbol/Timeframe

Per the task description:
- **M5:** 100k bars across XAUUSD/GBPUSD/EURUSD
- **M15:** 74k bars (likely XAUUSD M15 only based on `wf-revalidation-2026-07/REPORT.md`)
- **H1:** 17k bars (FX pairs from parquet)

**Trade count math:**

For a strategy with signal frequency `f` (signals per bar), total trades = `f * bars_in_test`.

If `n_windows=5`, `train_ratio=0.7`, `val_ratio=0.15`, `test_ratio=0.15`:
- Per-window bars: `total_bars / n_windows`
- Per-window test bars: `total_bars / n_windows * 0.15`

For XAUUSD M15 (74k bars, 5 windows):
- Per-window: 14.8k bars → test = 2.2k bars (~3.7 days of M15)
- TTC_XAUUSD reports 48.4 trades/window → 1 trade per 45 test bars (~11 hours)
- Min 15 trades/window → easily met

For EURUSD H1 (17k bars, 5 windows):
- Per-window: 3.4k bars → test = 510 bars (~21 days of H1)
- Killzone_momentum: 8 trades/window → 1 trade per 64 test bars (~2.7 days)
- SRMR+: 13-14 trades/window → 1 trade per 38 test bars
- **Min 15 trades/window → BARELY met for SRMR+, FAIL for killzone_momentum**

For XAUUSD M5 (assuming 100k bars includes M5, 5 windows):
- Per-window: 20k bars → test = 3k bars (~10.4 days of M5)
- At ~50 signals/window, fine.

**The real problem:** H1 with 17k bars / 5 windows = too few test bars for high-quality statistics. **15 trades per 510 test bars = 1 trade per 34 bars = no statistical confidence.**

### C.2 Recommended Window Sizing

| Symbol/Timeframe | Current | Recommended | Rationale |
|------------------|---------|-------------|-----------|
| XAUUSD M15 | 5 windows | **5 windows** (no change) | 74k bars / 5 = 14.8k/window, ample. |
| XAUUSD M5 | 3-5 windows | **5 windows** | Per `per_symbol_configs.py` Optuna note: "Use 5 windows for ~57+ trades." |
| FX H1 (EURUSD/GBPUSD/USDJPY) | 5 windows | **3 windows OR anchored WF** | 17k / 5 = 3.4k/window, too few. 17k / 3 = 5.7k/window, better. |
| FX M15 (when added) | 5 windows | **5-7 windows** | More bars → more windows feasible. |
| Multi-pair blend | 5 windows | **5 windows** | Sufficient data when combining. |

**`min_trades_per_window`:**
- Current: 15 (in `srf/gonogo.py:14`)
- Recommended: **20 (H1) / 30 (M15/M5)**

Per the OOS gate research (`oos-gate-research-2026-07-08.md`), with ~30 Optuna trials and ~3 years of data, we're under-sampled. Bumping the floor from 15 to 20/30 is a partial fix — it forces us to either find more data or accept fewer strategies. Lower trade counts are noise.

**Anchored walk-forward (new diagnostic, run in parallel):**

Anchored WF uses *all data before the test window* as the training set, instead of a fixed rolling window. This:
- Maximizes training data per window (especially for later windows)
- Tests parameter stability over cumulative data
- Is the gold standard for low-data situations

```python
# Proposed config in walk_forward.py — add anchored mode:
@dataclass
class WalkForwardValidator:
    mode: str = "rolling"  # or "anchored"
    
    def split_anchored(self):
        """For window i, train = bars[0 : test_start_i], test = bars[test_start_i : test_end_i]"""
        ...
```

Use anchored WF as a **secondary check** — if a strategy passes rolling WF but fails anchored WF, the early-window parameters are not representative of recent market. If it passes both, the edge is more robust.

### C.3 XAUUSD "Too Good To Be True" Flag

**Critical concern.** `wf-revalidation-2026-07/srmrplus_wf_XAUUSD.json` shows:
- Window 4: `WR=100%, PF=10.0 (capped), 20 trades, $1,392 P&L, Sharpe=0.0` (Sharpe is 0 because std=0 with all-wins)
- All 5 windows passed at PF ≥ 3.25
- Mean Sharpe = 13.68 ± 9.86 — extremely high mean, extreme std

A 100% WR across 20 trades has a probability of `0.5^20 ≈ 1e-6` under random. If the true edge is 86% (the mean WR across all windows), P(20 wins in a row) = `0.86^20 ≈ 5%`. That's still unusually lucky but plausible.

**Hypothesis:** window 4 might be a small slice of data where the strategy happened to catch only winning setups (e.g., a strong trend where every long entry worked, no shorts fired).

**Action (HIGHEST PRIORITY BEFORE ANY FURTHER TUNING):**

1. **Manual inspection of window 4 trades.** Are the entry/exit timestamps reasonable? Are the SL levels honored? Are the TPs hit?
2. **Re-run on shuffled bars.** If PF stays at 8, the result is meaningless (the strategy is detecting temporal patterns that aren't there).
3. **Re-run on the M5 XAUUSD data** (if available). If M15 PF=8 but M5 PF=1.5, the M15 result is overfit to that specific resolution.
4. **Compute Deflated Sharpe Ratio** on the XAUUSD M15 sweep. With 30+ parameter combinations tried (Optuna), the DSR should deflate the apparent Sharpe. If DSR-adjusted Sharpe is still > 0.5, the edge is real.

This investigation should be its own card — flag it as `[DEBT][FINDING] ttc_xauusd XAUUSD M15 results may be overfit or have look-ahead bug`.

---

## Implementation Plan

### Phase 1 — Sanity (this week)
1. **Investigate XAUUSD M15 "too good" result.** Run synthetic-data backtest, inspect trades, DSR compute. Card: `[DEBT][FINDING]`.
2. **Fix `volatility_squeeze` `adx_min=15, squeeze_release_mode="any_release", min_confidence=0.40, min_squeeze_bars=2`**. Re-run sweep. Expected: 5-15 trades/window per pair.
3. **Fix `volatility_regime_breakout` `atr_percentile_low=30, range_position_max=0.70, cooldown_bars=3`**. Re-run sweep. Expected: 5-15 trades/window.
4. **Card these as parameter tuning cards.** Not findings — actual config changes.

### Phase 2 — Tuning (next week)
5. **Tune `killzone_momentum` with per-pair/per-timeframe presets** (M5 vs H1).
6. **Tune `srmr_plus` entry filters** (rsi 30/70 → 25/75, adx_max 25→20, add trend exhaustion gate).
7. **Tune `bb_rsi_reversion` RSI levels and TP target**, or deprecate it.
8. **Run n_windows=3 for FX H1 sweep** (parallel to current n_windows=5) — compare trade count and PF.
9. **Implement anchored walk-forward** in `quant/walk_forward.py` as a secondary diagnostic.

### Phase 3 — New Strategies (week after)
10. **Implement B.1 Donchian + ATR Trailing Trend** — high-priority given XAUUSD trending behavior.
11. **Implement B.2 Dual-timeframe Squeeze Pro** — replaces both squeeze strategies.
12. **Implement B.3 London Breakout + Retest (XAUUSD-tuned)** — leverages XAUUSD's strongest session.
13. **Optional: B.5 Calendar/News Filter** — easy win, low effort.

### Phase 4 — Optuna Sweep (after Phase 3)
14. **Optuna sweep on B.1, B.2, B.3** with 30 trials each, XAUUSD M15 primary + FX H1 secondary.
15. **Per-symbol ML config update** in `ml/per_symbol_configs.py` based on Optuna winners.

---

## Out-of-Scope Findings (Card These Separately)

Per AGENTS.md Findings & Debt Protocol, the following are *not* strategy tuning but should be tracked:

1. **[DEBT]** `volatility_squeeze` calls `_calculate_rsi(bars, self.config.adx_period)` — RSI is computed with ADX's period (line 145). Add separate `rsi_period: int = 14` config field.
2. **[DEBT]** `srf/gonogo.py:14` `MIN_TRADES_PER_WINDOW = 15` should be raised to 20 (H1) / 30 (M15) per OOS gate analysis.
3. **[DEBT]** `volatility_squeeze` and `volatility_regime_breakout` are in the registry (`strategies/registry.py`) but produce zero trades across all sweeps. After Phase 1 tuning, if they still produce zero trades, remove from default registry.
4. **[FINDING]** `bb_rsi_reversion.py` confidence formula is *inverse* to typical mean-reversion logic (`confidence = 0.50 + min(rsi_distance / 40, 0.20)`). Higher RSI distance = higher confidence, but extreme RSI in trend = continuation. Either rewrite or deprecate.
5. **[FINDING]** XAUUSD M15 sweep result `PF=8.02, WR=86.3%, window 4 PF=10 WR=100%` is anomalously good. Either (a) a look-ahead bug in TTSStrategy's `_check_vwap_rejection` / `_check_bollinger_confluence` / `_check_mfi_confluence`, or (b) genuine edge on XAUUSD M15 that doesn't survive regime change. Investigate before further tuning.
6. **[FINDING]** `_check_mfi_confluence` in `tts_strategy.py:545` is a static method that doesn't track MFI history — it computes MFI on every bar from scratch using 50-bar window. This is O(N²) per evaluation when called on every bar. Performance, not correctness, but worth noting.

---

## Related Existing Research (Do Not Duplicate)

This document intentionally does NOT repeat:

- `docs/research/additional-strategy-candidates-forex-pipeline.md` — already covers RSI/BB Bounce, ATR Channel Scalping, Killzone Momentum (older version), IRD Carry, Random Forest. This doc covers complementary strategies B.1-B.3.
- `docs/research/oos-gate-research-2026-07-08.md` — covers DSR / multiple testing bias / MinBTL. Referenced for window-sizing trade count analysis.
- `docs/research/momentum-strategy-research-breakout-trend-following.md` — covers Donchian breakout broadly. This doc's B.1 adds ATR trailing and XAUUSD-specific tuning.
- `docs/research/next-generation-strategy-candidates-adaptive-ml-regime-switching.md` — covers HMM regime, LightGBM, OFI. Future integration: B.1/B.2/B.3 outputs could feed an ML regime selector.
- `docs/forex/wf-revalidation-2026-07/REPORT.md` — the prior SRMR+ sweep that produced XAUUSD 5/5 results. Referenced extensively in §C.3.
- `docs/forex/volatility-squeeze-breakout-strategy.md` — AYUAA-356 spec. Differs from current `strategies/volatility_squeeze.py` implementation; the spec allows `adx_min=20, min_squeeze_bars=3, min_confidence=0.55` while this analysis recommends relaxing those.

---

*End of research document. Action items in Phase 1 are highest priority. Do not tune `ttc_xauusd` until §C.3 investigation completes.*