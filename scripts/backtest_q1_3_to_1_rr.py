#!/usr/bin/env python3
"""
BACKTEST Q1: 3:1 R&R on EURUSD H1 M/W Formation

Question: Does EURUSD H1 M/W pattern actually produce 3:1 consistently?

Pass Criteria: >=60% of trades hitting 3:1 or better
"""

import json
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest.data_loader import CsvDataLoader  # noqa: E402
from backtest.engine import Bar, SessionType, determine_session  # noqa: E402


class SwingPoint:
    def __init__(
        self,
        index: int,
        price: float,
        is_high: bool,
        time: datetime,
        session: SessionType,
    ):
        self.index = index
        self.price = price
        self.is_high = is_high
        self.time = time
        self.session = session


class MWFormation:
    def __init__(self, points: list[SwingPoint], formation_type: str, sessions: set[SessionType]):
        self.points = points
        self.formation_type = formation_type
        self.sessions = sessions
        self.is_multi_session = len(sessions) > 1


def detect_swing_points(bars: list[Bar], lookback: int = 5) -> list[SwingPoint]:
    swing_points = []
    for i in range(lookback, len(bars) - lookback):
        is_swing_high = True
        is_swing_low = True

        for j in range(1, lookback + 1):
            if bars[i].high <= bars[i - j].high or bars[i].high <= bars[i + j].high:
                is_swing_high = False
            if bars[i].low >= bars[i - j].low or bars[i].low >= bars[i + j].low:
                is_swing_low = False

        if is_swing_high:
            swing_points.append(
                SwingPoint(
                    index=i,
                    price=bars[i].high,
                    is_high=True,
                    time=bars[i].time,
                    session=determine_session(bars[i].time),
                )
            )
        elif is_swing_low:
            swing_points.append(
                SwingPoint(
                    index=i,
                    price=bars[i].low,
                    is_high=False,
                    time=bars[i].time,
                    session=determine_session(bars[i].time),
                )
            )

    return swing_points


def find_mw_formations(
    bars: list[Bar],
    swing_points: list[SwingPoint],
    min_swing_pct: float = 0.003,
) -> list[MWFormation]:
    formations = []

    if len(swing_points) < 5:
        return formations

    for i in range(len(swing_points) - 4):
        pt1 = swing_points[i]
        pt2 = swing_points[i + 1]
        pt3 = swing_points[i + 2]
        pt4 = swing_points[i + 3]
        pt5 = swing_points[i + 4]

        if not pt1.is_high and not pt2.is_high and not pt3.is_high and not pt4.is_high and not pt5.is_high:
            continue

        w_condition = (
            pt1.price > pt3.price > pt5.price
            and abs(pt1.price - pt3.price) / pt3.price > min_swing_pct
            and abs(pt3.price - pt5.price) / pt5.price > min_swing_pct
        )

        if w_condition:
            sessions = {pt1.session, pt3.session, pt5.session}
            formations.append(
                MWFormation(
                    points=[pt1, pt2, pt3, pt4, pt5],
                    formation_type="W",
                    sessions=sessions,
                )
            )

        m_condition = (
            pt1.price < pt3.price < pt5.price
            and abs(pt3.price - pt1.price) / pt1.price > min_swing_pct
            and abs(pt5.price - pt3.price) / pt3.price > min_swing_pct
        )

        if m_condition:
            sessions = {pt1.session, pt3.session, pt5.session}
            formations.append(
                MWFormation(
                    points=[pt1, pt2, pt3, pt4, pt5],
                    formation_type="M",
                    sessions=sessions,
                )
            )

    return formations


def calculate_atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0001

    tr_sum = 0.0
    for i in range(len(bars) - period, len(bars)):
        if i > 0:
            tr = max(
                bars[i].high - bars[i].low,
                max(
                    abs(bars[i].high - bars[i - 1].close),
                    abs(bars[i].low - bars[i - 1].close),
                ),
            )
            tr_sum += tr
    return tr_sum / period


def evaluate_trade_outcome(
    bars: list[Bar],
    entry_idx: int,
    entry_direction: int,
    stop: float,
    tp1: float,
    tp2: float,
    tp3: float,
    max_hold_bars: int = 96,
) -> str | None:
    for i in range(entry_idx, min(entry_idx + max_hold_bars, len(bars))):
        bar = bars[i]

        if entry_direction == 1:
            if bar.high >= tp3:
                return "L3"
            elif bar.high >= tp2:
                return "L2"
            elif bar.high >= tp1:
                return "L1"
            elif bar.low <= stop:
                return "SL"
        else:
            if bar.low <= tp3:
                return "L3"
            elif bar.low <= tp2:
                return "L2"
            elif bar.low <= tp1:
                return "L1"
            elif bar.high >= stop:
                return "SL"

    return None


def compute_outcome_stats(
    outcomes: dict[str, int],
    rr_ratios: list[float],
) -> dict:
    total_closed = sum(v for k, v in outcomes.items() if k != "open")
    if total_closed == 0:
        return {
            "rr_3_1_hit_rate": 0.0,
            "average_rr": 0.0,
            "l1_hit_rate": 0.0,
            "l2_hit_rate": 0.0,
            "l3_hit_rate": 0.0,
            "stop_loss_rate": 0.0,
        }

    l1_hit_rate = outcomes.get("L1", 0) / total_closed
    l2_hit_rate = outcomes.get("L2", 0) / total_closed
    l3_hit_rate = outcomes.get("L3", 0) / total_closed
    stop_loss_rate = outcomes.get("SL", 0) / total_closed
    trades_with_3_1 = sum(1 for r in rr_ratios if r >= 3.0)
    rr_3_1_hit_rate = trades_with_3_1 / total_closed
    closed_rrs = [r for r in rr_ratios if r > 0.0]
    average_rr = sum(closed_rrs) / len(closed_rrs) if closed_rrs else 0.0

    return {
        "rr_3_1_hit_rate": round(rr_3_1_hit_rate, 2),
        "average_rr": round(average_rr, 2),
        "l1_hit_rate": round(l1_hit_rate, 2),
        "l2_hit_rate": round(l2_hit_rate, 2),
        "l3_hit_rate": round(l3_hit_rate, 2),
        "stop_loss_rate": round(stop_loss_rate, 2),
    }


def analyze_3_to_1_rr(bars: list[Bar]) -> dict:
    if len(bars) < 100:
        return {"error": "Insufficient data"}

    swing_points = detect_swing_points(bars)
    formations = find_mw_formations(bars, swing_points)

    if not formations:
        return {
            "question": "Q1",
            "test_period": "N/A",
            "instrument": "EURUSD",
            "timeframe": "H1",
            "sample_size": 0,
            "results": {},
            "pass": False,
            "notes": "No M/W formations detected",
        }

    atr = calculate_atr(bars)

    outcomes = {"L1": 0, "L2": 0, "L3": 0, "SL": 0, "open": 0}
    rr_ratios = []

    for formation in formations:
        last_point = formation.points[-1]
        entry_idx = last_point.index + 1

        if entry_idx >= len(bars):
            outcomes["open"] += 1
            continue

        entry_price = bars[entry_idx].close

        if formation.formation_type == "W":
            entry_direction = 1
            stop = last_point.price - atr * 1.5
            risk = entry_price - stop
            if risk <= 0:
                continue
            tp1 = entry_price + risk * 1
            tp2 = entry_price + risk * 2
            tp3 = entry_price + risk * 3
        else:
            entry_direction = -1
            stop = last_point.price + atr * 1.5
            risk = stop - entry_price
            if risk <= 0:
                continue
            tp1 = entry_price - risk * 1
            tp2 = entry_price - risk * 2
            tp3 = entry_price - risk * 3

        outcome = evaluate_trade_outcome(bars, entry_idx, entry_direction, stop, tp1, tp2, tp3)

        if outcome is None:
            outcomes["open"] += 1
            continue

        outcomes[outcome] += 1
        if outcome == "L3":
            rr_ratios.append(3.0)
        elif outcome == "L2":
            rr_ratios.append(2.0)
        elif outcome == "L1":
            rr_ratios.append(1.0)
        else:
            rr_ratios.append(0.0)

    start_date = bars[0].time.strftime("%Y-%m-%d")
    end_date = bars[-1].time.strftime("%Y-%m-%d")
    stats = compute_outcome_stats(outcomes, rr_ratios)

    results = {
        "question": "Q1",
        "test_period": f"{start_date} to {end_date}",
        "instrument": "EURUSD",
        "timeframe": "H1",
        "sample_size": len(formations),
        "results": stats,
        "pass": stats["rr_3_1_hit_rate"] >= 0.60,
        "notes": (
            f"{stats['rr_3_1_hit_rate'] * 100:.0f}% of trades hit 3:1 or better. "
            f"Average R:R was {stats['average_rr']:.2f}. "
            f"Sample size: {len(formations)} M/W formations. "
            f"Pass criteria: >=60% at 3:1."
        ),
    }

    return results


def main():
    data_dir = project_root / "data" / "forex" / "historical"
    eurusd_file = data_dir / "EURUSD_H1.csv"

    loader = CsvDataLoader()
    bars = loader.load(str(eurusd_file))

    if len(bars) < 100:
        print("Error: Insufficient EURUSD H1 data")
        return

    results = analyze_3_to_1_rr(bars)

    output_file = project_root / "reports" / "backtest_q1_3_to_1_rr.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(results, indent=2))

    print(json.dumps(results, indent=2))
    print(f"\nResults saved to {output_file}")


if __name__ == "__main__":
    main()
