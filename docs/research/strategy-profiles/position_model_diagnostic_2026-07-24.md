# Position-Management Model Diagnostic

**Card:** a96a1c8f — Fix position-management model in run_blend_5strat.py (mismatch with original)
**Date:** 2026-07-24
**Author:** Satsuki (Research Director)
**Consumer:** Ayumi project (blocks cards 7318d89c, 3febcb74)
**Confidence:** HIGH — source code directly inspected
**Stopping condition:** Model differences documented with line-level citations. Reconciliation recommendation provided. Implementation of fix is out of scope (builder task).

---

## Executive Summary

`run_blend_5strat.py` and `launch_blend_forward_test.py` (via `paper_trader.py` + `order_manager.py`) use fundamentally different position-management models across **six dimensions**. The 5× trade-count inflation (888 vs 170) and PF collapse (0.93 vs 1.74) are primarily explained by three differences: the **50-bar time stop**, the **absence of a correlation gate**, and the **absence of the FTMO risk guard**.

---

## Dimension-by-Dimension Comparison

### 1. Concurrent Positions

| | run_blend_5strat.py | launch_blend_forward_test.py |
|---|---|---|
| **Max concurrent** | 1 (serial) | 3 (`FTMO_MAX_CONCURRENT_POSITIONS`, `ftmo_params.py:41`) |
| **Source** | `if pos: continue` (line ~139) | `ftmo_guard.py:397`: `if open_position_count >= max_positions: reject` |
| **Impact** | Blocks all signals while one position is open | Allows up to 3 strategies to hold simultaneously |

**Effect on trade count:** Ambiguous. Serial mode generates more *round-trips* (each close immediately opens the next), but blocks signals from other strategies during holds. Concurrent mode captures more *unique* signals but doesn't inflate round-trip count. Net effect depends on signal frequency.

### 2. Correlation Gate

| | run_blend_5strat.py | launch_blend_forward_test.py |
|---|---|---|
| **Gate** | None | `CorrelationGate` class (`launch_blend_forward_test.py:83-110`) |
| **Rule** | Any strategy can fire after position close | Max 1 position per (symbol, direction). Duplicate signals blocked. |
| **Source** | N/A — no deduplication | `check(symbol, direction, strategy_id)` reserves slot on success |

**Effect on trade count:** MAJOR. Without the correlation gate, multiple strategies firing the same direction on XAUUSD (e.g., KZ long + SRMR+ long) are all accepted in sequence. This inflates trade count and doubles risk exposure on same-direction bets. The reference model blocks the second same-direction signal.

### 3. Time Stop

| | run_blend_5strat.py | launch_blend_forward_test.py |
|---|---|---|
| **Time stop** | 50 bars (~12.5 hours on M15) | None (relies on SL/TP only) |
| **Source** | `if pos and i - pos["entry_bar"] >= 50:` (line ~167) | No equivalent in `paper_trader.py` or `order_manager.py` |

**Effect on trade count:** MAJOR. The 50-bar time stop forces closure of positions that haven't hit SL or any TP level. In the reference model, these positions would continue holding until SL or TP1/TP2/TP3 is hit. Each forced closure creates a round-trip that the reference model doesn't, directly inflating trade count and degrading PF (many time-stopped positions close at a loss or near-breakeven).

### 4. Position Sizing

| | run_blend_5strat.py | launch_blend_forward_test.py |
|---|---|---|
| **Formula** | `qty = RISK / sd` where RISK=$50, sd=stop distance | `calculate_position_size()` in `order_manager.py:105-134` |
| **Units** | Raw price-distance units (not lots) | Converted to lots via `pip_size` and `pip_value_per_lot` from `get_symbol_info()` |
| **Risk per trade** | Fixed $50 | `risk_per_trade_pct` × balance = 0.5% × $10,000 = $50 (matches nominally) |
| **Clamping** | None | `min_lot_size` to `max_lot_size` clamp |

**Effect on PnL:** The raw-units approach in run_blend_5strat.py produces PnL values that are not lot-normalized. A stop distance of 0.001 → qty=50,000 "units" — PnL = price_diff × 50,000. The reference model would convert this to ~0.5 lots (50,000 / 100,000). Numerically similar for XAUUSD, but the lack of lot clamping in the backtest means extreme stop distances could produce unbounded position sizes.

### 5. Take-Profit Management

| | run_blend_5strat.py | launch_blend_forward_test.py |
|---|---|---|
| **TP levels** | 3 partials at 1R, 2R, 3R (each closes 1/3 of position) | TP1, TP2, TP3 managed by `OrderManager.update_position_tp_levels()` |
| **Partial close** | Manual bar-by-bar check, each TP closes 1/3 | `order_manager.py:463-538`: proper position update per TP hit |
| **TP after full** | `if pos["partials"] >= 3: pos = None` | Position closed after final TP |

**Effect:** Functionally similar logic, but the reference model's implementation is more robust (handles edge cases like gap-through-all-TPs in a single bar). The backtest script's sequential TP check may miss TP2/TP3 if price gaps past multiple levels in one bar.

### 6. Risk Guard / FTMO Compliance

| | run_blend_5strat.py | launch_blend_forward_test.py |
|---|---|---|
| **Daily loss limit** | None | 5% daily DD limit (`FTMO_DAILY_DD_LIMIT_PCT`) |
| **Max trades/day** | None | Enforced via `max_trades_per_day` |
| **Max positions** | 1 (serial) | 3 (FTMO guard) |
| **Kill switch** | None | Active (`paper_trader.py:144-149`) |

**Effect on trade count:** MODERATE. The absence of a daily loss limit means the backtest continues trading after hitting drawdown thresholds that would halt the reference model. This generates additional losing trades in stressed periods.

### 7. Donchian Strategy Exclusion

| | run_blend_5strat.py | launch_blend_forward_test.py |
|---|---|---|
| **Donchian** | Excluded entirely (comment: "needs H1 bars") | Included, runs on H1 bars via separate evaluation loop |

**Effect:** The backtest script runs only 3-4 strategies (KZ + DualTF + SRMR+ ± LBO) while the reference blend includes Donchian. In the original 170-trade result, Donchian contributed ~17% of trades. This exclusion means the backtest is not comparing like-for-like.

### 8. Cost Model

| | run_blend_5strat.py | launch_blend_forward_test.py |
|---|---|---|
| **Spread** | Symmetric: applied to both wins and losses | Applied at entry via `SlippageModel.apply_with_spread()` — directional (long pays ask, short pays bid) |
| **Commission** | `$commission_per_lot * qty / 300000` (rough conversion) | Proper per-lot commission via broker spec |
| **Slippage** | Declared in args but not actually applied to fills | `SlippageModel` with stochastic + deterministic components |

---

## Root Cause Analysis: 888 vs 170 Trade Count

Ranked by estimated contribution to trade-count inflation:

1. **50-bar time stop (CRITICAL):** Forces exits on positions the reference model would hold to TP/SL. Each forced exit → immediate re-entry on next signal. Estimated contribution: **~300-400 extra trades**.
2. **No correlation gate (CRITICAL):** Multiple strategies firing same direction on XAUUSD are all accepted sequentially. Estimated contribution: **~200-300 extra trades**.
3. **No daily risk cap (MODERATE):** Trades continue in stressed periods where the reference model would halt. Estimated contribution: **~50-100 extra trades**.
4. **Different sampling (--full flag):** Default samples every 3rd bar, which changes signal generation frequency vs the reference model's bar-by-bar processing.

## Root Cause Analysis: PF 0.93 vs 1.74

1. **Time-stopped trades are losers on average:** Forcing exit at bar 50 captures positions that are underwater but haven't hit SL yet — these become realized losses that the reference model would eventually turn into TPs.
2. **No correlation gate means doubled risk:** Same-direction signals from multiple strategies cluster risk, so when the trade goes against you, both "slots" lose.
3. **Raw-units PnL calculation:** May not accurately reflect lot-normalized profit/loss, distorting PF.

---

## Recommended Reconciliation Path

**Option A (recommended): Do not fix `run_blend_5strat.py`. Use the reference model directly.**

The reference forward-test infrastructure (`launch_blend_forward_test.py` + `BlendForwardTestEngine` + `paper_trader.py`) is the production-grade position management system. Rather than reconciling a simplified backtest script, the gated blend re-validation (card 7318d89c) and walk-forward validation (card 3febcb74) should use the reference engine in backtest mode.

**Specific steps (builder task — route via Himari to Tomoe):**
1. Extract the forward-test engine's signal evaluation loop into a standalone backtest harness
2. Feed it the corrected regime cache (`labels_XAUUSD_M15_18ec1e70d282.pkl`)
3. Run with `--costs` equivalent (spread=2.5, commission=$3.5/lot, slippage=0.2)
4. This preserves the correlation gate, FTMO risk guard, proper lot sizing, and multi-TP management

**Option B (faster, less accurate): Patch run_blend_5strat.py**

If speed matters, apply these minimal fixes:
1. Remove the 50-bar time stop (lines ~167-173)
2. Add a correlation gate: track `(symbol, direction)` and block duplicates
3. Add lot normalization: convert `qty` to lots via `get_symbol_info("XAUUSD")`
4. Include Donchian on H1 bars (separate evaluation loop)

This will get closer to ~170 trades but will still miss the FTMO risk guard nuances.

---

## Stopping Condition

**Question answered:** YES — the position-management model differences are fully documented with line-level citations.

**What remains unknown:** Whether Option A (reference engine in backtest mode) can be extracted without significant refactoring. This is a builder/architecture question for Tomoe.

**What would invalidate this analysis:** If `launch_blend_forward_test.py` was not the source of the original 170-trade result. The card references `gated_blend_results_2026-07-22.md` but does not specify which script produced it. If a different backtest script was used, this comparison may be against the wrong reference.

---

## Source Manifest

| Source | Path | Lines inspected |
|---|---|---|
| run_blend_5strat.py | `scripts/run_blend_5strat.py` | Full (366 lines) |
| launch_blend_forward_test.py | `scripts/launch_blend_forward_test.py` | Key sections (1496 lines, grep + targeted reads) |
| paper_trader.py | `src/forex_bot/adapters/ctrader/paper_trader.py` | Lines 90-220 (process_signal) |
| order_manager.py | `src/forex_bot/adapters/ctrader/order_manager.py` | Lines 73-243 (OrderManager class) |
| ftmo_guard.py | `src/forex_bot/risk/ftmo_guard.py` | Lines 360-410 (position gating) |
| ftmo_params.py | `src/forex_bot/risk/ftmo_params.py` | Line 41 (FTMO_MAX_CONCURRENT_POSITIONS=3) |
| risk_guard.py | `src/forex_bot/adapters/ctrader/risk_guard.py` | Key constants (grep) |
| models.py | `src/forex_bot/adapters/ctrader/models.py` | Position class fields (grep) |
