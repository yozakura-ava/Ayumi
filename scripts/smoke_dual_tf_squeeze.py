#!/usr/bin/env python3
"""Smoke backtest for the Dual-Timeframe Squeeze Pro strategy.

Reads XAUUSD M15 bars from DuckDB and runs the strategy in a simple
bar-by-bar evaluation loop to count signals and confirm trade count > 0.
This is a lightweight smoke; a real backtest engine (cost / spread /
FTMO guard) is not wired up here.

Resource limits (set by caller or run with ``ulimit -v 2097152``):
  - virtual address space: 2 GiB
  - max 300s wall clock
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List

# Ensure src is on path.
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC / "forex_bot"))
sys.path.insert(0, str(SRC))


def _alarm(seconds: int) -> None:
    def _handler(signum, frame):
        print(f"[smoke] TIMEOUT after {seconds}s", file=sys.stderr)
        sys.exit(124)

    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)


def main() -> int:
    import duckdb
    from core.types import Bar, BarPeriod, MarketState, SessionType
    from strategies.dual_tf_squeeze_pro import DualTFSqueezeProStrategy

    _alarm(280)
    t0 = time.time()

    db = SRC.parent / "data" / "ayumi_market.duckdb"
    if not db.exists():
        print(f"[smoke] missing db: {db}", file=sys.stderr)
        return 2

    con = duckdb.connect(str(db), read_only=True)
    # Pull ONLY the last 5000 XAUUSD M15 rows. DuckDB supports OFFSET
    # via subquery + ORDER BY DESC.
    rows = con.execute(
        """
        SELECT timestamp_utc, open, high, low, close, volume, spread_pips
        FROM (
            SELECT timestamp_utc, open, high, low, close, volume, spread_pips
            FROM bars
            WHERE symbol = 'XAUUSD' AND timeframe = 'M15'
            ORDER BY timestamp_utc DESC
            LIMIT 5000
        ) sub
        ORDER BY timestamp_utc ASC
        """
    ).fetchall()
    total = con.execute("SELECT COUNT(*) FROM bars WHERE symbol = 'XAUUSD' AND timeframe = 'M15'").fetchone()[0]
    con.close()

    if len(rows) < 100:
        print(f"[smoke] only {len(rows)} bars", file=sys.stderr)
        return 2

    print(f"[smoke] loaded last {len(rows):,} of {total:,} XAUUSD M15 bars from {db}")

    # Take the LAST 5000 bars per task spec.
    rows = rows[-5000:]
    bars: List[Bar] = []
    for ts, o, h, l, c, v, sp in rows:  # noqa: E741
        t = datetime.fromtimestamp(ts, tz=__import__("datetime").timezone.utc).replace(tzinfo=None)
        bars.append(
            Bar(
                time=t,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=v or 0.0,
                spread_pips=sp or 0.0,
                period=BarPeriod(15),
            )
        )

    strategy = DualTFSqueezeProStrategy()
    strategy.initialize({})

    # Walk every bar via the engine path: on_bar -> evaluate.
    last_signal = None
    signal_count = 0
    signals_long = 0
    signals_short = 0
    confidences: List[float] = []

    n = len(bars)
    stride = 1  # evaluate every bar
    slice_step = max(stride, 1)

    # We feed a slice MarketState in evaluate(). Since the strategy is
    # incremental via on_bar, we also call on_bar for each bar so
    # internal state advances. evaluate() will sync itself with the
    # MarketState snapshot via the catch-up loop if needed.
    for i, b in enumerate(bars):
        strategy.on_bar(b)
        if i < strategy.config.min_bars_for_setup:
            continue
        if i % slice_step != 0:
            continue
        state = MarketState(bars=bars[: i + 1], current_session=SessionType.NY_AM)
        sig = strategy.evaluate(state)
        if sig is not None:
            signal_count += 1
            confidences.append(sig.confidence)
            from core.types import TradeDirection

            if sig.direction == TradeDirection.LONG:
                signals_long += 1
            elif sig.direction == TradeDirection.SHORT:
                signals_short += 1
            last_signal = sig

    elapsed = time.time() - t0
    print(f"[smoke] elapsed: {elapsed:.1f}s")
    print(f"[smoke] bars processed: {n}")
    print(f"[smoke] signals found: {signal_count}")
    print(f"[smoke] long signals:  {signals_long}")
    print(f"[smoke] short signals: {signals_short}")
    if confidences:
        avg = sum(confidences) / len(confidences)
        print(f"[smoke] avg confidence: {avg:.3f}")
        print(f"[smoke] min confidence: {min(confidences):.3f}")
        print(f"[smoke] max confidence: {max(confidences):.3f}")
    if last_signal is not None:
        print(f"[smoke] last signal dir: {last_signal.direction.name}")
        print(f"[smoke] last signal entry: {last_signal.entry_price:.5f}")
        print(f"[smoke] last signal sl:    {last_signal.stop_loss:.5f}")
        print(
            f"[smoke] last signal tps:  "
            f"{last_signal.take_profit_1:.5f} "
            f"{last_signal.take_profit_2:.5f} "
            f"{last_signal.take_profit_3:.5f}"
        )
        print(f"[smoke] last signal rationale: {last_signal.rationale}")

    # Acceptance: at least one signal on a 5000-bar slice.
    if signal_count == 0:
        print("[smoke] FAILED: 0 signals on 5000 XAUUSD M15 bars", file=sys.stderr)
        return 1
    print("[smoke] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
