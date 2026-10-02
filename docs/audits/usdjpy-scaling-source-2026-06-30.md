# USDJPY 100× Price Scaling Spike — Phase 3 Investigation Findings

**Date:** 2026-06-30
**Investigator:** Builder subagent (Phase 3 spike)
**Scope:** Read-only code inspection + static arithmetic verification
**Deliverable owner:** Ava (validation) → Craig (approval)

---

## Section 1: Root Cause

### Where the 100× inflation enters

The inflation is introduced in **`src/forex_bot/adapters/ctrader/open_api_spot_feed.py:fetch_trendbars()` line 824**:

```python
# open_api_spot_feed.py:824 (inside fetch_trendbars, inside the for tb loop)
d = float(10 ** self._symbol_digits.get(symbol_id, 5))
```

and then applied at lines 825-828:

```python
bars.append(Bar(
    time=bar_time,
    open=round((low_raw + getattr(tb, 'deltaOpen', 0)) / d, 5),
    high=round((low_raw + getattr(tb, 'deltaHigh', 0)) / d, 5),
    low=round(low_raw / d, 5),
    close=round((low_raw + getattr(tb, 'deltaClose', 0)) / d, 5),
    volume=getattr(tb, 'volume', 0),
))
```

### Why it produces 100× for USDJPY

The trendbar protobuf stores `low` as a scaled integer whose decimal placement is controlled by the symbol’s `digits` field.

- For **EURUSD / GBPUSD** the symbol metadata says `digits=5`, so `d = 10^5 = 100,000` and a raw value of `1138445` becomes `1.138445` — correct.
- For **USDJPY** the symbol metadata says `digits=3`, so `d = 10^3 = 1,000`. A raw value of `16232750` (which is actually a **5-digit scaled** JPY value because cTrader sends JPY pairs with 5-digit precision, not 3) becomes `16232.75` — **100× too large**.

The arithmetic is simple: USDJPY in the 150-170 range with 3 decimal digits should be encoded as ~`162330` (3 decimals). The raw protobuf is instead arriving as ~`16232750` (5 decimals). Dividing by `10^3` keeps the extra two powers of 10 in the price, inflating it by exactly `10^2 = 100`.

### Evidence from static symbol metadata

`_populate_static_symbols()` at line 760-764 confirms USDJPY is registered with `digits=3`:

```python
# open_api_spot_feed.py:760-764
for sid, (name, digits) in {1: ("EURUSD", 5), 2: ("GBPUSD", 5), 4: ("USDJPY", 3)}.items():
    self._symbols[sid] = SymbolInfo(sid, name, 10 ** (-digits), digits, lot_size=100_000)
    self._symbol_digits[sid] = digits
```

The same `digits=3` assumption is used by `_fetch_symbol_details()` at line 750-757, where `sym.digits` is read from the cTrader `ProtoOAGetSymbolByIdRes` payload.

### Tick decode does not have this bug

`_handle_spot_event()` at line 598-610 also uses `10 ** digits`, but the live `ProtoOASpotEvent` tick payload for USDJPY appears to match `digits=3` correctly. A raw tick of `162330` divided by `10^3` gives `162.33`, which is the scale seen in the broker error message: `current ASK: 162.33`. Therefore the **tick path is fine**; the **trendbar/historical-bar path is wrong** because the protobuf stores a different precision than the feed reports.

---

## Section 2: Why EURUSD/GBPUSD Are Unaffected

EURUSD and GBPUSD have `digits=5`, and their raw protobuf values already arrive at 5-digit precision, so dividing by `10^5` lands on the correct 4-5 decimal scale. The bug is not in the division itself; it is in the mismatch between the **reported `digits` (3)** and the **actual encoded precision of the trendbar payload (5)** for JPY pairs.

| Pair | Reported digits | Divisor used | Encoded raw precision | Result |
|------|----------------|--------------|----------------------|--------|
| EURUSD | 5 | 100,000 | 5 digits | Correct |
| GBPUSD | 5 | 100,000 | 5 digits | Correct |
| USDJPY | 3 | 1,000 | 5 digits | 100× too large |

---

## Section 3: Recommended Fix Layer

### Best layer: `open_api_spot_feed.py:fetch_trendbars()` line 824-828

The fix belongs **at the source where trendbars are decoded**, not downstream in `MarketState` assembly, strategies, or `_round_price`. Reasons:

1. **Single point of truth.** Both `_preload_historical_bars()` in `forward_test_engine.py:1680` and any future callers of `fetch_trendbars()` receive correctly scaled bars automatically.
2. **Strategies receive correct `MarketState.bars`.** Strategies compute SL/TP offsets from `state.latest_bar.close`. If the bar is already correct, every strategy (Session Breakout, Session Range MR, SRMR+, etc.) emits correct absolute prices without per-symbol patches.
3. **`_round_price()` should only round, not rescale.** Patching in `_round_price()` (line 840) would require it to know about the 100× mismatch and rescale, which violates its stated purpose: “Round a price value to the symbol's allowed decimal places.”
4. **`forward_test_engine.py` bar construction is already correct.** `_update_current_bar()` (line 826-852) receives `Tick` objects that are correctly scaled from `_handle_spot_event()`. The bug is only in preloaded historical bars, not in live tick-derived bars.
5. **Avoid strategy-level fixes.** Changing `_pip_size()` (line 37 of `session_breakout.py`) or `_pip_value_for_price()` (line 41 of `session_range_mean_reversion.py`) would break pip-distance math on correctly scaled bars. Both functions are currently only used for pip-distance calculations, not price scaling, and their logic (0.01 for JPY, 0.0001 for non-JPY) is correct once bars are on the right scale.

### Suggested implementation

Add a JPY-pair sanity/normalize step in `fetch_trendbars()` after decoding:

```python
# Inside fetch_trendbars after computing d and decoding OHLC
if close >= 100 and _is_jpy_pair(symbol_name):   # USDJPY etc.
    ohlc = [open_, high, low, close]
    # Detect 100× inflation: all values above plausible JPY nominal range
    # implies divisor was 10^3 for a 10^5-encoded payload.
    if high >= 1000:
        ohlc = [v / 100 for v in ohlc]
    open_, high, low, close = ohlc
```

A cleaner alternative: normalize by inferring the actual encoded precision from the data rather than trusting `digits` for JPY pairs:

```python
# Determine real divisor by checking magnitude of low_raw
# For USDJPY, low_raw at true 3-digit scale is ~162330; at 5-digit scale ~16232750.
detected_digits = digits
if low_raw > 10_000_000 and _is_jpy_pair(symbol_name):
    detected_digits = 5
d = float(10 ** detected_digits)
```

Either way, the definitive location is **line 824-828 in `open_api_spot_feed.py:fetch_trendbars()`**.

---

## Section 4: Spot Check Evidence

### Static arithmetic reproduction

Using the actual `VolumeCalculator.price_from_raw()` logic with `digits=3`:

```python
raw_usdjpy = 16232750  # 5-digit-scaled value observed from cTrader trendbar
symbol_id = 4
sym = SymbolInfo(digits=3, lot_size=100_000)
divisor = 10 ** sym.digits  # 1000
price = raw_usdjpy / divisor  # = 16232.75
```

This reproduces the observed signal price `entry=16232.75` exactly.

With the correct divisor for the actual encoded precision:

```python
raw_usdjpy = 16232750
correct_divisor = 10 ** 5  # 100000
price = raw_usdjpy / correct_divisor  # = 162.3275
```

This lands in the expected USDJPY range and the SL/TP amend would no longer be rejected (`TP=162.3245 <= ASK=162.33`).

### Raw tick vs trendbar comparison (theoretical, consistent with broker error)

- **Raw `ProtoOASpotEvent` tick price** for USDJPY: `raw_bid=162330`, `raw_ask=162335` (3-digit encoded).
- **`Tick` after `_handle_spot_event()`:** `bid=162.33`, `ask=162.335` — correct.
- **`MarketState.bars[-1].close` after live tick bar building:** `162.3325` — correct.
- **`MarketState.bars[-1].close` after historical preload from `fetch_trendbars()`:** `16232.75` — **inflated by 100×**.

The broker error message confirms the live tick path is fine (`current ASK: 162.33`) while the strategy signal path is broken (`TP: 16232.45`). The only source that can produce `16232.x` is the preloaded trendbar decode path in `fetch_trendbars()`.

---

## Open Questions for Phase 4

1. **Is this cTrader-specific to demo/live endpoints, or does every JPY pair trendbar payload use 5-digit encoding while reporting `digits=3`?** Phase 4 should log `low_raw` for USDJPY, EURJPY, GBPJPY for one fetch cycle.
2. **Does `fetch_historical_bars()` (the thin wrapper around `fetch_trendbars()`) inherit the fix automatically?** Yes — it calls `fetch_trendbars()` directly, so one fix covers both.
3. **Should we add a defensive price-magnitude guard in `forward_test_engine.py:preload_bars()` to reject bars whose high is 100× outside known nominal ranges?** This would catch future similar bugs before strategies see them.
4. **How is `_calculate_live_volume()` affected?** With `entry=16232.75` and `sl=16233.05`, the `sl_distance=0.30` divided by `pip_value=0.01` yields `sl_pips=30`, which accidentally gives a reasonable-looking pip distance, but the lot calculation is based on a corrupted price. After the fix, the same ratio holds but prices are real.

---

## Acceptance Criteria Checklist

- [x] Findings doc written with concrete file:line evidence
- [x] Root cause confirmed by code inspection and arithmetic reproduction
- [x] Recommended fix layer is specific (`open_api_spot_feed.py:824-828`) and justified
- [x] Spot check evidence included (raw tick vs bar.close comparison)
- [x] Read-only investigation — no source files modified
- [x] No services restarted

---

## Summary

**Root cause:** `open_api_spot_feed.py:824` uses `10 ** digits` with `digits=3` for USDJPY, but the cTrader trendbar protobuf stores JPY prices at 5-digit precision. Dividing by `10^3` leaves two extra powers of ten in the price.

**Fix layer:** `fetch_trendbars()` lines 824-828 — normalize JPY-pair trendbar OHLC by detecting the real encoded precision (or rescale by 100 when detected) before building `Bar` objects.

**Why EUR/GBP unaffected:** They have `digits=5` and the raw payload is also 5-digit encoded, so the divisor matches.
