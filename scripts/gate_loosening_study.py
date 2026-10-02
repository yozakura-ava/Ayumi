#!/usr/bin/env python3
"""Gate Loosening Study v3 — cached precomputed labels.

Precomputes regime + ADX for all bars ONCE (cached to disk).
Then tests gate variants as fast filter lookups.
"""

from __future__ import annotations

import hashlib
import logging
import pickle
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

import duckdb
from core.types import Bar, BarPeriod, MarketState, SessionType
from indicators import adx as calc_adx
from regime.detector import Regime, RegimeConfig, RegimeDetector
from strategies.donchian_atr_trend_v2 import (
    DonchianATRConfig,
    DonchianATRTrendV2Strategy,
)
from strategies.dual_tf_squeeze_pro import (
    DualTFSqueezeProConfig,
    DualTFSqueezeProStrategy,
)
from strategies.killzone_momentum import (
    KillzoneMomentumConfig,
    KillzoneMomentumStrategy,
)
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

logging.basicConfig(level=logging.WARNING)
DB_PATH = project_root / "data" / "ayumi_market.duckdb"
CACHE_DIR = project_root / "data" / "cache"
CACHE_DIR.mkdir(exist_ok=True)
RISK = 50.0
ACCOUNT = 10000.0


@dataclass
class GateConfig:
    regimes: set
    adx_range: tuple
    sessions: Optional[set]

    def copy(self, **kw):
        d = {
            "regimes": self.regimes.copy(),
            "adx_range": self.adx_range,
            "sessions": self.sessions.copy() if self.sessions else None,
        }
        d.update(kw)
        return GateConfig(**d)


BASELINE = {
    "killzone_momentum": GateConfig({Regime.QUIET, Regime.CHOPPY}, (18.0, 25.0), {"london"}),
    "dual_tf_squeeze_pro": GateConfig({Regime.VOLATILE, Regime.CHOPPY}, (0.0, 100.0), {"asia", "ny_am"}),
    "donchian_atr_trend_v2": GateConfig({Regime.QUIET, Regime.CHOPPY, Regime.TRENDING}, (0.0, 30.0), None),
    "srmr_plus": GateConfig({Regime.QUIET}, (0.0, 100.0), {"london"}),
}


def load_bars(con, symbol, tf):
    rows = con.execute(
        f"SELECT timestamp_utc, open, high, low, close, volume FROM bars WHERE symbol='{symbol}' AND timeframe='{tf}' ORDER BY timestamp_utc ASC"  # noqa: E501, S608
    ).fetchall()
    bm = {"M15": BarPeriod.M15, "H1": BarPeriod.H1}
    # Auto-detect timestamp scale: > 1e12 = milliseconds, else seconds
    # (GBPUSD M1 is ms, everything else is seconds)
    is_ms = rows[0][0] > 1e12 if rows else False
    out = []
    for r in rows:
        ts = r[0] / 1000.0 if is_ms else r[0]
        out.append(
            Bar(
                time=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=r[1],
                high=r[2],
                low=r[3],
                close=r[4],
                volume=r[5],
                period=bm.get(tf, BarPeriod.M15),
            )
        )
    return out


def session_for(hour):
    if 0 <= hour < 7:
        return SessionType.ASIAN
    elif 7 <= hour < 12:
        return SessionType.LONDON
    elif 12 <= hour < 17:
        return SessionType.NY_AM
    elif 17 <= hour < 21:
        return SessionType.NY_PM
    return SessionType.OUTSIDE


def precompute(bars, detector):
    """Precompute regime + ADX for all bars using 100-bar rolling windows.

    100 bars is needed for the detector's atr_lookback=50 + adx_period=14 warmup.
    60-bar windows produce only trending/choppy because quiet/volatile need
    more history to classify.
    """
    n = len(bars)
    regimes = [None] * n
    adxs = [0.0] * n
    sess = [""] * n
    WINDOW = 100
    for i in range(n):
        if i >= WINDOW:
            w = bars[max(0, i - WINDOW) : i + 1]
            h = np.array([b.high for b in w])
            l = np.array([b.low for b in w])  # noqa: E741
            c = np.array([b.close for b in w])
            try:
                regimes[i] = detector.detect_current(h, l, c)
            except:  # noqa: E722, S110
                pass
            try:
                a = calc_adx(h, l, c, 14)
                if a is not None and len(a) > 0:
                    val = float(a.iloc[-1]) if hasattr(a, "iloc") else float(a[-1])
                    if not np.isnan(val):
                        adxs[i] = val
            except:  # noqa: E722, S110
                pass
        hr = bars[i].time.hour
        sess[i] = "asia" if 0 <= hr < 7 else "london" if 7 <= hr < 12 else "ny_am" if 12 <= hr < 17 else "other"
    return regimes, adxs, sess


def get_or_cache_labels(symbol, tf, bars, detector):
    """Cache precomputed labels to disk."""
    # Hash based on symbol, tf, bar count, first/last timestamps
    h = hashlib.md5(  # noqa: S324
        f"{symbol}_{tf}_{len(bars)}_{bars[0].time}_{bars[-1].time}".encode()
    ).hexdigest()[:12]
    cache_file = CACHE_DIR / f"labels_{symbol}_{tf}_{h}.pkl"
    if cache_file.exists():
        print(f"Loading cached labels from {cache_file.name}", flush=True)
        with open(cache_file, "rb") as f:
            return pickle.load(f)  # noqa: S301
    print(f"Precomputing labels for {symbol} {tf} ({len(bars)} bars)...", flush=True)
    t0 = time.time()
    result = precompute(bars, detector)
    print(f"  Done in {time.time() - t0:.1f}s — caching to {cache_file.name}", flush=True)
    with open(cache_file, "wb") as f:
        pickle.dump(result, f)
    return result


def gate_ok(i, gate, regimes, adxs, sess):
    r = regimes[i]
    if r is None or r not in gate.regimes:
        return False
    lo, hi = gate.adx_range
    if lo > 0 or hi < 100:
        if adxs[i] < lo or adxs[i] > hi:
            return False
    if gate.sessions is not None and sess[i] not in gate.sessions:
        return False
    return True


def run_variant(factory, bars, gate, regimes, adxs, sess):
    s = factory()
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
        if not gate_ok(i, gate, regimes, adxs, sess):
            continue
        start = max(0, i - 299)
        state = MarketState(bars=bars[start : i + 1], current_session=session_for(bar.time.hour))
        try:
            sig = s.evaluate(state)
        except:  # noqa: E722, S112
            continue
        if sig is None:
            continue
        sd = max(abs(sig.entry_price - sig.stop_loss), 0.0001)
        pos = {
            "dir": sig.direction.value,
            "entry": sig.entry_price,
            "stop": sig.stop_loss,
            "qty": RISK / sd,
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
    return metrics(trades)


def metrics(trades):
    if not trades:
        return {"trades": 0, "pf": 0, "net": 0, "dd_pct": 0, "wr": 0}
    w = [t for t in trades if t > 0]
    l = [t for t in trades if t <= 0]  # noqa: E741
    gw, gl = sum(w), abs(sum(l))
    pf = gw / gl if gl > 0 else 999.0
    eq = np.cumsum(trades)
    pk = np.maximum.accumulate(eq)
    dd = pk - eq
    return {
        "trades": len(trades),
        "pf": round(pf, 3),
        "net": round(sum(trades), 2),
        "dd_pct": round((max(dd) if len(dd) else 0) / ACCOUNT * 100, 2),
        "wr": round(len(w) / len(trades) * 100, 1),
    }


def variants_for(sid, base):
    v = []
    if sid == "killzone_momentum":
        v.append(("adx_[15,28]", base.copy(adx_range=(15.0, 28.0))))
        v.append(("adx_[15,30]", base.copy(adx_range=(15.0, 30.0))))
        v.append(("adx_[12,30]", base.copy(adx_range=(12.0, 30.0))))
        v.append(("sess+london_ny", base.copy(sessions={"london", "ny_am"})))
        v.append(("sess+all", base.copy(sessions={"asia", "london", "ny_am"})))
        v.append(("sess_none", base.copy(sessions=None)))
        v.append(
            (
                "regime+vol",
                base.copy(regimes={Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE}),
            )
        )
        v.append(
            (
                "regime+all",
                base.copy(
                    regimes={
                        Regime.QUIET,
                        Regime.CHOPPY,
                        Regime.VOLATILE,
                        Regime.TRENDING,
                    }
                ),
            )
        )
        v.append(
            (
                "combo_mod",
                base.copy(adx_range=(15.0, 30.0), sessions={"london", "ny_am"}),
            )
        )
        v.append(
            (
                "combo_aggro",
                base.copy(
                    adx_range=(12.0, 35.0),
                    sessions=None,
                    regimes={Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
                ),
            )
        )
    elif sid == "dual_tf_squeeze_pro":
        v.append(("sess+all", base.copy(sessions={"asia", "london", "ny_am"})))
        v.append(("sess_none", base.copy(sessions=None)))
        v.append(
            (
                "regime+trending",
                base.copy(regimes={Regime.VOLATILE, Regime.CHOPPY, Regime.TRENDING}),
            )
        )
        v.append(
            (
                "regime+all",
                base.copy(
                    regimes={
                        Regime.QUIET,
                        Regime.CHOPPY,
                        Regime.VOLATILE,
                        Regime.TRENDING,
                    }
                ),
            )
        )
        v.append(
            (
                "combo_mod",
                base.copy(
                    sessions={"asia", "london", "ny_am"},
                    regimes={Regime.VOLATILE, Regime.CHOPPY, Regime.TRENDING},
                ),
            )
        )
        v.append(
            (
                "combo_aggro",
                base.copy(
                    sessions=None,
                    regimes={
                        Regime.QUIET,
                        Regime.CHOPPY,
                        Regime.VOLATILE,
                        Regime.TRENDING,
                    },
                ),
            )
        )
    elif sid == "donchian_atr_trend_v2":
        v.append(("adx_[0,35]", base.copy(adx_range=(0.0, 35.0))))
        v.append(("adx_[0,40]", base.copy(adx_range=(0.0, 40.0))))
        v.append(("adx_none", base.copy(adx_range=(0.0, 100.0))))
        v.append(
            (
                "regime+vol",
                base.copy(
                    regimes={
                        Regime.QUIET,
                        Regime.CHOPPY,
                        Regime.TRENDING,
                        Regime.VOLATILE,
                    }
                ),
            )
        )
        v.append(
            (
                "combo_aggro",
                base.copy(
                    adx_range=(0.0, 100.0),
                    regimes={
                        Regime.QUIET,
                        Regime.CHOPPY,
                        Regime.TRENDING,
                        Regime.VOLATILE,
                    },
                ),
            )
        )
    elif sid == "srmr_plus":
        v.append(("sess+london_ny", base.copy(sessions={"london", "ny_am"})))
        v.append(("sess+all", base.copy(sessions={"asia", "london", "ny_am"})))
        v.append(("sess_none", base.copy(sessions=None)))
        v.append(("regime+choppy", base.copy(regimes={Regime.QUIET, Regime.CHOPPY})))
        v.append(
            (
                "regime+choppy_vol",
                base.copy(regimes={Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE}),
            )
        )
        v.append(
            (
                "combo_mod",
                base.copy(regimes={Regime.QUIET, Regime.CHOPPY}, sessions={"london", "ny_am"}),
            )
        )
        v.append(
            (
                "combo_aggro",
                base.copy(
                    regimes={Regime.QUIET, Regime.CHOPPY, Regime.VOLATILE},
                    sessions=None,
                ),
            )
        )
    return v


INFO = {
    "killzone_momentum": (
        ("XAUUSD", "M15"),
        lambda: KillzoneMomentumStrategy(KillzoneMomentumConfig()),
    ),
    "dual_tf_squeeze_pro": (
        ("XAUUSD", "M15"),
        lambda: DualTFSqueezeProStrategy(DualTFSqueezeProConfig()),
    ),
    "donchian_atr_trend_v2": (
        ("XAUUSD", "H1"),
        lambda: DonchianATRTrendV2Strategy(DonchianATRConfig()),
    ),
    "srmr_plus": (
        ("XAUUSD", "M15"),
        lambda: SRMRPlusStrategy(SRMRPlusConfig(symbol="XAUUSD")),
    ),
}


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    con.execute("SET threads=1; SET memory_limit='512MB'")
    det = RegimeDetector(RegimeConfig())

    tf_cache = {}
    for sid, ((sym, tf), _) in INFO.items():  # noqa: B007
        if (sym, tf) not in tf_cache:
            bars = load_bars(con, sym, tf)
            labels = get_or_cache_labels(sym, tf, bars, det)
            tf_cache[(sym, tf)] = (bars, *labels)

    print(
        f"\n# Gate Loosening Study — XAUUSD\n# Date: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n",
        flush=True,
    )
    all_r = {}
    for sid, ((sym, tf), factory) in INFO.items():
        bars, regimes, adxs, sess = tf_cache[(sym, tf)]
        base_gate = BASELINE[sid]
        print(f"## {sid} ({sym} {tf}, {len(bars)} bars)\n", flush=True)
        t0 = time.time()
        bm = run_variant(factory, bars, base_gate, regimes, adxs, sess)
        print(
            "| Variant | Trades | PF | Net $ | DD % | WR % |\n|---|---:|---:|---:|---:|---:|",
            flush=True,
        )
        print(
            f"| **BASELINE** | {bm['trades']} | {bm['pf']} | ${bm['net']} | {bm['dd_pct']}% | {bm['wr']}% |",
            flush=True,
        )
        vr = {"BASELINE": bm}
        for vn, vg in variants_for(sid, base_gate):
            m = run_variant(factory, bars, vg, regimes, adxs, sess)
            vr[vn] = m
            print(
                f"| {vn} | {m['trades']} | {m['pf']} | ${m['net']} | {m['dd_pct']}% | {m['wr']}% |",
                flush=True,
            )
        print(f"\n_({time.time() - t0:.1f}s)_\n", flush=True)
        all_r[sid] = vr

    # Summary
    print(
        "## Summary\n\n| Strategy | Best | Trades | PF | Net $ | DD % | Δ |\n|---|---|---:|---:|---:|---:|---:|",
        flush=True,
    )
    tb = ts = 0
    for sid, vr in all_r.items():
        b = vr["BASELINE"]
        tb += b["trades"]
        viable = {k: v for k, v in vr.items() if v["pf"] > 1.0 and v["dd_pct"] < 10.0 and k != "BASELINE"}
        if viable:
            bn = max(viable, key=lambda k: viable[k]["trades"])
            best = viable[bn]
            ts += best["trades"]
            print(
                f"| {sid} | {bn} | {best['trades']} | {best['pf']} | ${best['net']} | {best['dd_pct']}% | +{best['trades'] - b['trades']} |",  # noqa: E501
                flush=True,
            )
        else:
            ts += b["trades"]
            print(
                f"| {sid} | (none) | {b['trades']} | {b['pf']} | ${b['net']} | {b['dd_pct']}% | 0 |",
                flush=True,
            )
    print(f"| **TOTAL** | | **{ts}** | | | | +{ts - tb} |", flush=True)
    yrs = 4.5
    print(
        f"\n**Base:** {tb / yrs:.0f}/yr → **Loosened:** {ts / yrs:.0f}/yr → **Target:** 250/yr → **Gap:** {max(0, 250 - ts / yrs):.0f}/yr",  # noqa: E501
        flush=True,
    )

    out = project_root / "docs" / "research" / "strategy-profiles" / "gate_loosening_study_2026-07-22.md"
    with open(out, "w") as f:
        f.write(f"# Gate Loosening Study\n\n_Generated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}_\n\n")
        for sid, vr in all_r.items():
            f.write(f"## {sid}\n\n| Variant | Trades | PF | Net $ | DD % | WR % |\n|---|---:|---:|---:|---:|---:|\n")
            for vn, m in vr.items():
                f.write(f"| {vn} | {m['trades']} | {m['pf']} | ${m['net']} | {m['dd_pct']}% | {m['wr']}% |\n")
            f.write("\n")
    print(f"\nSaved to {out}", flush=True)
    con.close()


if __name__ == "__main__":
    main()
