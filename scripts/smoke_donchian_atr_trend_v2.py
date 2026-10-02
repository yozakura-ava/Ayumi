"""Smoke test: run Donchian ATR Trend v2 on XAUUSD M15 last 5000 bars.

Reads bar data from DuckDB at data/ayumi_market.duckdb (NEVER CSV).
Direct O(n) loop with pre-computed indicators. Simple trailing-stop
simulation with TP1/TP2/TP3 hit logic.

Usage:
    ulimit -v 2097152 && python3 scripts/smoke_donchian_atr_trend_v2.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Make strategies importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "forex_bot"))

from core.types import Bar, MarketState, SessionType, TradeDirection  # noqa: E402, I001
from strategies.donchian_atr_trend_v2 import (  # noqa: E402
    DonchianATRConfig,
    DonchianATRTrendV2Strategy,
)
from utils.pip_value import pip_value_for_symbol  # noqa: E402

DB_PATH = ROOT / "data" / "ayumi_market.duckdb"


@dataclass
class Trade:
    direction: TradeDirection
    entry_price: float
    exit_price: float
    pips: float
    profit: float
    exit_reason: str
    entry_time: datetime
    exit_time: datetime
    bars_held: int


def load_xauusd_m15_last_n(n: int = 5000) -> list[Bar]:
    """Read last N M15 bars for XAUUSD from DuckDB."""
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT timestamp_utc, open, high, low, close, volume, spread_pips
            FROM bars
            WHERE symbol = 'XAUUSD' AND timeframe = 'M15'
            ORDER BY timestamp_utc DESC
            LIMIT ?
            """,
            [n],
        ).fetchall()
    finally:
        con.close()

    rows = list(reversed(rows))
    bars: list[Bar] = []
    for ts_utc, o, h, l, c, vol, spread in rows:  # noqa: E741
        bars.append(
            Bar(
                time=datetime.fromtimestamp(int(ts_utc), tz=timezone.utc).replace(tzinfo=None),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(vol or 0),
                spread_pips=float(spread or 0),
            )
        )
    return bars


def main():
    print("=" * 60, flush=True)
    print("Donchian ATR Trailing Trend v2 — Smoke Test on XAUUSD M15", flush=True)
    print("=" * 60, flush=True)

    print("Loading last 5000 bars from DuckDB...", flush=True)
    bars = load_xauusd_m15_last_n(5000)
    print(f"Loaded {len(bars)} bars. Range: {bars[0].time} -> {bars[-1].time}", flush=True)

    cfg = DonchianATRConfig(symbol="XAUUSD")
    print(f"Config: {cfg}", flush=True)

    strategy = DonchianATRTrendV2Strategy(cfg)
    pip = pip_value_for_symbol(cfg.symbol)

    # Direct loop: walk bar-by-bar, accumulate state, fire signals
    # via strategy.evaluate, manage exits inline. Strategy state is
    # preserved across calls (cooldown, etc.).
    print("Running backtest...", flush=True)

    signal_count = 0
    trades: list[Trade] = []
    open_pos: dict | None = None  # one-at-a-time

    # Iterate bar-by-bar feeding growing history
    for i in range(len(bars)):
        bar = bars[i]

        # Update open position (if any) using the strategy's exit logic
        if open_pos is not None:
            entry_price = open_pos["entry_price"]
            stop = open_pos["stop"]
            trail_mult = cfg.atr_trail_multiplier
            direction = open_pos["direction"]

            # Compute bar-level ATR for trailing (Wilder)
            atr_trail = open_pos["atr_at_entry"]
            if i >= cfg.atr_period + 1:
                tr_sum = 0.0
                for k in range(i - cfg.atr_period, i):
                    tr = max(
                        bars[k + 1].high - bars[k + 1].low,
                        abs(bars[k + 1].high - bars[k].close),
                        abs(bars[k + 1].low - bars[k].close),
                    )
                    tr_sum += tr
                atr_trail = tr_sum / cfg.atr_period

            if direction == TradeDirection.LONG:
                # Trail
                if bar.high > entry_price:
                    new_stop = bar.high - trail_mult * atr_trail
                    if new_stop > stop:
                        stop = new_stop
                        open_pos["stop"] = stop
                # SL hit
                if bar.low <= stop:
                    exit_price = stop
                    exit_reason = "stop_loss"
                # Trend break (close < DC low over last N)
                elif i >= cfg.donchian_period + 1:
                    dc_low = min(b.low for b in bars[i - cfg.donchian_period : i + 1])
                    if bar.close < dc_low:
                        exit_price = bar.close
                        exit_reason = "trend_break"
                    # TPs
                    elif bar.high >= cfg.tp3_rr and open_pos["tp3"]:
                        exit_price = open_pos["tp3"]
                        exit_reason = "tp3"
                    elif bar.high >= open_pos["tp2"]:
                        exit_price = open_pos["tp2"]
                        exit_reason = "tp2"
                    elif bar.high >= open_pos["tp1"]:
                        exit_price = open_pos["tp1"]
                        exit_reason = "tp1"
                    else:
                        exit_price = None
                else:
                    # TPs
                    if bar.high >= cfg.tp3_rr and open_pos["tp3"]:
                        exit_price = open_pos["tp3"]
                        exit_reason = "tp3"
                    elif bar.high >= open_pos["tp2"]:
                        exit_price = open_pos["tp2"]
                        exit_reason = "tp2"
                    elif bar.high >= open_pos["tp1"]:
                        exit_price = open_pos["tp1"]
                        exit_reason = "tp1"
                    else:
                        exit_price = None
            else:  # SHORT
                if bar.low < entry_price:
                    new_stop = bar.low + trail_mult * atr_trail
                    if new_stop < stop:
                        stop = new_stop
                        open_pos["stop"] = stop
                if bar.high >= stop:
                    exit_price = stop
                    exit_reason = "stop_loss"
                elif i >= cfg.donchian_period + 1:
                    dc_high = max(b.high for b in bars[i - cfg.donchian_period : i + 1])
                    if bar.close > dc_high:
                        exit_price = bar.close
                        exit_reason = "trend_break"
                    elif bar.low <= open_pos["tp3"] and open_pos["tp3"]:
                        exit_price = open_pos["tp3"]
                        exit_reason = "tp3"
                    elif bar.low <= open_pos["tp2"]:
                        exit_price = open_pos["tp2"]
                        exit_reason = "tp2"
                    elif bar.low <= open_pos["tp1"]:
                        exit_price = open_pos["tp1"]
                        exit_reason = "tp1"
                    else:
                        exit_price = None
                else:
                    if bar.low <= open_pos["tp3"] and open_pos["tp3"]:
                        exit_price = open_pos["tp3"]
                        exit_reason = "tp3"
                    elif bar.low <= open_pos["tp2"]:
                        exit_price = open_pos["tp2"]
                        exit_reason = "tp2"
                    elif bar.low <= open_pos["tp1"]:
                        exit_price = open_pos["tp1"]
                        exit_reason = "tp1"
                    else:
                        exit_price = None

            if exit_price is not None:
                if direction == TradeDirection.LONG:
                    pips = (exit_price - entry_price) / pip
                    profit = exit_price - entry_price
                else:
                    pips = (entry_price - exit_price) / pip
                    profit = entry_price - exit_price
                trades.append(
                    Trade(
                        direction=direction,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        pips=pips,
                        profit=profit,
                        exit_reason=exit_reason,
                        entry_time=open_pos["entry_time"],
                        exit_time=bar.time,
                        bars_held=i - open_pos["entry_idx"],
                    )
                )
                open_pos = None

        # Evaluate for new entry (only if no position open)
        if open_pos is None and i >= cfg.min_bars_for_setup:
            # Feed the bar to the strategy's incremental buffer first.
            strategy.on_bar(bar)
            state = MarketState(bars=bars[: i + 1], current_session=SessionType.LONDON)
            signal = strategy.evaluate(state)
            if signal is not None:
                signal_count += 1
                # Compute ATR at this bar for trailing later
                atr_at_entry = 0.0
                if i >= cfg.atr_period + 1:
                    tr_sum = 0.0
                    for k in range(i - cfg.atr_period, i):
                        tr = max(
                            bars[k + 1].high - bars[k + 1].low,
                            abs(bars[k + 1].high - bars[k].close),
                            abs(bars[k + 1].low - bars[k].close),
                        )
                        tr_sum += tr
                    atr_at_entry = tr_sum / cfg.atr_period

                open_pos = {
                    "direction": signal.direction,
                    "entry_price": signal.entry_price,
                    "stop": signal.stop_loss,
                    "tp1": signal.take_profit_1,
                    "tp2": signal.take_profit_2,
                    "tp3": signal.take_profit_3,
                    "entry_time": bar.time,
                    "entry_idx": i,
                    "atr_at_entry": atr_at_entry,
                }

    # Close any open position at the end
    if open_pos is not None:
        exit_price = bars[-1].close
        if open_pos["direction"] == TradeDirection.LONG:
            pips = (exit_price - open_pos["entry_price"]) / pip
            profit = exit_price - open_pos["entry_price"]
        else:
            pips = (open_pos["entry_price"] - exit_price) / pip
            profit = open_pos["entry_price"] - exit_price
        trades.append(
            Trade(
                direction=open_pos["direction"],
                entry_price=open_pos["entry_price"],
                exit_price=exit_price,
                pips=pips,
                profit=profit,
                exit_reason="end_of_data",
                entry_time=open_pos["entry_time"],
                exit_time=bars[-1].time,
                bars_held=len(bars) - 1 - open_pos["entry_idx"],
            )
        )

    # Metrics
    print(f"\n{'=' * 50}", flush=True)
    print("RESULTS", flush=True)
    print(f"{'=' * 50}", flush=True)
    print(f"Signals:       {signal_count}", flush=True)
    print(f"Trades closed: {len(trades)}", flush=True)

    if not trades:
        print("\n❌ NO TRADES — smoke test FAILED (need >0 trades)", flush=True)
        return 1

    wins = [t for t in trades if t.profit > 0]
    losses = [t for t in trades if t.profit <= 0]
    win_rate = len(wins) / len(trades) * 100.0
    total_profit = sum(t.profit for t in wins)
    total_loss = abs(sum(t.profit for t in losses))
    pf = total_profit / total_loss if total_loss > 0 else float("inf")
    avg_bars = sum(t.bars_held for t in trades) / len(trades)

    print(f"Win rate:      {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)", flush=True)
    print(f"Profit Factor: {pf:.2f}", flush=True)
    print(f"Avg holding:   {avg_bars:.1f} bars", flush=True)
    print(f"Total pips:    {sum(t.pips for t in trades):.1f}", flush=True)

    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print("\nExit reasons:", flush=True)
    for r, c in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  {r}: {c}", flush=True)

    print(f"\n✅ Smoke test PASSED: {len(trades)} trades on XAUUSD M15", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
