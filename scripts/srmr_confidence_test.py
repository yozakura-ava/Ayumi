#!/usr/bin/env python3
"""SRMR+ Confidence-Enhanced Gate Test.

Tests whether adding confidence filters (volume, ATR ratio, recent volatility,
session volatility) rescues SRMR+ from its current PF<1 state when gates
are loosened. The hypothesis: SRMR+'s edge is real but low-quality signals
(small range, low volume, mid-vol regimes) destroy the edge.

Strategy: take SRMR+'s raw signals, add confidence filters, measure trades
and PF for each filter variant.

Usage:
    python3 scripts/srmr_confidence_test.py
"""

from __future__ import annotations

import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

project_root = Path("$AYUMI_ROOT")
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

import duckdb
from core.types import Bar, BarPeriod, MarketState, SessionType
from regime.detector import Regime
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

# Load XAUUSD M15
con = duckdb.connect(str(project_root / "data" / "ayumi_market.duckdb"), read_only=True)
con.execute("SET threads=1; SET memory_limit='512MB'")
rows = con.execute(
    "SELECT timestamp_utc, open, high, low, close, volume FROM bars "
    "WHERE symbol='XAUUSD' AND timeframe='M15' ORDER BY timestamp_utc ASC"
).fetchall()
con.close()
is_ms = rows[0][0] > 1e12
bars = [
    Bar(
        time=datetime.fromtimestamp(r[0] / 1000.0 if is_ms else r[0], tz=timezone.utc),
        open=r[1],
        high=r[2],
        low=r[3],
        close=r[4],
        volume=r[5],
        period=BarPeriod.M15,
    )
    for r in rows
]
print(f"Loaded {len(bars)} bars")

# Load cached labels
cache_dir = project_root / "data" / "cache"
cache_file = next(cache_dir.glob("labels_XAUUSD_M15_*.pkl"), None)
with open(cache_file, "rb") as f:
    regimes, adxs, sessions = pickle.load(f)  # noqa: S301
print("Loaded cached labels")

# Precompute per-bar indicators we need for confidence:
# - ATR ratio (current ATR / median ATR over 100 bars) → measures volatility regime
# - Recent range expansion (last 10 bars range vs prior 50 bars)
# - Volume relative to 100-bar median
print("Precomputing confidence indicators...")
n = len(bars)
atr_ratio = [1.0] * n
vol_ratio = [1.0] * n
range_ratio = [1.0] * n
for i in range(100, n):
    # ATR over last 14 bars
    tr_sum = 0.0
    for j in range(i - 13, i + 1):
        if j > 0:
            tr = max(
                bars[j].high - bars[j].low,
                abs(bars[j].high - bars[j - 1].close),
                abs(bars[j].low - bars[j - 1].close),
            )
            tr_sum += tr
    cur_atr = tr_sum / 14
    # Median ATR over last 100 bars
    atr_arr = []
    for k in range(max(0, i - 99), i + 1):
        if k >= 14:
            tr_s = 0.0
            for j in range(k - 13, k + 1):
                if j > 0:
                    tr_s += max(
                        bars[j].high - bars[j].low,
                        abs(bars[j].high - bars[j - 1].close),
                        abs(bars[j].low - bars[j - 1].close),
                    )
            atr_arr.append(tr_s / 14)
    median_atr = np.median(atr_arr) if atr_arr else 1.0
    atr_ratio[i] = cur_atr / max(median_atr, 0.0001)
    # Volume ratio
    vol_arr = [bars[j].volume for j in range(max(0, i - 99), i + 1)]
    median_vol = np.median(vol_arr) if vol_arr else 1.0
    vol_ratio[i] = bars[i].volume / max(median_vol, 1.0)
    # Range ratio: last 10 bars range vs prior 50
    recent_range = max(bars[j].high for j in range(i - 9, i + 1)) - min(bars[j].low for j in range(i - 9, i + 1))
    prior_high = max(bars[j].high for j in range(i - 59, i - 9))
    prior_low = min(bars[j].low for j in range(i - 59, i - 9))
    prior_range = prior_high - prior_low
    range_ratio[i] = recent_range / max(prior_range / 5, 0.0001)  # normalize for window difference

print("Done precomputing")


def gate_pass(
    i,
    regimes,
    adxs,
    sessions,
    atr_ratio,
    vol_ratio,
    range_ratio,
    regime_set=None,
    adx_max=100.0,
    session_set=None,
    atr_min=0.0,
    atr_max=10.0,
    vol_min=0.0,
    range_min=0.0,
):
    """Combined gate: regime + ADX + session + confidence filters."""
    r = regimes[i]
    if r is None:
        return False
    if regime_set is not None and r not in regime_set:
        return False
    if adxs[i] > adx_max:
        return False
    if session_set is not None and sessions[i] not in session_set:
        return False
    if atr_ratio[i] < atr_min or atr_ratio[i] > atr_max:
        return False
    if vol_ratio[i] < vol_min:
        return False
    if range_ratio[i] < range_min:
        return False
    return True


RISK = 50.0
ACCOUNT = 10000.0


def run_blend(
    name,
    regime_set=None,
    adx_max=100.0,
    session_set=None,
    atr_min=0.0,
    atr_max=10.0,
    vol_min=0.0,
    range_min=0.0,
):
    """Run SRMR+ with combined gates. Return trades + metrics."""
    s = SRMRPlusStrategy(SRMRPlusConfig(symbol="XAUUSD"))
    trades = []
    pos = None

    for i in range(100, len(bars)):  # need 100 bars precompute
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

        if not gate_pass(
            i,
            regimes,
            adxs,
            sessions,
            atr_ratio,
            vol_ratio,
            range_ratio,
            regime_set=regime_set,
            adx_max=adx_max,
            session_set=session_set,
            atr_min=atr_min,
            atr_max=atr_max,
            vol_min=vol_min,
            range_min=range_min,
        ):
            continue

        state = MarketState(
            bars=bars[: i + 1],
            current_session=SessionType.LONDON
            if 7 <= bar.time.hour < 12
            else SessionType.NY_AM
            if 12 <= bar.time.hour < 15
            else SessionType.OUTSIDE,
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
        if pos["dir"] == "long":
            trades.append((last.close - pos["entry"]) * pos["qty"])
        else:
            trades.append((pos["entry"] - last.close) * pos["qty"])

    if not trades:
        return {"name": name, "trades": 0, "pf": 0, "net": 0, "dd_pct": 0, "wr": 0}
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    pf = gw / gl if gl > 0 else 999.0
    eq = np.cumsum(trades)
    pk = np.maximum.accumulate(eq)
    dd = pk - eq
    return {
        "name": name,
        "trades": len(trades),
        "pf": round(pf, 3),
        "net": round(sum(trades), 2),
        "dd_pct": round((max(dd) if len(dd) else 0) / ACCOUNT * 100, 2),
        "wr": round(len(wins) / len(trades) * 100, 1),
    }


print()
print("# SRMR+ Confidence-Enhanced Gate Test — XAUUSD M15 (71k bars, 4.5 years)")
print(f"# Date: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
print()
print("Confidence indicators:")
print("  ATR ratio: current ATR / median ATR(100). >1.0 = above-average volatility")
print("  Volume ratio: current vol / median vol(100). >1.0 = high-volume bar")
print("  Range ratio: last-10-bar range / avg-10-bar-of-prior-50. >1.0 = range expansion")
print()

# Variant definitions
variants = [
    # baseline (validated original gate)
    ("BASELINE (QUIET + LONDON)", {Regime.QUIET}, 100.0, {"london"}),
    # loosened regime (matches prior loosening study)
    (
        "Regime+choppy_vol (loosened)",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
    ),
    # confidence filters on top of loosened regime
    (
        "+ ATR ratio [0.5, 1.5]",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        0.5,
        1.5,
    ),
    (
        "+ ATR ratio [0.7, 1.3]",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        0.7,
        1.3,
    ),
    (
        "+ ATR ratio [1.0, 1.5] (high vol)",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        1.0,
        1.5,
    ),
    (
        "+ ATR ratio [0.7, 1.0] (low vol)",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        0.7,
        1.0,
    ),
    # volume filter
    (
        "+ Volume > 1.5x median",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        0,
        10,
        1.5,
    ),
    (
        "+ Volume > 1.0x median",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        0,
        10,
        1.0,
    ),
    # range expansion
    (
        "+ Range > 1.5x",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        0,
        10,
        0,
        1.5,
    ),
    (
        "+ Range > 2.0x (strong expansion)",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        0,
        10,
        0,
        2.0,
    ),
    # combined
    (
        "ATR[0.7,1.3] + Vol>1.0 + Range>1.5",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        0.7,
        1.3,
        1.0,
        1.5,
    ),
    (
        "ATR[0.7,1.3] + Vol>1.5 + Range>2.0 (strict)",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        0.7,
        1.3,
        1.5,
        2.0,
    ),
    (
        "ATR[1.0,1.5] + Vol>1.5 + Range>1.5 (vol regime)",
        {Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
        100.0,
        {"london"},
        1.0,
        1.5,
        1.5,
        1.5,
    ),
]

print("| Variant | Trades | PF | Net $ | DD % | WR % |")
print("|---|---:|---:|---:|---:|---:|")
results = []
for v in variants:
    name = v[0]
    args = v[1:] if len(v) > 4 else v[1:]
    r = run_blend(name, *args)
    results.append(r)
    print(f"| {r['name']} | {r['trades']} | {r['pf']} | ${r['net']} | {r['dd_pct']}% | {r['wr']}% |")

# Find best
print()
print("## Best Variants (PF > 1.0, sorted by trades)")
viable = [r for r in results if r["pf"] > 1.0 and r["dd_pct"] < 10]
viable.sort(key=lambda r: r["trades"], reverse=True)
for r in viable[:5]:
    print(f"  {r['name']}: {r['trades']} trades, PF={r['pf']}, DD={r['dd_pct']}%")

# Annualized
years = len(bars) / (96 * 365)
print()
print(
    f"**Best annualized volume:** {viable[0]['trades'] / years:.1f}/year" if viable else "**No viable variant found**"
)
print(
    f"**FTMO viability:** {'✅ PASS' if viable and viable[0]['pf'] > 1.0 and viable[0]['dd_pct'] < 10 else '❌ FAIL'}"
)
