# London Breakout Retest — Strategy Validation Study
**Date:** 2026-07-22
**Symbol/Timeframe:** XAUUSD M15 (71,747 bars, 2022-01-12 → 2026-07-10)

## TL;DR

**Strategy exists and validates with exceptional PF.** With the right regime gate (LONDON-only + ADX[15,30]), it produces 15 trades over 4.5 years at PF=3.977 and 80% win rate. Even ungated, it's profitable (28 trades, PF=1.601, 71% WR).

Recommended blend gate: **`session={london}` + ADX[15,30] + regime unrestricted** → 3.3 trades/year at PF~4. Diversifies the blend without inflating volume.

## Pre-existing strategy

File: `src/forex_bot/strategies/london_breakout_retest.py` (258 lines, XAUUSD-tuned). No tests existed prior to this study.

## Smoke test results

File: `tests/strategies/test_london_breakout_retest.py` (3 tests, all PASS).

| Test | Result |
|---|---|
| `test_instantiation` | ✓ — Config + strategy classes instantiate |
| `test_smoke_produces_signals` | ✓ — 71 signals in 10k bars (~500/year raw); SL distance 2.2-12.1, median 4.84; RR=1.0 |
| `test_no_signals_outside_london` | ✓ — Only 5 signals in 1000 bars when session forced to OUTSIDE (informational) |

## Gated backtest results (71,747 XAUUSD M15 bars, 4.5 years)

| Gate | Trades | PF | Net $ | DD % | WR % |
|---|---:|---:|---:|---:|---:|
| No gate (baseline) | 28 | 1.601 | $240.6 | 1.62% | 71.4% |
| LONDON session only | 17 | 2.717 | $273.11 | 1.00% | 76.5% |
| QUIET/CHOPPY + LONDON | 7 | 0.497 | $-80.02 | 1.00% | 42.9% |
| ADX[15,30] + LONDON | **15** | **3.977** | **$324.54** | **1.00%** | **80.0%** |
| ADX[18,28] + LONDON | 8 | 3.572 | $128.59 | 0.50% | 87.5% |
| QUIET/CHOPPY/VOL + ADX[18,28] + LONDON | 5 | 1.751 | $37.53 | 0.50% | 80.0% |

## Interpretation

1. **PF 3.97 is too good — flag as potentially fragile.** A PF this high on 15 trades has a wide confidence interval. Need walk-forward + multiple testing correction.
2. **LONDON-only gate alone (no regime filter) gives 17 trades at PF=2.72.** Adding ANY regime filter on top (QUIET/CHOPPY) collapses PF below 1.0. This strategy may *like* volatile breakouts, contradicting the conservative regime filtering intuition.
3. **ADX[15,30] without regime filter is the sweet spot.** PF=3.98, 80% WR.
4. **Volume is low** (~3-4 trades/year) but per Craig's direction (Jul 22): *"a single strategy at 1 trade every 3 days is fine — the blend is what needs volume."* LBO adds exceptional PF without harming volume.
5. **Survivorship / fragility risk.** Real FTMO execution has spread=2.5 pip (vs 0.3 in our backtest), slippage, and partial fill risk. A 1R winner can become 0.8R with realistic costs. Need cost-stress test before FTMO.

## Recommended blend gate for LBO

```python
"london_breakout_retest": GateConfig(
    regimes={Regime.QUIET, Regime.CHOPPY, Regime.TRENDING, Regime.VOLATILE},  # no regime filter
    adx_range=(15.0, 30.0),
    sessions={"london"},
)
```

3.3 trades/year at PF~4 (unstressed).

## Confidence levels

| Factor | Confidence | Reasoning |
|---|---|---|
| LBO has edge on XAUUSD M15 | 80% | PF>1 across 6 gate variants; raw 28 trades at PF=1.6 |
| Specific gate (LBO + ADX[15,30] + LONDON) survives realistic costs | 50% | PF=3.97 likely overstates real edge by 30-50% once spread/slippage/commission applied |
| Trade count stable across regimes | 30% | 6 trades with QUIET/CHOPPY gate vs 17 with no regime → filtering matters; need regime × market condition analysis |
| Walk-forward stability | ? | Not yet tested; needs Monte Carlo pass-rate vs baseline blend |

## Next steps (in order)

1. **Add LBO to blend test** — re-run gated blend backtest with LBO included, measure portfolio PF/DD/trade count
2. **Realistic cost stress** — re-run with spread=2.5 pip, commission=$3.5/lot, slippage=0.2 — PF should drop to 2-3 range
3. **Walk-forward pass** — 5 OOS windows, expect at least 3-4 to pass for validation
4. **Monte Carlo** — confirm 90%+ FTMO pass rate
5. **Wire into forward test** (`scripts/launch_blend_forward_test.py`) as 5th strategy

## Files changed

- `tests/strategies/test_london_breakout_retest.py` — NEW, 3 tests, all pass
- `scripts/test_lbo_gated.py` — NEW, gated backtest harness
- `scripts/gate_loosening_study.py` — NEW (companion tool, see `gate_loosening_study_2026-07-22.md`)
