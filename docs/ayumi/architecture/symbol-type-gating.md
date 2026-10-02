# Symbol-Type Gating Architecture

## Overview

The confidence layer is asset-class agnostic by construction. It needs only a `symbol_type` tag to route symbols to the correct detector stack. This document describes the symbol-type enum, the substitution mapping from crypto-native detectors to forex equivalents, and the gating module that performs routing.

## Source

- **Research brief:** `docs/research/srb/SRB-AYUMI-008.md` (Satoshi, Tier 1, 2026-07-01)
- **Implementation card:** `592adae2` on the default workboard

## SymbolType Enum

The `SymbolType` enum in `src/forex_bot/models/instrument.py` defines seven asset-class tags:

| Member | Description | Examples |
|--------|-------------|----------|
| `crypto_perp` | Crypto perpetual futures | BTCUSDT perp, ETHUSDT perp |
| `crypto_spot` | Crypto spot | BTCUSD, ETHUSD |
| `forex_major` | Major FX pair | EURUSD, USDJPY, GBPUSD |
| `forex_cross` | Cross FX pair (no USD) | EURJPY, GBPJPY, AUDCAD |
| `forex_exotic` | Exotic FX pair | USDTRY, USDMXN |
| `metal` | Precious/base metals | XAUUSD, XAGUSD |
| `index` | Broad market index | US500, NAS100, GER40 |

## Detector Substitution Table (§4.1)

Crypto-native detectors have forex substitutes with different cadence and signal interpretation:

| Crypto Detector | Forex Substitute | Cadence | Signal Interpretation |
|-----------------|------------------|---------|----------------------|
| Open Interest | COT net non-commercial positioning | Weekly | Positioning shifts indicate regime changes, not entry timing |
| Funding Rate | Central-bank rate differential | Daily | Carry cost drives multi-week flow bias |
| Liquidations | Order-flow / DOM pressure proxies | Intraday | Spike detection in order flow substitutes for liquidation cascades |

### Per-Type Detector Stacks

| Symbol Type | Detectors |
|-------------|-----------|
| `crypto_perp` | `open_interest`, `funding_rate`, `liquidations` |
| `crypto_spot` | `open_interest` |
| `forex_major` | `cot_positioning`, `rate_differential`, `order_flow_proxy` |
| `forex_cross` | `cot_positioning`, `rate_differential`, `order_flow_proxy` |
| `forex_exotic` | `cot_positioning`, `rate_differential` |
| `metal` | `cot_positioning`, `rate_differential` |
| `index` | `cot_positioning` |

## Gating Module

`src/forex_bot/confidence/symbol_type_gating.py` provides:

### `SymbolTypeGate`

A non-blocking confidence gate. Always passes — its purpose is routing, not rejection.

**Integration:** Add to `ConfidenceEngine` via `engine.add_gate(SymbolTypeGate())`.

**API:**
- `check(ctx)` → `GateCheck` (pipeline-compatible, always `passed=True`)
- `route(ctx)` → `SymbolTypeRouting` (structured routing with detector list)

**Resolution priority:**
1. Explicit `symbol_type` in context (override)
2. Instrument registry lookup
3. Heuristic classification via `classify_symbol()`

### `classify_symbol(symbol: str) → SymbolType`

Heuristic fallback classifier. Recognizes:
- USDT/PERP suffix → `crypto_perp`
- XAU/XAG in symbol → `metal`
- Known index tickers → `index`
- 6-char with major currency codes → `forex_major`
- Fallback → `forex_exotic`

### `get_detector_stack(symbol_type) → list[str]`

Direct lookup for callers that already have a `SymbolType`.

## Compatibility Matrix

| Concern | Status |
|--------|--------|
| Crypto detector stack unchanged for `crypto_perp` | ✅ Verified — `["open_interest", "funding_rate", "liquidations"]` |
| Forex substitutes are additive | ✅ No existing code modified (new module only) |
| Existing `InstrumentSpec` in `risk/sl_position_sizer.py` | ✅ Unchanged — `Instrument` is a new dataclass, not a refactor |
| `ConfidenceEngine` integration | ✅ Optional via `engine.add_gate()` — no forced coupling |

## Out of Scope

- Implementing actual forex detectors (separate cards: COT, FRED, order flow)
- Migration of existing crypto symbols (automatic via enum default)
- Paid COT data providers (CFTC free tier sufficient)

## Test Coverage

`tests/unit/confidence/test_symbol_type_gating.py` covers:
- All 7 enum variants present
- Crypto perp routing → crypto detectors (unchanged stack)
- Forex major routing → forex detectors (substitute stack)
- Forex cross routing
- Forex exotic reduced stack
- Heuristic classification for unregistered symbols
- Registry-based classification
- Explicit override precedence
- Gate-always-passes behavior
- Edge cases (empty symbol, unknown types)
