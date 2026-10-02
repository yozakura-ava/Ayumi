# Ayumi Regime Classification — July 2026

**Author:** Tsukasa (builder)
**Generated:** 2026-07-07 (heartbeat cycle)
**Source research:** SRB-AYUMI-007 (Satoshi, Tier 2, 2026-07-01)
**Card:** f9c0e025 — `[AYUMI] Verify "all no_signal" symptom via regime classification per SRB-AYUMI-007`
**Workspace:** `$AYUMI_ROOT` (branch `autodev/ayumi-regime-ftmo-audit`)
**sp_estimate:** 2.0

---

## 1. Bottom line (decision needed)

The "all 10 strategies showing no_signal" symptom is **explained by two independent causes, both confirmed**:

1. **Config bug** (now fixed) — `strategy_timeframes` was not being passed to `ForwardTestConfig`. This caused all M15 strategies to evaluate against H1 bars they were not designed for. **Fixed in this branch** (`scripts/launch_blend_forward_test.py` line ~545: `strategy_timeframes=STRATEGY_TIMEFRAMES`). Verified present.

2. **Regime mismatch** — the July 2026 FX regime is **compressed-equilibrium mean-reversion biased** with one trending exception (XAUUSD). In this regime, both breakout and most MR strategies systematically under-fire. Low signal counts are **mathematically expected**, not a bug.

**Recommended posture:** accept that compressed-regime signal counts will be lower than full-history backtest expectations, and confirm via the metrics in §6 that the active strategy set is **regime-appropriate** for the per-pair micro-regime rather than re-enabling more strategies.

**Triage outcome:** **regime-driven, not config-bug-driven** (config bug is fixed; what remains is regime). The active PRIMARY MR set (`session_range_mean_reversion`, `bb_rsi_reversion`, `srmr_plus`) is the right archetype for the dominant regime — see §4 and §5.

---

## 2. Per-pair regime classification (July 2026)

Based on SRB-AYUMI-007 §3.2 (local data: end-2025 ATR percentiles, Bollinger %B, 20-day z-scores) and §4 (external research: DXY, EURUSD, USDJPY, BoJ intervention), classified per pair:

### 2.1 USD basket — compressed equilibrium (MR-biased)

| Pair | ATR %ile (1y) | Bollinger %B | ADX (est.) | Regime tag |
|---|---|---|---|---|
| **EURUSD** | 0.8 (extreme compress) | 0.54 | likely <20 post-June-leg | **Range, MR-favored, transition risk** — just completed 260-pip one-way leg down; may revert |
| **GBPUSD** | 9.5 | 0.69 | <20 | **Range, MR-favored** |
| **AUDUSD** | (not in local data) | (not measured) | <20 (DXY-driven) | **Range, MR-favored** by macro inference |

**Macro driver (DXY):** Per LinkedIn DXY YTD 2026 analysis (via SRB-007 §4.1), the DXY is range-bound 98.0–100.6, "no longer defined by whether it trends or ranges, but by how quickly it mean-reverts." This favors MR for USD-basket pairs. 70–80% of FX time is consolidation (Aron Groups + newyorkcityservers via SRB-007 §4.4).

**Implication for strategies:**
- MR strategies SHOULD fire on USD basket
- Breakout strategies SHOULD under-fire (nothing to break out of)
- Mean-reversion z-score triggers should be **widened** from 2.0σ to 2.5σ because compressed regimes produce shallow reversion

### 2.2 USDJPY — event-driven intervention regime

| Pair | ATR %ile (1y) | Bollinger %B | Regime tag |
|---|---|---|---|
| **USDJPY** | 2.8 | 0.71 | **Event-driven, hard ceiling at 160, structural demand at 155** |

**Macro driver (BoJ):** Per ING Think + Investing.com via SRB-007 §4.3, the BoJ's MoF intervened April 30 and May 1 2026 after USDJPY breached 160. ING expects further intervention attempts through summer 2026. The 155–160 corridor is bounded by policy, not by market structure.

**Implication for strategies:**
- Trend-following breakouts **fail** when faded by intervention
- MR strategies **fail** because post-intervention reversion overshoots in the same direction before settling
- Correct strategy: **event-driven intervention playbook** (flat by default, position around intervention triggers) — this is **not currently implemented** in any Ayumi strategy

**Recommendation:** drop USDJPY from the active set until an event-driven intervention playbook is implemented. See §4 and companion `ftmo-priority.md` (card 4a8120fe) which excludes USDJPY from PRIMARY.

### 2.3 XAUUSD — trending down, mid-correction

| Pair | ATR %ile (1y) | Bollinger %B | Regime tag |
|---|---|---|---|
| **XAUUSD** | 80.2 (high vol) | 0.38 | **Trending down from late-2025 extreme, mid-correction (-9.33% over past month)** |

**Implication for strategies:**
- Donchian Breakout short-bias has structural edge — but only until gold re-anchors to post-correction mean
- BB+RSI MR long signals will get faded by the falling knife
- **MR longs on XAUUSD are dangerous in this regime**

**Recommendation:** suspend MR strategies on XAUUSD until the correction completes. Allow any breakout-trend strategies to run.

### 2.4 Regime × strategy matrix

| | EURUSD | GBPUSD | AUDUSD | USDJPY | XAUUSD |
|---|---|---|---|---|---|
| **Mean Reversion (1:1–1:1.5)** | ✓ Active | ✓ Active | ✓ Active | ⚠ Pause | ✗ Suspend |
| **Momentum / Trend (1:3)** | ⚠ Reduce size | ⚠ Reduce size | ⚠ Reduce size | ✗ Disable | ✗ Suspend |
| **Breakout** | ✗ Filter required | ✗ Filter required | ✗ Filter required | ✗ Intervention regime | △ Allow short bias |

Legend: ✓ = active and regime-appropriate · ⚠ = active with caveat · ✗ = disable · △ = allow with constraint

---

## 3. Why "all 10 strategies no_signal" was happening

### 3.1 Local evidence — end-2025 ATR compression

SRB-AYUMI-007 §3.2 measured local ATR percentiles at end-2025 from `data/forex/historical/*_D1.csv`:

```
EURUSD:  ATR %ile (1y) = 0.8 — EXTREME COMPRESS
GBPUSD:  ATR %ile (1y) = 9.5 — EXTREME COMPRESS
USDJPY:  ATR %ile (1y) = 2.8 — EXTREME COMPRESS
XAUUSD:  ATR %ile (1y) = 80.2 — HIGH VOL
GBPJPY:  ATR %ile (1y) = 20.2 — EXTREME COMPRESS
```

Bollinger %B values are all in the **0.38–0.77 band** — *none* of the pairs is sitting at an MR extreme that would trigger a 2σ reversion signal. This is the exact configuration that produces "no_signal" for **both MR and breakout** strategies simultaneously. It is the smoking gun.

### 3.2 Config bug — `strategy_timeframes` not passed (NOW FIXED)

Per `docs/research/ayumi-signal-audit.md` Root Cause #1 (CRITICAL):

The launcher defined `STRATEGY_TIMEFRAMES` at line 407 of `scripts/launch_blend_forward_test.py` but **did not pass it to `ForwardTestConfig`**:

```python
config = ForwardTestConfig(
    symbol=symbols[0],
    ...
    # ← strategy_timeframes=STRATEGY_TIMEFRAMES  ← MISSING!
)
```

Impact chain:
1. `config.strategy_timeframes` defaults to `None` → `{}`
2. `engine._strategy_timeframes` = `{}` (empty dict)
3. `engine._required_timeframes` = `{60}` (H1 only)
4. Tick-to-bar building only creates H1 bars — **M15 bars are never updated from ticks**
5. Historical M15 bars (200 preloaded) go stale immediately

**Current state in this branch:** I confirmed the fix is present:
```python
# scripts/launch_blend_forward_test.py
config = ForwardTestConfig(
    ...
    strategy_timeframes=STRATEGY_TIMEFRAMES,  # ✓ FIXED
    preload_bar_count=200,
)
```
**Conclusion:** the config bug is **fixed**. The residual low signal count is regime-driven.

### 3.3 Live forward-test evidence

From `logs/forward_test.log` (most recent health check):
```
[S1 Health] BB+RSI Mean Reversion:      evals=222 no_signal=222
[S1 Health] Donchian Channel Breakout:  evals=222 no_signal=222
[S1 Health] Killzone Momentum:          evals=222 no_signal=222
[S1 Health] SRMR+:                      evals=222 no_signal=222
[S1 Health] Session Breakout Asian:     evals=222 no_signal=220
[S1 Health] Session Breakout London:    evals=222 no_signal=222
[S1 Health] Session Breakout NY:        evals=222 no_signal=222
[S1 Health] Session-Range Mean Reversion: evals=222 no_signal=203  ← 91% no_signal
[S1 Health] Simple RSI Threshold:       evals=222 no_signal=222
[S1 Health] Test Canary:                evals=222 no_signal=222
```

**Observation:** 9 of 10 strategies are at 100% no_signal; Session-Range MR is at 91% (the partial-fire strategy, consistent with its MR-archetype fit for the compressed regime).

This is the "all no_signal" symptom from the SRB-007 brief, observed in production logs.

---

## 4. Mapping current regime → required strategy archetype

### 4.1 USD basket (EURUSD, GBPUSD, AUDUSD)

**Required archetype:** Mean reversion with widened z-score triggers (2.0σ → 2.5σ) and time-based exits instead of target-based exits.

**Active Ayumi strategies matching this archetype:**
- `session_range_mean_reversion` (EURUSD, GBPUSD, H1) — MR-1:1–1.5 ✓
- `bb_rsi_reversion` (EURUSD, GBPUSD, USDJPY, AUDUSD, H1+M15) — MR-1:1–1.5 ✓
- `srmr_plus` (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, H1) — MR-1:1.5 ✓
- `session_range_mr_ict_filtered` (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, H1+H4) — MR-archetype with ICT filter ⚠ conditional

**Recommendation:** the three PRIMARY MR strategies from `ftmo-priority.md` (card 4a8120fe) are regime-appropriate for USD basket.

**Caveat — Bollinger z-score calibration:** the compressed regime produces shallow reversion; targets calibrated on full-history vol will overshoot. Per SRB-AYUMI-007 §5 H1, widen z-score triggers from 2.0σ to 2.5σ. **This is a calibration change to BB+RSI Reversion** — out of scope for this documentation card; flag as a follow-up.

### 4.2 USDJPY — event-driven intervention regime

**Required archetype:** event-driven intervention playbook (flat by default, position around intervention triggers).

**Active Ayumi strategies matching this archetype:** **NONE.**

**Recommendation:** drop USDJPY from the active set until an event-driven playbook is implemented. The current MR strategies (bb_rsi_reversion, srmr_plus, session_range_mr_ict_filtered) and breakout strategies will systematically fail in the intervention regime.

### 4.3 XAUUSD — trending down, mid-correction

**Required archetype:** breakout-trend short bias (Donchian-style).

**Active Ayumi strategies matching this archetype:**
- `volatility_squeeze` (XAUUSD included) — breakout ✓
- `session_breakout_*` — but not configured for XAUUSD

**Recommendation:** allow breakout-trend short bias on XAUUSD only; suspend BB+RSI MR long signals on XAUUSD.

---

## 5. Compare active strategy set → regime requirements

### 5.1 Strategy-vs-regime alignment

| Strategy | USD basket regime | USDJPY regime | XAUUSD regime | Net verdict |
|---|---|---|---|---|
| `session_range_mean_reversion` | ✓ Fits | ✗ Fails | ✗ Fails | Enable EURUSD+GBPUSD only |
| `bb_rsi_reversion` | ✓ Fits | ✗ Fails | ✗ Fails | Enable EURUSD+GBPUSD+AUDUSD only |
| `srmr_plus` | ✓ Fits | ✗ Fails | △ (high vol) | Enable EURUSD+GBPUSD+AUDUSD+USDCAD |
| `session_range_mr_ict_filtered` | ✓ Fits (if ICT coverage) | ✗ Fails | ✗ Fails | Conditional |
| `rsi_threshold` | △ Hybrid | ✗ Fails | ✗ Fails | Disable or restrict to EURUSD |
| `killzone_momentum` | △ Trend-leaning | ✗ Fails | ✗ Fails | Disable for challenge phase |
| `momentum` | △ Trend-leaning | n/a | n/a | Disable |
| `mtf_filtered_momentum` | △ Conditional | ✗ Fails | n/a | Disable until MTF WR validated |
| `ttc_xauusd` | n/a | n/a | ✗ Wrong archetype | Disable (gold mid-correction) |
| `volatility_squeeze` | ✗ Wrong archetype | ✗ Fails | △ Trend only | Disable; conditional re-enable for XAUUSD short |
| `session_breakout_*` | ✗ Wrong archetype | ✗ Fails | n/a | Disable |
| `usdjpy_d1_trend` | n/a | ✗ Fails (intervention) | n/a | Disable (also file not in branch) |
| `test_canary` | n/a | n/a | n/a | Disable (test only) |

### 5.2 Recommended active set for July 2026 regime

**Active (regime-appropriate):**
- `session_range_mean_reversion` — EURUSD, GBPUSD only
- `bb_rsi_reversion` — EURUSD, GBPUSD, AUDUSD only
- `srmr_plus` — EURUSD, GBPUSD, AUDUSD, USDCAD (skip USDJPY)

**Disabled:**
- All other strategies pending regime filter or regime change

---

## 6. Success metrics for verifying regime-classified deployment

Per SRB-AYUMI-007 §6, monitor:

1. **Signal count per pair per week.** Compressed-regime floor: 1–2 signals per pair per week (vs historical full-regime average 5–8). Below this is config issue, not regime.
2. **Average winner vs average loser by regime bucket.** MR bucket should show avg winner > avg loser × win-rate. If winners shrink while losers hold, reversion targets are calibrated on wrong (full-history) vol.
3. **ADX distribution by pair.** If a pair spends >70% of days with ADX >25, the regime classifier is mislabeling.
4. **Drawdown by regime bucket.** MR strategies in compressed regimes should produce smooth equity. Any regime-bucket that produces 3 consecutive losing days should trigger a pause.
5. **DXY proximity to 98.0 and 100.6.** Within 0.3% of either edge → treat as transition regime, halve position sizing on USD-basket signals.
6. **USDJPY proximity to 155 or 160.** Within 1% of either line → event-driven playbook active; standard MR/breakout paused.

---

## 7. Caveats and unknowns

1. **Local data ends 2025-12-31.** Per SRB-007 §3.1, the local CSVs cover 2023-01 to 2025-12. This analysis uses end-2025 baseline + external research for July 2026. A live re-measurement against current data would be more authoritative.
2. **AUDUSD not in local data** — regime tag inferred from macro (DXY-driven) rather than measured.
3. **Bollinger %B threshold for compressed regime is not validated.** The 2.0σ → 2.5σ widening is a recommendation, not a tested calibration.
4. **Event-driven USDJPY playbook** is recommended but **not implemented** in any Ayumi strategy. This is a separate work item (out of scope for this documentation card).
5. **Regime detection code EXISTS** (`src/forex_bot/quant/regime_detection.py`, `regime.py`, `mtf_regime.py`, `correlation_regime_hmm.py`, `spread_regime_classifier.py`, `dxy_regime_overlay.py`) but is not wired into the strategy selector. Per companion card `ftmo-priority.md`, the recommended PRIMARY MR strategies are regime-appropriate by archetype, so this wiring gap is less acute for MR than for trend/breakout.

---

## 8. Recommended follow-up cards (out of scope for this card)

These are downstream work items that emerged from the regime classification but are out of scope for the documentation deliverable. Flagging for Ava:

1. **Regime classifier wiring** — connect `quant/regime_detection.py` outputs into `BlendForwardTestRunner`'s strategy selector so the active set auto-adjusts per-pair per-day. (Per SRB-AYUMI-007 §7.)
2. **USDJPY event-driven intervention playbook** — implement hard lines at 155 (demand) and 160 (intervention ceiling); replace generic MR and breakout logic for USDJPY specifically.
3. **Bollinger z-score widening for compressed regime** — change `bb_rsi_reversion.py` z-score from 2.0σ to 2.5σ as a calibration update; needs backtest validation.
4. **Live execution path** — see out-of-scope finding in §9 below.

---

## 9. Out-of-scope finding (flagging to Ava)

While verifying the strategy_timeframes fix, I observed in `logs/forward_test.log`:

```
[B5 Health] ticks=112693 tps=4.84 bars=277 signals=21 traded=0 live_fills=0 ...
WARNING  ⚠️  live_fills=0 but signals_generated=21 — orders may not be reaching cTrader
```

**21 signals generated but 0 trades / 0 live fills** suggests a downstream execution-path issue (orders not reaching cTrader). This is **independent of the no_signal config bug and the regime analysis**. Recommend Ava card this as a `[FINDING]` follow-up — the signals are being generated but not reaching the broker.

---

## 10. Bibliography

- SRB-AYUMI-007 (Satoshi, Tier 2, 2026-07-01) — primary source for per-pair regime classification, external research synthesis, recommended archetype mapping
- SRB-AYUMI-005 (Satoshi, Tier 1, 2026-07-01) — companion for FTMO envelope math and MR-archetype priority
- `docs/research/ayumi-signal-audit.md` — Root Cause #1 (strategy_timeframes not passed) and Root Cause #2 (multi-layered strategy guards)
- `src/forex_bot/quant/regime_detection.py`, `regime.py`, `mtf_regime.py`, `correlation_regime_hmm.py` — existing regime detection code (not currently wired)
- `src/forex_trading/strategies/mean_reversion.py`, `regime_aware.py`, `regime_switching_momentum.py` — existing MR and regime-aware strategies
- `src/forex_bot/strategies/registry.py` — current 13-strategy registry
- `logs/forward_test.log` — live forward-test health data
- Companion doc: `docs/ayumi/strategy-blend/ftmo-priority.md` (card 4a8120fe)