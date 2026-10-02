using System;
using System.IO;
using System.Linq;

namespace ICTSMC
{
    public class Program
    {
        public static void Main(string[] args)
        {
            Console.WriteLine("ICT/SMC Trading System - Test Runner\n");

            if (args.Length > 0 && args[0] == "--backtest")
            {
                BacktestTests.RunAll();
            }
            else if (args.Length > 0 && args[0] == "--report")
            {
                RunBacktestReport();
            }
            else if (args.Length > 0 && args[0] == "--live-backtest")
            {
                RunLiveBacktest(args);
            }
            else if (args.Length > 0 && args[0] == "--mr-exit")
            {
                MRExitManagerTests.RunAll();
            }
            else if (args.Length > 0 && args[0] == "--mr-signal")
            {
                MRSignalModuleTests.RunAll();
            }
            else if (args.Length > 0 && args[0] == "--mr-config")
            {
                MRConfigTests.RunAll();
            }
            else if (args.Length > 0 && args[0] == "--mr-backtest")
            {
                RunMRBacktest(args);
            }
            else if (args.Length > 0 && args[0] == "--ict-scorer")
            {
                ICTConfirmationScorerTests.RunAll();
            }
            else if (args.Length > 0 && args[0] == "--h4-context")
            {
                H4ContextModuleTests.RunAll();
            }
            else
            {
                ConfluenceEngineTests.RunAll();
                Console.WriteLine();
                BacktestTests.RunAll();
                Console.WriteLine();
                MRExitManagerTests.RunAll();
                Console.WriteLine();
                MRSignalModuleTests.RunAll();
                Console.WriteLine();
                MRConfigTests.RunAll();
                Console.WriteLine();
                ICTConfirmationScorerTests.RunAll();
                Console.WriteLine();
                H4ContextModuleTests.RunAll();
            }
        }

        private static void RunBacktestReport()
        {
            Console.WriteLine("Running full backtest with sample data...\n");

            var bars = MockDataGenerator.GenerateBullishTrend(200);
            var engine = new BacktestEngine(BacktestConfig.Default);
            var metrics = engine.Run(bars);
            metrics.PrintReport();
        }

        private static void RunLiveBacktest(string[] args)
        {
            string dataDir = args.Length > 1
                ? args[1]
                : Path.Combine(
                    Directory.GetCurrentDirectory(),
                    "..", "..", "..", "..", "..", "..",
                    "data", "forex", "historical");

            if (!Directory.Exists(dataDir))
            {
                dataDir = "$AYUMI_ROOT/worktrees/kai/data/forex/historical";
            }

            var pairs = new[] { "EURUSD", "GBPUSD" };
            var configs = new (string name, BacktestConfig config)[]
            {
                ("Default", BacktestConfig.Default),
                ("Aggressive", BacktestConfig.Aggressive),
                ("Conservative", BacktestConfig.Conservative),
                ("Tuned", BacktestConfig.Tuned)
            };

            var loader = new CsvDataLoader();

            foreach (var pair in pairs)
            {
                string csvPath = Path.Combine(dataDir, $"{pair}_M15.csv");
                if (!File.Exists(csvPath))
                {
                    Console.WriteLine($"SKIP: {pair} M15 data not found at {csvPath}");
                    continue;
                }

                Console.WriteLine($"\n{'=',-60}");
                Console.WriteLine($"  {pair} M15 Backtest");
                Console.WriteLine($"  Data file: {csvPath}");
                Console.WriteLine($"{'=',-60}");

                var bars = loader.Load(csvPath);
                int maxBars = 10000;
                if (bars.Count > maxBars)
                {
                    bars = bars.GetRange(bars.Count - maxBars, maxBars);
                }
                Console.WriteLine($"  Loaded {bars.Count} bars");
                Console.WriteLine($"  Date range: {bars.First().Time:yyyy-MM-dd} to {bars.Last().Time:yyyy-MM-dd}\n");

                foreach (var (configName, config) in configs)
                {
                    Console.WriteLine($"  --- {configName} Config ---");
                    try
                    {
                        var engine = new BacktestEngine(config);
                        var metrics = engine.Run(bars);

                        Console.WriteLine($"  Trades: {metrics.TotalTrades} | " +
                            $"WR: {metrics.WinRate:F1}% | " +
                            $"PF: {metrics.ProfitFactor:F2} | " +
                            $"R:R: {metrics.AvgRiskReward:F2} | " +
                            $"P&L: ${metrics.TotalPnL:F2} | " +
                            $"Max DD: {metrics.MaxDrawdownPct:F2}% | " +
                            $"Sharpe: {metrics.SharpeRatio:F2} | " +
                            $"Rejected: {metrics.RejectedSignals}");

                        string goNoGo = EvaluateGoNoGo(metrics);
                        Console.WriteLine($"  GO/NO-GO: {goNoGo}");

                        if (metrics.Trades.Count > 0 && metrics.Trades.Count <= 30)
                        {
                            Console.WriteLine("  Trade details:");
                            for (int t = 0; t < metrics.Trades.Count; t++)
                            {
                                var tr = metrics.Trades[t];
                                Console.WriteLine($"    #{t+1} {tr.Direction} @ {tr.EntryPrice:F5} " +
                                    $"SL={tr.StopLoss:F5} TP1={tr.TakeProfit1:F5} TP2={tr.TakeProfit2:F5} " +
                                    $"Exit={tr.ExitPrice:F5} Pips={tr.Pips:F1} P&L=${tr.ProfitLoss:F2} " +
                                    $"{tr.Outcome} [{tr.ExitReason}] Conf={tr.ConfidenceScore:F2} " +
                                    $"Confl={tr.ConfluenceCount}");
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"  ERROR: {ex.Message}");
                    }
                    Console.WriteLine();
                }
            }
        }

        private static void RunMRBacktest(string[] args)
        {
            string dataDir = args.Length > 1
                ? args[1]
                : Path.Combine(
                    Directory.GetCurrentDirectory(),
                    "..", "..", "..", "..", "..", "..",
                    "data", "forex", "historical");

            if (!Directory.Exists(dataDir))
            {
                dataDir = "$AYUMI_ROOT/worktrees/kai/data/forex/historical";
            }

            var pairs = new[] { "EURUSD", "GBPUSD" };
            var presets = MRConfig.AllPresets;
            var loader = new CsvDataLoader();

            Console.WriteLine("╔══════════════════════════════════════════════╗");
            Console.WriteLine("║     MEAN REVERSION BACKTEST RESULTS           ║");
            Console.WriteLine("╚══════════════════════════════════════════════╝");

            foreach (var pair in pairs)
            {
                string h1Path = Path.Combine(dataDir, $"{pair}_H1.csv");
                string d1Path = Path.Combine(dataDir, $"{pair}_D1.csv");

                if (!File.Exists(h1Path))
                {
                    Console.WriteLine($"\nSKIP: {pair} H1 data not found at {h1Path}");
                    continue;
                }

                var h1Bars = loader.Load(h1Path);
                var d1Bars = loader.Load(d1Path);

                Console.WriteLine($"\n{'=',-60}");
                Console.WriteLine($"  {pair} H1 Mean Reversion Backtest");
                Console.WriteLine($"  H1 bars: {h1Bars.Count} | D1 bars: {d1Bars.Count}");
                Console.WriteLine($"  Date range: {h1Bars.First().Time:yyyy-MM-dd} to {h1Bars.Last().Time:yyyy-MM-dd}");
                Console.WriteLine($"{'=',-60}");

                foreach (var preset in presets)
                {
                    Console.WriteLine($"\n  --- {preset.PresetName} Config ---");
                    preset.PrintSummary();
                    Console.WriteLine();

                    try
                    {
                        var engine = new MRBacktestEngine(preset);
                        var metrics = engine.Run(h1Bars, d1Bars);

                        Console.WriteLine($"  Trades: {metrics.TotalTrades} | " +
                            $"WR: {metrics.WinRate:F1}% | " +
                            $"PF: {metrics.ProfitFactor:F2} | " +
                            $"R:R: {metrics.AvgRiskReward:F2} | " +
                            $"P&L: ${metrics.TotalPnL:F2} | " +
                            $"Max DD: {metrics.MaxDrawdownPct:F2}% | " +
                            $"Sharpe: {metrics.SharpeRatio:F2} | " +
                            $"Rejected: {metrics.RejectedSignals}");

                        string goNoGo = EvaluateMRGoNoGo(metrics, preset.PresetName);
                        Console.WriteLine($"  GO/NO-GO: {goNoGo}");

                        if (metrics.Trades.Count > 0 && metrics.Trades.Count <= 50)
                        {
                            Console.WriteLine("  Trade details:");
                            for (int t = 0; t < metrics.Trades.Count; t++)
                            {
                                var tr = metrics.Trades[t];
                                Console.WriteLine($"    #{t+1} {tr.Direction} @ {tr.EntryPrice:F5} " +
                                    $"SL={tr.StopLoss:F5} Exit={tr.ExitPrice:F5} " +
                                    $"Pips={tr.Pips:F1} P&L=${tr.ProfitLoss:F2} " +
                                    $"{tr.Outcome} [{tr.ExitReason}] " +
                                    $"HoldBars={tr.ExitBarIndex - tr.EntryBarIndex}");
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"  ERROR: {ex.Message}");
                    }
                }
            }

            RunMRWalkForward(dataDir, loader);
        }

        private static void RunMRWalkForward(string dataDir, CsvDataLoader loader)
        {
            Console.WriteLine("\n\n╔══════════════════════════════════════════════╗");
            Console.WriteLine("║     WALK-FORWARD ANALYSIS (Train/Test Split)    ║");
            Console.WriteLine("╚══════════════════════════════════════════════╝");

            DateTime splitDate = new DateTime(2025, 1, 1);
            var pairs = new[] { "EURUSD", "GBPUSD" };

            foreach (var pair in pairs)
            {
                string h1Path = Path.Combine(dataDir, $"{pair}_H1.csv");
                string d1Path = Path.Combine(dataDir, $"{pair}_D1.csv");

                if (!File.Exists(h1Path)) continue;

                var allH1 = loader.Load(h1Path);
                var allD1 = loader.Load(d1Path);

                var trainH1 = allH1.Where(b => b.Time < splitDate).ToList();
                var testH1 = allH1.Where(b => b.Time >= splitDate).ToList();
                var trainD1 = allD1.Where(b => b.Time < splitDate).ToList();

                if (trainH1.Count < 100 || testH1.Count < 100) continue;

                Console.WriteLine($"\n  {pair} - Train: {trainH1.First().Time:yyyy-MM-dd} to {trainH1.Last().Time:yyyy-MM-dd} ({trainH1.Count} bars)");
                Console.WriteLine($"  {pair} - Test:  {testH1.First().Time:yyyy-MM-dd} to {testH1.Last().Time:yyyy-MM-dd} ({testH1.Count} bars)");

                foreach (var preset in MRConfig.AllPresets)
                {
                    Console.WriteLine($"\n  [{pair}] {preset.PresetName}:");

                    try
                    {
                        var trainEngine = new MRBacktestEngine(preset);
                        var trainMetrics = trainEngine.Run(trainH1, trainD1);

                        var testEngine = new MRBacktestEngine(preset);
                        var testMetrics = testEngine.Run(testH1, allD1);

                        Console.WriteLine($"    Train: WR={trainMetrics.WinRate:F1}% PF={trainMetrics.ProfitFactor:F2} " +
                            $"P&L=${trainMetrics.TotalPnL:F2} DD={trainMetrics.MaxDrawdownPct:F2}% " +
                            $"Trades={trainMetrics.TotalTrades}");

                        Console.WriteLine($"    Test:  WR={testMetrics.WinRate:F1}% PF={testMetrics.ProfitFactor:F2} " +
                            $"P&L=${testMetrics.TotalPnL:F2} DD={testMetrics.MaxDrawdownPct:F2}% " +
                            $"Trades={testMetrics.TotalTrades}");

                        bool trainOk = trainMetrics.WinRate > 40 && trainMetrics.ProfitFactor > 0.8;
                        bool testOk = testMetrics.WinRate > 40 && testMetrics.ProfitFactor > 0.8;
                        string verdict = (trainOk && testOk) ? "PASS" : (trainOk || testOk) ? "MIXED" : "FAIL";
                        Console.WriteLine($"    Verdict: {verdict}");
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"    ERROR: {ex.Message}");
                    }
                }
            }
        }

        private static string EvaluateMRGoNoGo(BacktestMetrics m, string presetName)
        {
            int score = 0;
            int total = 10;

            if (m.WinRate >= 40) score++;
            if (m.WinRate >= 50) score++;
            if (m.ProfitFactor >= 1.0) score++;
            if (m.ProfitFactor >= 1.3) score++;
            if (m.AvgRiskReward >= 1.0) score++;
            if (m.MaxDrawdownPct <= 10.0) score++;
            if (m.MaxDrawdownPct <= 5.0) score++;
            if (m.SharpeRatio >= 0.5) score++;
            if (m.TotalPnL > 0) score++;
            if (m.TotalTrades >= 20) score++;

            string rating = score >= 8 ? "GO" : score >= 6 ? "MARGINAL" : "NO-GO";

            bool passesGate = false;
            if (presetName == "Conservative")
                passesGate = m.WinRate > 50 && m.ProfitFactor > 1.0 && m.MaxDrawdownPct < 10;
            else
                passesGate = m.WinRate > 40 && m.ProfitFactor > 0.8;

            return $"{score}/{total} {rating} (Phase 1 Gate: {(passesGate ? "PASS" : "FAIL")})";
        }

        private static string EvaluateGoNoGo(BacktestMetrics m)
        {
            int score = 0;
            int total = 10;

            if (m.WinRate >= 35) score++;
            if (m.WinRate >= 45) score++;
            if (m.ProfitFactor >= 1.3) score++;
            if (m.ProfitFactor >= 1.5) score++;
            if (m.AvgRiskReward >= 1.5) score++;
            if (m.MaxDrawdownPct <= 5.0) score++;
            if (m.MaxDrawdownPct <= 3.0) score++;
            if (m.SharpeRatio >= 1.0) score++;
            if (m.TotalPnL > 0) score++;
            if (m.TotalTrades >= 30) score++;

            return $"{score}/{total} {(score >= 8 ? "GO" : score >= 6 ? "MARGINAL" : "NO-GO")}";
        }
    }
}
