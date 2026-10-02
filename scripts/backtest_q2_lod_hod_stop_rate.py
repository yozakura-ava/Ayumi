#!/usr/bin/env python3
"""
BACKTEST Q2: LOD/HOD Stop Hit Rate Analysis

Analyzes what percentage of trades get stopped at the daily LOD/HOD level
vs intrabar volatility spikes. Measures slippage and optimal stop buffer.

Uses the StatisticalStudy framework from backtest.statistical_study.
"""

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "forex_bot"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd
from backtest.engine import Bar
from backtest.statistical_study import (
    GoNoGoCriteria,
    StatisticalStudy,
    StatisticalStudyResult,
)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "forex" / "parquet" / "EURUSD_1h.parquet"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "reports" / "backtest_q2_lod_hod_stop_rate.json"

PIP_VALUE = 0.0001


@dataclass
class DailyLevel:
    date: str
    lod: float
    lod_idx: int
    hod: float
    hod_idx: int
    bars: list[Bar]


@dataclass
class TradeResult:
    entry_idx: int
    entry_price: float
    direction: str
    stop_price: float
    tp_price: float
    lod_or_hod_level: float
    exit_reason: str
    exit_price: float
    exit_idx: int
    slippage_pips: float
    hit_type: str


def load_bars(filepath: str | Path) -> list[Bar]:
    df = pd.read_parquet(filepath)
    df = df.sort_index()
    bars = []
    for ts, row in df.iterrows():
        bars.append(
            Bar(
                time=ts.to_pydatetime(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row.get("Volume", 0)),
            )
        )
    return bars


def group_by_day(bars: list[Bar]) -> list[DailyLevel]:
    days: dict[str, list[Bar]] = {}
    for bar in bars:
        key = bar.time.strftime("%Y-%m-%d")
        days.setdefault(key, []).append(bar)

    result = []
    for date_str in sorted(days.keys()):
        day_bars = days[date_str]
        lod = min(b.low for b in day_bars)
        lod_idx = next(i for i, b in enumerate(day_bars) if b.low == lod)
        hod = max(b.high for b in day_bars)
        hod_idx = next(i for i, b in enumerate(day_bars) if b.high == hod)
        result.append(
            DailyLevel(
                date=date_str,
                lod=lod,
                lod_idx=lod_idx,
                hod=hod,
                hod_idx=hod_idx,
                bars=day_bars,
            )
        )
    return result


def simulate_trade(
    bars: list[Bar],
    entry_idx: int,
    direction: str,
    stop_level: float,
    risk_pips: float,
    rr_ratio: float = 3.0,
    max_hold_bars: int = 48,
) -> TradeResult:
    entry_bar = bars[entry_idx]
    entry_price = entry_bar.close

    if direction == "long":
        stop_price = stop_level
        tp_price = entry_price + (risk_pips * PIP_VALUE * rr_ratio)
    else:
        stop_price = stop_level
        tp_price = entry_price - (risk_pips * PIP_VALUE * rr_ratio)

    end_idx = min(entry_idx + max_hold_bars, len(bars))

    for i in range(entry_idx + 1, end_idx):
        bar = bars[i]

        if direction == "long":
            if bar.low <= stop_price:
                exit_price = stop_price
                slippage = max(0, stop_price - bar.low) / PIP_VALUE
                hit_type = classify_stop_hit(bar, stop_price, direction)
                return TradeResult(
                    entry_idx=entry_idx,
                    entry_price=entry_price,
                    direction=direction,
                    stop_price=stop_price,
                    tp_price=tp_price,
                    lod_or_hod_level=stop_level,
                    exit_reason="stop_loss",
                    exit_price=exit_price,
                    exit_idx=i,
                    slippage_pips=round(slippage, 2),
                    hit_type=hit_type,
                )
            if bar.high >= tp_price:
                return TradeResult(
                    entry_idx=entry_idx,
                    entry_price=entry_price,
                    direction=direction,
                    stop_price=stop_price,
                    tp_price=tp_price,
                    lod_or_hod_level=stop_level,
                    exit_reason="take_profit",
                    exit_price=tp_price,
                    exit_idx=i,
                    slippage_pips=0.0,
                    hit_type="none",
                )
        else:
            if bar.high >= stop_price:
                exit_price = stop_price
                slippage = max(0, bar.high - stop_price) / PIP_VALUE
                hit_type = classify_stop_hit(bar, stop_price, direction)
                return TradeResult(
                    entry_idx=entry_idx,
                    entry_price=entry_price,
                    direction=direction,
                    stop_price=stop_price,
                    tp_price=tp_price,
                    lod_or_hod_level=stop_level,
                    exit_reason="stop_loss",
                    exit_price=exit_price,
                    exit_idx=i,
                    slippage_pips=round(slippage, 2),
                    hit_type=hit_type,
                )
            if bar.low <= tp_price:
                return TradeResult(
                    entry_idx=entry_idx,
                    entry_price=entry_price,
                    direction=direction,
                    stop_price=stop_price,
                    tp_price=tp_price,
                    lod_or_hod_level=stop_level,
                    exit_reason="take_profit",
                    exit_price=tp_price,
                    exit_idx=i,
                    slippage_pips=0.0,
                    hit_type="none",
                )

    return TradeResult(
        entry_idx=entry_idx,
        entry_price=entry_price,
        direction=direction,
        stop_price=stop_price,
        tp_price=tp_price,
        lod_or_hod_level=stop_level,
        exit_reason="open",
        exit_price=entry_price,
        exit_idx=end_idx - 1,
        slippage_pips=0.0,
        hit_type="none",
    )


def classify_stop_hit(bar: Bar, stop_level: float, direction: str) -> str:
    threshold_pips = 2.0
    threshold = threshold_pips * PIP_VALUE

    if direction == "long":
        penetration = stop_level - bar.low
    else:
        penetration = bar.high - stop_level

    if penetration <= threshold:
        return "lod_hod_exact"
    else:
        return "intrabar_spike"


class Q2LODHODStudy(StatisticalStudy):
    def __init__(self, rr_ratio: float = 3.0):
        criteria = [
            GoNoGoCriteria(
                metric="net_expectancy_r",
                threshold=0.0,
                operator=">",
                weight=0.5,
            ),
            GoNoGoCriteria(
                metric="profit_factor",
                threshold=1.0,
                operator=">=",
                weight=0.5,
            ),
        ]
        super().__init__(
            question_id="Q2",
            instrument="EURUSD",
            timeframe="H1",
            go_nogo_criteria=criteria,
        )
        self.rr_ratio = rr_ratio

    def analyze(self, bars: list[Bar]) -> dict[str, Any]:
        daily_levels = group_by_day(bars)

        trades: list[TradeResult] = []

        for i in range(1, len(daily_levels)):
            prev_day = daily_levels[i - 1]
            curr_day = daily_levels[i]

            prev_range_pips = (prev_day.hod - prev_day.lod) / PIP_VALUE
            if prev_range_pips < 30:
                continue

            day_bars = curr_day.bars
            if len(day_bars) < 6:
                continue

            setup_bars = day_bars[:4]
            setup_high = max(b.high for b in setup_bars)
            setup_low = min(b.low for b in setup_bars)
            setup_range_pips = (setup_high - setup_low) / PIP_VALUE

            entry_idx = sum(len(daily_levels[j].bars) for j in range(i))

            for k in range(4, min(len(day_bars), 20)):
                bar = day_bars[k]
                bar_global_idx = entry_idx + k

                if bar.close > setup_high and setup_range_pips >= 5:
                    risk_pips = setup_range_pips
                    if 5 <= risk_pips <= 50:
                        trade = simulate_trade(
                            bars=bars,
                            entry_idx=bar_global_idx,
                            direction="long",
                            stop_level=prev_day.lod,
                            risk_pips=risk_pips,
                            rr_ratio=self.rr_ratio,
                        )
                        trades.append(trade)
                    break

                if bar.close < setup_low and setup_range_pips >= 5:
                    risk_pips = setup_range_pips
                    if 5 <= risk_pips <= 50:
                        trade = simulate_trade(
                            bars=bars,
                            entry_idx=bar_global_idx,
                            direction="short",
                            stop_level=prev_day.hod,
                            risk_pips=risk_pips,
                            rr_ratio=self.rr_ratio,
                        )
                        trades.append(trade)
                    break

        closed_trades = [t for t in trades if t.exit_reason != "open"]
        sl_trades = [t for t in closed_trades if t.exit_reason == "stop_loss"]
        tp_trades = [t for t in closed_trades if t.exit_reason == "take_profit"]

        lod_hod_hits = [t for t in sl_trades if t.hit_type == "lod_hod_exact"]
        intrabar_hits = [t for t in sl_trades if t.hit_type == "intrabar_spike"]

        stop_loss_rate = len(sl_trades) / len(closed_trades) if closed_trades else 0
        lod_hod_rate = len(lod_hod_hits) / len(sl_trades) if sl_trades else 0
        intrabar_rate = len(intrabar_hits) / len(sl_trades) if sl_trades else 0
        be_trades = len(closed_trades) - len(sl_trades) - len(tp_trades)
        be_rate = be_trades / len(closed_trades) if closed_trades else 0

        avg_slippage = 0.0
        if intrabar_hits:
            avg_slippage = sum(t.slippage_pips for t in intrabar_hits) / len(intrabar_hits)

        optimal_buffer = compute_optimal_buffer(sl_trades)

        total_wins_r = len(tp_trades) * self.rr_ratio
        total_losses_r = len(sl_trades)
        profit_factor = total_wins_r / total_losses_r if total_losses_r > 0 else 0.0

        tp_rate = len(tp_trades) / len(closed_trades) if closed_trades else 0
        net_expectancy_r = (tp_rate * self.rr_ratio) - (stop_loss_rate * 1.0)

        return {
            "sample_size": len(closed_trades),
            "total_trades": len(trades),
            "stop_loss_rate": round(stop_loss_rate, 4),
            "lod_hod_stop_rate": round(lod_hod_rate, 4),
            "intrabar_spike_rate": round(intrabar_rate, 4),
            "breakeven_rate": round(be_rate, 4),
            "avg_slippage_intrabar_pips": round(avg_slippage, 2),
            "optimal_buffer_pips": round(optimal_buffer, 1),
            "sl_count": len(sl_trades),
            "tp_count": len(tp_trades),
            "tp_rate": round(tp_rate, 4),
            "profit_factor": round(profit_factor, 4),
            "net_expectancy_r": round(net_expectancy_r, 4),
            "rr_ratio": self.rr_ratio,
        }


def compute_optimal_buffer(sl_trades: list[TradeResult]) -> float:
    if not sl_trades:
        return 0.0

    penetrations = [t.slippage_pips for t in sl_trades if t.slippage_pips > 0]

    if not penetrations:
        return 0.0

    return float(sorted(penetrations)[len(penetrations) // 2])


def run_study() -> StatisticalStudyResult:
    bars = load_bars(DATA_PATH)

    start = datetime(2023, 6, 22)
    end = datetime(2026, 4, 8)
    filtered = [b for b in bars if start <= b.time <= end]

    print(f"Loaded {len(filtered)} bars from {filtered[0].time} to {filtered[-1].time}")

    study = Q2LODHODStudy(rr_ratio=3.0)
    result = study.run(filtered)

    print("=" * 60)
    print("BACKTEST Q2: LOD/HOD Stop Hit Rate Analysis")
    print("=" * 60)
    print(f"Test Period: {result.test_period_start} to {result.test_period_end}")
    print(f"Instrument: {result.instrument}")
    print(f"Timeframe: {result.timeframe}")
    print(f"Sample Size: {result.sample_size}")
    print("\nResults:")
    for k, v in result.results.items():
        print(f"  {k}: {v}")
    print(f"\nGO/NO-GO: {'PASS' if result.go_nogo else 'FAIL'}")
    print(f"Notes: {result.notes}")
    print("=" * 60)

    result.save(OUTPUT_PATH)
    print(f"\nResults saved to: {OUTPUT_PATH}")

    return result


if __name__ == "__main__":
    run_study()
