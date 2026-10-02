# USDJPY Intervention-Regime Strategy Archetypes

**Author:** Tsukasa (builder lane)
**Date:** 2026-07-07
**Source:** SRB-AYUMI-009 (Satoshi, Tier 2) — source file not found on disk; card notes used as primary input
**Card:** 80f02554-f151-4d19-a70b-fc3d381d542f

---

## 1. Problem Statement

USDJPY has no passing walk-forward strategy. The existing multi-pair strategies (SRMR+, BB+RSI Reversion, Killzone Momentum, Volatility Squeeze, etc.) are calibrated for mean-reversion or breakout dynamics typical of EURUSD/GBPUSD — not for an intervention-capped regime.

### Market Context (July 2026)

| Factor | Value | Impact |
|--------|-------|--------|
| US-JP 10Y spread | 1.73% (narrowing from 1.77%) | Structural JPY pressure |
| Spot rate | ~162.52 | Above BoJ/MoF intervention line (~160) |
| Intervention spend | ~$73B / ¥11.7T (Apr-May 2026) | Active defense confirmed |
| Regime | Bullish with intervention ceiling | Neither pure trend nor pure range |

Standard mean-reversion fails because the pair is structurally trending. Standard breakout fails because intervention caps upside spikes. Neither archetype models the intervention floor/ceiling dynamic.

---

## 2. Existing Strategy Audit

### 2.1 Registered Strategies Trading USDJPY

| Strategy | Type | Timeframe | Walk-Forward Result |
|----------|------|-----------|---------------------|
| SRMR+ | Mean Reversion | H1 | Not tested for USDJPY |
| BB+RSI Reversion | Mean Reversion | H1/M15 | Not tested for USDJPY |
| Killzone Momentum | Momentum | M15/H1 | Not tested for USDJPY |
| MTF Filtered Momentum | Momentum | M15/H1/H4 | Not tested for USDJPY |
| Session Range MR (ICT) | Mean Reversion | H1/H4 | Not tested for USDJPY |
| USDJPY D1 Trend | Trend | D1 | Not tested (archived) |
| Volatility Squeeze | Breakout | H1/M15 | **FAILED** — 0/5 windows passed go/no-go |
| Session Breakout Asian | Breakout | M15 | Not tested for USDJPY |

### 2.2 Walk-Forward Evidence

The only USDJPY walk-forward result on disk is `reports/walk_forward/volatility_squeeze_usdjpy_5window.json`:

| Window | Win Rate | Profit Factor | Trades | PnL | Go/No-Go |
|--------|----------|---------------|--------|-----|----------|
| 0 | 1.00 | inf | 2 | +191 | FAIL |
| 1 | 0.20 | 0.22 | 5 | -332 | FAIL |
| 2 | 0.00 | 0.00 | 0 | 0 | FAIL |
| 3 | 0.00 | 0.00 | 1 | -104 | FAIL |
| 4 | 0.00 | 0.00 | 0 | 0 | FAIL |

**Aggregated:** Mean win rate 24%, windows passed 0/5. The strategy is effectively a no-op on USDJPY — too few signals, and the ones that fire are unprofitable.

### 2.3 Why Existing Strategies Fail

1. **Mean-reversion assumption breaks:** In a structurally bullish regime, "overbought" is the baseline state. RSI > 70 and upper BB touches persist for days. Fading them gets run over.

2. **Breakout assumption breaks:** Upside breakouts hit intervention risk — BoJ/MoF can cause 200+ pip reversals in minutes. Downside breakouts reverse as structural pressure reasserts.

3. **No intervention modeling:** No strategy has a concept of intervention zones, verbal warning thresholds, or intervention probability. All treat USDJPY as a "normal" pair.

4. **Known data bug:** USDJPY price scaling is inflated 100× in the live cTrader feed due to digit precision mismatch (`digits=3` vs actual 5-digit protobuf encoding). See `docs/audits/usdjpy-scaling-source-2026-06-30.md`. This must be resolved before any live strategy deployment.

---

## 3. Proposed Intervention-Regime Archetypes

Four archetypes designed specifically for the intervention regime. Each targets a different aspect of the intervention dynamic.

### Archetype A: Intervention Zone Fade (IZF)

**Concept:** Define explicit intervention zones (price bands where BoJ/MoF historically intervenes). When price enters these zones rapidly, fade the move — intervention is likely to reverse it.

**Regime condition:** Active only when `intervention_risk = HIGH` (defined below).

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Timeframe | M15 | Capture rapid spikes into intervention zones |
| Intervention zone | 159-162 (current) | Based on 2024-2026 intervention line; dynamically adjusted |
| Entry trigger | Rate of change > X pips in Y minutes, into zone | Detects spike, not drift |
| Direction | Fade spike (counter-trend) | Intervention reverses spikes |
| Stop loss | 2.0 × ATR(14) beyond zone edge | Allows for zone penetration before intervention fires |
| Take profit | Zone mid-point or 1.5R | Intervention typically reverts to zone center |
| Session filter | Asian session (00:00-06:00 UTC) priority | Most interventions occur in Asian hours |

**Indicators:**
- Price distance from intervention line (dynamic)
- Rate of Change (ROC, 5-bar and 10-bar)
- ATR(14) for volatility-scaled stops
- Asian session time filter
- Bollinger Band width (to detect compression before spikes)

**Intervention risk classifier:**
```
intervention_risk = HIGH when:
  - price > 160 (above known intervention line)
  - AND 10-bar ROC > 1.5 σ (rapid move)
  - AND session is Asian or London open
```

### Archetype B: Carry-Adjusted Trend Following (CATF)

**Concept:** USDJPY has the largest interest rate differential among major pairs (5.50% US vs 0.25% JP = 5.25% carry). Trend-following biased long when carry is favorable, with intervention-aware exit triggers.

**Regime condition:** Always active, but position sizing adjusts for intervention proximity.

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Timeframe | H4 or D1 | Captures structural trend, filters intraday noise |
| Trend detection | EMA(20) > EMA(50) with ADX(14) > 20 | Standard trend confirmation |
| Direction bias | Long-biased when US rate >> JP rate | Carry favors JPY weakness |
| Entry | Pullback to EMA(20) in trend direction | Better entries than breakout |
| Stop loss | Below EMA(50) or 2.5 × ATR(14) | Wider stops for intervention volatility |
| Take profit | Trailing: exit if EMA(20) crosses EMA(50) | Let trend run until reversal |
| Intervention exit | Close immediately if ROC > 3σ against position | Intervention spikes can destroy trend gains |

**Indicators:**
- EMA(20), EMA(50) on H4/D1
- ADX(14) for trend strength
- ATR(14) for stop sizing
- Interest rate differential (from `carry.py` RATE_DATA: 5.25% for USDJPY)
- ROC(10) for intervention spike detection (emergency exit)

### Archetype C: Intervention Volatility Regime Router (IVRR)

**Concept:** The pair alternates between "normal trending" and "intervention panic" volatility regimes. Use a regime classifier to route between trend-following (normal) and mean-reversion/fade (intervention panic).

**Regime condition:** This IS a regime-switching strategy — it classifies the regime and routes accordingly.

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Timeframe | H1 (primary), M15 (execution) | H1 for regime, M15 for entry timing |
| Regime detection | ATR percentile rank + implied vol proxy | High ATR percentile = intervention panic |
| Normal regime | Trend-following (EMA cross + ADX filter) | Structural carry trend |
| Panic regime | Fade extremes, tight stops | Intervention reverts panic spikes |
| Regime threshold | ATR > 80th percentile of 30-day rolling | Empirically tunable |

**Indicators:**
- ATR(14) percentile rank (30-day rolling window)
- EMA(20/50) cross for trend regime signals
- RSI(14) for fade entries in panic regime (RSI > 80 or < 20)
- ADX(14) for trend strength
- Bollinger Band width percentile (volatility compression/expansion proxy)

**Routing logic:**
```
if atr_percentile > 80:
    regime = "intervention_panic"
    → fade RSI extremes with tight stops
elif adx > 25 and ema20 > ema50:
    regime = "trending"
    → follow trend on pullbacks
else:
    regime = "range"
    → no trade (low conviction)
```

### Archetype D: Event-Driven Intervention Sentiment (EDIS)

**Concept:** Model verbal and actual intervention events as discrete signals. Verbal warnings (MoF statements, BoJ governor comments) and actual intervention (confirmed yen buying) create tradable dislocations.

**Regime condition:** Event-triggered — not continuously active.

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Timeframe | M5 (immediate), H1 (follow-through) | Events move price in minutes |
| Event types | Verbal warning, confirmed intervention, rate decision surprise | Three tiers of intensity |
| Verbal warning signal | Short USDJPY for 1-4 hours | Verbal warnings cause 30-80 pip drops typically |
| Confirmed intervention | Short USDJPY for 4-24 hours | Actual intervention: 100-300 pip reversals |
| Rate surprise (BoJ hike) | Short USDJPY for 24-72 hours | Structural shift |
| Stop loss | 2.5 × ATR(14) — wide due to event noise | Events are volatile |
| Take profit | 2R minimum, trail after 1R | Capture multi-hour dislocation |

**Indicators:**
- News/calendar feed integration (requires data source)
- Real-time price ROC(5) for event confirmation
- ATR(14) for stop sizing
- Session filter (Asian session priority)

**Data dependency:** This archetype requires a news/event feed (e.g., economic calendar API, MoF statement monitor). The current codebase has no news integration. This is the highest-complexity archetype to implement.

---

## 4. Data Availability Assessment

### Available Historical Data

| File | Period | Bars | Usable for Backtest |
|------|--------|-------|---------------------|
| `USDJPY_D1.csv` | Jan 2023 – Dec 2025 | 939 | ✅ Trend/Carry archetypes (B) |
| `USDJPY_H1.csv` | Jan 2023 – Dec 2025 | 17,854 | ✅ All archetypes except D |
| `USDJPY_H4.csv` | Jan 2023 – Dec 2025 | 4,803 | ✅ Trend/Carry (B), Regime Router (C) |
| `USDJPY_M15.csv` | Jan 2023 – Apr 2026 | 78,179 | ✅ All archetypes — primary dataset |
| `USDJPY_M5.csv` | Dec 2024 – Apr 2026 | 100,001 | ✅ Event-Driven (D), execution timing |

### Coverage of Historical Intervention Events

The M15 dataset (Jan 2023 – Apr 2026) covers:
- **Sept-Oct 2022:** First major BoJ intervention (≈142 → back below 140) — partially outside data range (starts Jan 2023)
- **2023 gradual depreciation:** 130 → 150 range
- **April-May 2024 intervention:** ~$62B spent, 152 → 157 range — ✅ covered
- **July 2024 surge:** 157 → 165+ area — ✅ covered
- **2025-2026 continuation:** up to 162 range — ✅ covered

### Data Gaps

1. **No tick data:** Intervention spikes happen in seconds; M5 is the finest granularity available
2. **No news/event timestamps:** Event-driven archetype (D) cannot be properly backtested without intervention event timestamps
3. **No order flow / options data:** Cannot model market positioning relative to intervention risk

---

## 5. Backtest Infrastructure Assessment

### Available Infrastructure

| Component | Path | Status |
|-----------|------|--------|
| Walk-forward runner | `src/forex_bot/backtest/walk_forward_runner.py` | Functional |
| Backtest engine | `src/forex_trading/services/backtest/` | Functional |
| Strategy base class | `src/forex_trading/services/backtest/strategies.py` | Functional (ABC protocol) |
| Backtest strategies | `src/forex_trading/strategies/` | 7 strategies including carry, regime_aware |
| Walk-forward reports | `reports/walk_forward/` | 15+ existing results |

### Backtest Feasibility Per Archetype

| Archetype | Can Backtest? | Blockers |
|-----------|---------------|----------|
| A (Intervention Zone Fade) | ⚠️ Partial | Need to encode intervention zone boundaries; otherwise standard infrastructure |
| B (Carry-Adjusted Trend) | ✅ Yes | Carry rate data available in `carry.py`; standard trend infrastructure |
| C (Regime Router) | ✅ Yes | ATR percentile ranking is computable from price data; regime-aware base exists |
| D (Event-Driven Sentiment) | ❌ No | Requires news/event feed integration not present in codebase |

---

## 6. Known Issues and Dependencies

### 6.1 USDJPY 100× Price Scaling Bug

**Critical:** A known price scaling bug (`docs/audits/usdjpy-scaling-source-2026-06-30.md`) inflates USDJPY prices 100× in the live cTrader feed. The historical CSV data appears correctly scaled (values like 155.927, not 15,592.7). However, any strategy deployed live must ensure the scaling fix is in place.

### 6.2 Missing Source Research Document

The source document `docs/research/srb/SRB-AYUMI-009.md` (Satoshi, Tier 2, 2026-07-01) is not present on disk. This document was created using the card notes as primary input, which contain the key findings (intervention spend, rate spread, trading level).

### 6.3 No CompositeScorer / Cabal Infrastructure

The Cabal scoring system (referenced in related cards) does not exist in the codebase. Strategy scoring/ranking would need to use the existing `StrategyRegistry` or a new lightweight approach.

---

## 7. Implementation Roadmap

### Phase 1: Foundation (0.5 SP each, separate cards)

| Step | Deliverable | Files | SP |
|------|-------------|-------|----|
| 1a | Implement Intervention Zone detector module | `src/forex_trading/strategies/intervention_zone.py` | 0.5 |
| 1b | Implement Carry-Adjusted Trend strategy (extends existing `carry.py` + `regime_aware.py`) | `src/forex_trading/strategies/carry_trend.py` | 0.5 |

### Phase 2: Validation (1.0 SP, separate card)

| Step | Deliverable | Files | SP |
|------|-------------|-------|----|
| 2a | Walk-forward backtest archetypes B + C on USDJPY M15/H1 data (2023-2025) | `scripts/backtest_usdjpy_intervention.py` + report | 1.0 |

### Phase 3: Production (conditional on Phase 2 results)

| Step | Deliverable | Files | SP |
|------|-------------|-------|----|
| 3a | Port winning archetype to live strategy format (forex_bot adapter) | `src/forex_bot/strategies/usdjpy_intervention.py` | 1.0 |
| 3b | Register in `StrategyRegistry`, add to walk-forward rotation | Modify `registry.py` | 0.5 |

### Phase 4: Advanced (deferred)

| Step | Deliverable | Notes |
|------|-------------|-------|
| 4a | Event-driven archetype (D) | Requires news feed integration — separate epic |
| 4b | Dynamic intervention zone calibration | Requires BoJ/MoF intervention database |

---

## 8. Recommendation

**Start with Archetype B (Carry-Adjusted Trend Following) and Archetype C (Intervention Volatility Regime Router).**

Rationale:
- **B** leverages the strongest structural edge (5.25% carry differential) and existing codebase patterns (`carry.py`, `regime_aware.py`)
- **C** addresses the core failure mode (strategies that don't adapt to regime shifts between trending and intervention panic)
- **A** requires encoding intervention zones, adding complexity before the base patterns are validated
- **D** requires infrastructure (news feed) that doesn't exist

Both B and C can be backtested on available historical data using the existing walk-forward infrastructure. If either passes go/no-go, it becomes the first viable USDJPY strategy.

---

## 9. Walk-Forward Cannot Be Run From This Card

**This card's `allowed_files` are documentation-only:**
- `docs/research/srb/SRB-AYUMI-009.md` (source, not on disk)
- `docs/ayumi/strategies/usdjpy-intervention-archetypes.md` (this document)

Running walk-forward backtests requires creating scripts (`scripts/backtest_usdjpy_intervention.py`) and strategy implementations (`src/forex_trading/strategies/*.py`) not in the allowed_files list. These are scoped as follow-up cards in the Phase 1-2 roadmap above.

**This document fulfills the research, archetype design, and indicator definition acceptance criteria.** The backtest, walk-forward pass/fail, and implementation criteria require separate builder cards with appropriate code-file scopes.
