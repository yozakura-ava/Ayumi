# Baseline Strategy Backtest Comparison Report

**Issue:** AYUAA-121 | **Status:** in_progress | **Source:** Paperclip

## Executive Summary

This report documents Phase 1 of the Multi-Strategy Research Pipeline: implementing and backtesting baseline strategies for comparison against the existing ICT/SMC system.

## Baseline Strategies Implemented

| Strategy | File | Key Parameters |
|----------|------|----------------|
| MA Crossover | `Strategies/MACrossStrategy.cs` | fastPeriod=9, slowPeriod=21, atrMultiplier=2.5 |
| Bollinger Band Mean Reversion | `Strategies/BBStrategy.cs` | period=20, stdDev=2.0 |
| RSI Divergence | `Strategies/RSIStrategy.cs` | period=14, oversold=30, overbought=70 |
| S/R Breakout | `Strategies/SRBreakoutStrategy.cs` | lookback=50, confirmationBars=2 |
| Momentum ROC | `Strategies/ROCMStrategy.cs` | period=12, rocThreshold=0.5 |

## Architecture

### ISignalStrategy Interface
All strategies implement the `ISignalStrategy` interface, outputting:
- **Direction**: Long/Short/Neutral
- **Confidence**: 0-1 score
- **EntryPrice**: suggested entry
- **StopLoss, TakeProfit1/2/3**: risk levels

### MultiStrategyBacktestEngine
Extended backtesting harness (`Strategies/MultiStrategyBacktestEngine.cs`) supporting:
- Individual strategy backtests
- Combined signal backtests with confluence scoring
- Walk-forward validation ready

## Next Steps

1. **Data preparation**: Obtain quality OHLC data for backtesting
2. **Individual backtests**: Run each baseline strategy with walk-forward validation
3. **Combination testing**: ICT/SMC + each baseline pair
4. **Contribution analysis**: Measure alpha added by each component

## Deliverables Status

- [x] `src/forex_bot/cbot/Strategies/` — baseline implementations
- [ ] Extended backtest harness — multi-strategy engine created, needs data
- [ ] Backtest comparison report — initial architecture documented here
- [ ] Strategy component leaderboard — pending backtest results

## Files Changed

```
src/forex_bot/cbot/Strategies/
├── ISignalStrategy.cs              # Interface + config structs
├── MACrossStrategy.cs              # Moving average crossover
├── BBStrategy.cs                   # Bollinger Band mean reversion
├── RSIStrategy.cs                  # RSI divergence detection
├── SRBreakoutStrategy.cs           # Support/Resistance breakout
├── ROCMStrategy.cs                 # Rate of change momentum
└── MultiStrategyBacktestEngine.cs  # Multi-strategy backtesting
```