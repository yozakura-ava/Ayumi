# CFTC COT Data Integration

## Overview

CFTC Commitments of Traders (COT) data provides weekly institutional
positioning for forex futures markets. This is one of the few free
sources of positioning data available to retail traders.

**Role:** Weekly confidence multiplier — NOT an entry signal.

COT data is integrated as a structural regime indicator. It adjusts
the ConfidenceEngine's output based on whether speculative positioning
aligns with or diverges from the trade direction.

## Data Source

| Property | Value |
|----------|-------|
| **Publisher** | U.S. Commodity Futures Trading Commission (CFTC) |
| **URL (Legacy)** | `https://www.cftc.gov/files/dea/history/fut_txt_{year}.zip` |
| **URL (Disaggregated)** | `https://www.cftc.gov/files/dea/history/com_dis_txt_{year}.zip` |
| **Format** | CSV (ZIP-compressed) |
| **Cadence** | Weekly (Friday publication for Tuesday close) |
| **Lag** | ~3 days |
| **Cost** | Free |

## Supported Formats

### Legacy Format

The traditional COT report format with non-commercial (speculative),
commercial (hedger), and non-reportable positions. Column indices:

| Index | Field |
|-------|-------|
| 0 | Market Name (e.g. "JAPANESE YEN") |
| 1 | CFTC Contract Market Code |
| 2 | Report Date (MM/DD/YYYY) |
| 4 | Non-Commercial Long |
| 5 | Non-Commercial Short |
| 6 | Non-Commercial Spreading |
| 7 | Commercial Long |
| 8 | Commercial Short |

### Disaggregated Format

More granular report separating Producer/Merchant/Processor from
Managed Money and Other Reportables. Column indices:

| Index | Field |
|-------|-------|
| 0 | Market Name |
| 2 | Report Date |
| 4 | Producer/Merchant Long |
| 5 | Producer/Merchant Short |
| 8 | Managed Money Long |
| 9 | Managed Money Short |
| 10 | Managed Money Spreading |

Managed Money positions are used as the non-commercial proxy.

## Forex Pair Mapping

CFTC reports futures by market name (e.g. "JAPANESE YEN"), not by
ISO currency pair. The fetcher maps:

| FX Pair | CFTC Market Name | Invert Signal |
|---------|------------------|---------------|
| USDJPY | JAPANESE YEN | Yes |
| EURUSD | EURO FX | No |
| GBPUSD | BRITISH POUND | No |
| USDCHF | SWISS FRANC | Yes |
| USDCAD | CANADIAN DOLLAR | Yes |
| AUDUSD | AUSTRALIAN DOLLAR | No |
| NZDUSD | NEW ZEALAND DOLLAR | No |

For USD-quoted pairs where the CFTC reports the non-USD currency
(USDJPY, USDCHF, USDCAD), the signal is inverted: a net-long position
in JPY means traders are bullish on JPY, which is bearish for USDJPY.

## Confidence Multiplier Integration

### Design

COT data acts as a confidence multiplier through `COTDivergenceSignal`:

1. **`COTFetcher.get_divergence_signal(pair)`** computes a directional
   bias from the net positioning trend over the past N weeks.
2. The signal includes a `confidence_adjustment` value (±0.05 max).
3. **`COTFetcher.apply_to_confidence(raw_confidence, pair, direction)`**
   is the integration helper: feed it the strategy's raw confidence
   and the trade direction, get back the COT-adjusted confidence.

### Integration Helper

The `apply_to_confidence()` method is the integration contract for
the ConfidenceEngine. Callers do not need to interpret the signal
manually — the helper handles alignment logic:

```python
from data.cot_fetcher import COTFetcher
from data.cot_cache import COTCache

cache = COTCache("data/cot_cache")
fetcher = COTFetcher(cache=cache)

# Strategy says: long USDJPY at 0.65 confidence
raw_confidence = 0.65
trade_direction = "long"

adjusted_confidence = fetcher.apply_to_confidence(
    raw_confidence, "USDJPY", trade_direction
)
# Returns: 0.65 ± 0.05 depending on COT alignment
```

**Alignment rules:**
- `signal.bias == direction` → COT confirms trade → confidence boosted
- `signal.bias != direction` → COT warns against trade → confidence reduced
- `signal.bias == "neutral"` → insufficient data → no change
- Output clamped to `[0.0, 1.0]`

### ConfidenceEngine Wiring (Future)

The current ConfidenceEngine (`src/forex_bot/confidence/engine.py`)
does not yet call `apply_to_confidence()`. A follow-up card should
wire the helper into the scoring pipeline, e.g.:

```python
# Inside ConfidenceEngine.score()
cot_fetcher = COTFetcher(cache=self._cot_cache)
adjusted = cot_fetcher.apply_to_confidence(
    raw_confidence, symbol, direction
)
# Then pass `adjusted` through the existing confluence + gate pipeline
```

The helper is the integration surface; only the wiring inside the
engine needs to be added.

### Regime Change Detection

When the bias flips (e.g., net-long → net-short), the signal is
amplified by 1.5× (still capped at ±0.05). This ensures regime
changes are visible without making COT a dominant signal.

### Cap Rationale

±0.05 adjustment means:
- Maximum strategy confidence (0.95) → adjusted to 1.0 with alignment
- Strong strategy confidence (0.70) → adjusted to 0.65 with divergence
- Weak strategy confidence (0.50) → adjusted to 0.45 with divergence

This is intentionally small. COT is a weekly structural indicator, not
a tactical signal. It should nudge, not override.

## Caching

Weekly cache at `data/cot_cache/`:

```
cot_legacy_2026.json     # Current year (stale after 4 days)
cot_disaggregated_2026.json
cot_legacy_2025.json     # Historical (never stale)
cot_meta.json            # Version + fetch metadata
```

- **Version:** Schema version 2 (bump on breaking changes)
- **Staleness:** Current year data re-fetches after 4 days; historical
  data is permanent
- **Cleanup:** Files older than 365 days are removed by `cache.cleanup()`

## Limitations

1. **3-day publication lag** — Friday release for Tuesday close.
   COT is useless for entry timing. Use it for weekly regime context.

2. **Futures ≠ spot FX** — COT reflects futures market positioning.
   Divergences between futures and spot during regime changes ARE
   the signal. Don't expect 1:1 correlation with spot flow.

3. **No intraweek granularity** — One data point per week. The
   confidence adjustment changes only on Friday/Saturday when new
   data arrives.

4. **CITS format not supported** — Only Legacy and Disaggregated
   formats are parsed. CITS (Combined Interval and Term Structure)
   can be added in a follow-up if needed.

5. **ConfidenceEngine wiring is a follow-up** — `COTFetcher.apply_to_confidence()`
   is the integration contract, but the ConfidenceEngine itself does
   not yet call it. A follow-up card should add a one-line call to
   `apply_to_confidence()` inside `ConfidenceEngine.score()` before
   the confluence/gate layers run.

## Usage

```python
from data.cot_fetcher import COTFetcher, COTFormat
from data.cot_cache import COTCache

# Initialise with cache
cache = COTCache("data/cot_cache")
fetcher = COTFetcher(cache=cache)

# Get latest positioning for USDJPY
pos = fetcher.get_positioning("USDJPY", COTFormat.LEGACY)
print(f"Net position: {pos.net_position:,.0f}")
print(f"Net ratio: {pos.net_ratio:.2%}")

# Get divergence signal (raw)
signal = fetcher.get_divergence_signal("USDJPY")
print(f"Bias: {signal.bias}")
print(f"Adjustment: {signal.confidence_adjustment:+.4f}")
print(f"Rationale: {signal.rationale}")

# Apply as confidence multiplier (recommended integration)
raw_confidence = 0.65
trade_direction = "long"
adjusted = fetcher.apply_to_confidence(raw_confidence, "USDJPY", trade_direction)
print(f"Adjusted confidence: {adjusted:.4f}")
```

## Source

- Research: `docs/research/srb/SRB-AYUMI-003.md` (Satoshi, Tier 2)
- Card: 946a48a5 — Wire CFTC COT data as weekly confidence multiplier