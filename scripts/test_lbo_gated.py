#!/usr/bin/env python3
"""Test London Breakout with regime gates using precomputed labels."""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

project_root = Path("$AYUMI_ROOT")
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

import pickle

import duckdb
from core.types import Bar, BarPeriod, MarketState, SessionType
from indicators import adx as calc_adx
from regime.detector import Regime, RegimeConfig, RegimeDetector
from strategies.london_breakout_retest import (
    LondonBreakoutConfig,
    LondonBreakoutRetestStrategy,
)

# Load bars (autodetect ms vs sec)
con = duckdb.connect(str(project_root / "data" / "ayumi_market.duckdb"), read_only=True)
con.execute("SET threads=1; SET memory_limit='512MB'")
rows = con.execute(
    "SELECT timestamp_utc, open, high, low, close, volume FROM bars WHERE symbol='XAUUSD' AND timeframe='M15' ORDER BY timestamp_utc ASC"  # noqa: E501
).fetchall()
is_ms = rows[0][0] > 1e12
bars = []
for r in rows:
    ts = r[0] / 1000.0 if is_ms else r[0]
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
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
con.close()
print(f"Loaded {len(bars)} bars")

# Precompute regime + adx + session (use cached labels if available)
cache_dir = project_root / "data" / "cache"
cache_file = next(cache_dir.glob("labels_XAUUSD_M15_*.pkl"), None) if cache_dir.exists() else None
if cache_file:
    with open(cache_file, "rb") as f:
        regimes, adxs, sessions = pickle.load(f)  # noqa: S301
    print(f"Loaded cached labels: {len(regimes)} entries")
else:
    print("No cached labels — computing...")
    det = RegimeDetector(RegimeConfig())
    regimes, adxs, sessions = [], [], []
    t0 = time.time()
    for i in range(len(bars)):
        r = None
        a = 0.0
        if i >= 100:
            w = bars[max(0, i - 100) : i + 1]
            h = np.array([b.high for b in w])
            l = np.array([b.low for b in w])  # noqa: E741
            c = np.array([b.close for b in w])
            try:
                r = det.detect_current(h, l, c)
            except:  # noqa: E722, S110
                pass
            try:
                av = calc_adx(h, l, c, 14)
                if av is not None and len(av) > 0:
                    val = float(av.iloc[-1]) if hasattr(av, "iloc") else float(av[-1])
                    if not np.isnan(val):
                        a = val
            except:  # noqa: E722, S110
                pass
        regimes.append(r)
        adxs.append(a)
        hr = bars[i].time.hour
        sessions.append("asia" if 0 <= hr < 7 else "london" if 7 <= hr < 12 else "ny_am" if 12 <= hr < 17 else "other")
    print(f"Computed in {time.time() - t0:.1f}s")

# Backtest London Breakout with default regime gates
# KZ-style gate for LBO: QUIET/CHOPPY + ADX 15-30 + London session
RISK = 50.0
ACCOUNT = 10000.0


def run_backtest(gate_regimes, gate_adx, gate_sessions, name):
    s = LondonBreakoutRetestStrategy(LondonBreakoutConfig())
    trades = []
    pos = None
    for i in range(len(bars)):
        bar = bars[i]
        if pos:
            hit = False
            if pos["dir"] == "long" and bar.low <= pos["stop"]:
                hit = True
            elif pos["dir"] == "short" and bar.high >= pos["stop"]:
                hit = True
            if hit:
                trades.append(-pos["risk"])
                pos = None
            else:
                sd = abs(pos["entry"] - pos["stop"])
                for rm, pi in [(1, 0), (2, 1), (3, 2)]:
                    if pi == pos["partials"]:
                        if pos["dir"] == "long":
                            tp = pos["entry"] + sd * rm
                            if bar.high >= tp:
                                trades.append((tp - pos["entry"]) * pos["qty"] / 3)
                                pos["partials"] += 1
                        else:
                            tp = pos["entry"] - sd * rm
                            if bar.low <= tp:
                                trades.append((pos["entry"] - tp) * pos["qty"] / 3)
                                pos["partials"] += 1
                if pos and pos["partials"] >= 3:
                    pos = None
                if pos and i - pos["entry_bar"] >= 50:
                    if pos["dir"] == "long":
                        trades.append((bar.close - pos["entry"]) * pos["qty"])
                    else:
                        trades.append((pos["entry"] - bar.close) * pos["qty"])
                    pos = None
        if pos:
            continue
        # Gate
        r = regimes[i]
        if r is None or r not in gate_regimes:
            continue
        if adxs[i] < gate_adx[0] or adxs[i] > gate_adx[1]:
            continue
        if sessions[i] not in gate_sessions:
            continue
        # Strategy signal — pass full bar history (strategies need warmup)
        state = MarketState(
            bars=bars[: i + 1],
            current_session=SessionType.LONDON if 7 <= bar.time.hour < 12 else SessionType.OUTSIDE,
        )
        try:
            sig = s.evaluate(state)
        except:  # noqa: E722, S112
            continue
        if sig is None:
            continue
        sd = max(abs(sig.entry_price - sig.stop_loss), 0.0001)
        qty = RISK / sd
        pos = {
            "dir": sig.direction.value,
            "entry": sig.entry_price,
            "stop": sig.stop_loss,
            "qty": qty,
            "entry_bar": i,
            "risk": RISK,
            "partials": 0,
        }
    if pos:
        last = bars[-1]
        pnl = (
            (last.close - pos["entry"]) * pos["qty"]
            if pos["dir"] == "long"
            else (pos["entry"] - last.close) * pos["qty"]
        )
        trades.append(pnl)
    if not trades:
        return {"trades": 0, "pf": 0, "net": 0, "dd_pct": 0, "wr": 0}
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    pf = gw / gl if gl > 0 else 999
    eq = np.cumsum(trades)
    pk = np.maximum.accumulate(eq)
    dd = pk - eq
    return {
        "trades": len(trades),
        "pf": round(pf, 3),
        "net": round(sum(trades), 2),
        "dd_pct": round((max(dd) if len(dd) else 0) / ACCOUNT * 100, 2),
        "wr": round(len(wins) / len(trades) * 100, 1),
    }


# Test multiple gate variants
print()
variants = [
    (
        "BASELINE LBO (no gate)",
        {Regime.QUIET, Regime.CHOPPY, Regime.TRENDING, Regime.VOLATILE},
        (0, 100),
        {"asia", "london", "ny_am", "other"},
    ),
    (
        "LBO + LONDON-only",
        {Regime.QUIET, Regime.CHOPPY, Regime.TRENDING, Regime.VOLATILE},
        (0, 100),
        {"london"},
    ),
    (
        "LBO + QUIET/CHOPPY + LONDON",
        {Regime.QUIET, Regime.CHOPPY},
        (0, 100),
        {"london"},
    ),
    (
        "LBO + ADX[15,30] + LONDON",
        {Regime.QUIET, Regime.CHOPPY, Regime.TRENDING, Regime.VOLATILE},
        (15, 30),
        {"london"},
    ),
    (
        "LBO + ADX[18,28] + LONDON",
        {Regime.QUIET, Regime.CHOPPY, Regime.TRENDING, Regime.VOLATILE},
        (18, 28),
        {"london"},
    ),
    (
        "LBO + strictest (qu/ch/v + ADX[18,28] + LONDON)",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        (18, 28),
        {"london"},
    ),
]
print("| Variant | Trades | PF | Net $ | DD % | WR % |")
print("|---|---:|---:|---:|---:|---:|")
for name, gr, ga, gs in variants:
    m = run_backtest(gr, ga, gs, name)
    print(f"| {name} | {m['trades']} | {m['pf']} | ${m['net']} | {m['dd_pct']}% | {m['wr']}% |")
