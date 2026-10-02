using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class BacktestEngine
    {
        private readonly BacktestConfig _config;
        private readonly SignalConfluenceEngine _signalEngine;
        private readonly MarketStructureAnalyzer _structureAnalyzer;
        private List<Bar> _bars;
        private double _balance;
        private double _peakBalance;
        private double _maxDrawdown;
        private double _dailyStartBalance;
        private DateTime _currentDay;
        private double _maxDailyLoss;
        private int _rejectedSignals;
        private int _lastTradeBarIndex;

        public BacktestEngine(BacktestConfig config)
        {
            _config = config;
            _structureAnalyzer = new MarketStructureAnalyzer();
            _signalEngine = new SignalConfluenceEngine(
                minConfidence: 0.15,
                defaultSLMultiplier: 3.0,
                tp1RR: 1.0,
                tp2RR: 2.0,
                tp3RR: 3.0);
        }

        public BacktestMetrics Run(List<Bar> bars)
        {
            _bars = bars ?? throw new ArgumentNullException(nameof(bars));
            if (_bars.Count < _config.MinBarsBeforeSignal)
                throw new ArgumentException(
                    $"Need at least {_config.MinBarsBeforeSignal} bars, got {_bars.Count}");

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

                if (openTrades.Count < _config.MaxOpenTrades && i >= _config.MinBarsBeforeSignal && _balance > 0 && (i - _lastTradeBarIndex) >= _config.MinBarsBetweenTrades)
                {
                    var signal = EvaluateSignal(i);
                    if (signal.HasValue && PassesFilters(signal.Value))
                    {
                        if (IsDuplicateSignal(signal.Value, openTrades))
                        {
                            _rejectedSignals++;
                        }
                        else
                        {
                            var trade = OpenTrade(signal.Value, bar, i);
                            if (trade.HasValue)
                            {
                                openTrades.Add(trade.Value);
                                _lastTradeBarIndex = i;
                            }
                            else
                                _rejectedSignals++;
                        }
                    }
                    else if (signal.HasValue)
                    {
                        _rejectedSignals++;
                    }
                }

                equityCurve.Add(_balance);
            }

            CloseAllOpenTrades(openTrades, _bars.Count - 1, trades);

            return CalculateMetrics(trades, equityCurve);
        }

        private void Reset()
        {
            _balance = _config.StartingBalance;
            _peakBalance = _config.StartingBalance;
            _maxDrawdown = 0;
            _maxDailyLoss = 0;
            _currentDay = DateTime.MinValue;
            _dailyStartBalance = _config.StartingBalance;
            _rejectedSignals = 0;
            _lastTradeBarIndex = -100;
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
            var toClose = new List<int>();

            for (int t = 0; t < openTrades.Count; t++)
            {
                var trade = openTrades[t];
                var (hit, exitPrice, exitReason) = CheckTradeExit(ref trade, bar);

                if (_config.TrailingStopEnabled && !hit)
                {
                    UpdateTrailingStop(ref trade, bar);
                }

                openTrades[t] = trade;

                if (hit)
                {
                    CloseTrade(ref trade, barIndex, bar.Time, exitPrice, exitReason);
                    openTrades[t] = trade;
                    closedTrades.Add(trade);
                    toClose.Add(t);
                    equityCurve.Add(_balance);
                }
            }

            for (int i = toClose.Count - 1; i >= 0; i--)
                openTrades.RemoveAt(toClose[i]);
        }

        private (bool hit, double exitPrice, ExitReason reason) CheckTradeExit(
            ref SimulatedTrade trade, Bar bar)
        {
            if (trade.Direction == TradeDirection.Long)
            {
                if (bar.Low <= trade.StopLoss)
                    return (true, trade.StopLoss, ExitReason.StopLoss);

                if (_config.PartialCloseEnabled && !trade.PartialClosed &&
                    bar.High >= trade.TakeProfit1)
                {
                    trade.PartialClosed = true;
                    trade.PartialClosePrice = trade.TakeProfit1;
                    double partialLots = trade.LotSize * _config.PartialClosePct;
                    double partialPips = (trade.TakeProfit1 - trade.EntryPrice) /
                        GetPipValue(trade.EntryPrice);
                    trade.PartialClosePnL = partialPips * partialLots * GetPipValue(trade.EntryPrice) * 100000.0;
                    _balance += trade.PartialClosePnL;
                    trade.LotSize *= (1.0 - _config.PartialClosePct);
                }

                if (bar.High >= trade.TakeProfit2)
                    return (true, trade.TakeProfit2, ExitReason.TakeProfit2);

                if (bar.High >= trade.TakeProfit3)
                    return (true, trade.TakeProfit3, ExitReason.TakeProfit3);
            }
            else
            {
                if (bar.High >= trade.StopLoss)
                    return (true, trade.StopLoss, ExitReason.StopLoss);

                if (_config.PartialCloseEnabled && !trade.PartialClosed &&
                    bar.Low <= trade.TakeProfit1)
                {
                    trade.PartialClosed = true;
                    trade.PartialClosePrice = trade.TakeProfit1;
                    double partialLots = trade.LotSize * _config.PartialClosePct;
                    double partialPips = (trade.EntryPrice - trade.TakeProfit1) /
                        GetPipValue(trade.EntryPrice);
                    trade.PartialClosePnL = partialPips * partialLots * GetPipValue(trade.EntryPrice) * 100000.0;
                    _balance += trade.PartialClosePnL;
                    trade.LotSize *= (1.0 - _config.PartialClosePct);
                }

                if (bar.Low <= trade.TakeProfit2)
                    return (true, trade.TakeProfit2, ExitReason.TakeProfit2);

                if (bar.Low <= trade.TakeProfit3)
                    return (true, trade.TakeProfit3, ExitReason.TakeProfit3);
            }

            return (false, 0, ExitReason.StopLoss);
        }

        private void UpdateTrailingStop(ref SimulatedTrade trade, Bar bar)
        {
            double atr = _bars.Count > 14 ? CalculateATR() : 0;
            if (atr == 0) return;

            double trailDistance = atr * _config.TrailingStopATRMultiplier;

            if (trade.Direction == TradeDirection.Long)
            {
                double newSL = bar.Close - trailDistance;
                if (newSL > trade.StopLoss)
                    trade.StopLoss = newSL;
            }
            else
            {
                double newSL = bar.Close + trailDistance;
                if (newSL < trade.StopLoss)
                    trade.StopLoss = newSL;
            }
        }

        private double CalculateATR()
        {
            int period = Math.Min(14, _bars.Count - 1);
            double atrSum = 0;
            for (int i = _bars.Count - period; i < _bars.Count; i++)
            {
                if (i > 0)
                {
                    double tr = Math.Max(
                        _bars[i].High - _bars[i].Low,
                        Math.Max(
                            Math.Abs(_bars[i].High - _bars[i - 1].Close),
                            Math.Abs(_bars[i].Low - _bars[i - 1].Close)
                        )
                    );
                    atrSum += tr;
                }
            }
            return atrSum / period;
        }

        private ConfluenceSignal? EvaluateSignal(int barIndex)
        {
            var state = new MarketState
            {
                Bars = _bars.GetRange(0, barIndex + 1),
                CurrentSession = DetermineSession(_bars[barIndex].Time)
            };

            return _signalEngine.Evaluate(state);
        }

        private SessionType DetermineSession(DateTime time)
        {
            int hour = time.Hour;
            if (hour >= 8 && hour < 12) return SessionType.London;
            if (hour >= 12 && hour < 16) return SessionType.NYAM;
            if (hour >= 16 && hour < 20) return SessionType.NYPM;
            return SessionType.Outside;
        }

        private bool PassesFilters(ConfluenceSignal signal)
        {
            if (signal.ConfidenceScore < _config.MinConfidence) return false;
            if (signal.ConfluenceCount < _config.MinConfluences) return false;
            if (signal.RiskRewardRatio < _config.MinRiskReward) return false;

            if (_config.RegimeFilterEnabled && !signal.HasStructureAlignment)
                return false;

            if (_config.NewsVolatilityFilterEnabled && IsATRSpikeDetected())
                return false;

            return true;
        }

        private bool IsATRSpikeDetected()
        {
            if (_bars.Count < 30) return false;

            double currentATR = 0;
            double trSum = 0;
            int period = 14;
            for (int i = _bars.Count - period; i < _bars.Count; i++)
            {
                if (i > 0)
                {
                    double tr = Math.Max(
                        _bars[i].High - _bars[i].Low,
                        Math.Max(
                            Math.Abs(_bars[i].High - _bars[i - 1].Close),
                            Math.Abs(_bars[i].Low - _bars[i - 1].Close)
                        )
                    );
                    trSum += tr;
                }
            }
            currentATR = trSum / period;

            double longerATRSum = 0;
            int longerPeriod = Math.Min(50, _bars.Count - 1);
            for (int i = _bars.Count - longerPeriod; i < _bars.Count; i++)
            {
                if (i > 0)
                {
                    double tr = Math.Max(
                        _bars[i].High - _bars[i].Low,
                        Math.Max(
                            Math.Abs(_bars[i].High - _bars[i - 1].Close),
                            Math.Abs(_bars[i].Low - _bars[i - 1].Close)
                        )
                    );
                    longerATRSum += tr;
                }
            }
            double longerATR = longerATRSum / longerPeriod;

            if (longerATR == 0) return false;

            return currentATR / longerATR > 2.0;
        }

        private bool IsDuplicateSignal(ConfluenceSignal signal, List<SimulatedTrade> openTrades)
        {
            double pipValue = GetPipValue(signal.EntryPrice);
            double maxPipDistance = 10.0;

            foreach (var trade in openTrades)
            {
                if (trade.Direction != signal.Direction) continue;
                double entryDistancePips = Math.Abs(trade.EntryPrice - signal.EntryPrice) / pipValue;
                if (entryDistancePips < maxPipDistance) return true;
            }
            return false;
        }

        private SimulatedTrade? OpenTrade(ConfluenceSignal signal, Bar bar, int barIndex)
        {
            double riskAmount = _balance * _config.RiskPerTradePct;
            double risk = Math.Abs(signal.EntryPrice - signal.StopLoss);

            if (risk == 0) return null;

            double pipValue = GetPipValue(signal.EntryPrice);
            double spreadCost = _config.SpreadPips * pipValue;

            double effectiveEntry = signal.Direction == TradeDirection.Long
                ? signal.EntryPrice + spreadCost
                : signal.EntryPrice - spreadCost;

            double adjustedRisk = Math.Abs(effectiveEntry - signal.StopLoss);
            if (adjustedRisk == 0) return null;

            double lotSize = riskAmount / (adjustedRisk * 100000.0);
            if (lotSize <= 0) return null;

            double maxNotional = _balance * _config.Leverage;
            if (lotSize * 100000.0 * effectiveEntry > maxNotional)
                lotSize = maxNotional / (100000.0 * effectiveEntry);

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
                ConfidenceScore = signal.ConfidenceScore,
                ConfluenceCount = signal.ConfluenceCount,
                Rationale = signal.Rationale,
                Outcome = TradeOutcome.Open,
                PartialClosed = false,
                PartialClosePrice = 0,
                PartialClosePnL = 0
            };
        }

        private void CloseTrade(ref SimulatedTrade trade, int barIndex,
            DateTime exitTime, double exitPrice, ExitReason reason)
        {
            trade.ExitBarIndex = barIndex;
            trade.ExitPrice = exitPrice;
            trade.ExitTime = exitTime;
            trade.ExitReason = reason;

            double pipValue = GetPipValue(trade.EntryPrice);
            double pipValuePerLot = pipValue * 100000.0;
            double commissionCost = trade.LotSize * _config.CommissionPerLot;

            if (trade.Direction == TradeDirection.Long)
            {
                trade.Pips = (exitPrice - trade.EntryPrice) / pipValue;
            }
            else
            {
                trade.Pips = (trade.EntryPrice - exitPrice) / pipValue;
            }

            double closePnL = trade.Pips * trade.LotSize * pipValuePerLot - commissionCost;
            trade.ProfitLoss = closePnL + trade.PartialClosePnL;
            _balance += closePnL;

            trade.Outcome = trade.ProfitLoss > 0.01 ? TradeOutcome.Win :
                trade.ProfitLoss < -0.01 ? TradeOutcome.Loss : TradeOutcome.Breakeven;

            if (_balance > _peakBalance)
                _peakBalance = _balance;

            double drawdown = (_peakBalance - _balance) / _peakBalance;
            if (drawdown > _maxDrawdown)
                _maxDrawdown = drawdown;
        }

        private void CloseAllOpenTrades(List<SimulatedTrade> openTrades, int barIndex,
            List<SimulatedTrade> closedTrades)
        {
            for (int i = 0; i < openTrades.Count; i++)
            {
                var trade = openTrades[i];
                var lastBar = _bars[barIndex];
                double exitPrice = lastBar.Close;

                CloseTrade(ref trade, barIndex, lastBar.Time, exitPrice, ExitReason.EndOfData);
                openTrades[i] = trade;
                closedTrades.Add(trade);
            }
            openTrades.Clear();
        }

        private BacktestMetrics CalculateMetrics(List<SimulatedTrade> trades,
            List<double> equityCurve)
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
                RejectedSignals = _rejectedSignals,
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

                metrics.AvgRiskReward = losses.Count > 0
                    ? Math.Abs(metrics.AvgWin / metrics.AvgLoss) : 0;

                metrics.Expectancy = (metrics.WinRate / 100 * metrics.AvgWin) -
                    ((1 - metrics.WinRate / 100) * Math.Abs(metrics.AvgLoss));

                metrics.AvgHoldingBars = trades.Average(t =>
                    t.ExitBarIndex - t.EntryBarIndex);

                metrics.TotalSpreadCost = trades.Count * _config.SpreadPips *
                    GetPipValue(trades[0].EntryPrice);

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
            {
                if (equityCurve[i - 1] != 0)
                    returns.Add((equityCurve[i] - equityCurve[i - 1]) / equityCurve[i - 1]);
            }

            if (returns.Count == 0) return 0;

            double meanReturn = returns.Average();
            double stdDev = Math.Sqrt(returns.Average(r => Math.Pow(r - meanReturn, 2)));

            if (stdDev == 0) return meanReturn > 0 ? 999 : 0;

            return (meanReturn / stdDev) * Math.Sqrt(252);
        }

        private static double GetPipValue(double price)
        {
            if (price > 50) return 0.01;
            return 0.0001;
        }
    }
}
