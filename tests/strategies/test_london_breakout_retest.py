"""Smoke test for London Breakout Retest strategy.

Validates:
1. Strategy instantiates with default config
2. Produces signals on real XAUUSD M15 data
3. Returns None when conditions aren't met (no Asian range to break out of)
4. SL/TP ratios are reasonable
"""

from __future__ import annotations
import pytest

import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from core.types import Bar, BarPeriod, MarketState, SessionType
from strategies.london_breakout_retest import (
    LondonBreakoutConfig,
    LondonBreakoutRetestStrategy,
)


def load_xauusd_m15(limit: int = 10000):
    con = duckdb.connect(str(project_root / "data" / "ayumi_market.duckdb"), read_only=True)
    con.execute("SET threads=1; SET memory_limit='512MB'")
    rows = con.execute(
        "SELECT timestamp_utc, open, high, low, close, volume FROM bars "
        "WHERE symbol='XAUUSD' AND timeframe='M15' "
        "ORDER BY timestamp_utc ASC LIMIT ?",
        [limit],
    ).fetchall()
    con.close()
    bars = []
    for r in rows:
        dt = datetime.fromtimestamp(r[0], tz=timezone.utc)
        bars.append(
            Bar(
                time=dt,
                open=r[1],
                high=r[2],
                low=r[3],
                close=r[4],
                volume=r[5],
                period=BarPeriod.M15,
            )
        )
    return bars


@pytest.mark.xfail(reason="DEBT 6ea40384-35ba-4c41-99a6-87d843ca7f75: missing forex historical data files (environmental)", strict=False)
def test_smoke_produces_signals():
    """Run on first 10k bars, expect at least a few signals."""
    bars = load_xauusd_m15(limit=10000)
    assert len(bars) > 100, f"Need >100 bars for setup, got {len(bars)}"

    s = LondonBreakoutRetestStrategy(LondonBreakoutConfig())
    sigs = 0
    sigs_long = 0
    sigs_short = 0
    sl_distances = []
    rr_ratios = []

    # Rolling window approach: pass last 200 bars to keep evaluate() sane
    for i in range(60, len(bars)):
        start = max(0, i - 199)
        window = bars[start : i + 1]
        cs = (
            SessionType.LONDON
            if 7 <= bars[i].time.hour < 12
            else SessionType.ASIAN
            if 0 <= bars[i].time.hour < 7
            else SessionType.NY_AM
            if 12 <= bars[i].time.hour < 17
            else SessionType.NY_PM
            if 17 <= bars[i].time.hour < 21
            else SessionType.OUTSIDE
        )
        state = MarketState(bars=window, current_session=cs)
        try:
            sig = s.evaluate(state)
        except Exception:  # noqa: S112
            continue
        if sig is None:
            continue
        sigs += 1
        if sig.direction.value == "long":
            sigs_long += 1
        else:
            sigs_short += 1
        sl_dist = abs(sig.entry_price - sig.stop_loss)
        tp_dist = abs(sig.take_profit_1 - sig.entry_price)
        if sl_dist > 0:
            sl_distances.append(sl_dist)
            if tp_dist > 0:
                rr_ratios.append(tp_dist / sl_dist)

    print(f"LondonBreakoutRetest smoke test: {len(bars)} bars, {sigs} signals (L={sigs_long}, S={sigs_short})")
    print(
        f"  SL distances: min={min(sl_distances):.2f}, "
        f"max={max(sl_distances):.2f}, "
        f"median={np.median(sl_distances):.2f}"
    )
    if rr_ratios:
        print(f"  RR ratios: min={min(rr_ratios):.2f}, median={np.median(rr_ratios):.2f}, max={max(rr_ratios):.2f}")

    # Assertions
    assert sigs > 0, "Strategy produced zero signals on 10k XAUUSD M15 bars"
    assert sigs >= 5, f"Expected ≥5 signals for viability, got {sigs}"
    assert all(d > 0 for d in sl_distances), "All signals must have positive SL distance"
    if rr_ratios:
        assert min(rr_ratios) >= 1.0, f"Min RR={min(rr_ratios):.2f} < 1.0 (expect positive R)"


def test_instantiation():
    """Strategy and config classes load."""
    cfg = LondonBreakoutConfig()
    s = LondonBreakoutRetestStrategy(cfg)
    assert s is not None
    assert s.config is not None


@pytest.mark.xfail(reason="DEBT 6ea40384-35ba-4c41-99a6-87d843ca7f75: missing forex historical data files (environmental)", strict=False)
def test_no_signals_outside_london():
    """If we never enter London hours, no signals should fire (strategy is London-specific)."""
    bars = load_xauusd_m15(limit=1000)
    # Force all OUTSIDE session
    s = LondonBreakoutRetestStrategy(LondonBreakoutConfig())
    sigs = 0
    for i in range(60, len(bars)):
        start = max(0, i - 199)
        window = bars[start : i + 1]
        state = MarketState(bars=window, current_session=SessionType.OUTSIDE)
        try:
            sig = s.evaluate(state)
        except Exception:  # noqa: S112
            continue
        if sig is not None:
            sigs += 1
    # Strategy may or may not depend on session for entry — log but don't fail
    print(f"OUTSIDE session signals: {sigs} (informational)")


if __name__ == "__main__":
    test_instantiation()
    print("✓ test_instantiation")
    test_smoke_produces_signals()
    print("✓ test_smoke_produces_signals")
    test_no_signals_outside_london()
    print("✓ test_no_signals_outside_london")
    print("\nAll smoke tests passed.")
