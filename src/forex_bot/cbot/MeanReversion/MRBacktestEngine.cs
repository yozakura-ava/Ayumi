using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class MRBacktestEngine
    {
        private readonly MRConfig _config;
        private readonly MRSignalModule _signalModule;
        private readonly MRExitManager _exitManager;
        private readonly BacktestConfig _btConfig;
        private List<Bar> _h1Bars;
        private List<Bar> _d1Bars;
        private double _balance;
        private double _peakBalance;
        private double _maxDrawdown;
        private double _dailyStartBalance;
        private DateTime _currentDay;
        private double _maxDailyLoss;
        private int _rejectedSignals;
        private int _lastTradeBarIndex;

        public MRBacktestEngine(MRConfig config)
        {
            _config = config;
            _signalModule = new MRSignalModule(config.ToSignalConfig());
            _exitManager = new MRExitManager(config.ToExitConfig());
            _btConfig = config.ToBacktestConfig();
        }

        public BacktestMetrics Run(List<Bar> h1Bars, List<Bar> d1Bars)
        {
            _h1Bars = h1Bars ?? throw new ArgumentNullException(nameof(h1Bars));
            _d1Bars = d1Bars ?? throw new ArgumentNullException(nameof(d1Bars));

            int minH1Bars = _btConfig.MinBarsBeforeSignal;
            if (_h1Bars.Count < minH1Bars)
                throw new ArgumentException(
                    $"Need at least {minH1Bars} H1 bars, got {_h1Bars.Count}");

            Reset();

            var trades = new List<SimulatedTrade>();
            var equityCurve = new List<double> { _balance };
            var openTrades = new List<MRTradeState>();

            var h1Slice = new List<Bar>(_h1Bars.Count);
            int d1SliceEnd = 0;

            for (int i = 0; i < _h1Bars.Count; i++)
            {
                var bar = _h1Bars[i];
                h1Slice.Add(bar);

                UpdateDailyTracking(bar.Time);

                d1SliceEnd = ExpandD1Slice(bar.Time, d1SliceEnd);

                var bands = MRExitManager.CalculateBollingerBands(
                    h1Slice, _config.BBPeriod, _config.BBStdDevMultiplier);
                double d1Ema = MRExitManager.CalculateEMA(_d1Bars.GetRange(0, d1SliceEnd), _config.EMAPeriod);
                double currentATR = MRExitManager.CalculateATR(h1Slice, _config.ATRPeriod);

                CheckOpenTrades(openTrades, bar, i, bands, d1Ema, currentATR,
                    trades, equityCurve);

                if (_balance <= 0) break;
                if (IsMaxDrawdownBreached()) break;

                bool dailyLossBreached = IsMaxDailyLossBreached();

                if (!dailyLossBreached &&
                    openTrades.Count < _btConfig.MaxOpenTrades &&
                    i >= _btConfig.MinBarsBeforeSignal &&
                    _balance > 0 &&
                    (i - _lastTradeBarIndex) >= _btConfig.MinBarsBetweenTrades)
                {
                    var d1Slice = _d1Bars.GetRange(0, d1SliceEnd);
                    var signal = _signalModule.GenerateSignal(h1Slice, d1Slice);

                    if (signal.IsValid)
                    {
                        bool duplicate = false;
                        foreach (var ot in openTrades)
                        {
                            if (ot.Direction == signal.Direction)
                            {
                                double pipValue = GetPipValue(signal.EntryPrice);
                                if (Math.Abs(ot.EntryPrice - signal.EntryPrice) / pipValue < 10.0)
                                {
                                    duplicate = true;
                                    break;
                                }
                            }
                        }

                        if (duplicate)
                        {
                            _rejectedSignals++;
                        }
                        else
                        {
                            var trade = OpenTrade(signal, bar, i, currentATR);
                            if (trade.HasValue)
                            {
                                openTrades.Add(trade.Value);
                                _lastTradeBarIndex = i;
                            }
                            else
                            {
                                _rejectedSignals++;
                            }
                        }
                    }
                }

                equityCurve.Add(_balance);
            }

            CloseAllOpenTrades(openTrades, _h1Bars.Count - 1, trades);

            return CalculateMetrics(trades, equityCurve);
        }

        private void Reset()
        {
            _balance = _btConfig.StartingBalance;
            _peakBalance = _btConfig.StartingBalance;
            _maxDrawdown = 0;
            _maxDailyLoss = 0;
            _currentDay = DateTime.MinValue;
            _dailyStartBalance = _btConfig.StartingBalance;
            _rejectedSignals = 0;
            _lastTradeBarIndex = -100;
        }

        private int ExpandD1Slice(DateTime h1Time, int currentEnd)
        {
            while (currentEnd < _d1Bars.Count && _d1Bars[currentEnd].Time.Date <= h1Time.Date)
                currentEnd++;
            return currentEnd;
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
            return drawdownPct >= _btConfig.MaxTotalDrawdownPct;
        }

        private bool IsMaxDailyLossBreached()
        {
            double dailyLossPct = (_dailyStartBalance - _balance) / _dailyStartBalance;
            return dailyLossPct >= _btConfig.MaxDailyDrawdownPct;
        }

        private void CheckOpenTrades(List<MRTradeState> openTrades, Bar bar,
            int barIndex, BollingerBands bands, double d1Ema, double currentATR,
            List<SimulatedTrade> closedTrades, List<double> equityCurve)
        {
            var toClose = new List<int>();

            for (int t = 0; t < openTrades.Count; t++)
            {
                var trade = openTrades[t];
                trade.BarsSinceEntry++;

                var exitSignal = _exitManager.CheckExit(
                    ref trade, bar, bands, d1Ema, currentATR);

                openTrades[t] = trade;

                if (exitSignal.ShouldPartialClose && !exitSignal.ShouldClose)
                {
                    _balance += trade.PartialClosePnL;
                    if (_balance > _peakBalance)
                        _peakBalance = _balance;
                }

                if (exitSignal.ShouldClose)
                {
                    var exitReason = MapExitReason(exitSignal.Reason);
                    var simTrade = ConvertToSimulatedTrade(trade, barIndex, bar.Time,
                        exitSignal.ExitPrice, exitReason);

                    ProcessTradePnL(ref simTrade);
                    closedTrades.Add(simTrade);
                    toClose.Add(t);
                    equityCurve.Add(_balance);
                }
            }

            for (int i = toClose.Count - 1; i >= 0; i--)
                openTrades.RemoveAt(toClose[i]);
        }

        private MRTradeState? OpenTrade(MRSignal signal, Bar bar, int barIndex, double atr)
        {
            if (atr <= 0) return null;

            double sl = _exitManager.CalculateStopLoss(signal.Direction, signal.EntryPrice, atr);
            double risk = Math.Abs(signal.EntryPrice - sl);
            if (risk == 0) return null;

            double riskAmount = _balance * _config.RiskPerTradePct;
            double pipValue = GetPipValue(signal.EntryPrice);
            double spreadCost = _btConfig.SpreadPips * pipValue;

            double effectiveEntry = signal.Direction == TradeDirection.Long
                ? signal.EntryPrice + spreadCost
                : signal.EntryPrice - spreadCost;

            double adjustedRisk = Math.Abs(effectiveEntry - sl);
            if (adjustedRisk == 0) return null;

            double lotSize = riskAmount / (adjustedRisk * 100000.0);
            if (lotSize <= 0) return null;

            double maxNotional = _balance * _btConfig.Leverage;
            if (lotSize * 100000.0 * effectiveEntry > maxNotional)
                lotSize = maxNotional / (100000.0 * effectiveEntry);

            return new MRTradeState
            {
                Direction = signal.Direction,
                EntryPrice = effectiveEntry,
                CurrentStopLoss = sl,
                LotSize = lotSize,
                TP1Hit = false,
                PartialClosed = false,
                PartialClosePrice = 0,
                PartialClosePnL = 0,
                BarsSinceEntry = 0,
                OriginalATR = atr,
                StructureBreakTriggered = false
            };
        }

        private SimulatedTrade ConvertToSimulatedTrade(MRTradeState mrTrade,
            int barIndex, DateTime exitTime, double exitPrice, ExitReason reason)
        {
            return new SimulatedTrade
            {
                EntryBarIndex = barIndex - mrTrade.BarsSinceEntry,
                ExitBarIndex = barIndex,
                Direction = mrTrade.Direction,
                EntryPrice = mrTrade.EntryPrice,
                StopLoss = mrTrade.CurrentStopLoss,
                TakeProfit1 = 0,
                TakeProfit2 = 0,
                TakeProfit3 = 0,
                ExitPrice = exitPrice,
                LotSize = mrTrade.LotSize,
                RiskAmount = 0,
                Pips = 0,
                ProfitLoss = 0,
                Outcome = TradeOutcome.Open,
                ExitReason = reason,
                EntryTime = exitTime.AddHours(-mrTrade.BarsSinceEntry),
                ExitTime = exitTime,
                ConfidenceScore = 0,
                ConfluenceCount = 0,
                Rationale = "MR Strategy",
                PartialClosed = mrTrade.PartialClosed,
                PartialClosePrice = mrTrade.PartialClosePrice,
                PartialClosePnL = mrTrade.PartialClosePnL
            };
        }

        private void ProcessTradePnL(ref SimulatedTrade trade)
        {
            double pipValue = GetPipValue(trade.EntryPrice);
            double pipValuePerLot = pipValue * 100000.0;
            double commissionCost = trade.LotSize * _btConfig.CommissionPerLot;

            if (trade.Direction == TradeDirection.Long)
                trade.Pips = (trade.ExitPrice - trade.EntryPrice) / pipValue;
            else
                trade.Pips = (trade.EntryPrice - trade.ExitPrice) / pipValue;

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

        private void CloseAllOpenTrades(List<MRTradeState> openTrades, int barIndex,
            List<SimulatedTrade> closedTrades)
        {
            for (int i = 0; i < openTrades.Count; i++)
            {
                var mrTrade = openTrades[i];
                var lastBar = _h1Bars[barIndex];

                var simTrade = ConvertToSimulatedTrade(mrTrade, barIndex,
                    lastBar.Time, lastBar.Close, ExitReason.EndOfData);

                ProcessTradePnL(ref simTrade);
                closedTrades.Add(simTrade);
            }
            openTrades.Clear();
        }

        private BacktestMetrics CalculateMetrics(List<SimulatedTrade> trades,
            List<double> equityCurve)
        {
            var metrics = new BacktestMetrics
            {
                StartingBalance = _btConfig.StartingBalance,
                EndingBalance = _balance,
                TotalPnL = _balance - _btConfig.StartingBalance,
                TotalPnLPct = (_balance - _btConfig.StartingBalance) / _btConfig.StartingBalance,
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

                metrics.TotalSpreadCost = trades.Count * _btConfig.SpreadPips *
                    GetPipValue(trades[0].EntryPrice);

                metrics.TotalCommissionCost = trades.Sum(t => t.LotSize * _btConfig.CommissionPerLot);
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

        private static ExitReason MapExitReason(MRExitReason mrReason)
        {
            return mrReason switch
            {
                MRExitReason.TakeProfit1 => ExitReason.TakeProfit1,
                MRExitReason.TakeProfit2 => ExitReason.TakeProfit2,
                MRExitReason.StopLoss => ExitReason.StopLoss,
                MRExitReason.TimeStop => ExitReason.MaxDailyLoss,
                MRExitReason.StructureBreak => ExitReason.SignalFlip,
                MRExitReason.TrailingStopHit => ExitReason.StopLoss,
                _ => ExitReason.EndOfData
            };
        }

        private static double GetPipValue(double price)
        {
            if (price > 50) return 0.01;
            return 0.0001;
        }
    }
}
