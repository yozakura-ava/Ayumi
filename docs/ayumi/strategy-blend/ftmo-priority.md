# Ayumi FTMO Strategy Priority — Mean Reversion First

**Author:** Tsukasa (builder)
**Generated:** 2026-07-07 (heartbeat cycle)
**Source research:** SRB-AYUMI-005 (Satoshi, Tier 1, 2026-07-01)
**Card:** 4a8120fe — `[AYUMI] Prioritize mean-reversion strategies with 1:1-1:1.5 R:R for FTMO challenge phase`
**Workspace:** `$AYUMI_ROOT` (branch `autodev/ayumi-regime-ftmo-audit`)
**sp_estimate:** 1.0

---

## 1. Decision (Craig call required)

**Recommended FTMO challenge-phase blend:** mean-reversion strategies with 1:1–1:1.5 R:R as the **primary** posture, with one trend-following strategy kept at reduced position size (0.25–0.5% per trade) as a secondary hedge, and breakout strategies **disabled** until a regime filter is in place.

Concretely, from the current 13-strategy registry, the FTMO challenge should enable:

| Tier | Strategies | Position sizing |
|---|---|---|
| **PRIMARY (FTMO-friendly)** | `session_range_mean_reversion`, `bb_rsi_reversion`, `srmr_plus` | 1.0% risk per trade |
| **SECONDARY (sized-down trend)** | `usdjpy_d1_trend` (if file exists in branch) or `mtf_filtered_momentum` | 0.25–0.5% risk per trade |
| **DISABLED for challenge phase** | `killzone_momentum`, `momentum`, `ttc_xauusd`, `volatility_squeeze`, `session_breakout_*`, `rsi_threshold`, `session_range_mr_ict_filtered`¹, `test_canary` | n/a |

¹ `session_range_mr_ict_filtered` is MR-archetype but is gated by ICT filters that reduce signal frequency; it can be promoted to PRIMARY once ICT confluence coverage is validated. Treat as conditional.

**Re-enable the disabled strategies after funded status is achieved**, when the 10% trailing loss constraint is replaced by the funded-account rules (which are more permissive).

This recommendation is consistent with `docs/research/ayumi-signal-audit.md` (which identified Root Cause #1: missing `strategy_timeframes` argument — **fixed in this branch** by `strategy_timeframes=STRATEGY_TIMEFRAMES` in `ForwardTestConfig`) and with SRB-AYUMI-005's mathematical finding that **drawdown survival, not expectancy, is the binding FTMO constraint**.

---

## 2. SRB-AYUMI-005 key finding (the math)

**The single biggest predictor of FTMO pass rate is not strategy type, but equity-curve smoothness under drawdown constraint.**

FTMO's binding constraints (per `https://ftmo.com/en/trading-objectives/`):
- 3% daily loss limit
- 10% trailing overall loss limit

These define a **survival budget** that low-WR, high-R:R strategies can breach before any winning trade lands.

### Drawdown survival math (binomial, exact)

For a strategy risking 1% per trade on a $100k account:

**Strategy A (mean reversion, 60% WR, 1:1 R:R):**
- P(3 consecutive losses) = 0.4³ = 6.4% — survives
- P(5 consecutive losses) = 0.4⁵ = 1.0% — survives
- P(10 consecutive losses) = 0.4¹⁰ = 0.01% — survives

**Strategy B (trend following, 35% WR, 1:3 R:R):**
- P(3 consecutive losses) = 0.65³ = 27.5% — common
- P(5 consecutive losses) = 0.65⁵ = 11.6% — happens ~once per 100 trades
- P(8 consecutive losses) = 0.65⁸ = 3.2% — breaches 8% with 1% risk
- P(10 consecutive losses) = 0.65¹⁰ = 1.3% — **breaches 10% trailing loss**

Strategy B has higher *theoretical* expectancy (0.40 vs 0.20 per trade) but is **structurally fragile** under FTMO's 30-day challenge window. Strategy A passes more often *because variance is bounded*, not because expectancy is higher.

### The optimal R:R for FTMO is NOT 1:3 (the internet default)

Under the 3%/10% envelope, **1:1 to 1:1.5 R:R with 55–65% win rate** is mathematically more robust than 1:3 with 35% WR.

| R:R | Break-even WR | At 60% WR expectancy | At 35% WR expectancy |
|---|---|---|---|
| 1:1 | 50% | +0.20/trade | −0.30/trade |
| 1:1.5 | 40% | +0.30/trade | −0.275/trade |
| 1:3 | 25% | +0.80/trade | +0.40/trade (loses 3% before winning) |

Source: Audacity Capital, "Risk to Reward Ratio in Prop Trading" (Jun 16 2026), via SRB-AYUMI-005 §4.2.

---

## 3. Audit of current strategy blend

The current registry (`src/forex_bot/strategies/registry.py`) defines 13 strategies. Audited against the SRB-AYUMI-005 archetype criteria:

### 3.1 Mean reversion strategies — match 1:1–1:1.5 archetype ✓

| Strategy | Symbols | Timeframes | R:R profile | FTMO fit |
|---|---|---|---|---|
| `session_range_mean_reversion` | EURUSD, GBPUSD | H1 | tp1=1.0, tp2=1.5 | **Excellent** — tight R:R, smooth equity, default ON |
| `bb_rsi_reversion` | EURUSD, GBPUSD, USDJPY, AUDUSD | H1, M15 | tp1=1.0, tp2=1.5 | **Excellent** — Bollinger+RSI is FTMO's own documented working setup (SRB-AYUMI-005 §4.4) |
| `srmr_plus` | EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD | H1 | tp1=1.5, tp2=1.5 | **Excellent** — sits at upper bound of 1:1.5; comment in source notes it was raised from 1.0 to pass `min_risk_reward=1.5` gate |
| `session_range_mr_ict_filtered` | EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD | H1, H4 | ICT-gated (no explicit tp_rr visible; uses session range SL) | **Conditional** — MR-archetype but ICT confluence gating may suppress frequency; enable after ICT validation |
| `rsi_threshold` | EURUSD (registered for EURUSD only) | M15 | tp1=1.0, tp2=2.0, tp3=3.0 | **Mixed** — TP2/TP3 exceed 1:1.5; effective blend is hybrid |

### 3.2 Momentum / trend strategies — 1:3 archetype, FTMO-fragile ⚠

| Strategy | Symbols | Timeframes | R:R profile | FTMO fit |
|---|---|---|---|---|
| `killzone_momentum` | EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD | M15, H1 | tp1=1.0, tp2=2.0, tp3=3.0 | **Fragile** — 30–40% WR expected; needs 0.25–0.5% sizing |
| `momentum` (raw) | EURUSD | M15 | tp1=1.0, tp2=2.0, tp3=3.0 | **Fragile** — same; reduce size or disable |
| `mtf_filtered_momentum` | EURUSD, GBPUSD, USDJPY | M15, H1, H4 | Multi-TF filter; tp1/tp2/tp3 multi-tier | **Conditional** — MTF filter may improve WR; keep at 0.5% sizing |
| `ttc_xauusd` | XAUUSD | M15 | not visible in source (defaults assumed) | **Disable** — XAUUSD in mid-correction per SRB-AYUMI-007; trend-following is wrong archetype right now |

### 3.3 Trend (D1) — usable with sizing reduction ⚠

| Strategy | Symbols | Timeframes | R:R profile | FTMO fit |
|---|---|---|---|---|
| `usdjpy_d1_trend` (registered but file not found in branch — possible drift) | USDJPY | D1 | not visible | **Conditional** — USDJPY is in intervention regime per SRB-AYUMI-007 §4.3; trend will fail at 160 ceiling |

### 3.4 Breakout strategies — high variance, regime-dependent ✗

| Strategy | Symbols | Timeframes | R:R profile | FTMO fit |
|---|---|---|---|---|
| `volatility_squeeze` | EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD | H1, M15 | tp1=1.0, tp2=2.0, tp3=3.0 | **Disable** — bimodal equity; needs regime filter (none present) |
| `session_breakout_london` | GBPUSD, EURUSD | M15 | not visible | **Disable** |
| `session_breakout_ny` | GBPUSD, EURUSD | M15 | not visible | **Disable** |
| `session_breakout_asian` | USDJPY | M15 | not visible | **Disable** — USDJPY in intervention regime |

### 3.5 Scalping — not represented in current registry

No scalping strategies in the registry. Per SRB-AYUMI-005 H3, this is correct: retail prop-firm spreads + commissions consume 30–60% of scalp profit on 0.5–1.5 pip targets. Scalping should not be added.

### 3.6 Test / canary — not for production

| Strategy | Symbols | Timeframes | FTMO fit |
|---|---|---|---|
| `test_canary` | (test only) | (test only) | **Disable** — health check, not a strategy |

---

## 4. Recommended FTMO challenge-phase blend

### 4.1 Primary (must be enabled)

- **`session_range_mean_reversion`** (EURUSD, GBPUSD, H1)
- **`bb_rsi_reversion`** (EURUSD, GBPUSD, USDJPY, AUDUSD, H1+M15)
- **`srmr_plus`** (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, H1)

These three cover 5 symbols with MR-archetype R:R profiles. Their combined signal density should comfortably exceed FTMO's pace requirement without breaching daily/trailing loss constraints.

**Pair coverage:** all USD-basket pairs from SRB-AYUMI-001's Core 4 (EURUSD, AUDUSD) plus GBPUSD and USDCAD. USDJPY and XAUUSD are intentionally excluded from PRIMARY per the regime analysis in `2026-07-classification.md` (card f9c0e025).

### 4.2 Secondary (reduced size, conditional)

- **`mtf_filtered_momentum`** at 0.5% per-trade risk — MTF filter may stabilize WR; if it under-performs after 2 weeks, disable.

### 4.3 Disabled for challenge phase

- `killzone_momentum`, `momentum`, `ttc_xauusd`, `volatility_squeeze`, `session_breakout_*`, `rsi_threshold`, `usdjpy_d1_trend`, `session_range_mr_ict_filtered` (conditional re-enable after ICT validation), `test_canary`.

Rationale: each disabled strategy either (a) has wrong R:R profile for FTMO envelope, (b) is in a wrong-archetype regime per SRB-AYUMI-007, or (c) lacks a regime filter.

### 4.4 Post-challenge (funded account)

After passing the challenge, re-enable strategies that the regime analysis says are appropriate for the funded phase:
- USDJPY: `mtf_filtered_momentum` and `usdjpy_d1_trend` once the BoJ intervention campaign ends
- XAUUSD: `volatility_squeeze` and `ttc_xauusd` once the late-2025 correction completes
- `session_range_mr_ict_filtered` once ICT confluence is validated

---

## 5. Per-symbol coverage matrix (post-priority)

| Symbol | PRIMARY (MR, 1:1–1:1.5) | Coverage status |
|---|---|---|
| EURUSD | session_range_mr, bb_rsi_mr, srmr_plus | ✓ Triple coverage |
| GBPUSD | session_range_mr, bb_rsi_mr, srmr_plus | ✓ Triple coverage |
| USDJPY | bb_rsi_mr, srmr_plus | △ Secondary; regime caveat — see 2026-07-classification.md |
| AUDUSD | bb_rsi_mr, srmr_plus | △ Secondary; no SRMR coverage |
| USDCAD | srmr_plus | △ Single coverage |
| XAUUSD | (none in PRIMARY) | ✗ Excluded — mid-correction regime |

**Observation:** EURUSD + GBPUSD have triple MR coverage. This is intentional per SRB-AYUMI-001's "never run GBPUSD and EURUSD in the same direction simultaneously" rule (correlation +0.77, see 2026-07-classification.md §3.3). The strategy selector must enforce direction-aware correlation limits.

---

## 6. Open questions for Craig

1. **Challenge phase position sizing baseline** — confirm 1.0% per trade for PRIMARY MR strategies, 0.5% for SECONDARY MTF momentum.
2. **Symbol universe** — should the challenge phase restrict to just EURUSD + GBPUSD (highest MR conviction, simplest correlation story) or run the full USD basket?
3. **Challenge vs. verification phase** — should this blend change between Phase 1 (challenge) and Phase 2 (verification)?
4. **Should `usdjpy_d1_trend` be re-introduced post-challenge** despite the intervention regime caveat, given the long D1 timeframe may avoid the whipsaw?

---

## 7. Caveats and unknowns

1. **No live win-rate data for current strategies.** The audit is based on declared R:R profiles in source code, not measured WR from forward tests. Backtest validation of the recommended PRIMARY trio is a separate work item.
2. **`session_range_mr_ict_filtered` R:R profile not visible** in source code grep — need to confirm TP levels before relying on it as MR-archetype.
3. **`usdjpy_d1_trend` not found in branch** despite being in registry. Possible drift between registry metadata and source code; flag for cleanup.
4. **The `strategy_timeframes` fix (Root Cause #1 from `ayumi-signal-audit.md`) is present in this branch** (confirmed at `scripts/launch_blend_forward_test.py` line ~545). Signals are now flowing through the correct timeframes.

---

## 8. Bibliography

- SRB-AYUMI-005 (Satoshi, Tier 1, 2026-07-01) — primary source for R:R math, FTMO envelope analysis, strategy archetype recommendations
- Audacity Capital, "Risk to Reward Ratio in Prop Trading," https://audacity.capital/trading-guides/risk-to-reward-ratio/, Jun 16 2026
- FTMO Trading Objectives, https://ftmo.com/en/trading-objectives/
- FTMO Academy, "Mean Reversion + Divergence Setup Strategy," https://ftmo.com/en/blog/mean-reversion-divergence-setup-strategy/
- `docs/research/ayumi-signal-audit.md` — context for the "all no_signal" symptom and the strategy_timeframes fix
- `src/forex_bot/strategies/registry.py` — current 13-strategy registry
- Companion doc: `docs/ayumi/regime/2026-07-classification.md` (card f9c0e025)

---

## 9. Out-of-scope finding (flagging to Ava)

While auditing the live forward-test state, I observed in `logs/forward_test.log`:

```
[B5 Health] ticks=112693 tps=4.84 bars=277 signals=21 traded=0 live_fills=0 ...
WARNING  ⚠️  live_fills=0 but signals_generated=21 — orders may not be reaching cTrader
```

**21 signals generated but 0 trades / 0 live fills** suggests a downstream execution-path issue unrelated to the no_signal config bug or regime analysis. This is out of scope for this card but is a separate, actionable investigation. Recommend Ava card this as a `[FINDING]` follow-up.