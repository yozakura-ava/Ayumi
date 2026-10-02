# TP/SL Chain Trace — Where TP/SL Gets Lost

**Card:** [AYUMI] TP/SL chain trace — find where TP/SL gets lost (read-only)
**Parent:** b6372ce8 — Implement and verify TP/SL placement on live positions
**Scope:** read-only investigation, no code changes
**Workspace:** $AYUMI_ROOT (main branch, uncommitted doc only)

---

## TL;DR — Where TP/SL Gets Lost

| Step | Component | TP1 | TP2 | TP3 | SL | Note |
|------|-----------|:---:|:---:|:---:|:--:|------|
| 1 | `core/types.py:StrategySignal` | ✅ | ✅ | ✅ | ✅ | Dataclass field exists |
| 2 | Strategy (e.g. `session_breakout.py`) | ✅ calculated | ✅ calculated | ✅ calculated | ✅ calculated | 1.5x/2x/3x range width |
| 3 | `adapters/ctrader/signal_adapter.py:cTraderSignalAdapter` | ✅ preserved | ✅ preserved | ✅ preserved | ✅ preserved | All 3 TPs copied into `TradeSignal` |
| 4 | `engine/trading_orchestrator.py:_execute_signal` | ✅ preserved | ✅ preserved | ✅ preserved | ✅ preserved | Passes full `TradeSignal` to paper_trader |
| 5 | `adapters/ctrader/models.py:TradeSignal` | ✅ field | ✅ field | ✅ field | ✅ field | All 3 TP fields exist on the dataclass |
| 6 | `forward_test_engine.py:_execute_signal_live` (MARKET order) | ❌ sent as `None` | ❌ sent as `None` | ❌ sent as `None` | ❌ sent as `None` | Intentional — naked market, then amend (see note 1) |
| 7 | `forward_test_engine.py:_execute_signal_live` (post-fill `amend_sl_tp`) | ✅ amended | ❌ **DROPPED** | ❌ **DROPPED** | ✅ amended | Only `take_profit_1` is passed |
| 8 | `forward_test_engine.py:_register_late_fill_callbacks` | ✅ amended | ❌ **DROPPED** | ❌ **DROPPED** | ✅ amended | Late-fill path — same bug |
| 9 | `paper_trader.py:_execute_order` | ✅ forwarded | ❌ **DROPPED** | ❌ **DROPPED** | ✅ forwarded | Paper trader is single-TP too |
| 10 | `position_monitor.py` | n/a | n/a | n/a | n/a | No ratcheting / TP2-TP3 management exists |

**TP/SL is correctly produced, preserved, and passed through every layer EXCEPT the broker-boundary calls (`amend_sl_tp`, paper_trader, position monitor).** SL is fine. TP1 is fine. **TP2 and TP3 are calculated by every strategy but never reach the broker and are never acted on by the position monitor.**

---

## Note 1: Why MARKET orders are sent naked (intentional, not a bug)

`forward_test_engine.py:1271-1272` comment:
> cTrader rejects absolute SL/TP on MARKET orders with INVALID_REQUEST.
> Send the market order naked, then attach SL/TP via position amend after fill.

This is correct cTrader behavior — the proto `ProtoOANewOrderReq` accepts SL/TP, but in practice the broker rejects them on market orders. The two-phase pattern (naked market → amend) is the standard workaround and is **not** the bug. The bug is in step 2 of the amend path: only TP1 is passed.

---

## Detailed Chain with File:Line Citations

### Step 1 — `StrategySignal` dataclass (TP/SL fields exist)

**File:** `src/forex_bot/core/types.py:89-99`
```python
@dataclass
class StrategySignal:
    direction: TradeDirection
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    rationale: str
    is_volatile: bool = False
```
✅ All three TP fields present on the core strategy signal.

### Step 2 — Strategy actually computes TP1/TP2/TP3

**File:** `src/forex_bot/strategies/session_breakout.py:240-255` (representative — same pattern in `srmr_plus.py`, `momentum.py`, `rsi_threshold.py`, `bb_rsi_reversion.py`, `volatility_squeeze.py`)
```python
if direction == TradeDirection.LONG:
    stop_loss = entry - sl_distance
    take_profit_1 = entry + 1.5 * range_width
    take_profit_2 = entry + 2.0 * range_width
    take_profit_3 = entry + 3.0 * range_width
else:
    stop_loss = entry + sl_distance
    take_profit_1 = entry - 1.5 * range_width
    take_profit_2 = entry - 2.0 * range_width
    take_profit_3 = entry - 3.0 * range_width
```
✅ TP1/TP2/TP3 are computed per strategy and returned in the `StrategySignal`.

### Step 3 — `cTraderSignalAdapter` preserves all three TPs

**File:** `src/forex_bot/adapters/ctrader/signal_adapter.py:64-74`
```python
trade_signal = TradeSignal(
    symbol=self._symbol,
    direction=trade_direction,
    entry_price=signal.entry_price,
    stop_loss=signal.stop_loss,
    take_profit_1=signal.take_profit_1,
    take_profit_2=signal.take_profit_2,
    take_profit_3=signal.take_profit_3,
    volume=0.0,
    confidence=signal.confidence,
    rationale=signal.rationale,
    strategy_id=self._strategy.name,
)
```
✅ Adapter copies all three TPs into the cTrader `TradeSignal`. No loss here.

### Step 4 — `engine/trading_orchestrator.py` also preserves them

**File:** `src/forex_bot/engine/trading_orchestrator.py:684-694`
```python
trade_signal = TradeSignal(
    symbol=symbol,
    direction=c_dir,
    entry_price=signal.entry_price,
    stop_loss=signal.stop_loss,
    take_profit_1=signal.take_profit_1,
    take_profit_2=signal.take_profit_2,
    take_profit_3=signal.take_profit_3,
    volume=0.01,
    confidence=signal.confidence,
    rationale=f"[{strategy_key}] {signal.rationale}",
    source=f"strategy:{strategy_key}",
)
```
✅ Engine orchestrator also preserves all three. No loss here.

### Step 5 — cTrader `TradeSignal` dataclass has all three fields

**File:** `src/forex_bot/adapters/ctrader/models.py:89-101`
```python
@dataclass
class TradeSignal:
    symbol: str
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    volume: float
    confidence: float
    rationale: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    strategy_id: str = ""
```
✅ All three TP fields present on the adapter TradeSignal dataclass.

### Step 6 — Market order is sent naked (intentional)

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py:1271-1276`
```python
# cTrader rejects absolute SL/TP on MARKET orders with INVALID_REQUEST.
# Send the market order naked, then attach SL/TP via position amend after fill.
order = self._market_feed.new_order(
    symbol_id=symbol_id,
    side=side,
    volume=volume_raw,
    order_type=ProtoOAOrderType.MARKET,
    sl=None,
    tp=None,
    comment=signal.rationale,
)
```
✅ Intentional. The naked-market-then-amend pattern is the documented cTrader workaround.

### Step 7 — Post-fill amend drops TP2 and TP3 (THE BUG)

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py:1274-1285`
```python
# If the order filled, attach SL/TP to the resulting position.
if outcome.status == LiveExecutionStatus.FILLED and signal.stop_loss and signal.take_profit_1:
    position_id = getattr(order, "position_id", None) or getattr(order, "order_id", None)
    try:
        amended = self._market_feed.amend_sl_tp(
            position_id, signal.stop_loss, signal.take_profit_1,  # <-- only TP1
            symbol_id=symbol_id,
        )
```

❌ **GAP #1 (primary bug):** Only `signal.take_profit_1` is passed to `amend_sl_tp`. `signal.take_profit_2` and `signal.take_profit_3` are silently dropped at this boundary.

Also note: `signal.take_profit_1` is used in the conditional guard (`signal.stop_loss and signal.take_profit_1`) — if TP1 is None the whole amend is skipped, so a strategy that only sets TP2/TP3 would silently never get protection.

### Step 8 — Late-fill callback also drops TP2/TP3 (SAME BUG)

**File:** `src/forex_bot/adapters/ctrader/forward_test_engine.py:1567-1582`
```python
if (signal.stop_loss is not None
        and signal.take_profit_1 is not None
        and self._market_feed is not None
        and isinstance(ctrader_position_id, int)
        and ctrader_position_id != 0):
    try:
        symbol_id = self._market_feed.resolve_symbol_id(signal.symbol)
        amended = self._market_feed.amend_sl_tp(
            ctrader_position_id, signal.stop_loss, signal.take_profit_1,  # <-- only TP1
            symbol_id=symbol_id,
        )
```

❌ **GAP #2 (duplicate):** The late-fill callback path (line ~1567-1582) has the identical bug. Any fill that arrives after the synchronous wait would get the same wrong SL/TP treatment.

### Step 9 — `paper_trader` is single-TP only (parallel bug)

**File:** `src/forex_bot/adapters/ctrader/paper_trader.py:236-249`
```python
def _execute_order(
    self,
    signal: TradeSignal,
    volume: float,
    spread: float = 0.0,
    bid: float = 0.0,
    ask: float = 0.0,
) -> OrderExecutionResult:
    if self.is_live_mode:
        return self._order_manager.execute_live_order(
            symbol=signal.symbol,
            direction=signal.direction,
            volume=volume,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_1,   # <-- only TP1
            comment=signal.rationale,
        )
    return self._order_manager.execute_paper_order(
        ...
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit_1,   # <-- only TP1
        ...
    )
```
❌ **GAP #3 (paper path):** Even the paper/paper-live path forwards only TP1 to the order manager. Paper sim would only have TP1 as a target.

Confirmed by `grep -n "take_profit" src/forex_bot/adapters/ctrader/paper_trader.py`:
```
125:                take_profit=signal.take_profit_1,
200:                                tp_price=float(signal.take_profit_1),
240:                take_profit=signal.take_profit_1,
249:            take_profit=signal.take_profit_1,
```
Four references — all to TP1, zero to TP2/TP3.

### Step 10 — `position_monitor` does no TP2/TP3 ratcheting

**File:** `src/forex_bot/adapters/ctrader/position_monitor.py:60-127`

The monitor only tracks MAE/MFE, water marks, and time-in-trade. There is no TP-ratcheting logic — once TP1 hits, the position continues to run (against TP2/TP3 if they were on the position) but the bot has no software-side TP2/TP3 management.

### Step 11 — `amend_sl_tp` proto limitation (root architectural constraint)

**File:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py:1018-1068`
```python
def amend_sl_tp(self, position_id, sl, tp, *, symbol_id=None, timeout=_AMEND_TIMEOUT_SEC) -> bool:
    ...
    req = ProtoOAAmendPositionSLTPReq()
    req.ctidTraderAccountId = self._ctid_account_id
    req.positionId = position_id
    if symbol_id:
        sl = self._round_price(symbol_id, sl)
        tp = self._round_price(symbol_id, tp)
    req.stopLoss = sl
    req.takeProfit = tp     # <-- SINGULAR takeProfit
```
The cTrader proto `ProtoOAAmendPositionSLTPReq` only has one `stopLoss` and one `takeProfit` per position. **A position cannot have multiple TPs at the broker.** This is the architectural reason TP2/TP3 cannot be passed through `amend_sl_tp` — the proto doesn't support it.

---

## Suggested Fix Locations for Child B

Three fix locations are available, in increasing order of complexity:

### Option A — Accept single-TP and document it (smallest fix)

1. **`src/forex_bot/strategies/session_breakout.py` (and siblings)** — keep computing TP2/TP3 for stats/logging, but only forward TP1 to the broker.
2. **`src/forex_bot/adapters/ctrader/signal_adapter.py:64-74`** — already correct, no change.
3. **`docs/forex/forex-position-size-risk-calculator-research.md`** — document that the live engine uses TP1 as the broker-side TP, and TP2/TP3 are software-side exit levels requiring in-process management (see Option C).

This is the minimum-change path: the live execution layer already does TP1 correctly. The behavior gap is in strategy docs that imply multi-level TP exits happen at the broker — they don't.

### Option B — Partial-close ladder at TP1 (medium fix)

When TP1 hits:
1. Close 50% of position volume at TP1 (partial close).
2. Move SL to breakeven on the remaining 50%.
3. Set TP2 on the remaining 50% via `amend_sl_tp(position_id, sl=breakeven, tp=signal.take_profit_2)`.

When TP2 hits:
1. Close 50% of remaining (i.e. 25% of original).
2. Move SL to TP1.
3. Set TP3 on remaining via `amend_sl_tp(position_id, sl=tp1, tp=signal.take_profit_3)`.

**Where to wire this:** Add a new method `PositionMonitor.on_price_cross(position, level) -> Optional[Action]` that fires on TP1/TP2/TP3 crossing. New code lives in:
- `src/forex_bot/adapters/ctrader/position_monitor.py` — add `check_tp_levels()` called from the existing `update_positions()` loop.
- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` — already has `close_position(position_id, volume, ...)` that accepts partial volume.

**Risk:** requires price-crossing detection + position-state tracking to know whether TP1 has already fired (no idempotency in current monitor).

### Option C — Full in-process TP ladder manager (largest fix)

Replace broker-side SL/TP entirely with a software TP manager:
- `PositionMonitor` watches tick prices; when TP1/TP2/TP3 cross, it sends `close_position(partial_volume)` calls to cTrader.
- Removes the `amend_sl_tp` post-fill step entirely (or uses it only for SL breakeven moves).
- Requires: tracking per-position "remaining volume" and "which TP levels already fired", plus a kill switch if the bot dies while a TP is uncrossed.

**Where:** New module `src/forex_bot/adapters/ctrader/tp_ladder.py` with a `TpLadderManager` class. Wired into `forward_test_engine._wire_live_fill_callbacks` and `position_monitor.update_positions`.

**Risk:** requires careful reconciliation if bot restarts mid-position — must replay broker state on startup.

### Recommended for Child B: **Option A + Option B lite**

- Keep TP1 as the broker-side TP via the existing `amend_sl_tp` (no change needed — it works).
- Add a `PositionMonitor.check_tp_levels()` that:
  - Closes 50% at TP1, moves SL to breakeven, sets TP2 via `amend_sl_tp`.
  - Does NOT attempt TP3 ladder for now (deferred to a separate card).
- Add idempotency keys to `Position` so the monitor knows whether TP1 already fired.

This gives multi-level TP exits within the cTrader proto constraint without the complexity of a full software-side ladder.

---

## Summary of Findings

| ID | Type | Location | Severity | Fix scope |
|----|------|----------|----------|-----------|
| F1 | Drop | `forward_test_engine.py:1278` `amend_sl_tp(..., signal.take_profit_1)` | high | live path never sets TP2/TP3 |
| F2 | Drop (duplicate) | `forward_test_engine.py:1575` late-fill `amend_sl_tp` | high | same bug on late-fill path |
| F3 | Drop | `paper_trader.py:240,249` `_execute_order` | medium | paper/backtest-sim also single-TP |
| F4 | Architectural | `open_api_spot_feed.py:1018-1068` `amend_sl_tp` proto | constraint | cTrader position model supports only 1 TP |
| F5 | Missing | `position_monitor.py` — no TP-level ratcheting | medium | no software-side TP2/TP3 management |
| F6 | Type confusion | `orchestrator/signal_orchestrator.py:18` `TradeSignal.take_profit` (singular) vs `adapters/ctrader/models.py:89` `take_profit_1/2/3` | low | two `TradeSignal` dataclasses in same codebase — confusing but not blocking |

**SL is correctly set everywhere.** **TP1 is correctly set at the broker.** **TP2/TP3 are silently lost at the broker boundary in three places** (live amend, late-fill amend, paper-trader execute).

---

## Evidence

- `grep -n "take_profit_2\|take_profit_3" src/forex_bot/adapters/ctrader/forward_test_engine.py` → no matches.
- `grep -n "take_profit_2\|take_profit_3" src/forex_bot/adapters/ctrader/paper_trader.py` → no matches.
- `grep -rn "take_profit_2\|take_profit_3" src/forex_bot/adapters/ctrader/` (excluding backtest/) → only `models.py`, `signal_adapter.py`, `portfolio_risk_guard.py`, `protocols.py`, and `forward_test_engine.py:1274/1278` references — the latter two are guards/checks for `tp1 is not None`, never `tp2`/`tp3` usage.

The TP2/TP3 fields exist on every dataclass and are computed by every strategy, but no broker-bound call site references them.
---

## Post-Sprint Status

**Sprint:** TP/SL chain trace resolution (card a7b8e896)
**Status as of sprint close:** all actionable findings resolved; F4 is a documented platform constraint; F5 is addressed by the position_monitor ratcheting (Task 1.5).

### F1 — `forward_test_engine.py:1278` drops TP2/TP3 on live amend

**Status:** ✅ **FIXED**
**Commit(s):**
- `cf070a2` — `fix: F1+F2 — store TP2/TP3 on Position after amend_sl_tp` (Task 1.3)
- `7e98496` — merge to main

TP2/TP3 are now persisted onto the `Position` dataclass after `amend_sl_tp` runs, so the values are available to the in-process TP ladder manager even though the broker only sees TP1.

### F2 — `forward_test_engine.py:1575` late-fill amend drops TP2/TP3

**Status:** ✅ **FIXED**
**Commit(s):**
- `cf070a2` — `fix: F1+F2 — store TP2/TP3 on Position after amend_sl_tp` (Task 1.3, same fix covers both call sites)
- `7e98496` — merge to main

The same Position-storage fix covers the late-fill callback path, so both synchronous and late-fill flows now retain TP2/TP3 for downstream software-side use.

### F3 — `paper_trader.py` single-TP only

**Status:** ✅ **FIXED**
**Commit(s):**
- `e99cd98` — `fix(F3): paper_trader forwards all 3 TPs to OrderManager` (Task 1.4)
- `99567a0` — merge to main

The paper/paper-live path now forwards all three TPs to the order manager, so paper sims can exercise the same TP2/TP3 ladder logic that the live path uses.

### F4 — cTrader `ProtoOAAmendPositionSLTPReq` supports only single SL/TP

**Status:** 📋 **DOCUMENTED PLATFORM CONSTRAINT (not a bug)**
**This document (Task 1.6).**

The proto has exactly one `stopLoss` and one `takeProfit` field per position amend request. cTrader's position model therefore cannot hold multiple broker-side TPs. There is no fix to apply at the broker boundary — instead, the architecture is:

- **TP1** is the active broker-side TP, set via `amend_sl_tp` after the naked market order fills.
- **TP2 / TP3** are software-side exit levels, stored on the `Position` dataclass (Task 1.3, F1/F2 fix) and acted on by the position_monitor TP ratcheting (F5 / Task 1.5).

The inline comment at `open_api_spot_feed.py:1018` (just above the `amend_sl_tp` definition) makes this constraint visible to anyone reading the broker-boundary code, with a back-reference to this section. This prevents future "why are TP2/TP3 not in the amend call?" confusion.

### F5 — `position_monitor` has no TP ratcheting / TP2/TP3 management

**Status:** ✅ **ADDRESSED by Task 1.5 (position_monitor TP ratcheting)**

Task 1.5 introduces a software-side TP ladder on the `position_monitor`: it watches tick prices, fires partial closes + SL-breakeven ratchets when TP levels cross, and uses the `tp_levels_fired` idempotency set (added to `Position` in Task 1.1, commit `fe2134c`) to prevent double-firing. This is the in-process TP2/TP3 manager that the cTrader proto constraint (F4) forced us to build.

See also: F1/F2 commit `cf070a2` — the `Position.tp2 / Position.tp3` fields and `tp_levels_fired` set are the data substrate the ratcheting manager reads.

### F6 — Two `TradeSignal` dataclasses in the codebase

**Status:** 🟡 **DEFERRED (low severity, non-blocking)**
Two `TradeSignal` dataclasses exist (`adapters/ctrader/models.py` with TP1/TP2/TP3 vs `orchestrator/signal_orchestrator.py` with a singular `take_profit`). They do not collide at runtime — orchestrator constructs the cTrader one for broker-bound calls — but the naming inconsistency is a footgun. Tracked as a follow-up cleanup, not a sprint blocker.

---

### Summary — End-State TP/SL Architecture

| Layer | TP1 | TP2 | TP3 | SL |
|-------|:---:|:---:|:---:|:--:|
| Strategy calc (e.g. `session_breakout`) | ✅ 1.5× range | ✅ 2.0× range | ✅ 3.0× range | ✅ entry ± sl_distance |
| `StrategySignal` / cTrader `TradeSignal` dataclass | ✅ field | ✅ field | ✅ field | ✅ field |
| `cTraderSignalAdapter` → `forward_test_engine` | ✅ preserved | ✅ preserved | ✅ preserved | ✅ preserved |
| Naked market order (intentional) | — | — | — | — |
| `amend_sl_tp` (proto) — **broker-bound** | ✅ set | ❌ proto can't | ❌ proto can't | ✅ set |
| `Position` dataclass (post-amend) | ✅ stored | ✅ stored | ✅ stored | ✅ stored |
| `position_monitor` (Task 1.5 ratcheting) | n/a | ✅ partial close + SL→BE | ✅ partial close + SL→TP1 | ✅ trailing |

**Active broker TP:** TP1 (the only TP cTrader holds).
**Software-managed exit levels:** TP2 and TP3, owned by the position_monitor ratcheting on top of the `Position` dataclass.
**Single source of truth for TP exit behavior:** `docs/forex/tp-sl-chain-trace.md` + Task 1.5 ratcheting logic.
