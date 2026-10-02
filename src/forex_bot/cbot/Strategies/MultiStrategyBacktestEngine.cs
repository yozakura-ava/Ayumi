using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class MultiStrategyBacktestEngine
    {
        private readonly BacktestConfig _config;
        private readonly List<ISignalStrategy> _strategies;
        private readonly MultiStrategyConfig _multiConfig;
        private readonly SignalConfluenceEngine _ictsicEngine;

        private List<Bar> _bars;
        private double _balance;
        private double _peakBalance;
        private double _maxDrawdown;
        private double _dailyStartBalance;
        private DateTime _currentDay;
        private double _maxDailyLoss;

        public MultiStrategyBacktestEngine(
            BacktestConfig config,
            List<ISignalStrategy> strategies,
            MultiStrategyConfig multiConfig = null)
        {
            _config = config;
            _strategies = strategies;
            _multiConfig = multiConfig ?? new MultiStrategyConfig();
            _ictsicEngine = new SignalConfluenceEngine(
                minConfidence: config.MinConfidence,
                defaultSLMultiplier: 2.5,
                tp1RR: 1.0,
                tp2RR: 2.0,
                tp3RR: 3.0);
        }

        public Dictionary<string, StrategyBacktestResult> RunAllStrategies(List<Bar> bars)
        {
            var results = new Dictionary<string, StrategyBacktestResult>();

            foreach (var strategy in _strategies)
            {
                var result = RunSingleStrategy(strategy, bars);
                results[strategy.Name] = result;
            }

            return results;
        }

        public StrategyBacktestResult RunSingleStrategy(ISignalStrategy strategy, List<Bar> bars)
        {
            _bars = bars ?? throw new ArgumentNullException(nameof(bars));
            if (_bars.Count < _config.MinBarsBeforeSignal)
                throw new ArgumentException($"Need at least {_config.MinBarsBeforeSignal} bars");

            Reset();
            var trades = new List<SimulatedTrade>();
            var equityCurve = new List<double> { _balance };
            var openTrades = new List<SimulatedTrade>();
            StrategySignal? lastSignal = null;

            for (int i = 0; i < _bars.Count; i++)
            {
                var bar = _bars[i];
                UpdateDailyTracking(bar.Time);

                if (_balance <= 0) break;
                if (IsMaxDrawdownBreached()) break;
                if (IsMaxDailyLossBreached()) continue;

                CheckOpenTrades(openTrades, bar, i, trades, equityCurve);

                if (openTrades.Count < _config.MaxOpenTrades && i >= _config.MinBarsBeforeSignal)
                {
                    var state = new MarketState
                    {
                        Bars = _bars.GetRange(0, i + 1),
                        CurrentSession = DetermineSession(_bars[i].Time)
                    };

                    var signal = strategy.Evaluate(state);
                    if (signal.HasValue && PassesFilters(signal.Value))
                    {
                        var trade = OpenTrade(signal.Value, bar, i);
                        if (trade.HasValue)
                        {
                            openTrades.Add(trade.Value);
                            lastSignal = signal.Value;
                        }
                    }
                }

                equityCurve.Add(_balance);
            }

            CloseAllOpenTrades(openTrades, _bars.Count - 1, trades);
            var metrics = CalculateMetrics(trades, equityCurve);

            return new StrategyBacktestResult
            {
                StrategyName = strategy.Name,
                Metrics = metrics,
                LastSignal = lastSignal
            };
        }

        public (Dictionary<string, StrategyBacktestResult> individual, BacktestMetrics combined) RunCombinedStrategies(
            List<ISignalStrategy> strategies, List<Bar> bars)
        {
            var individual = new Dictionary<string, StrategyBacktestResult>();
            var allSignals = new List<StrategySignal>();

            _bars = bars ?? throw new ArgumentNullException(nameof(bars));
            Reset();
            var trades = new List<SimulatedTrade>();
            var equityCurve = new List<double> { _balance };
            var openTrades = new List<SimulatedTrade>();

            for (int i = 0; i < _bars.Count; i++)
            {
                var bar = _bars[i];
                UpdateDailyTracking(bar.Time);

                if (_balance <= 0) break;
                if (IsMaxDrawdownBreached()) break;
                if (IsMaxDailyLossBreached()) continue;

                CheckOpenTrades(openTrades, bar, i, trades, equityCurve);

                if (openTrades.Count < _config.MaxOpenTrades && i >= _config.MinBarsBeforeSignal)
                {
                    var state = new MarketState
                    {
                        Bars = _bars.GetRange(0, i + 1),
                        CurrentSession = DetermineSession(_bars[i].Time)
                    };

                    allSignals.Clear();
                    foreach (var strategy in strategies)
                    {
                        var signal = strategy.Evaluate(state);
                        if (signal.HasValue)
                            allSignals.Add(signal.Value);
                    }

                    if (allSignals.Count > 0)
                    {
                        var combined = CombineSignals(allSignals);
                        if (combined.HasValue && combined.Value.Confidence >= _multiConfig.MinCombinedConfidence)
                        {
                            var trade = OpenTrade(combined.Value, bar, i);
                            if (trade.HasValue)
                                openTrades.Add(trade.Value);
                        }
                    }
                }

                equityCurve.Add(_balance);
            }

            foreach (var strategy in strategies)
            {
                individual[strategy.Name] = RunSingleStrategy(strategy, bars);
            }

            CloseAllOpenTrades(openTrades, _bars.Count - 1, trades);
            var combinedMetrics = CalculateMetrics(trades, equityCurve);

            return (individual, combinedMetrics);
        }

        private StrategySignal? CombineSignals(List<StrategySignal> signals)
        {
            if (signals.Count == 0) return null;

            var longSignals = signals.Where(s => s.Direction == TradeDirection.Long).ToList();
            var shortSignals = signals.Where(s => s.Direction == TradeDirection.Short).ToList();

            double longConf = longSignals.Sum(s => s.Confidence) / Math.Max(1, longSignals.Count);
            double shortConf = shortSignals.Sum(s => s.Confidence) / Math.Max(1, shortSignals.Count);

            TradeDirection direction;
            double confidence;
            double entry, sl, tp1, tp2, tp3;

            if (longConf > shortConf && longConf >= _multiConfig.MinCombinedConfidence)
            {
                direction = TradeDirection.Long;
                confidence = longConf;
                entry = longSignals.Average(s => s.EntryPrice);
                sl = longSignals.Max(s => s.StopLoss);
                tp1 = longSignals.Average(s => s.TakeProfit1);
                tp2 = longSignals.Average(s => s.TakeProfit2);
                tp3 = longSignals.Average(s => s.TakeProfit3);
            }
            else if (shortConf > longConf && shortConf >= _multiConfig.MinCombinedConfidence)
            {
                direction = TradeDirection.Short;
                confidence = shortConf;
                entry = shortSignals.Average(s => s.EntryPrice);
                sl = shortSignals.Min(s => s.StopLoss);
                tp1 = shortSignals.Average(s => s.TakeProfit1);
                tp2 = shortSignals.Average(s => s.TakeProfit2);
                tp3 = shortSignals.Average(s => s.TakeProfit3);
            }
            else
            {
                return null;
            }

            string rationale = $"Combined {signals.Count} signals: {longSignals.Count} long, {shortSignals.Count} short";

            return new StrategySignal
            {
                Direction = direction,
                Confidence = confidence,
                EntryPrice = entry,
                StopLoss = sl,
                TakeProfit1 = tp1,
                TakeProfit2 = tp2,
                TakeProfit3 = tp3,
                Rationale = rationale
            };
        }

        private void Reset()
        {
            _balance = _config.StartingBalance;
            _peakBalance = _config.StartingBalance;
            _maxDrawdown = 0;
            _maxDailyLoss = 0;
            _currentDay = DateTime.MinValue;
            _dailyStartBalance = _config.StartingBalance;
        }

        private void UpdateDailyTracking(DateTime barTime)
        {
            DateTime day = barTime.Date;
            if (_currentDay == DateTime.MinValue)
            {
                _currentDay = day;
                _dailyStartBalance = _balance;
            }
            else if (day != _currentDay)
            {
                double dailyLoss = _dailyStartBalance - _balance;
                if (dailyLoss > _maxDailyLoss)
                    _maxDailyLoss = dailyLoss;
                _currentDay = day;
                _dailyStartBalance = _balance;
            }
        }

        private bool IsMaxDrawdownBreached()
        {
            double drawdownPct = (_peakBalance - _balance) / _peakBalance;
            return drawdownPct >= _config.MaxTotalDrawdownPct;
        }

        private bool IsMaxDailyLossBreached()
        {
            double dailyLossPct = (_dailyStartBalance - _balance) / _dailyStartBalance;
            return dailyLossPct >= _config.MaxDailyDrawdownPct;
        }

        private void CheckOpenTrades(List<SimulatedTrade> openTrades, Bar bar,
            int barIndex, List<SimulatedTrade> closedTrades, List<double> equityCurve)
        {
            var toClose = new List<SimulatedTrade>();

            foreach (var trade in openTrades)
            {
                var (hit, exitPrice, exitReason) = CheckTradeExit(trade, bar);
                if (hit)
                {
                    CloseTrade(trade, barIndex, bar.Time, exitPrice, exitReason);
                    closedTrades.Add(trade);
                    toClose.Add(trade);
                    equityCurve.Add(_balance);
                }
            }

            foreach (var t in toClose)
                openTrades.Remove(t);
        }

        private (bool hit, double exitPrice, ExitReason reason) CheckTradeExit(SimulatedTrade trade, Bar bar)
        {
            if (trade.Direction == TradeDirection.Long)
            {
                if (bar.Low <= trade.StopLoss) return (true, trade.StopLoss, ExitReason.StopLoss);
                if (bar.High >= trade.TakeProfit3) return (true, trade.TakeProfit3, ExitReason.TakeProfit3);
                if (bar.High >= trade.TakeProfit2) return (true, trade.TakeProfit2, ExitReason.TakeProfit2);
            }
            else
            {
                if (bar.High >= trade.StopLoss) return (true, trade.StopLoss, ExitReason.StopLoss);
                if (bar.Low <= trade.TakeProfit3) return (true, trade.TakeProfit3, ExitReason.TakeProfit3);
                if (bar.Low <= trade.TakeProfit2) return (true, trade.TakeProfit2, ExitReason.TakeProfit2);
            }
            return (false, 0, ExitReason.StopLoss);
        }

        private bool PassesFilters(StrategySignal signal)
        {
            if (signal.Confidence < _config.MinConfidence) return false;
            return true;
        }

        private SimulatedTrade? OpenTrade(StrategySignal signal, Bar bar, int barIndex)
        {
            double riskAmount = _balance * _config.RiskPerTradePct;
            double risk = Math.Abs(signal.EntryPrice - signal.StopLoss);
            if (risk == 0) return null;

            double pipValue = GetPipValue(signal.EntryPrice);
            double spreadCost = _config.SpreadPips * pipValue;
            double effectiveEntry = signal.Direction == TradeDirection.Long
                ? signal.EntryPrice + spreadCost : signal.EntryPrice - spreadCost;
            double adjustedRisk = Math.Abs(effectiveEntry - signal.StopLoss);
            if (adjustedRisk == 0) return null;

            double lotSize = riskAmount / adjustedRisk;
            if (lotSize * effectiveEntry > _balance) return null;

            return new SimulatedTrade
            {
                EntryBarIndex = barIndex,
                ExitBarIndex = -1,
                Direction = signal.Direction,
                EntryPrice = effectiveEntry,
                StopLoss = signal.StopLoss,
                TakeProfit1 = signal.TakeProfit1,
                TakeProfit2 = signal.TakeProfit2,
                TakeProfit3 = signal.TakeProfit3,
                LotSize = lotSize,
                RiskAmount = riskAmount,
                EntryTime = bar.Time,
                ConfidenceScore = signal.Confidence,
                ConfluenceCount = 1,
                Rationale = signal.Rationale,
                Outcome = TradeOutcome.Open,
                PartialClosed = false,
                PartialClosePrice = 0,
                PartialClosePnL = 0
            };
        }

        private void CloseTrade(SimulatedTrade trade, int barIndex, DateTime exitTime, double exitPrice, ExitReason reason)
        {
            trade.ExitBarIndex = barIndex;
            trade.ExitPrice = exitPrice;
            trade.ExitTime = exitTime;
            trade.ExitReason = reason;

            double pipValue = GetPipValue(trade.EntryPrice);
            double commissionCost = trade.LotSize * _config.CommissionPerLot;

            if (trade.Direction == TradeDirection.Long)
                trade.Pips = (exitPrice - trade.EntryPrice) / pipValue;
            else
                trade.Pips = (trade.EntryPrice - exitPrice) / pipValue;

            trade.ProfitLoss = trade.Pips * trade.LotSize * pipValue - commissionCost;
            _balance += trade.ProfitLoss;

            trade.Outcome = trade.ProfitLoss > 0.01 ? TradeOutcome.Win :
                trade.ProfitLoss < -0.01 ? TradeOutcome.Loss : TradeOutcome.Breakeven;

            if (_balance > _peakBalance) _peakBalance = _balance;
            double drawdown = (_peakBalance - _balance) / _peakBalance;
            if (drawdown > _maxDrawdown) _maxDrawdown = drawdown;
        }

        private void CloseAllOpenTrades(List<SimulatedTrade> openTrades, int barIndex, List<SimulatedTrade> closedTrades)
        {
            foreach (var trade in openTrades)
            {
                var lastBar = _bars[barIndex];
                CloseTrade(trade, barIndex, lastBar.Time, lastBar.Close, ExitReason.EndOfData);
                closedTrades.Add(trade);
            }
            openTrades.Clear();
        }

        private BacktestMetrics CalculateMetrics(List<SimulatedTrade> trades, List<double> equityCurve)
        {
            var metrics = new BacktestMetrics
            {
                StartingBalance = _config.StartingBalance,
                EndingBalance = _balance,
                TotalPnL = _balance - _config.StartingBalance,
                TotalPnLPct = (_balance - _config.StartingBalance) / _config.StartingBalance,
                TotalTrades = trades.Count,
                WinningTrades = trades.Count(t => t.Outcome == TradeOutcome.Win),
                LosingTrades = trades.Count(t => t.Outcome == TradeOutcome.Loss),
                BreakevenTrades = trades.Count(t => t.Outcome == TradeOutcome.Breakeven),
                Trades = trades,
                EquityCurve = equityCurve,
                RejectedSignals = 0,
                MaxDrawdownDollar = _maxDrawdown * _peakBalance,
                MaxDrawdownPct = _maxDrawdown * 100,
                MaxDailyLossDollar = _maxDailyLoss
            };

            if (trades.Count > 0)
            {
                metrics.WinRate = metrics.WinningTrades / (double)metrics.TotalTrades * 100;
                var wins = trades.Where(t => t.Outcome == TradeOutcome.Win).ToList();
                var losses = trades.Where(t => t.Outcome == TradeOutcome.Loss).ToList();
                metrics.AvgWin = wins.Count > 0 ? wins.Average(t => t.ProfitLoss) : 0;
                metrics.AvgLoss = losses.Count > 0 ? losses.Average(t => t.ProfitLoss) : 0;
                metrics.LargestWin = wins.Count > 0 ? wins.Max(t => t.ProfitLoss) : 0;
                metrics.LargestLoss = losses.Count > 0 ? losses.Min(t => t.ProfitLoss) : 0;
                double totalWins = wins.Sum(t => t.ProfitLoss);
                double totalLosses = Math.Abs(losses.Sum(t => t.ProfitLoss));
                metrics.ProfitFactor = totalLosses > 0 ? totalWins / totalLosses : totalWins > 0 ? 999 : 0;
                metrics.AvgRiskReward = losses.Count > 0 ? Math.Abs(metrics.AvgWin / metrics.AvgLoss) : 0;
                metrics.Expectancy = (metrics.WinRate / 100 * metrics.AvgWin) - ((1 - metrics.WinRate / 100) * Math.Abs(metrics.AvgLoss));
                metrics.AvgHoldingBars = trades.Average(t => t.ExitBarIndex - t.EntryBarIndex);
                metrics.TotalSpreadCost = trades.Count * _config.SpreadPips * GetPipValue(trades[0].EntryPrice);
                metrics.TotalCommissionCost = trades.Sum(t => t.LotSize * _config.CommissionPerLot);
            }

            metrics.SharpeRatio = CalculateSharpeRatio(equityCurve);
            return metrics;
        }

        private double CalculateSharpeRatio(List<double> equityCurve)
        {
            if (equityCurve.Count < 2) return 0;
            var returns = new List<double>();
            for (int i = 1; i < equityCurve.Count; i++)
                if (equityCurve[i - 1] != 0)
                    returns.Add((equityCurve[i] - equityCurve[i - 1]) / equityCurve[i - 1]);
            if (returns.Count == 0) return 0;
            double meanReturn = returns.Average();
            double stdDev = Math.Sqrt(returns.Average(r => Math.Pow(r - meanReturn, 2)));
            if (stdDev == 0) return meanReturn > 0 ? 999 : 0;
            return (meanReturn / stdDev) * Math.Sqrt(252);
        }

        private static double GetPipValue(double price) => price > 50 ? 0.01 : price > 10 ? 0.01 : 0.0001;

        private static SessionType DetermineSession(DateTime time)
        {
            int hour = time.Hour;
            if (hour >= 8 && hour < 12) return SessionType.London;
            if (hour >= 12 && hour < 16) return SessionType.NYAM;
            if (hour >= 16 && hour < 20) return SessionType.NYPM;
            return SessionType.Outside;
        }
    }
}