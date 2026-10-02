# Strategy Factory — Scoping Document

**Status:** v1 vision (not implementation plan)  
**Author:** Ava  
**Date:** 2026-08-01  
**Trigger:** Craig direction — "we need to build a much stronger strategy factory"  

---

## The Problem

Current approach: manually write strategies, manually backtest, manually validate. This produces ~1 validated strategy per 2-3 weeks of effort, with high variance in quality. The current pool of 6 strategies has minimal DSR evidence, and most haven't been walk-forward tested.

This doesn't scale. We need 10-20 validated strategies for a robust blend, and we need them fast.

---

## What Is the Strategy Factory?

A systematic pipeline that:
1. **Generates** strategy candidates from parameterized templates and market regime signals
2. **Backtests** each candidate across multiple symbols, timeframes, and market regimes
3. **Validates** via walk-forward analysis + DSR
4. **Promotes** only statistically significant survivors to the deployable pool
5. **Monitors** deployed strategies for regime decay and auto-replaces underperformers

Think of it as an assembly line: raw market data in, validated strategies out.

---

## High-Level Architecture (Conceptual)

```
[Market Data] → [Strategy Templates] → [Parameter Sweep] → [Backtest Engine]
                                                              ↓
[Deploy Pool] ← [DSR Gate] ← [Walk-Forward] ← [Regime Filter]
     ↓
[Live Monitor] → [Decay Detector] → [Auto-Replace] → back to generation
```

### Components

1. **Strategy Template Library** — parameterized strategy archetypes (momentum, mean-reversion, breakout, trend-following, session-based). Each template exposes tunable parameters.
2. **Parameter Optimizer** — Optuna-based parameter sweep with multiple-testing awareness. Builds in DSR correction from the start (n_trials tracked).
3. **Backtest Engine** — existing backtest infrastructure, extended to batch-process candidates.
4. **Walk-Forward Runner** — automated WF with configurable window sizes per timeframe.
5. **DSR Gate** — hard gate. No strategy enters the deploy pool without Tier A or B.
6. **Regime Monitor** — classifies current market regime and weights strategy allocation accordingly.
7. **Decay Detector** — monitors deployed strategies for performance degradation, triggers replacement.

---

## What We Already Have

- Backtest engine (`src/forex_bot/backtest/`)
- Walk-forward framework (partial — used for SRMR+ pipeline)
- DSR computation (`src/forex_bot/backtest/dsr.py`)
- DSR integration as post-WF gate (`src/forex_bot/quant/dsr_integration.py`)
- Optuna integration for parameter optimization (`src/forex_bot/ml/blend_optimizer.py`)
- Strategy registry (`src/forex_bot/strategies/registry.py`)
- Blend backtest and portfolio management (`src/forex_bot/backtest/portfolio_blend.py`)

**Gap:** These are disconnected components, not a pipeline. The factory wires them together with automation.

---

## Phased Approach (Not Committed — For Discussion)

### Phase 1: Pipeline Assembly (1-2 weeks)
Wire existing components into an end-to-end pipeline. No new strategies yet — just automate the flow from "parameterized template" to "DSR verdict."

### Phase 2: Template Library Expansion (1-2 weeks)
Convert existing strategies into parameterized templates. Add 2-3 new archetypes (e.g., volatility-breakout, session-momentum, multi-timeframe-trend).

### Phase 3: Sweep & Validate (1 week)
Run the full pipeline: 50-100 parameter combinations across 4-6 symbols and 2-3 timeframes. Target: identify 5-10 Tier A/B strategies.

### Phase 4: Live Deployment (1 week)
Deploy validated strategies as a blend. Wire up decay monitoring.

### Phase 5: Continuous Operation (ongoing)
Factory runs nightly/weekly sweeps. New strategies auto-promote when they pass DSR. Underperformers auto-demote.

---

## Why This Is the Real Path

Manual strategy development is artisanal. It produces 1 strategy per 2-3 weeks with ~30% pass rate. The factory approach produces 50-100 candidates per sweep with automated filtering. Even at a 10% pass rate, that's 5-10 validated strategies per sweep — more than the entire current pool.

The factory is not optional infrastructure. It's the difference between hoping strategies work and systematically finding the ones that do.

---

## Next Step

This document captures vision. Once DSR results land for the current pool (Sprint A), we'll know how urgent the factory is. If most strategies fail DSR, factory Phase 1 becomes the #1 priority immediately.
