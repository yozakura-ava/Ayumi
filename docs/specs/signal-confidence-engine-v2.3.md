# Signal & Confidence Engine — Architecture Spec v2.2

**Version:** 2.3  
**Date:** 2026-04-09  
**Author:** Ava (Senior Engineer)  
**Status:** Design Phase — Revised after 14 independent reviews  
**Changes from v2.2:** G4 resolved (soft gate), reversal_score gate added, quality_score gate added, glossary expanded (21→31 terms), G3 explicit fail, D3 validation, structure invalidation defined, HTF dual-mechanism reconciled, weight slack explained, crypto normalization fixed, FL-003/004 gates added, missing scoring definitions (ema_bounce, asia_control, dxy_correlation), flat-EMA no-trade, continuation reset patterns, staircase expansion, 3-push hard gate, AOI weakness qualifier, Asia directional inference, heat map stops, NY open manipulation, fib confluence, partial exit by trade type, stop-move rule, 3-hit volume context, instrument notes expanded

---

## 1. Overview

The Signal & Confidence Engine converts raw market data into scored trade opportunities based on the TTC/TBD confluence framework. It produces a **confidence score** (0.0–1.0) for each candidate setup, which the strategy engine acts on based on configurable thresholds.

**Core Principle:** The TTC/TBD system is a **confluence stack**, not a signal system. No single component is a trade trigger. Confidence is built by stacking confirmations — but some confirmations are *required gates* (missing them disqualifies the trade), while others are *boosters* (they increase conviction).

### 1.1 Glossary of Terms

All terms used in this spec are defined here. If a term appears in the spec but not here, it is a gap — flag it.

| Term | Definition |
|------|-----------|
| **Market Maker (MM) Candle** | A candle with body ≥ 70% of total range (same structural definition as SVC body, but does NOT require volume ≥ 1.5x average). Used in level validation and pattern detection. |
| **Vector Candle** | An MM candle that occurs at a counted level and shows aggressive rejection. Has the same structural requirements as an MM candle (body ≥ 70%) PLUS occurs at L1/L2/L3/R1/R2/R3 or an AOI. Used for stop placement ("cover the vector"). |
| **SVC (Stopping Volume Candle)** | A vector candle with additional volume confirmation: body ≥ 70% of range, close ≥ 80% of body from the favorable end, volume ≥ 1.5x the 20-bar average, located at a counted level or AOI. The #1 confirmation signal. |
| **AOI (Area of Interest)** | A price zone where multiple reactions have occurred — defined as any level where price has reversed at least 2 times within the last 50 bars. Can be at a counted level or at a standalone reaction zone. **Extended definition:** An AOI also includes zones showing *weakness in the move* — two vector candles with consolidation sandwiched between them, indicating the move is losing conviction. |
| **Swing High** | A bar whose high is the highest within a lookback window of 5 bars on each side (i.e., bar[i].high > bar[i-N].high for all N in 1..5 and bar[i].high > bar[i+N].high for all N in 1..5). |
| **Swing Low** | Mirror of Swing High. |
| **HiW / LoW** | High of Week / Low of Week. The highest traded price and lowest traded price from Monday 00:00 UTC to Friday 23:59 UTC (forex) or rolling 7-day window (crypto). |
| **HoD / LoD** | High of Day / Low of Day. The highest and lowest traded price since 00:00 UTC of the current day. |
| **iLoD / iHoD** | Initial Low/High of Day. The low/high established during the Asian session (defined as 00:00-07:00 UTC for forex). Used for ILOD/IHOD setups. |
| **ILOD / IHOD** | Interchangeable with iLoD/iHoD. Both forms refer to the same concept. |
| **Kill Zone** | The first 90 minutes of a session's active trading period. Entry signals during kill zones have higher conviction due to elevated liquidity and directional intent. |
| **Boardroom** | A consolidation zone where price is contained in a tight range (≤ 0.5% over ≥ 20 H1 bars) with declining volume. Represents accumulation/distribution. NOT the same as a retrace — boardrooms are flat and compressed; retraces have wave structure. |
| **Retrace** | A pullback within an existing trend with clear wave structure and normal volume. The opposite of a boardroom. |
| **Consolidation** | A period where price moves sideways within a narrow range with declining volume. A general term that includes boardrooms (tight, long-duration) and simple ranges (wider, shorter). |
| **Breakout** | Price moving decisively beyond a defined level (counted level, Asia range, period extreme). Must have body close beyond the level, not just a wick. |
| **Retest** | Price returning to a previously broken level to test whether the break holds. The retest is the confirmation entry point — NOT the initial break. |
| **Level** | A completed rise or drop as defined in §4.3. Not just any swing — must meet completion criteria for L1, L2, or L3. |
| **Rise / Drop** | A directional move that completes at a swing extreme. Rises are bullish moves; drops are bearish. Both must meet completion criteria to count as levels. |
| **Near** | Within 0.5% of a reference price. Used as the default proximity threshold unless a specific section overrides it. |
| **At** | Within 0.2% of a reference price. Tighter than "near." |
| **Push** | A directional price move within a larger rise/drop that shows intent (body ≥ 50% of range). On LTF, pushes appear as individual legs of momentum. A "sub-push" is a push visible only on a lower timeframe within a larger push on the trading timeframe. |
| **Trigger Candle** | The candle that confirms a pattern is complete (e.g., the higher-low candle after the second peak of a W). Entry is typically on the candle AFTER the trigger, not the trigger itself. |
| **Unrecovered Vector** | A vector candle whose price extreme has not yet been reached by subsequent price action. If a bullish vector candle closed at 1.0850 with a high of 1.0870, and price has only reached 1.0860 since, the vector is unrecovered — its high (1.0870) remains the target. |
| **Breakeven** | Moving stop loss to the exact entry price, eliminating risk. Done only after price reaches 1:1 R:R. |
| **Gray Zone** | Confidence scores between 0.40–0.49. These setups have marginal confluence and require manual review or extra confirmation before trading. |
| **Trending** | Price making consistent higher highs and higher lows (bullish) or lower highs and lower lows (bearish). Characterized by expanding range and directional 50 EMA slope. The opposite of consolidation. |
| **Tight Range** | A consolidation with range < 2.0% and no clear directional bias. Asia sessions often produce tight ranges when no major moves are occurring. |
| **Type 1 Reset** | A consolidation pattern on a lower timeframe that appears as a single swing on a higher timeframe. Can cause miscounting of HTF levels if not detected. See §4.2. |
| **High-Impact Event** | Any economic event classified as "High Impact" or "Red" by the Forex Factory economic calendar (or equivalent data source). Examples: NFP, FOMC rate decisions, CPI releases. The classification comes from the data source, not from the spec. |
| **HTF / LTF** | Higher Timeframe / Lower Timeframe. HTF = any timeframe above the trading timeframe (H4, D1 when trading H1). LTF = any timeframe below (M15, M5 when trading H1). |
| **MTTF** | Multi-Timeframe Framework. The system of analyzing levels and patterns across multiple timeframes simultaneously, with HTF gating LTF. Defined in §4. |
| **ATR** | Average True Range. A volatility measure calculated over 14 periods. Used as a fallback stop placement method when no structural stop is available. |
| **Absorption** | High volume at a level where price fails to continue — indicating the other side is absorbing all selling/buying pressure. The volume appears but price doesn't move. Key characteristic of SVC candles. |
| **Exhaustion** | A state where the market has pushed to an extreme (R3/D3 at period high/low) and is running out of directional energy. Characterized by erratic price action, long wicks, and volume divergences. |
| **Lookback** | The N-bar window used in swing detection and other calculations. Default: 5 bars. A swing high/low requires `lookback` bars on each side to confirm. |
| **Structure Invalidation** | Price breaks the 50 EMA in the opposite direction of the trade, or forms a new swing extreme that contradicts the original pattern's thesis. On invalidation, close remaining position immediately at market. |
| **50% Asia Level** | The midpoint of the Asian session's high-low range. A key intraday level — 50% retracements often gravitate here. Used as a confluence level and potential entry zone. |
| **50% Retracement** | The halfway point of a price move. In the context of M/W entries (§5.8), the 50% retracement of the 50 EMA breakout vector — price pulls back to this level before continuing. |

### 1.2 Proximity Thresholds (Master Table)

All percentage-based proximity checks in the spec reference this table. If a section uses a different threshold, it is explicitly stated.

| Context | Threshold | Notes |
|---------|----------|-------|
| Default "near" | ≤ 0.5% | General proximity |
| Default "at" | ≤ 0.2% | Tight proximity (e.g., "at a counted level") |
| M/W symmetry gate (G1) | ≤ 1.5% | Between SL1/SL2 or SH1/SH2 |
| M/W equal-lows threshold | ≤ 0.05% | Below this = single swing (pattern invalid); above this up to 1.5% = valid G1 |
| Trap break threshold (London/NY) | ≥ 0.2% | Minimum break distance |
| Trap break threshold (Asia) | ≥ 0.4% | Higher due to thin liquidity |
| Trap break threshold (crypto) | ≥ 0.3% | Higher volatility |
| Asia range qualification | < 2.0% | Asia total range for valid setups |
| Near period extreme (score 1.0) | ≤ 0.3% | Distance to HiW/LoW |
| Near period extreme (score 0.7) | ≤ 1.0% | Distance to HiW/LoW |
| Near period extreme (score 0.3) | ≤ 2.0% | Distance to HiW/LoW |
| Near period extreme (score 0.0) | > 2.0% | Too far from HiW/LoW — no period extreme bonus |
| EMA tolerance ("close enough") | ≤ 0.1% | Price doesn't need pip-perfect EMA touch |
| Boardroom range (H1) | ≤ 0.5% | Over ≥ 20 bars |
| Level completion (R3 = R2 magnitude) | R3 ≥ 90% of R2 | Minimum R3 size to count |
| Level completion (D3 = D2 magnitude) | D3 ≥ 90% of D2 | Mirror of R3 rule |

### 1.3 Two Variants

| Aspect | Forex Engine | Crypto Engine |
|--------|-------------|---------------|
| Session structure | Asia → London → NY (fixed, well-defined) | 24/7, sessions still relevant but less rigid |
| Kill zones | Well-defined session opens + overlaps | Kill zones exist but lower conviction |
| Instrument behavior | Pair-specific dynamics (see §8.6) | BTC leads, alts follow (BTC dominance matters) |
| OI data | Not available (OTC market) | Available via Coinglass, exchanges |
| Funding rates | N/A | Key contrarian signal |
| Heat maps | Volume profile only | Liquidation heat maps available |
| 50% Asia level | High priority intraday level | Less reliable (24/7 liquidity) |
| Weekend behavior | Weekend gaps matter | Weekend traps + CME gaps |
| DXY correlation | Critical directional filter | N/A |
| Spread/slippage | Critical (tight spreads required) | Wider but more volatile |

### 1.4 Pipeline

```
Raw Data (OHLCV + optional OI/funding/liquidations)
    ↓
[1. Swing Detector] — Identify swing highs/lows across all TFs (M5-D1)
    ↓
[2. Level Counter] — Count rises/drops per TF, validate L1/L2/L3 completion, feed across TFs
    ↓
[3. HTF Context Analyzer] — Evaluate H4/D1 state (directional, consolidating, exhaustion)
    ↓
[4. Pattern Detector] — Detect formations: M/W (11-pt), SVC, traps, ILOD/IHOD, liquidity grabs, FL strategies
    ↓
[5. Gate Validator] — Hard requirements (all must pass) + quality threshold
    ↓
[6. Confluence Scorer] — Booster factors (only if gates pass)
    ↓
[7. Confidence Engine] — Weighted score + interaction bonuses
    ↓
[8. Signal Output] — Structured JSON for strategy engine
```

**Note:** Steps 1-8 cover Phase 1-5 of the roadmap (§12). Phase 6 (crypto extensions) adds factors to existing steps, not new pipeline stages. Phase 8 (boardroom) enhances Step 3.

---

## 2. Data Requirements

### 2.1 Required (Core)

| Data | Source | Timeframes |
|------|--------|-----------|
| OHLCV (bid + ask) | cTrader Open API | M5, M15, H1, H4, D1 |
| Session timestamps | System clock (timezone-aware, UTC) | — |
| Economic calendar | Forex Factory API / manual | — |

### 2.2 Required (Crypto Only)

| Data | Source | Notes |
|------|--------|-------|
| Open Interest | Coinglass API | Per-exchange, BTC + ETH + alts |
| Funding Rates | Coinglass / exchange APIs | 8h intervals, extremes = contrarian signal |
| Liquidation Heat Maps | Coinglass / TensorCharts | Key levels where trapped positions cluster |
| BTC Dominance | CoinGecko / TradingView | Alt rotation signal |
| CME Gap Data | TradingView / Barchart | Weekend gaps in futures market |

### 2.3 Optional (Enhancement)

| Data | Source | Value |
|------|--------|-------|
| Volume Profile | cTrader / exchange order book | Confirm AOI with actual traded volume |
| Tick data | cTrader Open API | Finer-grained SVC detection |
| Order book depth | Exchange APIs (crypto) | Real-time trapped position detection |
| DXY index | cTrader or data feed | Forex directional filter (§7.1) |
| Liquidation heat map (forex) | Volume profile proxy | Avoid placing stops in high-volume clusters |

---

## 3. Swing Detection (Foundation)

All pattern detection and level counting depends on correctly identified swing highs and lows.

### 3.1 Algorithm

```python
def detect_swings(highs, lows, lookback=5):
    """
    Identify swing highs and lows using N-bar lookback.
    
    A swing high is a bar whose high exceeds all highs within 
    `lookback` bars on either side.
    
    Parameters:
        lookback: 5 bars (default). The swing is confirmed
        5 bars after it forms — a lagging indicator.
    
    Returns:
        Lists of (bar_index, price) tuples for swing_highs and swing_lows.
    """
    swing_highs = []
    swing_lows = []
    for i in range(lookback, len(highs) - lookback):
        is_swing_high = True
        is_swing_low = True
        for j in range(1, lookback + 1):
            if highs[i] <= highs[i-j] or highs[i] <= highs[i+j]:
                is_swing_high = False
            if lows[i] >= lows[i-j] or lows[i] >= lows[i+j]:
                is_swing_low = False
        if is_swing_high:
            swing_highs.append((i, highs[i]))
        if is_swing_low:
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows
```

### 3.2 Edge Cases

| Edge Case | Rule |
|-----------|------|
| Equal highs (within 0.05%) | Treat as single swing high — one level, not two. For M/W G1: two equal peaks within 0.05% = pattern invalid (G1 cannot be satisfied). |
| Equal lows (within 0.05%) | Treat as single swing low — same invalidation logic as equal highs. |
| Peaks between 0.05%–1.5% apart | Two distinct swings. Pass G1 symmetry (≤ 1.5%). Engineer note: this range represents tight but valid formations. |
| Inside bar (high < prev high, low > prev low) | Cannot be a swing — skip |
| Outside bar (high > prev high, low < prev low) | Evaluated normally |
| Large momentum candle spanning multiple swings | Only the actual extreme counts as the swing |

---

## 4. Multi-Timeframe Framework

### 4.1 Timeframe Hierarchy

```
D1 (Weekly context)
 └── H4 (Primary trend / major levels)
      └── H1 (Trading timeframe — pattern detection)
           └── M15 (Entry timeframe — precision entries)
                └── M5 (Scalp / confirmation — separate strategy)
```

**Rules:**
- HTF **always** gates LTF. A valid H1 pattern in the same direction as H4/D1 is scored differently than one against the HTF trend.
- LTF patterns are **degraded** when HTF is in a consolidation-like state (see §4.4).
- The primary trading timeframe is **H1**. M15 is for entries. H4/D1 are for direction and major level identification.

### 4.2 Level Counting & Timeframe Feeding

```
3 × M5 levels = 1 × M15 level
3 × M15 levels = 1 × H1 level
3 × H1 levels = 1 × H4 level
3 × H4 levels = 1 × D1 level
```

**Additional feeding rules:**
- 3 hits to a level on a lower timeframe can feed up as 1 confirmed level on the next higher timeframe (strength-based, not just count-based)
- A 3-day consolidation on H1 may actually be a Type 1 reset on 15M feeding upward — NOT a valid H1 level count. Before counting HTF levels, check if the "level" is actually a boardroom/reset on the LTF.
- 3 hits to a level WITH serious volume = likely the last hit, don't assume a 4th comes. 3 hits with LOW volume may still get a 4th test. Volume context matters for hit interpretation.

### 4.3 Level Validation Criteria

**Rise Level 1 (R1):**
- Price makes higher high + higher low from swing low
- Confirmation: MM candle (body ≥ 70%) breaks and closes above 50 EMA
- Must attempt toward 200 EMA: price reaches within 0.5% of 200 EMA within 10 bars

**Rise Level 2 (R2):**
- Extends above R1 high with new higher high + higher low
- Confirmation: MM candle (body ≥ 70%) breaks and closes above 200 EMA
- MM candle must show rejection of opposite momentum: candle body closes in the direction of the rise

**Rise Level 3 (R3):**
- Extends above R2 high with new higher high + higher low
- R3 magnitude must be ≥ 90% of R2 magnitude (if smaller, treat as reset, not extension)
- Located at or near HiW (≤ 0.5% of weekly high)
- High volatility, erratic price action (exhaustion zone)
- This is the **exhaustion zone** — reversal setups appear here

**Drop Levels:** Mirror of above. Explicitly:
- **D1:** Mirror of R1. Drop confirmation at 50 EMA.
- **D2:** Mirror of R2. Drop confirmation at 200 EMA. MM candle closes in bearish direction.
- **D3:** Mirror of R3. D3 magnitude must be ≥ 90% of D2 magnitude (if smaller, treat as reset). Located at or near LoW (≤ 0.5%).

**Trading Implications by Level:**

| Level | Context | Setup Type | Implication |
|-------|---------|-----------|------------|
| R1/D1 at 50 EMA | Continuation | EMA bounce / retest | Pullback in trend, not reversal |
| R2/D2 at 200 EMA | Extension | 200 EMA retest | Must reassess at 200 EMA (exit, reduce, or continue) |
| R3/D3 at period extreme | Reversal zone | M/W formation, trap setups | Highest conviction for reversal |

### 4.4 HTF Context Scoring

Before evaluating any LTF pattern, check HTF state. This produces TWO outputs used differently in the pipeline:

**Output A: `htf_phase`** (categorical — used by Gate Validator §6)
- `"aligned"` — H4/D1 direction matches H1 pattern direction
- `"conflicting"` — H4/D1 direction opposes H1 pattern direction  
- `"consolidating"` — H4 is flat (range ≤ 0.5% over 20 bars, EMA slope near zero)
- `"exhaustion"` — D1 is at R3/D3 (exhaustion zone)
- `"neutral"` — D1 shows no clear directional bias

**Output B: `htf_modifier`** (numeric — used by Confluence Scorer §7)
- Aligned: +0.15
- Conflicting: -0.25 (degrades but does NOT hard-fail — gates handle hard fails)
- Consolidating: -0.15
- Exhaustion: +0.10 (reversal setups favored here)
- Neutral: -0.05 (slight degrade — no directional confirmation available)

**Phase 1 implementation:** Consolidation check uses simplified proxy (H4 range ≤ 0.5% over 20 bars + |50 EMA slope| < 0.0001). Full boardroom detection (declining volume, specific entry types) deferred to Phase 3+. The `htf_not_consolidating` factor in §7.1 is the absence of the "consolidating" phase.

### 4.5 Multi-Timeframe Alignment Scoring

| TF Agreement | Score | Description |
|-------------|-------|-------------|
| All 4 TFs agree (D1, H4, H1, M15) | 1.0 | Maximum confluence |
| 3/4 TFs agree (incl. H4) | 0.75 | Strong |
| 2/4 TFs agree (incl. H1 + H4) | 0.50 | Moderate |
| 2/4 TFs agree (only LTFs) | 0.25 | Weak — HTF doesn't confirm |
| 0-1/4 TFs agree | 0.0 | No trade |

**Note:** Direction of agreement matters. H1 bullish + H4 bullish + D1 bearish = 2/4 (0.50), NOT 3/4.

### 4.6 Level Context for Pattern Interpretation

| Pattern at Level | Interpretation | Confidence Modifier |
|-----------------|---------------|-------------------|
| W at D3 (near LoW) | Reversal — market exhausted | +0.10 |
| W at D1 (near 50 EMA) | Continuation reset — pullback in drop | -0.10 |
| W at R3 (near HiW) | M forming — bearish reversal | +0.10 (for M) |
| W at R1 (near 50 EMA) | Bounce in rise — not a reversal | -0.10 |

---

## 5. Pattern Detector

### 5.1 M/W Formation Detection — Full Checklist

A formation must pass **Gate requirements** (mandatory) and achieve **minimum quality score** before any confluence scoring applies. The checklist is derived from Cajun's 11-point validation framework.

#### Gate Requirements (ALL must be true)

**G0: 3 completed levels prerequisite**
- The formation must occur AFTER at least 3 rise levels or 3 drop levels have been counted on the trading timeframe (H1)
- Looking for M/W before 3 levels = anticipatory, not confirmation-based
- Exception: ILOD/IHOD setups don't require 3 levels (they are their own pattern type)

**G1: Two swing extremes with swing between**
- W: Two swing lows (SL1, SL2) with a swing high (SH) between them
- M: Two swing highs (SH1, SH2) with a swing low (SL) between them
- SL1 and SL2 must be within 1.5% of each other (symmetry gate)
- If two lows are equal (within 0.05%), they register as a single swing low — G1 cannot be satisfied (pattern invalid). This is intentional: equal lows = no valid W.

**G2: Formation at a counted level**
- Must be at (≤ 0.2%) or near (≤ 0.5%) L1, L2, L3, R1, R2, or R3 on at least one timeframe
- If no level context exists, pattern is NOT tradeable

**G3: First peak shows rejection characteristics**
- First peak (SH for W, SL for M) must have at least ONE of:
  - MM candle body (body ≥ 70% of range) in the reversal direction
  - Long wick (≥ 50% of range) showing rejection
  - Volume ≥ 1.3x 20-bar average
- **If the first peak has NONE of these characteristics, G3 fails (hard gate fail).**

**G4: Middle peak proximity to 50 EMA (SOFT GATE)**
- The middle swing (SH for W, SL for M) should reach within 0.5% of the 50 EMA
- Scoring tiers:
  - ≤ 0.5% = full quality score for this factor (1.0)
  - 0.5%–1.0% = reduced quality score (0.5)
  - > 1.0% = minimal quality score (0.1)
- G4 does NOT hard-fail the formation — it reduces quality. A middle peak far from the 50 EMA is a weaker formation but not necessarily invalid.

**G5: Lower high / higher low after second peak**
- After the second peak forms, price must produce:
  - For W (bullish): a higher low than SL2 — confirming reversal intent
  - For M (bearish): a lower high than SH2 — confirming reversal intent
- Without this confirmation, the formation may be incomplete

**G6: 50 EMA broken on breakout move AND retested**
- After formation completes (G5 confirmed), price must break 50 EMA
- Then price must retest the 50 EMA — this is the **conservative entry point**
- Both break and retest must occur (sequential)
- If only break occurs without retest, the aggressive entry (see §5.8) may apply

#### Hard Post-Gate Requirements

**Q0: Minimum quality score**
- After all gates pass, quality_score must be ≥ 0.50 for the formation to proceed to confluence scoring
- quality_score < 0.50 = NO TRADE (regardless of confluence factors)

**Q1: Reversal score threshold**
- reversal_score (see §5.7) must be ≥ 0.30
- reversal_score < 0.30 = formation treated as continuation retrace, NOT a reversal trade = NO TRADE for reversal setups
- Continuation reset patterns (see §5.12) are handled separately and do not require this threshold

#### Quality Scoring (0.0–1.0, only if ALL gates pass)

| Factor | Weight | Logic |
|--------|--------|-------|
| Multi-session | 0.18 | 3+ sessions = 1.0, 2 = 0.7, 1 = 0.2 |
| MM candle pushes into second peak | 0.15 | **3+ pushes = 1.0, 2 = 0.6, 1 = 0.2, 0 = 0.0.** Cajun's requirement: ideally 3 MM candles pushing into the second peak. Less than 3 reduces conviction. |
| Middle peak EMA proximity | 0.12 | Per G4 tiers: ≤0.5% = 1.0, 0.5-1.0% = 0.5, >1.0% = 0.1 |
| Second peak candle type | 0.10 | Valid patterns (see §5.9) = 1.0, borderline = 0.5, invalid = 0.1 |
| Volume on second low/high (absorption) | 0.10 | Increasing vs first low = 1.0, equal = 0.5, decreasing = 0.2 |
| Formation duration (H1 bars) | 0.08 | 15-60 = 1.0, 8-14 = 0.7, 61-90 = 0.4, >90 = 0.2 |
| HTF alignment | 0.12 | Per §4.5 scoring |
| Near period extreme | 0.10 | Per proximity table in §1.2 |
| Reversal vs retrace filter | 0.05 | Per §5.7 (strong reversal = 1.0, ambiguous = 0.3, likely retrace = 0.0) |

**Threshold for "qualified" formation:** quality_score ≥ 0.50

### 5.2 SVC Detection

**Rules:**
```
SVC Bullish (at support):
  1. Body ≥ 70% of total candle range
  2. Close ≥ 80% of body from the low (near the top)
  3. Volume ≥ 1.5x average of last 20 candles
  4. Located at (≤ 0.2%) a counted level or AOI

SVC Bearish: mirror (close near bottom)
```

### 5.3 Trap Detection

**Rules:**
```
Trap Long (failed breakdown):
  1. Price breaks below key level by ≥ threshold (per §1.2 table)
  2. Reclaims level within 3-5 candles
  3. SVC candle on the reclaim
  4. Volume spike on the initial break (retail stop triggers)
  5. Entry on retest AFTER reclaim

Trap Short (failed breakout): mirror
```

**First Touch Is Trap principle:** The first time price approaches a key level (LOD, HOD, EMA) is usually a trap — market makers hunt the stops clustered there. The valid entry comes on the **retest** after the first touch fails. Do NOT enter on the first touch of a level.

### 5.4 Asia Liquidity Grab (Distinct from Generic Trap)

**Definition:** Specific to Asia session. Price breaks Asia High/Low → wick forms → fails to hold beyond the range → reversal. This is the core Asia→UK reversal mechanic.

**Rules:**
```
Asia Liquidity Grab:
  1. Asia session establishes range (high + low)
  2. Price breaks below Asia Low or above Asia High
  3. Candle closes with a wick back inside the Asia range
  4. Volume spike on the break
  5. Entry within 15 minutes of the grab candle close
  6. Stop beyond the wick of the grab candle (not beyond the range extreme)
```

**LTF entry fine-tuning:** After the stop-hunt candle closes, drop to 1-minute TF to refine entry. Maximum 15-minute window. If the next 1-min candle moves away strongly → market entry. If 1-min shows hesitation → wait for next 1-min candle. Do not let the window expire without acting.

### 5.5 ILOD / IHOD Break-Retest

**Rules:**
```
ILOD Break-Retest (bearish):
  1. Asian session establishes the iLoD (initial low of day, 00:00-07:00 UTC)
  2. London or NY session breaks below iLoD
  3. Price retests the iLoD from below
  4. Entry on the retest (NOT on the initial break — first touch is trap)
  5. Stop above the wick of the break candle
  6. Target: next counted level below

IHOD Break-Retest: mirror
```

### 5.6 Dead Gap Fill (Forex)

```
Dead Gap Fill:
  1. Gap between Friday close and Monday open > 5 pips
  2. Trade the retracement toward the gap fill level
  3. Entry: first pullback after gap (usually within 2 hours)
  4. Stop: beyond the extreme of the gap
  5. Target: Friday close price
  6. If gap > 30 pips: may only partially fill — reduce target to 50% of gap
```

### 5.7 Reversal vs Retrace Differentiation

Not every M/W is a reversal. A formation that looks like a reversal may only be a pullback within the existing trend.

**Reversal indicators (score positively):**
- M/W at R3/D3 (exhaustion zone) = strong reversal signal
- Volume on 50 EMA break is strong (≥ 1.5x average) = reversal
- Price reaches toward 200 EMA after break = reversal
- Follow-through after break holds = reversal
- Multi-session formation = reversal more likely

**Retrace indicators (score negatively):**
- M/W at R1/D1 (mid-move, near 50 EMA) = likely retrace
- Volume on 50 EMA break is weak (< 1.0x average) = retrace
- Price fails to reach 200 EMA after break = retrace
- 50 EMA breaks but price quickly returns below = retrace
- Single-session formation at R1 = likely retrace

**Implementation:** reversal_score is calculated as a weighted sum of 5 binary indicators:

```python
def calculate_reversal_score(formation):
    score = 0.0
    
    # Level context (weight: 0.30) — strongest signal
    if formation.at_r3_d3:  # Near period extreme
        score += 0.30
    elif formation.at_r1_d1:  # Near 50 EMA
        score += 0.05  # Weak reversal signal
    else:  # At R2/D2
        score += 0.15
    
    # Volume on 50 EMA break (weight: 0.25)
    if formation.ema_break_volume >= 1.5 * avg_volume:
        score += 0.25
    elif formation.ema_break_volume >= 1.0 * avg_volume:
        score += 0.10
    else:
        score += 0.0
    
    # Price reaches 200 EMA after break (weight: 0.20)
    if formation.reaches_200_ema:  # Within 0.5% of 200 EMA within 20 bars
        score += 0.20
    else:
        score += 0.0
    
    # Follow-through after break holds (weight: 0.15)
    if formation.follow_through_holds:  # Price holds above 50 EMA for 5+ bars after break
        score += 0.15
    else:
        score += 0.0
    
    # Multi-session formation (weight: 0.10)
    if formation.multi_session:  # Peaks span 2+ sessions
        score += 0.10
    else:
        score += 0.0
    
    return min(1.0, score)  # Cap at 1.0
```

**Threshold:** reversal_score < 0.30 → continuation retrace (see §5.12), NOT a reversal trade.

### 5.8 Conservative vs Aggressive Entry Types

**Aggressive Entry:**
- Enter on the second peak of M/W when a higher low (for W) or lower high (for M) has formed
- Before 50 EMA break — higher risk, earlier entry
- Stop: beyond the second peak
- Target: 50 EMA then 200 EMA
- R:R often higher but win rate lower

**Conservative Entry (DEFAULT):**
- Wait for price to break 50 EMA with vector candle
- Then wait for retest of the 50% retracement of that breakout vector
- Enter on the retest candle showing weakness
- Stop: below the vector candle (cover the vector)
- R:R lower but win rate higher
- This is G6 in the gate requirements

**The spec defaults to conservative entries. Aggressive entries may be added as a configurable option after validation.**

### 5.9 Valid Candlestick Confirmation Patterns

**Valid trigger/confirmation candles (bullish):**
- Bullish engulfing
- Hammer (body in upper 1/3, lower wick ≥ 2x body)
- Morning star (3-candle pattern)
- Railroad tracks (two large-body candles in same direction)

**Valid trigger/confirmation candles (bearish):**
- Bearish engulfing
- Inverted hammer / shooting star
- Evening star (3-candle pattern)
- Railroad tracks (bearish variant)

**Invalid / non-confirming candles:**
- Doji (indecision) — NOT a valid entry trigger
- Spinning top (small body, long wicks both sides) — NOT valid
- Small body candles (< 30% of range) — insufficient conviction

### 5.10 Flight Log Strategies (Named Setups)

The following are specific, named strategies from the Flight Log series. Each has distinct entry/exit rules beyond the generic pattern detector:

**FL-001 — Single Session M/W (Asia→UK):**
- Both peaks form entirely within Asia session
- Gate: Asia range must be < 2.0% and consolidating (not trending)
- Entry: on the 15-min candle AFTER the trigger candle (market order on open of the next 15-min candle after the trigger candle closes)
- Stop: beyond the first peak (not the second peak — distinct from generic M/W)
- Mandatory: Pre-US exit — close before 8:00 AM NY time regardless of P&L

**FL-002 — Liquidity Grab (Asia Range Stop-Hunt):**
- See §5.4 (Asia Liquidity Grab) — this IS FL-002

**FL-003 — Multi-Session M/W Within Asia Range:**
- Trigger: price attempts Asia extreme but FAILS to break it (not a breakout-retest)
- Asia range stays intact throughout
- Gate: `asia_range_intact` — Asia High/Low not broken during formation
- Entry: on the failed-revisit rejection candle

**FL-004 — Multi-Session M/W Fakeout:**
- Price briefly accepts outside Asia range then fails
- The SECOND failure is the trigger (not the initial break)
- Gate: `asia_compressed` — Asia range < 2.0% AND consolidating (tight sideways, not trending)

**FL-005 — 33 Trade Setup (Staircase Expansion):**
- Price expands in **staircase pattern**: up→sideways→up→sideways→up with NO deep pullbacks
- Each rise closes near its high (body in upper 30%)
- This is the structural opposite of swing structure — no meaningful retracements between rises
- "33" = 3 rises with 3 **sub-pushes** in the final rise (visible on LTF as high-volume vector candles)
- Rise 3 must contain 3 internal pushes before rejection is expected
- Target: 50 EMA → 200 EMA (reassess at 200 EMA per §9.4)

**FL-006 — NYC Reversal Day Trade:**
- Gate (non-negotiable): UK session must NOT have swept Asia High or Low
- Time window: reversal must form within first 3 hours of NY session (hard gate)
- NY open manipulation: first 30-60 min often includes stop-hunt candles, brief fakeouts, or single push that fails. **Do not trade the manipulation — the manipulation IS the signal.** Wait for the manipulation to fail, then enter the reversal.
- Target: 50 EMA → middle of daily range → HoD/LoD (recovery, not full reversal)

### 5.11 Boardroom Detection (Phase 3+ — Deferred)

**Phase 1 workaround:** Use simplified consolidation check (H4 range ≤ 0.5% over 20 bars) for HTF gating (§4.4). Full boardroom detection with entry types (fib retrace, stop-hunt W, second peak) deferred to Phase 3+.

### 5.12 Continuation Reset Patterns (Distinct from Reversal M/W)

A W at R1/D1 near the 50 EMA is NOT a reversal — it's a **continuation reset**. The market pulls back to the 50 EMA (forming a W), then continues in the original trend direction.

**Characteristics:**
- W/M forms at R1/D1 (near 50 EMA), NOT at R3/D3 (near period extreme)
- reversal_score < 0.30 (per §5.7)
- The "retest" of the 50 EMA is actually the continuation point, not a reversal

**How to handle:**
- These formations do NOT trigger reversal trades
- They may trigger **continuation entries** (enter in the original trend direction after the reset completes)
- Continuation entries are lower-conviction and use the aggressive entry type (§5.8)
- Stop: beyond the reset formation extreme
- Target: next counted level in the trend direction

### 5.13 Dead Gap / Weekend Trap (Crypto)

```
Weekend Trap:
  1. Friday close establishes a level
  2. Weekend price moves in one direction on low volume
  3. Monday session reverses the weekend move
  4. Entry: trade the Monday reversal
  5. Stop: beyond the weekend extreme
  6. Target: Friday close level or next counted level
```

---

## 6. Gate Validator

Before any confluence scoring, validate hard requirements. If ANY gate fails, the candidate is rejected immediately.

### 6.1 Universal Gates

| Gate | Requirement |
|------|-----------|
| `pattern_valid` | Passes structural detection rules (§5) |
| `pattern_at_level` | Formation at a counted level (G2) |
| `rr_minimum` | Natural target ≥ 3:1 from entry to stop |
| `no_major_event` | No high-impact event (per Forex Factory "Red" classification) within 2 hours of entry |
| `levels_complete` | 3+ levels counted on trading TF (G0) |
| `quality_threshold` | quality_score ≥ 0.50 (Q0) |
| `reversal_threshold` | reversal_score ≥ 0.30 (Q1) — skip for continuation entries |

### 6.2 M/W-Specific Gates

| Gate | Requirement |
|------|-----------|
| `g1_structure` | Two swing extremes with swing between, symmetry ≤ 1.5%, peaks > 0.05% apart |
| `g2_level_context` | At a counted level on at least one TF |
| `g3_first_peak_rejection` | First peak has ≥1 rejection characteristic. Characteristics: (a) MM candle body ≥ 70% of range in reversal direction, (b) wick ≥ 50% of range showing rejection, (c) volume ≥ 1.3x 20-bar average. Zero characteristics = hard fail. |
| `g4_middle_peak_ema` | SOFT GATE: middle peak proximity to 50 EMA scored per tiers (≤0.5% = 1.0, 0.5-1.0% = 0.5, >1.0% = 0.1). Does not hard-fail. |
| `g5_post_peak_confirm` | Lower high / higher low after second peak |
| `g6_ema_break_retest` | 50 EMA broken AND retested (conservative entry) |

### 6.3 Strategy-Specific Gates

| Gate | Requirement | Applies To |
|------|-----------|-----------|
| `asia_range_qualified` | Asia range < 2.0% and not trending | FL-001, FL-002, FL-003, FL-004 |
| `asia_range_intact` | Asia High/Low not broken during formation | FL-003 |
| `asia_compressed` | Asia range < 2.0% AND consolidating (tight sideways) | FL-004 |
| `uk_no_sweep` | UK did NOT sweep Asia High or Low | FL-006 |
| `ny_time_window` | Within first 3 hours of NY session | FL-006 |
| `pre_us_exit` | Must close before 8:00 AM NY | FL-001, FL-002, FL-003, FL-004 |
| `flat_ema_no_trade` | 50 EMA and 200 EMA separation > 0.3% on H1 (start value, calibrate during backtesting) | All M/W setups |

### 6.4 Gate Outcomes

| Outcome | Meaning |
|---------|---------|
| All gates pass + quality ≥ 0.50 + reversal ≥ 0.30 | Proceed to confluence scoring |
| All gates pass + quality < 0.50 | NO TRADE — formation too weak |
| All gates pass + reversal < 0.30 | NO TRADE for reversal setups (continuation reset — see §5.12) |
| Any hard gate fails | NO TRADE — fundamental requirement not met |

---

## 7. Confluence Scorer (Post-Gate)

Only scored if ALL gates pass AND quality_score ≥ 0.50.

### 7.1 Forex Weight Table

| Factor | Weight | Scoring Logic |
|--------|--------|---------------|
| `mtf_alignment` | 0.14 | §4.5 table |
| `multi_session` | 0.10 | 3+ sessions = 1.0, 2 = 0.7, 1 = 0.2 |
| `svc_present` | 0.10 | True = 1.0, False = 0.0 |
| `hits_to_level` | 0.08 | 0 = 0.0, 1 = 0.4, 2 = 0.7, 3+ = 1.0 |
| `hits_with_volume` | 0.05 | Increasing vol on each hit = 1.0, flat = 0.3, decreasing = 0.1 |
| `near_period_extreme` | 0.08 | §1.2 proximity table |
| `htf_not_consolidating` | 0.08 | H4 NOT in "consolidating" phase (§4.4) = 1.0, consolidating = 0.0 |
| `kill_zone` | 0.06 | In session kill zone (§8.1) = 1.0, otherwise = 0.0 |
| `session_overlap` | 0.04 | In session overlap (§8.1) = 1.0, otherwise = 0.0 |
| `session_phase` | 0.04 | Opening = 1.0, mid = 0.5, closing = 0.2 (§8.2) |
| `day_of_week` | 0.04 | §8.3 weekly model. NOTE: The §8.3 modifiers (Monday −0.10, Wednesday +0.05) are additive offsets to the FINAL confidence score, NOT multiplied by this weight. The weight (0.04) captures session-phase timing quality; the §8.3 modifiers capture the weekly structural bias separately. |
| `asia_control` | 0.03 | Tight range + consolidating = 1.0, trending = 0.0, neutral = 0.5 (§8.4) |
| `ema_bounce` | 0.05 | Price touches 50 EMA (within 0.2%) and closes away with rejection candle = 1.0, no touch = 0.0. Defined inline; no separate section. |
| `dxy_correlation` | 0.06 | DXY direction agrees with trade direction = 1.0, opposes = 0.0, flat/unknown = 0.5. Forex only (§1.3). Data from cTrader or external feed. |
| **Total** | **0.95** | **5% slack for future factors without renormalizing existing weights** |

### 7.2 Crypto Weight Table

| Factor | Weight | Scoring Logic |
|--------|--------|---------------|
| All forex factors EXCEPT `dxy_correlation` | 0.89 | (forex total minus 0.06) |
| `oi_signal` | 0.08 | OI increasing in trade direction = 1.0, decreasing = 0.0 |
| `funding_extreme` | 0.05 | Funding at extreme (top/bottom 5% of 90d range) opposing trade = 1.0 (contrarian) |
| `btc_dominance` | 0.05 | Falling BTC dom + long alt = 1.0, rising dom + short alt = 1.0 |
| `liquidation_cluster` | 0.06 | Target aligns with liquidation cluster = 1.0 |
| `cme_gap_target` | 0.04 | Target aligns with CME gap = 1.0 |
| **Total** | **1.17** | (normalized to 1.0 in calculation) |

### 7.3 Confidence Calculation

```python
if not all_gates_passed or quality_score < 0.50:
    return None  # No signal

# Base confluence score from weight table
raw = sum(factor_score * weight for each applicable factor)

# Apply HTF context modifier (§4.4 Output B)
raw += htf_modifier  # Range: -0.25 to +0.15

# Floor at 0.0 before bonuses
raw = max(0.0, raw)

# Interaction bonuses (applied to floored raw, before cap)
# These are placeholder values (8% and 5%) to be calibrated during backtesting.
# They represent the observed synergy when strong confluence factors co-occur.
if mtf_alignment >= 0.75 and multi_session >= 0.7:
    raw *= 1.08  # Strong TF + multi-session
if svc_present and hits_to_level >= 0.7:
    raw *= 1.05  # SVC + exhaustion

# Normalize crypto scores (weights sum > 1.0)
# For crypto: raw max ≈ 1.17 before normalization.
# Interaction bonuses make raw potentially > 1.17.
# Solution: normalize first (raw / 1.17), then bonuses can push above 1.0, capped at 1.0.
if variant == "crypto":
    raw /= 1.17  # Normalize base weights to 1.0 scale
    # Interaction bonuses were already applied above.
    # After normalization, max possible ≈ 1.08 × 1.05 ≈ 1.13, capped at 1.0.

# Cap at 1.0
confidence = min(raw, 1.0)

# Action thresholds:
#   ≥ 0.65 → STRONG (full position)
#   0.50-0.64 → MODERATE (half position)
#   0.40-0.49 → GRAY ZONE (quarter position, requires manual review or extra confirmation)
#   < 0.40 → NO TRADE (insufficient confluence despite passing gates)
```

### 7.4 Fibonacci Confluence Entry

When a Fibonacci retracement level (particularly the 50%) coincides with a counted TBD level or an AOI, it creates a high-probability entry zone. The 50% retracement is the strongest confluence level — it's where retracements most commonly stall and reverse.

**Rules:**
- Calculate fib levels of the most recent significant swing (H4 or H1)
- If any fib level (38.2%, 50%, 61.8%) falls within 0.2% of a counted level or AOI → confluence confirmed
- 50% fib + TBD level = highest conviction entry zone
- Score: fib_confluence = 1.0 if 50% matches, 0.7 if 38.2% or 61.8% matches, 0.0 otherwise
- This is a bonus factor — not in weight tables yet. During calibration, if fib_confluence predicts profitable trades, add to weight table with ~0.05 weight.

---

## 8. Session Logic

### 8.1 Session Definitions (Forex, UTC)

| Session | Active Period | Kill Zone |
|---------|--------------|-----------|
| Asia | 00:00–07:00 | 00:00–01:30 |
| London | 07:00–16:00 | 07:00–08:30 |
| New York | 12:00–21:00 | 12:30–14:00 |

**Overlaps:**
- Asia→London: 07:00–08:00 (elevated conviction during transition)
- London→NY: 12:00–16:00 (highest liquidity window)

### 8.2 Session Phase Scoring

| Phase | Time in Session | Score |
|-------|----------------|-------|
| Opening | First 90 minutes | 1.0 |
| Mid | After 90 min, before last 60 min | 0.5 |
| Closing | Last 60 minutes | 0.2 |

### 8.3 Weekly Structural Model

| Day | Structural Role | Confidence Modifier |
|-----|----------------|-------------------|
| Monday | Fake move day — spike in one direction, then reverses. Do NOT chase the spike. | -0.10 for setups in the spike direction |
| Tuesday | True trend day — follows Monday's reversal direction | +0.05 for setups in Monday's reversal direction |
| Wednesday | Midweek reversal window — peak of weekly range common. Wednesday reversals often produce the week's largest range extension. | +0.05 for reversal setups |
| Thursday | Typical trading day | 0.0 (neutral) |
| Friday | Unpredictable — reduce size or skip entirely | -0.10 for all new setups |

### 8.4 Asia Session Rules

| Rule | Description | Scoring |
|------|-------------|---------|
| Asia range qualification | Total range < 2.0% and consolidating (not trending) for valid Asia→UK setups | Gate (§6.3) |
| Asia measurement window | From 00:00 UTC, excluding final 30 min (07:00-07:30) where profit-taking distorts range | Used for range calculation |
| Asia directional control | If Asia trends strongly (range > 2%), it may suppress typical London/NY session behavior | asia_control factor: tight+consolidating = 1.0, trending = 0.0, neutral = 0.5 |
| Asia directional inference | If Asia sets the HIGH first (then drops), expect bearish follow-through at London open. If Asia sets the LOW first (then rallies), expect bullish follow-through. | Directional bias for session-open setups |
| Asia timeout | If no setup in first 60-90 minutes, close platform and return at London open. Session changeover often delivers the setups. | Operational rule (no score impact) |

### 8.5 Pre-US Mandatory Exit

For all Asia→UK trade setups (FL-001, FL-002, FL-003, FL-004):
- **Mandatory close** before 8:00 AM NY time (13:00 UTC) regardless of P&L
- Holding into US session defeats the setup logic
- This is a risk management gate, not a confluence factor

### 8.6 Instrument-Specific Notes (Forex)

| Instrument | Notes |
|-----------|-------|
| EURUSD | Highest quality spreads (0.1-0.2 pip), cleanest formations, DXY correlation critical |
| GBPUSD | Higher volatility than EURUSD, cleaner formations when GBP-driven news absent. Good for M/W at London open. |
| GBPJPY | London-open volatility pair — first 30-60 min often gap-fills from Asia. Choppy outside London session. Higher spread. |
| XAUUSD | Wide spreads, strong session structure, excellent for Asian range → London expansion setups. Avoid during low-liquidity (Asia late). |
| USDCAD | Oil correlation influences direction. Check crude inventory days (Wednesday EIA report). |

---

## 9. Stop Loss & Target Placement

### 9.1 Stop Loss Rules (Priority Order)

1. **Cover the vector** — Stop beyond the most recent vector candle that price came FROM (body extreme, not wick)
2. **Beyond the SVC candle** — If SVC is present at entry, stop beyond the SVC body extreme
3. **Beyond the formation extreme** — SL2 for W, SH2 for M (exception: FL-001 uses first peak)
4. **Beyond the counted level** — The level the pattern is at
5. **ATR-based** (last resort) — 1.5 × ATR(14) from entry price

**Heat map rule:** When liquidation/volume heat map data is available, avoid placing stops in the brightest (highest volume/liquidity) clusters — these are exactly where stop-hunts target. Prefer stops just outside heat map clusters.

**Stop movement rule:** Stop loss should ONLY be moved in the direction of the trade, and ONLY after price has pushed away in your favor (not during retraces). Moving stop during a retrace = getting stopped out at a worse price. Specifically:
- Move to breakeven ONLY after price reaches 1:1 R:R
- Trail stop ONLY after price pushes to new extremes
- Never tighten stop during a pullback

**Never place stops at:**
- Exact round numbers (hunted)
- Exact prior highs/lows without vector buffer (hunted)
- Inside the formation (guaranteed fill on noise)
- Bright heat map clusters (stop-hunt magnets)

### 9.2 Target Rules (Priority Order)

1. **Unrecovered vector candle** — The first vector candle in the trade direction that hasn't been recovered
2. **Next counted level** — R1 → 50 EMA, R2 → 200 EMA
3. **Liquidity pool** — Prior highs/lows where trapped positions exist
4. **Mean reversion** — Middle of daily range (for NYC reversal setups specifically)
5. **Fib confluence level** — If 50% fib aligns with a TBD level, target that zone (§7.4)

**3:1 minimum filter (gate requirement):**
- Natural target must be ≥ 3:1 from stop distance
- If natural target < 3:1, **skip the trade**

### 9.3 Partial Exit Strategy

Partial exit sizing depends on trade type:

**Normal trades (H1 M/W, multi-session):**
| Milestone | Action |
|-----------|--------|
| 1:1 R:R reached | Close 25% of position, move stop to breakeven |
| 3:1 R:R reached | Close 25% of position, trail remaining stop |
| Natural target | Close remaining 50% or on structure invalidation |

**Scalp trades (M15 FL strategies, single-session):**
| Milestone | Action |
|-----------|--------|
| 1:1 R:R reached | Close 50% of position, move stop to breakeven |
| 2:1 R:R reached | Close 25% of position, trail remaining stop |
| Natural target | Close remaining 25% or on structure invalidation |

**Structure invalidation** (see §1.1 glossary): Price breaks the 50 EMA in the opposite direction of the trade, or forms a new swing extreme that contradicts the original pattern thesis. On invalidation, close remaining position immediately at market.

### 9.4 200 EMA Reassessment Rule

When price reaches the 200 EMA (target for R2 setups):
- **Do NOT assume automatic break** — reassess:
  - Exit (take profit)
  - Reduce position size
  - Flip bias (if reversal signals present)
  - Stand aside (if unclear)
- 200 EMA is a major decision point, not a guaranteed pass-through

---

## 10. Signal Output Schema

```json
{
  "signal_id": "uuid",
  "timestamp": "2026-04-09T14:30:00Z",
  "instrument": "EURUSD",
  "direction": "long",
  "setup_type": "multi_session_w",
  "setup_subtype": "conservative",
  "confidence": 0.72,
  "action": "strong",
  "variant": "forex",
  
  "gates": {
    "passed": true,
    "universal": {
      "pattern_valid": true,
      "pattern_at_level": true,
      "rr_minimum": true,
      "no_major_event": true,
      "levels_complete": true,
      "quality_threshold": true,
      "reversal_threshold": true
    },
    "pattern_specific": {
      "g1_structure": true,
      "g2_level_context": true,
      "g3_first_peak_rejection": true,
      "g4_middle_peak_ema": "soft",
      "g5_post_peak_confirm": true,
      "g6_ema_break_retest": true
    },
    "strategy_specific": {}
  },

  "entry": {
    "type": "zone",
    "price_low": 1.08350,
    "price_high": 1.08400,
    "trigger": "retest_of_50_ema_after_break",
    "timeframe": "M15"
  },
  
  "stop_loss": {
    "price": 1.08150,
    "basis": "below_vector_candle",
    "pips": 200,
    "method": "cover_vector"
  },
  
  "targets": [
    { "level": "tp1", "price": 1.08550, "rr": 1.0, "action": "close_25pct_move_sl_to_be" },
    { "level": "tp2", "price": 1.08750, "rr": 3.0, "action": "close_25pct_trail_stop" },
    { "level": "tp3", "price": 1.08950, "rr": 5.0, "action": "natural_target_unrecovered_vector" }
  ],

  "reversal_score": 0.78,
  "quality_score": 0.68,
  "trade_type": "normal",
  
  "confluence": {
    "mtf_alignment": 0.75,
    "multi_session": 1.0,
    "svc_present": true,
    "hits_to_level": 2,
    "hits_with_volume": 0.7,
    "near_period_extreme": 0.7,
    "htf_not_consolidating": true,
    "htf_phase": "aligned",
    "kill_zone": true,
    "session_overlap": true,
    "session_phase": "opening",
    "day_of_week": "wednesday",
    "asia_control": "tight_range",
    "ema_bounce": true,
    "dxy_correlation": true,
    "fib_confluence": null,
    "_scoring_note": "Boolean fields above are scored 1.0/0.0 in the weight multiplication. Raw counts (hits_to_level) are mapped per §7.1 table. The confluence object stores both human-readable values AND the scored values used in the confidence calculation.",
    "_scored_values": {
      "mtf_alignment": 0.75,
      "multi_session": 1.0,
      "svc_present": 1.0,
      "hits_to_level": 0.7,
      "hits_with_volume": 0.7,
      "near_period_extreme": 0.7,
      "htf_not_consolidating": 1.0,
      "kill_zone": 1.0,
      "session_overlap": 1.0,
      "session_phase": 1.0,
      "day_of_week": 0.0,
      "asia_control": 1.0,
      "ema_bounce": 1.0,
      "dxy_correlation": 1.0
    }
  },
  
  "mtf_context": {
    "d1": { "direction": "bullish", "level": 2, "note": "mid-move approaching R3" },
    "h4": { "direction": "bullish", "level": 1, "note": "trending, not consolidating" },
    "h1": { "direction": "neutral_forming_w", "level": -3, "note": "W at drop level 3 = reversal zone" },
    "m15": { "direction": "neutral", "level": null, "note": "awaiting H1 confirmation for entry" }
  },
  
  "session_context": {
    "current_session": "london",
    "kill_zone": true,
    "session_overlap": "london_ny",
    "day_of_week": "wednesday",
    "weekly_role": "midweek_reversal",
    "pre_us_exit": null,
    "asia_range": {
      "high": 1.08600,
      "low": 1.08200,
      "pct_50": 1.08400,
      "pct_range": 0.37,
      "qualified": true,
      "directional_inference": "high_set_first_expect_dip"
    }
  },
  
  "meta": {
    "engine_version": "2.2",
    "data_source": "ctrader_openapi",
    "backtest_compatible": true
  }
}
```

---

## 11. Backtesting Integration

### 11.1 Approach

Test setups that pass ALL gates with confidence ≥ threshold. Optimize for **expectancy**, not hit rate.

### 11.2 Test Matrix

| Dimension | Values |
|-----------|--------|
| Confidence threshold | 0.40, 0.50, 0.65, 0.75 |
| Instrument | EURUSD, GBPUSD, GBPJPY, XAUUSD (forex) / BTC, ETH (crypto) |
| Timeframe | H1 primary, M15 entries |
| Session | All, Asia-only, London-only, NY-only |
| Gate relaxation | All gates vs. drop G4, G5, G6 individually |
| Sample period | Minimum 24 months |

### 11.3 Metrics

- **Expectancy** = (hit_rate × avg_rr) - ((1 - hit_rate) × 1.0) — PRIMARY
- **Hit rate**, **Average R:R**, **Profit factor**, **Max drawdown**, **Sharpe ratio** (100+ samples)

### 11.4 Calibration Requirements

- **Minimum 500 labeled examples** for weight optimization
- **Per-factor analysis:** Which individual factors most predict profitable trades?
- **Gate sensitivity:** Does dropping any single gate significantly change results?
- **Walk-forward:** 12-month optimize, 6-month validate, rolling
- **Regime testing:** Trending, ranging, volatile, low-volatility periods

---

## 12. Implementation Roadmap

| Phase | Scope | Dependencies | Est. |
|-------|-------|-------------|------|
| **1** | Swing detector + Level counter + MTTF framework | cTrader data (M5-D1) | 2-3d |
| **2** | Pattern detector (M/W 11-pt, SVC, ILOD/IHOD, traps, Asia liquidity grab, continuation reset) | Phase 1 | 4-5d |
| **3** | Gate validator + confluence scorer + session logic + DXY filter | Phase 2 | 2-3d |
| **4** | Confidence engine + signal output + stop/target + fib confluence | Phase 3 | 2d |
| **5** | Backtest integration + calibration (500+ samples) | Phase 4 + 24mo data | 5-7d |
| **6** | Crypto extensions (OI, funding, BTC dom, liquidations, weekend trap) | Phase 3 + Coinglass | 3-4d |
| **7** | Forward test (paper trading) | Phase 5/6 + cTrader execution | Ongoing |
| **8** | Boardroom detection + Flight Log strategy sub-types (staircase, 33-trade internal structure) | Phase 5 validated | 3-4d |

---

## 13. File Structure

```
src/forex_bot/
├── signal_engine/
│   ├── __init__.py
│   ├── glossary.py                 # All term definitions (from §1.1)
│   ├── thresholds.py               # Proximity thresholds (from §1.2)
│   ├── swing_detector.py           # §3 — N-bar swing detection
│   ├── level_counter.py            # §4.2-4.3 — Rise/drop counting + TF feeding
│   ├── htf_analyzer.py             # §4.4-4.6 — HTF phase + alignment scoring
│   ├── pattern_detector.py         # §5 — M/W (11-pt), SVC, traps, ILOD, liquidity grab, continuation reset
│   ├── flight_log_strategies.py    # §5.10 — FL-001 through FL-006 named strategies
│   ├── reversal_retrace.py         # §5.7 — Reversal vs retrace scoring
│   ├── gate_validator.py           # §6 — Hard + soft gates, quality threshold
│   ├── confluence_scorer.py        # §7 — Booster scoring + weight tables + fib confluence
│   ├── session_logic.py            # §8 — Sessions, phases, weekly model, Asia rules
│   ├── stop_target.py              # §9 — Stop (cover vector) + targets + partial exits
│   ├── signal_output.py            # §10 — JSON schema generation
│   ├── dxy_filter.py               # DXY correlation filter (forex only, Phase 3)
│   ├── crypto_extensions.py        # Crypto-only factors (Phase 6): OI, funding, BTC dom
│   └── backtest_bridge.py          # Signal → backtest format (Phase 5)
├── data/
│   ├── forex/
│   └── crypto/
└── tests/
    ├── test_swing_detector.py
    ├── test_level_counter.py
    ├── test_htf_analyzer.py
    ├── test_pattern_detector.py
    ├── test_gate_validator.py
    ├── test_confluence_scorer.py
    ├── test_session_logic.py
    ├── test_stop_target.py
    ├── test_signal_output.py
    └── fixtures/
```

---

## 14. Open Questions

1. **Pattern labeling** — How to efficiently label 500+ historical setups? Manual chart review is the bottleneck.
2. **G4 strictness** — Current implementation: soft gate with quality tiers. Validate during backtesting whether making it a hard gate improves or hurts expectancy.
3. **Instrument start** — EURUSD only first, or multi-instrument from day one?
4. **Crypto data costs** — Coinglass API rate limits and historical data availability?
5. **Weight initialization** — These estimated weights for initial calibration, or start equal?
6. **ML for pattern detection** — Rule-based (interpretable) vs ML classifier (potentially more accurate but black box)?
7. **DXY data source** — Which provider for real-time DXY index in the cTrader ecosystem?
8. **Flat-EMA threshold** — How close must 50 EMA and 200 EMA be to trigger the "bunched" no-trade gate? Suggest: start with 0.3% separation on H1, calibrate.
