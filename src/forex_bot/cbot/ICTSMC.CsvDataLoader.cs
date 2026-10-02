using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;

namespace ICTSMC
{
    public class CsvDataLoader
    {
        private const string DefaultDateFormat = "yyyy-MM-dd HH:mm";
        private const string AlternativeDateFormat = "MM/dd/yyyy HH:mm";

        public List<Bar> Load(string filePath, TimeFrame timeFrame = default)
        {
            if (!File.Exists(filePath))
                throw new FileNotFoundException($"CSV file not found: {filePath}");

            var lines = File.ReadAllLines(filePath);
            return ParseLines(lines, timeFrame);
        }

        public List<Bar> LoadFromString(string csvContent, TimeFrame timeFrame = default)
        {
            var lines = csvContent.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
            return ParseLines(lines, timeFrame);
        }

        private List<Bar> ParseLines(string[] lines, TimeFrame timeFrame)
        {
            if (lines.Length < 2)
                throw new InvalidDataException("CSV must have a header row and at least one data row.");

            var headers = lines[0].Split(new[] { ',', ';', '\t' },
                StringSplitOptions.RemoveEmptyEntries);

            int dateCol = FindColumn(headers, "date", "time");
            int openCol = FindColumn(headers, "open");
            int highCol = FindColumn(headers, "high");
            int lowCol = FindColumn(headers, "low");
            int closeCol = FindColumn(headers, "close");
            int volumeCol = FindColumn(headers, "volume", "vol");

            bool hasSeparateTime = headers.Any(h =>
                h.Trim().ToLowerInvariant() == "time");

            var bars = new List<Bar>();
            var culture = CultureInfo.InvariantCulture;

            for (int i = 1; i < lines.Length; i++)
            {
                string line = lines[i].Trim();
                if (string.IsNullOrEmpty(line) || line.StartsWith("#") || line.StartsWith("//"))
                    continue;

                var cols = line.Split(new[] { ',', ';', '\t' },
                    StringSplitOptions.RemoveEmptyEntries);

                if (cols.Length < 5) continue;

                DateTime time;
                if (hasSeparateTime)
                {
                    int timeColIdx = FindColumn(headers, "time");
                    string dateStr = cols[dateCol].Trim();
                    string timeStr = cols.Length > timeColIdx ? cols[timeColIdx].Trim() : "00:00";
                    time = ParseDateTime(dateStr + " " + timeStr);
                }
                else
                {
                    time = ParseDateTime(cols[dateCol].Trim());
                }

                if (!double.TryParse(cols[openCol].Trim(), NumberStyles.Any, culture, out double open))
                    continue;
                if (!double.TryParse(cols[highCol].Trim(), NumberStyles.Any, culture, out double high))
                    continue;
                if (!double.TryParse(cols[lowCol].Trim(), NumberStyles.Any, culture, out double low))
                    continue;
                if (!double.TryParse(cols[closeCol].Trim(), NumberStyles.Any, culture, out double close))
                    continue;

                double volume = 0;
                if (volumeCol >= 0 && volumeCol < cols.Length)
                    double.TryParse(cols[volumeCol].Trim(), NumberStyles.Any, culture, out volume);

                bars.Add(new Bar
                {
                    Time = time,
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Volume = volume,
                    Period = timeFrame.Minutes > 0 ? timeFrame : InferTimeFrame(bars, time)
                });
            }

            if (bars.Count > 1)
            {
                bars.Sort((a, b) => a.Time.CompareTo(b.Time));
                if (timeFrame.Minutes == 0)
                {
                    var inferred = InferTimeFrameFromBars(bars);
                    for (int i = 0; i < bars.Count; i++)
                        bars[i] = new Bar
                        {
                            Time = bars[i].Time,
                            Open = bars[i].Open,
                            High = bars[i].High,
                            Low = bars[i].Low,
                            Close = bars[i].Close,
                            Volume = bars[i].Volume,
                            Period = inferred
                        };
                }
            }

            return bars;
        }

        private static int FindColumn(string[] headers, params string[] names)
        {
            for (int i = 0; i < headers.Length; i++)
            {
                string h = headers[i].Trim().ToLowerInvariant().Replace("\"", "");
                foreach (string name in names)
                {
                    if (h == name || h.Contains(name))
                        return i;
                }
            }
            return -1;
        }

        private static DateTime ParseDateTime(string dateStr)
        {
            if (DateTime.TryParseExact(dateStr, DefaultDateFormat,
                CultureInfo.InvariantCulture, DateTimeStyles.None, out var dt))
                return dt;

            if (DateTime.TryParseExact(dateStr, AlternativeDateFormat,
                CultureInfo.InvariantCulture, DateTimeStyles.None, out var dt2))
                return dt2;

            if (DateTime.TryParse(dateStr, CultureInfo.InvariantCulture,
                DateTimeStyles.None, out var dt3))
                return dt3;

            throw new FormatException($"Cannot parse date: {dateStr}");
        }

        private static TimeFrame InferTimeFrame(List<Bar> existingBars, DateTime current)
        {
            if (existingBars.Count == 0)
                return TimeFrame.M15;

            var last = existingBars[existingBars.Count - 1];
            double diffMinutes = (current - last.Time).TotalMinutes;

            if (diffMinutes < 1) return TimeFrame.M15;
            if (diffMinutes <= 20) return TimeFrame.M15;
            if (diffMinutes <= 70) return TimeFrame.H1;
            if (diffMinutes <= 300) return TimeFrame.H4;
            return TimeFrame.D1;
        }

        private static TimeFrame InferTimeFrameFromBars(List<Bar> bars)
        {
            if (bars.Count < 2) return TimeFrame.M15;

            var diffs = new List<double>();
            for (int i = 1; i < Math.Min(bars.Count, 50); i++)
            {
                double diff = (bars[i].Time - bars[i - 1].Time).TotalMinutes;
                if (diff > 0) diffs.Add(diff);
            }

            if (diffs.Count == 0) return TimeFrame.M15;

            double median = diffs.OrderBy(d => d).ElementAt(diffs.Count / 2);

            if (median <= 20) return TimeFrame.M15;
            if (median <= 70) return TimeFrame.H1;
            if (median <= 300) return TimeFrame.H4;
            return TimeFrame.D1;
        }

        public static void GenerateSampleCsv(string filePath, int barCount = 500)
        {
            var rng = new Random(12345);
            double price = 1.1000;
            var baseTime = new DateTime(2026, 1, 5, 0, 0, 0, DateTimeKind.Utc);
            double vol = 0.0008;

            using var writer = new StreamWriter(filePath);
            writer.WriteLine("Date,Open,High,Low,Close,Volume");

            for (int i = 0; i < barCount; i++)
            {
                double trendBias = Math.Sin(i * 0.02) * vol * 0.2;
                double noise = (rng.NextDouble() - 0.5) * vol * 2;
                double momentum = (rng.NextDouble() - 0.5) * vol * 0.3;
                double change = trendBias + noise + momentum;

                double open = price;
                double close = price + change;
                double high = Math.Max(open, close) + rng.NextDouble() * vol * 0.5;
                double low = Math.Min(open, close) - rng.NextDouble() * vol * 0.5;

                string time = baseTime.AddMinutes(i * 15).ToString("yyyy-MM-dd HH:mm");
                writer.WriteLine($"{time},{open:F5},{high:F5},{low:F5},{close:F5},{(int)(1000 + rng.NextDouble() * 5000)}");

                price = close;
            }
        }
    }
}
