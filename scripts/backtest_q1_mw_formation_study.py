#!/usr/bin/env python3
"""
BACKTEST Q1: M/W Formation 3:1 R&R Study

Question: Does EURUSD H1 M/W pattern produce 3:1 Risk:Reward consistently?

Pass Criteria: >=60% hit rate at 3:1

Methodology:
- M/W pattern detection using MWPatternDetector (swing-based)
- Entry: bar after formation completes (pt5 + 1)
- SL: 1.5x ATR below/above neckline
- TP levels: 1R, 2R, 3R from entry
- Exit: check TP3 first, then TP2, TP1, then SL

Implements: Q1BacktestStudy(StatisticalStudy)
Framework: StatisticalStudy base class from AYUAA-641
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest.data_loader import CsvDataLoader  # noqa: E402
from backtest.engine import Bar  # noqa: E402
from backtest.pattern_detector import MWPattern, MWPatternDetector  # noqa: E402
from backtest.statistical_study import (  # noqa: E402
    GoNoGoCriteria,
    StatisticalStudy,
)


class Q1BacktestStudy(StatisticalStudy):
    SWING_LOOKBACK = 5
    MIN_DEPTH_ATR = 0.5
    MIN_BAR_SPAN = 10
    MAX_BAR_SPAN = 200
    SYMMETRY_TOLERANCE = 0.30
    ATR_PERIOD = 14
    MAX_BARS_AHEAD = 96

    def __init__(self):
        super().__init__(
            question_id="Q1",
            instrument="EURUSD",
            timeframe="H1",
            go_nogo_criteria=[
                GoNoGoCriteria(metric="rr_3_1_hit_rate", threshold=0.60, operator=">="),
            ],
        )
        self.detector = MWPatternDetector(
            swing_lookback=self.SWING_LOOKBACK,
            symmetry_tolerance=self.SYMMETRY_TOLERANCE,
            min_depth_atr=self.MIN_DEPTH_ATR,
            min_bar_span=self.MIN_BAR_SPAN,
            max_bar_span=self.MAX_BAR_SPAN,
        )

    def analyze(self, bars: list[Bar]) -> dict:
        atr_values = self.compute_atr(bars, period=self.ATR_PERIOD)
        patterns = self.detector.detect(bars, atr_values)

        if not patterns:
            return {
                "sample_size": 0,
                "rr_3_1_hit_rate": 0.0,
                "average_rr": 0.0,
                "l1_hit_rate": 0.0,
                "l2_hit_rate": 0.0,
                "l3_hit_rate": 0.0,
                "stop_loss_rate": 0.0,
            }

        outcomes: dict[str, int] = {"L1": 0, "L2": 0, "L3": 0, "SL": 0, "open": 0}
        closed_rrs: list[float] = []
        trades_with_3_1_or_better = 0

        for pattern in patterns:
            result = self._evaluate_pattern(pattern, bars, atr_values)
            outcome = result["outcome"]
            rr = result["rr"]

            outcomes[outcome] = outcomes.get(outcome, 0) + 1

            if outcome != "open":
                closed_rrs.append(rr)
                if rr >= 3.0:
                    trades_with_3_1_or_better += 1

        total_closed = sum(v for k, v in outcomes.items() if k != "open")
        sample_size = len(patterns)

        l1_hit_rate = outcomes.get("L1", 0) / total_closed if total_closed > 0 else 0.0
        l2_hit_rate = outcomes.get("L2", 0) / total_closed if total_closed > 0 else 0.0
        l3_hit_rate = outcomes.get("L3", 0) / total_closed if total_closed > 0 else 0.0
        stop_loss_rate = outcomes.get("SL", 0) / total_closed if total_closed > 0 else 0.0
        rr_3_1_hit_rate = trades_with_3_1_or_better / total_closed if total_closed > 0 else 0.0
        average_rr = sum(closed_rrs) / len(closed_rrs) if closed_rrs else 0.0

        return {
            "sample_size": sample_size,
            "rr_3_1_hit_rate": round(rr_3_1_hit_rate, 4),
            "average_rr": round(average_rr, 4),
            "l1_hit_rate": round(l1_hit_rate, 4),
            "l2_hit_rate": round(l2_hit_rate, 4),
            "l3_hit_rate": round(l3_hit_rate, 4),
            "stop_loss_rate": round(stop_loss_rate, 4),
        }

    def _evaluate_pattern(self, pattern: MWPattern, bars: list[Bar], atr_values: list[float]) -> dict:
        entry_idx = pattern.right_shoulder_idx + 1
        if entry_idx >= len(bars):
            return {"outcome": "open", "rr": 0.0}

        entry_price = bars[entry_idx].close
        atr = atr_values[entry_idx] if entry_idx < len(atr_values) else 0.0001

        if pattern.is_bullish:
            return self._evaluate_long(pattern, bars, entry_idx, entry_price, atr)
        else:
            return self._evaluate_short(pattern, bars, entry_idx, entry_price, atr)

    def _evaluate_long(
        self,
        pattern: MWPattern,
        bars: list[Bar],
        entry_idx: int,
        entry_price: float,
        atr: float,
    ) -> dict:
        neckline = pattern.neckline_level
        stop = neckline - atr * 1.5
        risk = entry_price - stop
        if risk <= 0:
            return {"outcome": "open", "rr": 0.0}

        tp1 = entry_price + risk * 1
        tp2 = entry_price + risk * 2
        tp3 = entry_price + risk * 3

        for i in range(entry_idx + 1, min(entry_idx + self.MAX_BARS_AHEAD, len(bars))):
            bar = bars[i]
            if bar.high >= tp3:
                return {"outcome": "L3", "rr": 3.0}
            elif bar.high >= tp2:
                return {"outcome": "L2", "rr": 2.0}
            elif bar.high >= tp1:
                return {"outcome": "L1", "rr": 1.0}
            elif bar.low <= stop:
                return {"outcome": "SL", "rr": 0.0}

        return {"outcome": "open", "rr": 0.0}

    def _evaluate_short(
        self,
        pattern: MWPattern,
        bars: list[Bar],
        entry_idx: int,
        entry_price: float,
        atr: float,
    ) -> dict:
        neckline = pattern.neckline_level
        stop = neckline + atr * 1.5
        risk = stop - entry_price
        if risk <= 0:
            return {"outcome": "open", "rr": 0.0}

        tp1 = entry_price - risk * 1
        tp2 = entry_price - risk * 2
        tp3 = entry_price - risk * 3

        for i in range(entry_idx + 1, min(entry_idx + self.MAX_BARS_AHEAD, len(bars))):
            bar = bars[i]
            if bar.low <= tp3:
                return {"outcome": "L3", "rr": 3.0}
            elif bar.low <= tp2:
                return {"outcome": "L2", "rr": 2.0}
            elif bar.low <= tp1:
                return {"outcome": "L1", "rr": 1.0}
            elif bar.high >= stop:
                return {"outcome": "SL", "rr": 0.0}

        return {"outcome": "open", "rr": 0.0}


def main():
    data_path = Path(__file__).parent.parent / "data" / "forex" / "historical" / "EURUSD_H1.csv"
    report_path = Path(__file__).parent.parent / "reports" / "backtest_q1_mw_formation.json"

    loader = CsvDataLoader()
    bars = loader.load(str(data_path))

    if len(bars) < 100:
        print("Error: Insufficient EURUSD H1 data")
        sys.exit(1)

    study = Q1BacktestStudy()
    result = study.run(bars)

    print(result.to_json())
    print(f"\nResults saved to {report_path}")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(str(report_path))


if __name__ == "__main__":
    main()
