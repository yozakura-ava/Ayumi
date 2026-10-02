# Sprint Plan: Order System Modularization

**Card:** b98e4c0e-ec60-49ac-b67b-dafc83805c2e
**SP Estimate:** 4.0 (autobuild pipeline) — revised after council review
**Date:** 2026-06-27
**Status:** APPROVED — Craig approved 2026-06-27 17:06 EDT with all amendments

---

## 1. Scope

### Files Modified (7)
| File | Lines Changed | What Changes |
|------|--------------|--------------|
| `src/forex_bot/adapters/ctrader/market_data_feed.py` | ~15 | `SymbolInfo` dataclass expanded — add `lot_size`, `min_volume`, `max_volume`, `step_volume` fields |
| `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` | ~60 | Replace `_lots_to_units`, fix `_fetch_symbol_details`, fix price decoding (tick + trendbar), fix volume decoding in `new_order`/`reconcile`/`_handle_execution_event` |
| `src/forex_bot/adapters/ctrader/forward_test_engine.py` | ~5 | Replace hardcoded `100_000` in `_execute_signal_live` with `VolumeCalculator` |
| `src/forex_bot/adapters/ctrader/order_manager.py` | ~15 | Replace hardcoded `100000` in P&L calculation |
| `src/forex_bot/adapters/ctrader/position_monitor.py` | ~10 | Replace hardcoded `100000` in notional/P&L/drawdown |
| `src/forex_bot/adapters/ctrader/account_state.py` | ~10 | Replace `_CONTRACT_SIZE` hardcoded with per-symbol lookup |
| `src/forex_bot/adapters/ctrader/models.py` | ~5 | Update `SymbolInfo` defaults to reference shared constant |

### Files Created (3)
| File | Purpose |
|------|---------|
| `src/forex_bot/adapters/ctrader/volume_calculator.py` | `VolumeCalculator` class — per-symbol volume conversion and validation |
| `tests/unit/ctrader/test_volume_calculator.py` | Unit tests for VolumeCalculator (no cTrader connection needed) |
| `tests/unit/ctrader/test_symbol_info.py` | Unit tests for unified SymbolInfo + price decoding helpers |

### Files NOT Modified (constraints)
| File | Reason |
|------|--------|
| `src/forex_bot/hybrid/paper_trader.py` | Paper trader has its own `100_000` hardcodes for backtest simulation — out of scope for this sprint (separate paper-vs-live path). Card scope is the live order pipeline. |
| `src/forex_bot/quant/portfolio.py` | Portfolio analytics module — same rationale, paper/backtest path. |
| `src/forex_bot/hybrid/risk_manager.py` | Risk manager uses `suggested_lot_size` in lots — doesn't touch raw volume. Not a 100k hardcode site. |
| `kill_switch.py` / `token_lifecycle.py` | Already disabled (`_disabled=True` / `_refresh_disabled=True`). Not in scope. |
| `SOUL.md` / `IDENTITY.md` | Never modified. |

---

## 2. Task Breakdown

### Task 1: SymbolInfo Unification (0.5 SP)
**Builder:** A
**Files (2):** `market_data_feed.py`, `models.py`

Expand the legacy `SymbolInfo` in `market_data_feed.py` (currently has `symbol_id`, `name`, `pip_size`, `digits`) to include:
- `lot_size: int = 100_000`
- `min_volume: int = 0`
- `max_volume: int = 0`
- `step_volume: int = 1`

Update `models.py` `SymbolInfo` (the order-manager variant) to reference `lot_size` from the same source rather than its own duplicate `_CONTRACT_SIZE`. The `SYMBOL_METADATA` dict in `models.py` already has `lot_size` — make it the canonical source for static lookups.

**No file overlap with other tasks.**

**Deliverables:**
- Unified `SymbolInfo` dataclass in `market_data_feed.py` with all volume fields
- `models.py` `SymbolInfo` updated to align (or re-export)
- Backward-compatible defaults (forex = 100k, crypto = varies)

---

### Task 2: VolumeCalculator Module (0.5 SP)
**Builder:** A
**Files (1):** `volume_calculator.py` (NEW)

Create `VolumeCalculator` class:

```python
class VolumeCalculator:
    """Per-symbol volume conversion between lots and cTrader raw volume.
    
    cTrader represents volume in integer units where:
        raw_volume = lots × lot_size
    
    For forex (EURUSD): lot_size = 100,000 → 1 lot = 100,000 units
    For crypto (BTCUSD): lot_size = 100 → 1 lot = 100 units
    """
    
    def __init__(self, symbols: dict[int, SymbolInfo]):
        self._symbols = symbols  # reference to OpenApiSpotFeed._symbols
    
    def lots_to_volume(self, symbol_id: int, lots: float) -> int:
        """Convert lots to cTrader integer volume."""
        sym = self._symbols.get(symbol_id)
        if sym is None:
            raise ValueError(f"Unknown symbol_id: {symbol_id}")
        return int(round(lots * sym.lot_size))
    
    def volume_to_lots(self, symbol_id: int, volume: int) -> float:
        """Convert cTrader integer volume to lots."""
        sym = self._symbols.get(symbol_id)
        if sym is None:
            raise ValueError(f"Unknown symbol_id: {symbol_id}")
        return volume / sym.lot_size
    
    def validate_volume(self, symbol_id: int, volume: int) -> tuple[bool, str]:
        """Validate volume against symbol min/max/step constraints."""
        sym = self._symbols.get(symbol_id)
        if sym is None:
            return False, f"Unknown symbol_id: {symbol_id}"
        if sym.min_volume and volume < sym.min_volume:
            return False, f"Volume {volume} < min {sym.min_volume}"
        if sym.max_volume and volume > sym.max_volume:
            return False, f"Volume {volume} > max {sym.max_volume}"
        if sym.step_volume and sym.step_volume > 0:
            remainder = volume % sym.step_volume
            if remainder != 0:
                return False, f"Volume {volume} not aligned to step {sym.step_volume}"
        return True, "OK"
    
    def price_from_raw(self, symbol_id: int, raw_price: int) -> float:
        """Decode a cTrader raw price using per-symbol digits."""
        sym = self._symbols.get(symbol_id)
        if sym is None:
            return raw_price / 100_000  # legacy fallback
        return raw_price / (10 ** sym.digits)
```

**No file overlap with other tasks.**

---

### Task 3: _fetch_symbol_details + Price Decoding Fix (1 SP)
**Builder:** B
**Files (1):** `open_api_spot_feed.py`

**Depends on:** Task 1 (SymbolInfo fields) and Task 2 (VolumeCalculator API)

#### 3a: _fetch_symbol_details (line 675-692)
Capture `lotSize`, `minVolume`, `maxVolume`, `stepVolume` from protobuf `ProtoOASymbol`:

```python
def _fetch_symbol_details(self, symbol_id: int) -> bool:
    # ... existing request code ...
    sym = payload.symbol[0]
    self._symbols[symbol_id] = SymbolInfo(
        symbol_id=symbol_id,
        name=self._id_to_name.get(symbol_id, str(symbol_id)),
        pip_size=10 ** (-sym.digits),
        digits=sym.digits,
        lot_size=sym.lotSize if sym.lotSize else 100_000,
        min_volume=sym.minVolume if sym.minVolume else 0,
        max_volume=sym.maxVolume if sym.maxVolume else 0,
        step_volume=sym.stepVolume if sym.stepVolume else 1,
    )
```

**Verified protobuf fields (from ProtoOASymbol descriptor):**
- `lotSize` (int64) — contract size per lot
- `minVolume` (int64) — minimum trade volume
- `maxVolume` (int64) — maximum trade volume
- `stepVolume` (int64) — volume increment
- `digits` (int32) — price precision

#### 3b: Price decoding — tick handler (line 555)
Replace:
```python
bid, ask = raw_bid / 100_000, raw_ask / 100_000
```
With:
```python
digits = self._symbol_digits.get(symbol_id, 5)
divisor = 10 ** digits
bid, ask = raw_bid / divisor, raw_ask / divisor
```

#### 3c: Price decoding — trendbar handler (lines 759-764)
Replace:
```python
d = 100000.0
```
With:
```python
d = 10 ** self._symbol_digits.get(symbol_id, 5)
```
(Note: `symbol_id` must be threaded into `_fetch_historical_bars` if not already — verify.)

#### 3d: Wire VolumeCalculator into OpenApiSpotFeed
- Add `self._volume_calc = VolumeCalculator(self._symbols)` in `__init__`
- Replace `_lots_to_units()` calls with `self._volume_calc.lots_to_volume()`
- Replace `/ 100_000.0` volume decoding with `self._volume_calc.volume_to_lots()`

**Sites in open_api_spot_feed.py (verified line numbers):**

| Line | Current | Replacement |
|------|---------|-------------|
| 126-127 | `_lots_to_units(lots)` → `lots * 100_000` | `VolumeCalculator.lots_to_volume(symbol_id, lots)` |
| 555 | `raw_bid / 100_000, raw_ask / 100_000` | `raw_bid / divisor` where `divisor = 10 ** digits` |
| 759 | `d = 100000.0` | `d = 10 ** self._symbol_digits.get(symbol_id, 5)` |
| 791 | `volume=volume / 100_000.0` (rejected order) | `volume=self._volume_calc.volume_to_lots(symbol_id, volume)` |
| 803 | `volume=volume / 100_000.0` (new order) | `volume=self._volume_calc.volume_to_lots(symbol_id, volume)` |
| 918 | `getattr(td, "volume", 0) / 100_000.0` (reconcile) | `self._volume_calc.volume_to_lots(symbol_id, int(getattr(td, "volume", 0)))` |
| 986 | `ev / 100_000.0` (execution event) | `self._volume_calc.volume_to_lots(symbol_id, ev)` |

**Note on `send_order` (line 862-868):** Currently calls `_lots_to_units(volume)` to convert lots → raw volume before calling `new_order`. Replace with `self._volume_calc.lots_to_volume(symbol_id, volume)`.

---

### Task 4: Hardcoded 100k Replacement — Downstream Modules (0.5 SP)
**Builder:** B
**Files (4):** `forward_test_engine.py`, `order_manager.py`, `position_monitor.py`, `account_state.py`

**Depends on:** Task 2 (VolumeCalculator API exists)

**NOTE: File overlap with Task 3 on `open_api_spot_feed.py` — but Task 4 does NOT touch that file. No overlap among these 4 files.**

#### 4a: forward_test_engine.py line 1100
Replace:
```python
volume_raw = int(round(volume_lots * 100_000))
```
With volume calculator lookup via `self._market_feed._volume_calc.lots_to_volume(symbol_id, volume_lots)`.

**Better approach:** Expose a `lots_to_volume` method on `OpenApiSpotFeed`'s public API rather than reaching into `_volume_calc`. Add:
```python
# In OpenApiSpotFeed
def lots_to_volume(self, symbol_id: int, lots: float) -> int:
    return self._volume_calc.lots_to_volume(symbol_id, lots)
```
Then in forward_test_engine:
```python
volume_raw = self._market_feed.lots_to_volume(symbol_id, volume_lots)
```

#### 4b: order_manager.py lines 457, 462, 538, 540
P&L calculation hardcodes `100000`. The `OrderManager` doesn't have access to a `VolumeCalculator` or symbol map.

**Strategy:** Add an optional `contract_size: float = 100_000.0` parameter to the P&L calculation methods, defaulting to forex. The caller (PaperTrader) passes the symbol's contract size. This keeps OrderManager testable without a symbol database.

```python
# Before:
pnl = (exit_price - position.entry_price) * position.volume * 100000
# After:
pnl = (exit_price - position.entry_price) * position.volume * contract_size
```

#### 4c: position_monitor.py lines 135, 144, 250
Same pattern — `p.volume * 100000` for notional. Add `contract_size` parameter or look up from injected symbol map.

**Strategy:** PositionMonitor already receives positions from the forward test engine. Add a `contract_sizes: dict[str, int]` lookup that maps symbol names to contract sizes, injected at construction or fetched from the market feed.

#### 4d: account_state.py line 761
```python
volume_lots = Decimal(int(raw_volume)) / Decimal(_CONTRACT_SIZE)
```
Replace `_CONTRACT_SIZE` with a per-symbol lot_size. The `AccountState` parses reconcile responses — needs a symbol_id → lot_size lookup.

**Strategy:** Pass the `OpenApiSpotFeed._symbols` dict (or a callback) into `AccountState` so it can look up `lot_size` per symbol.

---

### Task 5: Unit Tests (0.5 SP)
**Builder:** C
**Files (2):** `test_volume_calculator.py` (NEW), `test_symbol_info.py` (NEW)

**Depends on:** Tasks 1 + 2 complete

#### test_volume_calculator.py
```
tests/unit/ctrader/__init__.py   (empty, for package)
tests/unit/ctrader/test_volume_calculator.py
```

**Test cases (all run without cTrader connection):**

1. **test_forex_lots_to_volume:** EURUSD (lot_size=100000), 1.0 lots → 100000
2. **test_forex_volume_to_lots:** EURUSD, 100000 → 1.0 lots
3. **test_crypto_lots_to_volume:** BTCUSD (lot_size=100), 1.0 lots → 100
4. **test_crypto_volume_to_lots:** BTCUSD, 100 → 1.0 lots
5. **test_round_trip_forex:** lots → volume → lots == original (0.1, 0.5, 1.0, 2.5)
6. **test_round_trip_crypto:** lots → volume → lots == original (0.1, 1.0, 10.0)
7. **test_validate_volume_min:** volume below min_volume → (False, reason)
8. **test_validate_volume_max:** volume above max_volume → (False, reason)
9. **test_validate_volume_step:** volume not aligned to step → (False, reason)
10. **test_validate_volume_ok:** valid volume → (True, "OK")
11. **test_price_from_raw_forex:** EURUSD digits=5, raw=109450 → 1.09450
12. **test_price_from_raw_crypto:** BTCUSD digits=2, raw=674525 → 6745.25
13. **test_price_from_raw_jpy:** USDJPY digits=3, raw=156780 → 156.780
14. **test_unknown_symbol_raises:** unknown symbol_id → ValueError
15. **test_default_lot_size_forex:** SymbolInfo() with no args → lot_size=100000

#### test_symbol_info.py
1. **test_symbol_info_defaults:** Default SymbolInfo has forex-compatible defaults
2. **test_symbol_info_crypto:** BTCUSD SymbolInfo with lot_size=100
3. **test_symbol_info_with_volume_constraints:** min/max/step populated from protobuf

---

### Task 6: Integration Test Design (0.5 SP)
**Builder:** C
**Files (1):** `tests/unit/ctrader/test_integration_design.py` (NEW — documentation/skipping test)

**Depends:** All previous tasks

This task designs (but does not fully implement) the live integration test. The test exercises the **real forward test pipeline** — not a standalone script.

#### Design

```python
@pytest.mark.live
@pytest.mark.skipif(not os.getenv("CTRADER_OPENAPI_ACCESS_TOKEN"), reason="No cTrader creds")
class TestForwardTestOrderIntegration:
    """Integration test that places a small order through the real pipeline.
    
    NOT a standalone script — uses ForwardTestEngine._execute_signal_live().
    """
    
    def test_small_buy_order_eurusd(self):
        """Verify: strategy signal → _execute_signal_live → OpenApiSpotFeed.new_order → cTrader demo fill."""
        # 1. Create ForwardTestEngine with live_mode=True
        # 2. Connect OpenApiSpotFeed to demo
        # 3. Fabricate a TradeSignal for EURUSD with small volume (0.01 lots)
        # 4. Call engine._execute_signal_live(signal, strategy_id="integration_test")
        # 5. Assert order status == FILLED
        # 6. Verify position appears in engine.positions
        # 7. Clean up: close position via engine._market_feed.close_position()
        # 8. Assert position closed
    
    def test_small_buy_order_btcusd(self):
        """Verify crypto volume path: BTCUSD with lot_size=100."""
        # Same flow, different symbol. Validates VolumeCalculator picks up
        # the correct lot_size from _fetch_symbol_details.
```

#### Triggering mechanism
The test calls `engine._execute_signal_live()` directly with a fabricated `TradeSignal`. This exercises:
- `_calculate_live_volume()` → returns lots
- `self._market_feed.lots_to_volume()` → VolumeCalculator converts to raw volume
- `OpenApiSpotFeed.new_order()` → protobuf order to cTrader
- Execution event handling → fill confirmation

#### Verification
- Check `order.status == OrderStatus.FILLED`
- Check `order.filled_price > 0`
- Call `engine._market_feed.reconcile()` → position appears with correct volume

#### Cleanup
- `engine._market_feed.close_position(position_id, volume)`
- Verify position status moves to CLOSED

---

## 3. VolumeCalculator Design

**Location:** `src/forex_bot/adapters/ctrader/volume_calculator.py`

**Interface:**
```python
class VolumeCalculator:
    def __init__(self, symbols: dict[int, SymbolInfo]): ...
    def lots_to_volume(self, symbol_id: int, lots: float) -> int: ...
    def volume_to_lots(self, symbol_id: int, volume: int) -> float: ...
    def validate_volume(self, symbol_id: int, volume: int) -> tuple[bool, str]: ...
    def price_from_raw(self, symbol_id: int, raw_price: int) -> float: ...
```

**How it gets lot_size:** Holds a reference to `OpenApiSpotFeed._symbols` dict (passed at construction). When `_fetch_symbol_details` updates a `SymbolInfo` entry, VolumeCalculator sees the updated lot_size automatically since it's the same dict reference.

**Lifecycle:** Created in `OpenApiSpotFeed.__init__`. Lives as long as the feed. The feed exposes `self._volume_calc` and delegates public methods (`lots_to_volume`, `volume_to_lots`).

---

## 4. SymbolInfo Unification

### Current State — 3 Separate Definitions

| Location | Fields | Used By |
|----------|--------|---------|
| `market_data_feed.py:306` | `symbol_id, name, pip_size, digits` | `OpenApiSpotFeed` (live tick/order path) |
| `models.py:147` | `pip_size, pip_value_per_lot, lot_size, contract_size` | `OrderManager`, `RiskManager` (paper/backtest path) |
| (implicit in `open_api_spot_feed.py`) | `_symbol_digits: dict[int, int]` parallel lookup | Price decoding |

### Target: Single Unified SymbolInfo

**Location:** `market_data_feed.py` (the live trading path canonical source)

```python
@dataclass
class SymbolInfo:
    """Unified symbol metadata for live trading and paper simulation."""
    symbol_id: int
    name: str
    pip_size: float = 0.0001
    digits: int = 5
    lot_size: int = 100_000          # contract size per lot (100k forex, 100 crypto)
    min_volume: int = 0              # minimum cTrader volume
    max_volume: int = 0              # maximum cTrader volume (0 = unlimited)
    step_volume: int = 1             # volume step increment
    
    @property
    def contract_size(self) -> float:
        """Alias for lot_size as float (backward compat with models.py consumers)."""
        return float(self.lot_size)
    
    @property
    def pip_value_per_lot(self) -> float:
        """Approximate pip value per standard lot (USD)."""
        return 10.0  # standard forex default; override in SYMBOL_METADATA for non-FX
```

**`models.py` SymbolInfo:** Keep as a separate type for the paper/backtest path (it has `pip_value_per_lot` which is paper-specific). Add a `from_live_symbol_info()` classmethod to bridge:
```python
@classmethod
def from_live_symbol_info(cls, live: 'market_data_feed.SymbolInfo', pip_value_per_lot: float = 10.0):
    return cls(pip_size=live.pip_size, pip_value_per_lot=pip_value_per_lot, 
               lot_size=live.lot_size, contract_size=live.contract_size)
```

**Rationale for not forcing a single class:** The paper trading path (`OrderManager`, `PaperTrader`) doesn't have a `symbol_id` and works with symbol name strings. Forcing unification would cascade changes into the backtest engine — out of scope.

---

## 5. _fetch_symbol_details Update

**Current** (lines 675-692):
```python
sym = payload.symbol[0]
self._symbols[symbol_id] = SymbolInfo(
    symbol_id=symbol_id,
    name=self._id_to_name.get(symbol_id, str(symbol_id)),
    pip_size=10 ** (-sym.digits),
    digits=sym.digits,
)
self._symbol_digits[symbol_id] = sym.digits
```

**After:**
```python
sym = payload.symbol[0]
self._symbols[symbol_id] = SymbolInfo(
    symbol_id=symbol_id,
    name=self._id_to_name.get(symbol_id, str(symbol_id)),
    pip_size=10 ** (-sym.digits),
    digits=sym.digits,
    lot_size=sym.lotSize if sym.lotSize else 100_000,
    min_volume=sym.minVolume if sym.minVolume else 0,
    max_volume=sym.maxVolume if sym.maxVolume else 0,
    step_volume=sym.stepVolume if sym.stepVolume else 1,
)
self._symbol_digits[symbol_id] = sym.digits
```

**Verified protobuf fields available on `ProtoOASymbol`:**
`symbolId`, `digits`, `pipPosition`, `lotSize`, `minVolume`, `maxVolume`, `stepVolume`, `maxExposure` (all int64/int32).

---

## 6. Price Decoding Fix

### Tick Price (line 555)
Currently: `bid, ask = raw_bid / 100_000, raw_ask / 100_000`

The `_symbol_digits` dict is already populated per symbol. Replace with:
```python
digits = self._symbol_digits.get(symbol_id, 5)
divisor = 10 ** digits
bid, ask = raw_bid / divisor, raw_ask / divisor
```

### Trendbar Price (line 759)
Currently: `d = 100000.0`

The `_fetch_historical_bars` method receives `symbol_id` as a parameter. Replace with:
```python
d = float(10 ** self._symbol_digits.get(symbol_id, 5))
```

**Verify:** Read line ~730 to confirm `symbol_id` is in scope at line 759.

---

## 7. All Hardcoded 100,000 Replacement Sites

### Primary (open_api_spot_feed.py) — Task 3

| Line | Context | Current | Replacement |
|------|---------|---------|-------------|
| 127 | `_lots_to_units()` | `lots * 100_000` | `VolumeCalculator.lots_to_volume(symbol_id, lots)` |
| 555 | Tick decode | `/ 100_000` | `/ (10 ** digits)` |
| 759 | Trendbar decode | `100000.0` | `float(10 ** digits)` |
| 791 | Order (rejected) volume decode | `volume / 100_000.0` | `self._volume_calc.volume_to_lots(symbol_id, volume)` |
| 803 | Order (pending) volume decode | `volume / 100_000.0` | `self._volume_calc.volume_to_lots(symbol_id, volume)` |
| 918 | Reconcile volume decode | `getattr(td, "volume", 0) / 100_000.0` | `self._volume_calc.volume_to_lots(symbol_id, int(getattr(td, "volume", 0)))` |
| 986 | Execution event volume decode | `ev / 100_000.0` | `self._volume_calc.volume_to_lots(symbol_id, ev)` |

### Downstream — Task 4

| File | Line(s) | Context | Replacement |
|------|---------|---------|-------------|
| `forward_test_engine.py` | 1100 | `volume_lots * 100_000` | `self._market_feed.lots_to_volume(symbol_id, volume_lots)` |
| `order_manager.py` | 457, 462 | P&L long/short `* 100000` | `* contract_size` (parameterized) |
| `order_manager.py` | 538, 540 | Close P&L `* 100000` | `* contract_size` (parameterized) |
| `position_monitor.py` | 135 | `p.volume * 100000` (total notional) | `p.volume * self._contract_size_for(p.symbol)` |
| `position_monitor.py` | 144 | `p.volume * 100000` (largest position) | Same |
| `position_monitor.py` | 250 | `position.volume * 100000` (drawdown notional) | Same |
| `account_state.py` | 761 | `Decimal(_CONTRACT_SIZE)` | Per-symbol lot_size lookup |

### Out of Scope (paper/backtest path)

| File | Lines | Reason |
|------|-------|--------|
| `hybrid/paper_trader.py` | 108, 109, 117, 293, 295, 338, 340, 398, 400 | Paper trading simulation — separate path from live order pipeline. Starting balance defaults are fine. P&L uses its own lot_size field. |
| `quant/portfolio.py` | 339, 357, 364 | Portfolio analytics — backtest-only module. |

---

## 8. Unit Test Design

**Location:** `tests/unit/ctrader/`

**No cTrader connection required.** All tests use constructed `SymbolInfo` objects.

### test_volume_calculator.py

```python
import pytest
from src.forex_bot.adapters.ctrader.market_data_feed import SymbolInfo
from src.forex_bot.adapters.ctrader.volume_calculator import VolumeCalculator

@pytest.fixture
def symbols():
    return {
        1: SymbolInfo(symbol_id=1, name="EURUSD", pip_size=0.0001, digits=5, 
                      lot_size=100_000, min_volume=1000, max_volume=10000000, step_volume=1000),
        2: SymbolInfo(symbol_id=2, name="BTCUSD", pip_size=0.01, digits=2, 
                      lot_size=100, min_volume=1, max_volume=100000, step_volume=1),
        3: SymbolInfo(symbol_id=3, name="USDJPY", pip_size=0.01, digits=3, 
                      lot_size=100_000, min_volume=1000, max_volume=10000000, step_volume=1000),
    }

@pytest.fixture
def calc(symbols):
    return VolumeCalculator(symbols)
```

**Test list:**

| # | Test | Asserts |
|---|------|---------|
| 1 | `test_forex_lots_to_volume` | calc.lots_to_volume(1, 1.0) == 100000 |
| 2 | `test_forex_volume_to_lots` | calc.volume_to_lots(1, 100000) == 1.0 |
| 3 | `test_crypto_lots_to_volume` | calc.lots_to_volume(2, 1.0) == 100 |
| 4 | `test_crypto_volume_to_lots` | calc.volume_to_lots(2, 100) == 1.0 |
| 5 | `test_round_trip_forex` | For lots in [0.01, 0.1, 0.5, 1.0, 2.5]: volume_to_lots(1, lots_to_volume(1, lots)) ≈ lots |
| 6 | `test_round_trip_crypto` | For lots in [0.01, 1.0, 10.0]: volume_to_lots(2, lots_to_volume(2, lots)) ≈ lots |
| 7 | `test_validate_below_min` | calc.validate_volume(1, 500) → (False, contains "min") |
| 8 | `test_validate_above_max` | calc.validate_volume(1, 99999999) → (False, contains "max") |
| 9 | `test_validate_bad_step` | calc.validate_volume(1, 1500) → (False, contains "step") with step=1000 |
| 10 | `test_validate_ok` | calc.validate_volume(1, 5000) → (True, "OK") |
| 11 | `test_price_forex_5digit` | calc.price_from_raw(1, 109450) == 1.09450 |
| 12 | `test_price_crypto_2digit` | calc.price_from_raw(2, 674525) == 6745.25 |
| 13 | `test_price_jpy_3digit` | calc.price_from_raw(3, 156780) == 156.780 |
| 14 | `test_unknown_symbol_lots` | calc.lots_to_volume(999, 1.0) raises ValueError |
| 15 | `test_unknown_symbol_price` | calc.price_from_raw(999, 100000) ≈ 1.0 (legacy fallback) |
| 16 | `test_default_lot_size` | SymbolInfo(symbol_id=0, name="X").lot_size == 100_000 |
| 17 | `test_crypto_volume_not_forex` | calc.lots_to_volume(2, 0.01) == 1 (not 1000) |

### test_symbol_info.py

| # | Test | Asserts |
|---|------|---------|
| 1 | `test_defaults_forex` | SymbolInfo() has lot_size=100000, digits=5 |
| 2 | `test_crypto_config` | BTCUSD SymbolInfo with lot_size=100 |
| 3 | `test_contract_size_property` | sym.contract_size == float(sym.lot_size) |
| 4 | `test_volume_constraints` | min/max/step are settable independently |

---

## 9. Integration Test Design

### Approach: Exercise the real pipeline through `_execute_signal_live`

The test does NOT use a standalone script. It constructs a `ForwardTestEngine` in live mode, connects to the demo account, and triggers a real order through the same code path the engine uses during normal operation.

```python
@pytest.mark.live
class TestOrderPipelineIntegration:
    
    @pytest.fixture(scope="class")
    def engine(self):
        """Forward test engine connected to demo."""
        config = ForwardTestConfig(
            symbol="EURUSD",
            starting_balance=100_000.0,
            live_mode=True,
        )
        engine = ForwardTestEngine(config, strategies=[])
        assert engine.start(timeout=30), "Engine failed to start"
        yield engine
        engine.stop()
    
    def test_eurusd_small_order(self, engine):
        """Place 0.01 lot EURUSD buy through the real pipeline."""
        signal = TradeSignal(
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            entry_price=1.0850,  # approx
            stop_loss=1.0800,
            take_profit_1=1.0900,
            take_profit_2=1.0950,
            take_profit_3=1.1000,
            volume=0.01,
            confidence=0.8,
            rationale="integration_test_eurusd",
        )
        
        outcome = engine._execute_signal_live(signal, strategy_id="integration_test")
        
        assert outcome is not None, "Pre-flight failure — check logs"
        assert outcome.status == LiveExecutionStatus.FILLED, f"Order not filled: {outcome}"
        assert outcome.order.volume > 0
        
        # Verify position exists
        positions = engine._market_feed.reconcile()
        test_positions = [p for p in positions if "integration_test" in (p.comment or "")]
        assert len(test_positions) >= 1, "No position found after order"
        
        # Cleanup: close the position
        pos = test_positions[0]
        closed = engine._market_feed.close_position(pos.position_id, raw_volume)
        assert closed, "Failed to close test position"
    
    def test_btcusd_volume_correct(self, engine):
        """Verify BTCUSD uses lot_size=100, not 100000."""
        # This test validates the fix: if lot_size were still hardcoded,
        # the volume sent to cTrader would be 10000x too large and rejected.
        signal = TradeSignal(
            symbol="BTCUSD",
            direction=TradeDirection.LONG,
            entry_price=67000,
            stop_loss=66500,
            take_profit_1=68000,
            take_profit_2=69000,
            take_profit_3=70000,
            volume=0.01,
            confidence=0.8,
            rationale="integration_test_btcusd",
        )
        
        outcome = engine._execute_signal_live(signal, strategy_id="integration_test")
        
        assert outcome is not None
        # If volume calc is wrong, cTrader rejects with INVALID_VOLUME
        assert outcome.status == LiveExecutionStatus.FILLED, \
            f"BTCUSD order failed — volume calc may still be hardcoded: {outcome}"
```

### Verification checklist
- [ ] Order lands on demo account (check via reconcile)
- [ ] Volume is correct for forex (0.01 lots → 1000 raw units)
- [ ] Volume is correct for crypto (0.01 lots → 1 raw unit for BTCUSD)
- [ ] Position closes cleanly
- [ ] No kill switch / token refresh interruption

### Cleanup
Always close test positions in the test. Add a `conftest.py` fixture that reconciles and force-closes any "integration_test" positions on teardown.

---

## 10. Acceptance Criteria

All binary and testable:

| # | Criterion | Verification |
|---|-----------|-------------|
| AC1 | `volume_calculator.py` exists and passes `py_compile` | `python3 -m py_compile src/forex_bot/adapters/ctrader/volume_calculator.py` |
| AC2 | `VolumeCalculator.lots_to_volume` works for forex (100k) and crypto (100) | Unit test #1, #3 |
| AC3 | `VolumeCalculator.volume_to_lots` inverse works | Unit test #5, #6 |
| AC4 | `VolumeCalculator.validate_volume` enforces min/max/step | Unit test #7-#10 |
| AC5 | `VolumeCalculator.price_from_raw` uses per-symbol digits | Unit test #11-#13 |
| AC6 | `_fetch_symbol_details` captures lotSize, minVolume, maxVolume, stepVolume | Code review — SymbolInfo construction includes all protobuf fields |
| AC7 | No hardcoded `100_000` or `100000` remains in live order path | `grep -rn "100_000\|100000" src/forex_bot/adapters/ctrader/open_api_spot_feed.py src/forex_bot/adapters/ctrader/forward_test_engine.py src/forex_bot/adapters/ctrader/order_manager.py src/forex_bot/adapters/ctrader/position_monitor.py src/forex_bot/adapters/ctrader/account_state.py` returns only comments/docstrings |
| AC8 | Tick price decoding uses per-symbol digits | Code review — line 555 uses `10 ** digits` |
| AC9 | Trendbar price decoding uses per-symbol digits | Code review — line 759 uses `10 ** digits` |
| AC10 | All unit tests pass | `pytest tests/unit/ctrader/ -v` |
| AC11 | All modified files pass `py_compile` | `python3 -m py_compile <each file>` |
| AC12 | SymbolInfo dataclass has lot_size, min_volume, max_volume, step_volume | Code review + unit test #16 |

---

## 11. Risks

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| R1 | **Dict reference mutation** — VolumeCalculator holds a reference to `_symbols`. If the dict is replaced (not mutated), the calculator loses its lookup. | Medium | Ensure `_symbols` is mutated in-place (current pattern: `self._symbols[symbol_id] = ...`). Add a defensive note in the docstring. |
| R2 | **Protobuf field availability** — `lotSize` is optional in the protobuf. Some symbols might return 0. | Low | Default to `100_000` when `sym.lotSize` is falsy (0 or None). Log a warning. |
| R3 | **Price decode regression** — Changing the tick divisor could break if `_symbol_digits` is stale (symbol not fetched yet). | Medium | Keep the `default=5` fallback in `.get(symbol_id, 5)`. Add log warning when fallback is used. |
| R4 | **Paper trader divergence** — Paper trader still uses `100_000` hardcodes. Live and paper P&L will diverge for crypto. | Low | Out of scope for this sprint. Document as known debt. Paper trader has its own `lot_size` field on positions. |
| R5 | **Integration test flakiness** — Live cTrader demo can be slow or reject orders during maintenance windows. | Medium | Mark `@pytest.mark.live`, skip without creds, use generous timeouts, always clean up positions. |
| R6 | **_populate_static_symbols stale** — The hardcoded static symbols (EURUSD, GBPUSD, USDJPY) won't have lot_size/min/max/step until `_fetch_symbol_details` is called. | Low | Update `_populate_static_symbols` to include sensible defaults for lot_size (100k for FX). |
| R7 | **OrderManager contract_size parameter** — Adding a parameter changes the method signature. Existing callers must be updated. | Low | Use `contract_size: float = 100_000.0` default so existing callers work unchanged. |
| R8 | **close_position raw volume** — `close_position(position_id, volume)` currently expects raw cTrader volume. If we change the semantics, existing callers break. | Medium | Keep close_position expecting raw volume (as cTrader API requires). Document clearly. |

---

## 12. Pre-Council Checklist

No `docs/plans/planner-pre-council-checklist.md` exists in the repo. Self-checking against standard plan quality criteria:

- [x] **Scope is bounded** — 7 files modified, 3 created. Clear out-of-scope list.
- [x] **Task breakdown is ordered** — Tasks 1→2 are independent, Task 3 depends on 1+2, Task 4 depends on 2, Tasks 5+6 depend on all prior.
- [x] **File overlap identified** — Task 3 and Task 4 do NOT share files. Within tasks, no builder touches another builder's files.
- [x] **File overlap forced sequential** — Tasks 1+2 → Task 3 → Task 4 (sequential dependency chain via VolumeCalculator API).
- [x] **SP estimates per task** — T1: 0.5, T2: 0.5, T3: 1.0, T4: 0.5, T5: 0.5, T6: 0.5. Total: 3.5 SP. Within 3-4 estimate.
- [x] **Max 4 files per builder** — T3 has 1 file, T4 has 4 files. All within limit.
- [x] **Acceptance criteria are binary** — 12 criteria, each checkable with a command or test.
- [x] **Risks enumerated** — 8 risks with severity and mitigation.
- [x] **Source files read directly** — All line numbers verified against current code as of 2026-06-27 commit.
- [x] **Protobuf fields verified** — `ProtoOASymbol` descriptor confirms `lotSize`, `minVolume`, `maxVolume`, `stepVolume`, `digits` are available.
- [x] **No implementation in plan** — Plan only. No code changes made.
- [x] **Backward compatibility** — Default `lot_size=100_000` preserves forex behavior. Paper trader path untouched.

---

## Execution Order Summary

```
Task 1 (SymbolInfo)  ──┐
                        ├──► Task 3 (open_api_spot_feed.py) ──► Task 5 (Unit Tests)
Task 2 (VolumeCalc)  ──┤                                                       
                        └──► Task 4 (downstream modules) ──────────► Task 6 (Integration)
```

**Parallelizable:** Tasks 1+2 can run in parallel. Tasks 3+4 can overlap (different files) once 1+2 land. Tasks 5+6 can overlap once their dependencies land.

**Critical path:** T1 → T2 → T3 → T5 → T6 = 3.0 SP on the critical path.

---

*Plan authored by Ava Daigo. 2026-06-27.*
