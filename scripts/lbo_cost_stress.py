#!/usr/bin/env python3
"""LBO Cost-Stress Backtest (v2 - correct commission).

Validates London Breakout under realistic broker costs:
- Spread: 2.5 pips (FTMO standard) on XAUUSD = $0.25/lot/0.1
- Commission: $3.5/lot round turn
- Slippage: 0.2 pips on entry + 0.2 pips on exit

Cost model:
- Each round turn (entry+exit) costs: spread + commission + 2x slippage
- Per 0.1 lot trade: spread $0.025 + commission $0.35 + slippage $0.02 = ~$0.40
- This is ~0.8% of risk ($50), so it's material
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
from strategies.london_breakout_retest import (
    LondonBreakoutConfig,
    LondonBreakoutRetestStrategy,
)

# Load bars
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
if not cache_file:
    print("ERROR: No cached labels. Run gate_loosening_study.py first.")
    sys.exit(1)
with open(cache_file, "rb") as f:
    regimes, adxs, sessions = pickle.load(f)  # noqa: S301

RISK = 50.0
ACCOUNT = 10000.0


def per_trade_cost(qty, spread_pips, commission_per_lot, slippage_pips):
    """Cost in USD per round turn (entry + exit)."""
    spread_cost = spread_pips * qty * 0.10  # XAUUSD: 1 pip on 0.1 lot = $0.10
    commission_cost = commission_per_lot * qty
    slippage_cost = 2 * slippage_pips * qty * 0.10  # entry + exit slippage
    return spread_cost + commission_cost + slippage_cost


def run_lbo(spread_pips, commission, slippage_pips, name):
    """Run LBO with specified cost parameters.

    Cost application: subtract per-trade cost from each completed trade.
    For partial exits (1R/2R/3R), distribute cost proportionally.
    """
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
                pnl = -pos["risk"]
                # Apply exit-side cost
                pnl -= per_trade_cost(pos["qty"], spread_pips, commission, slippage_pips) * 0.5
                trades.append(pnl)
                pos = None
            else:
                sd = abs(pos["entry"] - pos["stop"])
                for rm, pi in [(1, 0), (2, 1), (3, 2)]:
                    if pi == pos["partials"]:
                        if pos["dir"] == "long":
                            tp = pos["entry"] + sd * rm
                            if bar.high >= tp:
                                pnl = (tp - pos["entry"]) * pos["qty"] / 3
                                pnl -= (
                                    per_trade_cost(
                                        pos["qty"],
                                        spread_pips,
                                        commission,
                                        slippage_pips,
                                    )
                                    * 0.3
                                )
                                trades.append(pnl)
                                pos["partials"] += 1
                        else:
                            tp = pos["entry"] - sd * rm
                            if bar.low <= tp:
                                pnl = (pos["entry"] - tp) * pos["qty"] / 3
                                pnl -= (
                                    per_trade_cost(
                                        pos["qty"],
                                        spread_pips,
                                        commission,
                                        slippage_pips,
                                    )
                                    * 0.3
                                )
                                trades.append(pnl)
                                pos["partials"] += 1
                if pos and pos["partials"] >= 3:
                    pos = None
                if pos and i - pos["entry_bar"] >= 50:
                    if pos["dir"] == "long":
                        pnl = (bar.close - pos["entry"]) * pos["qty"]
                    else:
                        pnl = (pos["entry"] - bar.close) * pos["qty"]
                    pnl -= per_trade_cost(pos["qty"], spread_pips, commission, slippage_pips) * 0.5
                    trades.append(pnl)
                    pos = None
        if pos:
            continue

        if adxs[i] < 15.0 or adxs[i] > 30.0:
            continue
        if sessions[i] != "london":
            continue

        state = MarketState(bars=bars[: i + 1], current_session=SessionType.LONDON)
        try:
            sig = s.evaluate(state)
        except:  # noqa: E722, S112
            continue
        if sig is None:
            continue

        sd = max(abs(sig.entry_price - sig.stop_loss), 0.0001)
        qty = RISK / sd
        # Apply entry-side cost to entry price (slippage only at entry)
        entry_cost_per_unit = slippage_pips * 0.10
        if sig.direction.value == "long":
            adjusted_entry = sig.entry_price + entry_cost_per_unit
        else:
            adjusted_entry = sig.entry_price - entry_cost_per_unit

        pos = {
            "dir": sig.direction.value,
            "entry": adjusted_entry,
            "stop": sig.stop_loss,
            "qty": qty,
            "entry_bar": i,
            "risk": RISK,
            "partials": 0,
        }

    if pos:
        last = bars[-1]
        if pos["dir"] == "long":
            pnl = (last.close - pos["entry"]) * pos["qty"]
        else:
            pnl = (pos["entry"] - last.close) * pos["qty"]
        pnl -= per_trade_cost(pos["qty"], spread_pips, commission, slippage_pips) * 0.5
        trades.append(pnl)

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
print("# LBO Cost-Stress Test — XAUUSD M15 (71k bars, 4.5 years)")
print(f"# Date: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
print()

# Per-trade cost examples for reference
example_qty = 0.1
print(f"## Cost reference (qty={example_qty} lot on XAUUSD):")
print(f"  Spread: 2.5 pips × 0.10 × 0.1 = ${2.5 * example_qty * 0.10:.2f}")
print(f"  Commission: $3.5 × 0.1 = ${3.5 * example_qty:.2f}")
print(f"  Slippage: 2 × 0.2 pips × 0.10 × 0.1 = ${2 * 0.2 * example_qty * 0.10:.2f}")
print(f"  Total per round turn: ${per_trade_cost(example_qty, 2.5, 3.5, 0.2):.2f}")
print(f"  Risk = $50, so cost = {per_trade_cost(example_qty, 2.5, 3.5, 0.2) / 50 * 100:.1f}% of risk per trade")
print()

variants = [
    ("Zero cost (backtest)", 0.0, 0.0, 0.0),
    ("Realistic (FTMO)", 2.5, 3.5, 0.2),
    ("High spread (5 pips)", 5.0, 3.5, 0.2),
    ("High slippage (1 pip)", 2.5, 3.5, 1.0),
    ("Stress combo (5p spread + 1p slip)", 5.0, 3.5, 1.0),
]

print("| Variant | Trades | PF | Net $ | DD % | WR % |")
print("|---|---:|---:|---:|---:|---:|")
results = []
for name, spread, comm, slip in variants:
    r = run_lbo(spread, comm, slip, name)
    results.append(r)
    print(f"| {r['name']} | {r['trades']} | {r['pf']} | ${r['net']} | {r['dd_pct']}% | {r['wr']}% |")

# Comparison
print()
print("## PF Degradation Under Costs")
base = results[0]
for r in results[1:]:
    delta = r["pf"] - base["pf"]
    pct = (r["pf"] / base["pf"] - 1) * 100 if base["pf"] > 0 else 0
    print(f"  {r['name']}: PF {base['pf']} → {r['pf']} ({delta:+.3f}, {pct:+.1f}%)")

years = len(bars) / (96 * 365)
ftmo = results[1]
print(
    f"\n**FTMO realistic:** {ftmo['trades'] / years:.1f} trades/year, PF={ftmo['pf']}, DD={ftmo['dd_pct']}%",
    "✅ PASS" if ftmo["pf"] > 1.0 and ftmo["dd_pct"] < 10 else "❌ FAIL",
)
