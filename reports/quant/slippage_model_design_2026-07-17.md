# Slippage-Adjusted Fill Model — Design Document

**Date:** 2026-07-17
**Author:** Tsukasa (autonomous build)
**Card:** 1955036d — [Tier3] Slippage-adjusted fill model
**Status:** Complete

---

## 1. Problem Statement

The existing backtest engine uses a fixed `slippage_pips` value (default 0.2 pips) applied uniformly across all trades, sessions, and market conditions. The [tick quality comparison](../../docs/forex/tick-quality-comparison-2026-07.md) found this significantly underestimates real-world trading costs, particularly during volatile periods and outside liquid sessions.

This module provides a configurable, standalone slippage model that can be dropped into the backtest pipeline to replace the fixed-pip assumption with a more realistic cost simulation.

## 2. Design Goals

1. **Standalone** — no dependencies on existing engine internals beyond pip-size convention
2. **Backwards-compatible** — FIXED model with `base_slippage_pips` matches existing behavior exactly
3. **Progressive fidelity** — users can opt into LINEAR or SQUARE_ROOT models for more realism
4. **Composable** — session and volatility adjustments are independent, can be enabled separately
5. **Bounded** — hard cap (`max_slippage_pips`) prevents pathological values from breaking simulations

## 3. Model Architecture

### 3.1 Three Slippage Regimes

| Model | Formula (pips) | Use Case |
|-------|---------------|----------|
| FIXED | `base_slippage_pips` | Baseline, backwards-compatible, quick estimates |
| LINEAR | `base + vol_coeff × (lots / ADV) × 100` | Linear market impact for moderate trade sizes |
| SQUARE_ROOT | `base + vol_coeff × √(lots / ADV × 100)` | Almgren-Chriss institutional standard, sub-linear impact |

### 3.2 Adjustment Layers

Applied multiplicatively (session) or additively (volatility) after the base model:

- **Session liquidity factor:** Maps trading session to a multiplier:
  - London / NY AM: 1.0 (full liquidity)
  - NY PM: 1.15 (slight thinning)
  - Asian: 1.3 (reduced liquidity for non-Asian pairs)
  - Outside: 1.6 (gap risk, minimal depth)

- **Volatility component:** `volatility_coefficient × current_volatility_pips` added to base slippage before session adjustment. Allows ATTR-based scaling.

### 3.3 Pip Size Convention

Mirrors `ExecutionSimulator._get_pip_value()` from `types.py`:
- Price ≥ 50 (JPY pairs, XAUUSD): pip = 0.01
- Price ≥ 1 (standard FX): pip = 0.0001
- Price < 1 (crypto/exotic): pip = 0.00000001

## 4. API Surface

```python
from backtest.slippage_model import (
    SlippageConfig,      # Configuration dataclass
    SlippageContext,     # Per-trade market context
    SlippageModel,       # Enum: FIXED | LINEAR | SQUARE_ROOT
    TradeSide,           # Enum: BUY | SELL
    compute_slippage,    # (config, context) → price units
    compute_slippage_pips,  # (config, context) → pips
    apply_to_trade,      # (config, context) → adjusted fill price
    apply_slippage_to_price,  # utility for simple fixed-pip adjustments
)
```

## 5. Integration Points

### 5.1 Current Engine (BacktestConfig)

The existing `BacktestConfig.slippage_pips = 0.2` field maps directly to `SlippageConfig(base_slippage_pips=0.2, model=SlippageModel.FIXED)`.

**Migration path:** Replace the inline slippage calculation in `ExecutionSimulator._calculate_exit()` with a `SlippageConfig` instance stored on the config object. The FIXED model produces identical results, so no regression in existing backtests.

### 5.2 Walk-Forward Runner

The walk-forward runner (`walk_forward_runner.py`) passes `BacktestConfig` to the engine. Adding an optional `slippage_config: SlippageConfig` field to `BacktestConfig` would allow per-strategy slippage tuning during optimization.

### 5.3 Pair-Specific Configuration

The `get_spread_for_pair()` function in `types.py` already maintains per-pair spread defaults. A similar `get_slippage_config_for_pair()` factory could provide sensible defaults:

```python
PAIR_SLIPPAGE_CONFIGS = {
    "EURUSD": SlippageConfig(base_slippage_pips=0.2, adv_lots=200_000),
    "XAUUSD": SlippageConfig(base_slippage_pips=0.5, adv_lots=50_000),
    "GBPJPY": SlippageConfig(base_slippage_pips=0.8, adv_lots=30_000),
}
```

This is left for a follow-up card — the current module is self-contained.

## 6. Assumptions and Limitations

1. **No tick-level simulation:** This is a bar-level model. It does not simulate order-book dynamics or partial fills.
2. **Symmetric slippage:** Buy and sell experience the same slippage magnitude. Real markets may show asymmetric impact.
3. **Static ADV:** The `adv_lots` parameter is a scalar, not time-varying. In practice, ADV varies by session and day-of-week.
4. **No correlation between slippage and signal quality:** Assumes slippage is independent of the strategy's edge.
5. **Session factors are heuristic:** Derived from the tick-quality comparison doc, not calibrated against actual fill data.

## 7. Testing

32 unit tests covering:
- Config/context validation (positive and negative cases)
- All three model regimes (FIXED, LINEAR, SQUARE_ROOT)
- Session liquidity adjustments (all session types)
- Volatility adjustments
- Directional application (buy/sell asymmetry)
- Edge cases: JPY pairs, XAUUSD, sub-unit prices, max cap
- Utility function parity

All tests pass. `py_compile` and `ruff` clean.

## 8. References

- Almgren, R., Thum, C., Hauptmann, E., Li, H. (2005). "Direct Estimation of Equity Market Impact." *Risk*.
- `docs/forex/tick-quality-comparison-2026-07.md` — Ayumi tick data analysis
- `docs/_archive/forex/architecture-v2.md` — slippage_pips precedent
- `src/forex_bot/backtest/types.py` — `ExecutionSimulator._get_pip_value()`, `BacktestConfig.slippage_pips`
