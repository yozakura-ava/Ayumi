#!/usr/bin/env python3
"""Gated Blend Backtest — Two-Phase Signal Generation + Gate Filtering.

Tests the existing 4-strategy blend vs a 5-strategy blend (with London Breakout).

Uses a gate-first evaluation model matching the original gated blend study:
1. For each bar: check gate FIRST, only call evaluate() if gate passes
2. DualTF: on_bar() called on EVERY bar (for H1 synthesis), evaluate() only on gate-pass
3. Simulate every signal as a trade (3-leg partial exits)
4. Aggregate into portfolio metrics

This reconciles the position-management model mismatch. The previous versions
interleaved evaluation with position management, corrupting DualTF's internal
state (which requires on_bar() on every bar for H1 synthesis).

Key model details:
- Rolling 300-bar window for M15, 100 for H1
- DualTF: on_bar() called every bar, _h1_bars capped at 200
- Every gate-passing signal becomes a trade (no position blocking)
- 3-leg partial exits: 1/3 at 1R, 1/3 at 2R, 1/3 at 3R
- Time stop: 50 bars
- $50 risk per trade

Usage:
    python3 scripts/run_blend_5strat.py [--costs] [--full]
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

import duckdb
from core.types import Bar, BarPeriod, MarketState, SessionType, TradeDirection
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
from strategies.london_breakout_retest import (
    LondonBreakoutConfig,
    LondonBreakoutRetestStrategy,
)
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

# Use the real repo for data files (worktree may not have the DuckDB)
_REPO_ROOT = Path("$AYUMI_ROOT")
DB_PATH = _REPO_ROOT / "data" / "ayumi_market.duckdb"
CACHE_DIR = _REPO_ROOT / "data" / "cache"
RISK = 50.0
ACCOUNT = 10000.0
MAX_BARS_IN_TRADE = 50  # Time stop: 50 bars

# Rolling window sizes (matching original blend_eval.py)
WINDOW_SIZES = {
    ("killzone_momentum", "M15"): 300,
    ("killzone_momentum", "H1"): 100,
    (
        "dual_tf_squeeze_pro",
        "M15",
    ): 300,  # window used for evaluate(); on_bar gets every bar
    ("dual_tf_squeeze_pro", "H1"): 100,
    ("donchian_atr_trend_v2", "M15"): 300,
    ("donchian_atr_trend_v2", "H1"): 100,
    ("srmr_plus", "M15"): 300,
    ("srmr_plus", "H1"): 100,
    ("london_breakout_retest", "M15"): 300,
    ("london_breakout_retest", "H1"): 100,
}


@dataclass
class GateConfig:
    regimes: set
    adx_range: tuple
    sessions: object  # set or None


# Per-strategy gates — must match launch_blend_forward_test.py RegimeGate exactly
GATES = {
    "killzone_momentum": GateConfig({Regime.QUIET, Regime.CHOPPY}, (18.0, 25.0), {"london"}),
    "dual_tf_squeeze_pro": GateConfig({Regime.VOLATILE, Regime.CHOPPY}, (0.0, 100.0), {"asia", "ny_am"}),
    "donchian_atr_trend_v2": GateConfig({Regime.QUIET, Regime.CHOPPY, Regime.TRENDING}, (0.0, 30.0), None),
    "srmr_plus": GateConfig({Regime.QUIET}, (0.0, 100.0), {"london"}),
    "london_breakout_retest": GateConfig(
        {Regime.QUIET, Regime.CHOPPY, Regime.TRENDING, Regime.VOLATILE},
        (15.0, 30.0),
        {"london"},
    ),
}

STRATEGY_TIMEFRAMES = {
    "killzone_momentum": "M15",
    "dual_tf_squeeze_pro": "M15",
    "donchian_atr_trend_v2": "H1",
    "srmr_plus": "M15",
    "london_breakout_retest": "M15",
}


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


def session_str(hour):
    if 0 <= hour < 7:
        return "asia"
    elif 7 <= hour < 12:
        return "london"
    elif 12 <= hour < 17:
        return "ny_am"
    return "other"


def load_bars(symbol, tf):
    con = duckdb.connect(str(DB_PATH), read_only=True)
    con.execute("SET threads=1; SET memory_limit='512MB'")
    rows = con.execute(
        f"SELECT timestamp_utc, open, high, low, close, volume FROM bars "  # noqa: S608
        f"WHERE symbol='{symbol}' AND timeframe='{tf}' ORDER BY timestamp_utc ASC"
    ).fetchall()
    con.close()
    is_ms = rows[0][0] > 1e12
    bp_map = {"M15": BarPeriod.M15, "H1": BarPeriod.H1}
    return [
        Bar(
            time=datetime.fromtimestamp(r[0] / 1000.0 if is_ms else r[0], tz=timezone.utc),
            open=r[1],
            high=r[2],
            low=r[3],
            close=r[4],
            volume=r[5],
            period=bp_map.get(tf, BarPeriod.M15),
        )
        for r in rows
    ]


def get_labels(symbol, tf, bars, detector):
    """Get or compute regime/ADX/session labels for each bar."""
    cache_file = next(CACHE_DIR.glob(f"labels_{symbol}_{tf}_*.pkl"), None) if CACHE_DIR.exists() else None
    if cache_file:
        with open(cache_file, "rb") as f:
            return pickle.load(f)  # noqa: S301
    print(f"Precomputing labels for {symbol} {tf}...", flush=True)
    t0 = time.time()
    regimes, adxs, sess = [], [], []
    for i in range(len(bars)):
        r = None
        a = 0.0
        if i >= 100:
            w = bars[max(0, i - 100) : i + 1]
            h = np.array([b.high for b in w])
            l = np.array([b.low for b in w])  # noqa: E741
            c = np.array([b.close for b in w])
            try:
                r = detector.detect_current(h, l, c)
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
        sess.append(session_str(bars[i].time.hour))
    print(f"  Done in {time.time() - t0:.1f}s")
    cache_file = (
        CACHE_DIR
        / f"labels_{symbol}_{tf}_{hash((symbol, tf, len(bars), bars[0].time, bars[-1].time)) & 0xFFFFFFFF:x}.pkl"
    )
    with open(cache_file, "wb") as f:
        pickle.dump((regimes, adxs, sess), f)
    return regimes, adxs, sess


def compute_atr_percentile(bars, i, window=100):
    """Compute ATR percentile for bar i over the given window."""
    if i < window:
        return 1.0
    w = bars[i - window : i + 1]
    trs = []
    for j in range(1, len(w)):
        tr = max(
            w[j].high - w[j].low,
            abs(w[j].high - w[j - 1].close),
            abs(w[j].low - w[j - 1].close),
        )
        trs.append(tr)
    if not trs:
        return 1.0
    current_tr = trs[-1]
    return sum(1 for t in trs if t <= current_tr) / len(trs)


def gate_pass(i, gate, regimes, adxs, sessions, bars=None, strategy_id=None):
    """Check if bar i passes the regime/ADX/session gate."""
    r = regimes[i]
    if r is None or r not in gate.regimes:
        return False
    lo, hi = gate.adx_range
    if lo > 0 or hi < 100:
        if adxs[i] < lo or adxs[i] > hi:
            return False
    if gate.sessions is not None and sessions[i] not in gate.sessions:
        return False
    if strategy_id == "donchian_atr_trend_v2" and bars is not None:
        pctile = compute_atr_percentile(bars, i)
        if pctile >= 0.50:
            return False
    return True


def make_factory(strategy_id):
    return {
        "killzone_momentum": lambda: KillzoneMomentumStrategy(KillzoneMomentumConfig()),
        "dual_tf_squeeze_pro": lambda: DualTFSqueezeProStrategy(DualTFSqueezeProConfig()),
        "donchian_atr_trend_v2": lambda: DonchianATRTrendV2Strategy(DonchianATRConfig()),
        "srmr_plus": lambda: SRMRPlusStrategy(SRMRPlusConfig(symbol="XAUUSD")),
        "london_breakout_retest": lambda: LondonBreakoutRetestStrategy(LondonBreakoutConfig()),
    }[strategy_id]


# ---------------------------------------------------------------------------
# Phase 1: Signal Generation (matching original blend_eval.py)
# ---------------------------------------------------------------------------


def generate_signals_gated(
    strategy_id: str,
    bars: list,
    timeframe: str,
    gate: GateConfig,
    regimes: list,
    adxs: list,
    sessions: list,
) -> tuple[list, dict]:
    """Walk bars with gate-first evaluation.

    CRITICAL: The gate is checked BEFORE calling evaluate(). This matches the
    original gated blend study: evaluate() is only called on gate-accepted bars.
    Calling evaluate() on gate-rejected bars changes strategy internal state,
    producing spurious signals on later gate-accepted bars.

    For dual_tf_squeeze_pro: on_bar() is called on EVERY bar (regardless of
    gate) to advance H1 synthesis state. evaluate() is only called on
    gate-accepted bars. _h1_bars is capped at 200.
    For all others: evaluate() is called only on gate-accepted bars.

    Returns (signals, stats) where signals is list of (bar_index, signal)
    and stats has gate/signal counts.
    """
    window_size = WINDOW_SIZES.get((strategy_id, timeframe), 300)
    strat = make_factory(strategy_id)()

    # DualTF: cap _h1_bars to prevent O(n²) (matching original blend_eval.py)
    if strategy_id == "dual_tf_squeeze_pro":
        orig_update = strat._update_h1
        H1_MAX = 200

        def _bounded_update_h1(bar):
            orig_update(bar)
            if len(strat._h1_bars) > H1_MAX:
                strat._h1_bars = strat._h1_bars[-H1_MAX:]

        strat._update_h1 = _bounded_update_h1

    signals = []
    window = []
    n = len(bars)
    last_progress = 0
    gate_rejected = 0
    gate_accepted = 0
    no_signal = 0

    for i in range(n):
        bar = bars[i]

        # Progress output every 10K bars
        if i - last_progress >= 10000:
            last_progress = i
            print(
                f"    [{strategy_id}] bar {i}/{n} ({i * 100 // n}%), accepted={gate_accepted}, signals={len(signals)}",
                flush=True,
            )

        # Manage rolling window (always advances, even on gate-rejected bars)
        if len(window) < window_size:
            window.append(bar)
            # DualTF: advance internal state during warmup
            if strategy_id == "dual_tf_squeeze_pro":
                strat.on_bar(bar)
            continue

        window.pop(0)
        window.append(bar)

        # DualTF: advance internal state on EVERY bar (H1 synthesis)
        if strategy_id == "dual_tf_squeeze_pro":
            strat.on_bar(bar)

        # Gate check BEFORE evaluate (matches original gated study)
        if not gate_pass(i, gate, regimes, adxs, sessions, bars=bars, strategy_id=strategy_id):
            gate_rejected += 1
            continue

        gate_accepted += 1

        # Evaluate only on gate-accepted bars
        state = MarketState(bars=list(window), current_session=session_for(bar.time.hour))
        try:
            sig = strat.evaluate(state)
        except Exception:
            no_signal += 1
            continue
        if sig is None or sig.direction == TradeDirection.NEUTRAL:
            no_signal += 1
            continue
        if sig.entry_price <= 0 or sig.stop_loss <= 0 or sig.take_profit_1 <= 0:
            no_signal += 1
            continue

        signals.append((i, sig))

    stats = {
        "total_bars": n,
        "gate_rejected": gate_rejected,
        "gate_accepted": gate_accepted,
        "no_signal": no_signal,
        "signals": len(signals),
    }
    return signals, stats


# ---------------------------------------------------------------------------
# Phase 2: Trade Simulation (matching original simulate_trade)
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    strategy_id: str
    entry_bar: int
    direction: str  # "long" or "short"
    entry: float
    stop: float
    pnl: float
    outcome: str  # SL, TP1, TP2, TP3, TIME


def simulate_trade(
    strategy_id: str,
    signal,
    entry_bar: int,
    bars: list,
    spread_pips: float = 0.0,
    commission_per_lot: float = 0.0,
) -> Trade:
    """Simulate a single trade with 3-leg partial exits.

    Entry on the signal's entry_price. Then walk forward bars checking:
    1. Stop loss (full exit of remaining legs)
    2. Take profits (sequential: TP1, TP2, TP3)
    3. Time stop at MAX_BARS_IN_TRADE (50 bars)

    Returns one Trade with aggregated PnL.
    """
    is_long = signal.direction == TradeDirection.LONG
    entry_price = signal.entry_price
    sl = signal.stop_loss
    tps = [signal.take_profit_1, signal.take_profit_2, signal.take_profit_3]
    risk_per_unit = abs(entry_price - sl)

    if risk_per_unit == 0:
        return Trade(
            strategy_id,
            entry_bar,
            "long" if is_long else "short",
            entry_price,
            sl,
            0.0,
            "INVALID",
        )

    pos_size = RISK / risk_per_unit  # units
    start = entry_bar + 1
    end = min(start + MAX_BARS_IN_TRADE, len(bars))

    legs_open = 3
    leg_pnls = []
    exit_bar = end - 1
    outcome = "TIME"

    for j in range(start, end):
        bar = bars[j]

        # Check SL first (exits all remaining legs)
        if is_long:
            hit_sl = bar.low <= sl
        else:
            hit_sl = bar.high >= sl
        if hit_sl:
            remaining = legs_open
            pnl = -risk_per_unit * pos_size * (1.0 / 3.0) * remaining
            if spread_pips > 0:
                pnl -= risk_per_unit * pos_size * spread_pips / 10000
            if commission_per_lot > 0:
                pnl -= commission_per_lot * pos_size / 100000
            leg_pnls.append(pnl)
            legs_open = 0
            exit_bar = j
            outcome = "SL"
            break

        # Check TPs (sequential)
        for leg_idx in range(min(legs_open, 3)):
            tp = tps[leg_idx]
            if is_long:
                hit_tp = bar.high >= tp
            else:
                hit_tp = bar.low <= tp
            if hit_tp:
                pnl = abs(tp - entry_price) * pos_size * (1.0 / 3.0)
                if spread_pips > 0:
                    pnl -= abs(tp - entry_price) * pos_size * spread_pips / 10000
                if commission_per_lot > 0:
                    pnl -= commission_per_lot * pos_size / 300000
                leg_pnls.append(pnl)
                legs_open -= 1
                outcome = f"TP{leg_idx + 1}"

        if legs_open == 0:
            exit_bar = j
            break

    # Time stop: exit remaining legs at close
    if legs_open > 0:
        last_close = bars[exit_bar].close
        if is_long:
            pnl = (last_close - entry_price) * pos_size * (1.0 / 3.0) * legs_open
        else:
            pnl = (entry_price - last_close) * pos_size * (1.0 / 3.0) * legs_open
        if spread_pips > 0:
            pnl -= abs(last_close - entry_price) * pos_size * spread_pips / 10000
        if commission_per_lot > 0:
            pnl -= commission_per_lot * pos_size / 100000
        leg_pnls.append(pnl)
        outcome = "TIME"

    total_pnl = sum(leg_pnls)
    return Trade(
        strategy_id,
        entry_bar,
        "long" if is_long else "short",
        entry_price,
        sl,
        total_pnl,
        outcome,
    )


def backtest_strategy_gated(
    strategy_id: str,
    bars: list,
    gate: GateConfig,
    regimes: list,
    adxs: list,
    sessions: list,
    timeframe: str,
    spread_pips: float = 0.0,
    commission_per_lot: float = 0.0,
) -> list[Trade]:
    """Gate-first gated backtest for a single strategy.

    Phase 1: Walk bars with gate-first evaluation (gate check → evaluate only if passed).
    Phase 2: Simulate each signal as a trade.

    Returns list of Trade objects.
    """
    t0 = time.time()

    # Phase 1: Generate signals (gate-first)
    signals, stats = generate_signals_gated(strategy_id, bars, timeframe, gate, regimes, adxs, sessions)

    # Phase 2: Simulate trades
    trades = []
    for bar_idx, sig in signals:
        trade = simulate_trade(
            strategy_id,
            sig,
            bar_idx,
            bars,
            spread_pips=spread_pips,
            commission_per_lot=commission_per_lot,
        )
        trades.append(trade)

    elapsed = time.time() - t0
    gate_pct = stats["gate_accepted"] * 100 / stats["total_bars"] if stats["total_bars"] else 0
    print(
        f"  {strategy_id} ({timeframe}): "
        f"bars={stats['total_bars']}, rejected={stats['gate_rejected']}, "
        f"accepted={stats['gate_accepted']} ({gate_pct:.1f}%), "
        f"no_signal={stats['no_signal']}, trades={len(trades)} "
        f"in {elapsed:.1f}s"
    )

    return trades


def metrics(trades):
    """Compute portfolio metrics from list of Trade objects."""
    if not trades:
        return {"trades": 0, "pf": 0, "net": 0, "dd_pct": 0, "wr": 0, "per_strat": {}}

    # Sort by entry_bar for correct equity curve ordering
    sorted_trades = sorted(trades, key=lambda t: t.entry_bar)
    pnls = [t.pnl for t in sorted_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    pf = gw / gl if gl > 0 else 999.0
    eq = np.cumsum(pnls)
    pk = np.maximum.accumulate(eq)
    dd = pk - eq
    max_dd = max(dd) if len(dd) else 0

    per_strat = {}
    for t in sorted_trades:
        per_strat.setdefault(t.strategy_id, []).append(t.pnl)

    return {
        "trades": len(trades),
        "pf": round(pf, 3),
        "net": round(sum(pnls), 2),
        "dd_pct": round((max_dd / ACCOUNT) * 100, 2),
        "wr": round(len(wins) / len(pnls) * 100, 1),
        "per_strat": per_strat,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--costs", action="store_true", help="Apply realistic costs")
    parser.add_argument("--full", action="store_true", help="Use all bars (default: all bars)")
    args = parser.parse_args()

    detector = RegimeDetector(RegimeConfig())

    spread = 2.5 if args.costs else 0.0
    comm = 3.5 if args.costs else 0.0
    if args.costs:
        print("APPLYING REALISTIC COSTS: spread=2.5p, commission=$3.5/lot")
    else:
        print("NO COSTS (spread/commission/slippage all 0)")

    # Load bars per timeframe
    tf_bars = {}
    tf_labels = {}
    for tf in ["M15", "H1"]:
        bars = load_bars("XAUUSD", tf)
        regimes, adxs, sessions = get_labels("XAUUSD", tf, bars, detector)
        if len(regimes) != len(bars):
            print(
                f"  WARNING: label/bar mismatch ({len(regimes)} labels vs {len(bars)} bars) — truncating to {len(regimes)}"  # noqa: E501
            )
            bars = bars[: len(regimes)]
        tf_bars[tf] = bars
        tf_labels[tf] = (regimes, adxs, sessions)
        print(f"Loaded {len(bars)} {tf} bars with {len(regimes)} labels")

    m15_bars = tf_bars["M15"]
    h1_bars = tf_bars["H1"]

    blend_4 = [
        "killzone_momentum",
        "dual_tf_squeeze_pro",
        "donchian_atr_trend_v2",
        "srmr_plus",
    ]
    blend_5 = [
        "killzone_momentum",
        "dual_tf_squeeze_pro",
        "donchian_atr_trend_v2",
        "srmr_plus",
        "london_breakout_retest",
    ]

    print("\n# Gated Blend Backtest — XAUUSD (Two-Phase Model v6)")
    print(f"# Date: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    print("# Methodology: Phase 1: generate all signals (rolling 300/100-bar window)")
    print("#              Phase 2: filter by gate → simulate each signal as trade")
    print("# DualTF: on_bar() on every bar, _h1_bars capped at 200")
    print("# Trade model: 3-leg partials (1/3 at 1R/2R/3R), 50-bar time stop, $50 risk")
    print("# No position blocking — every gate-passing signal becomes a trade")
    print(f"# Bars: M15={len(m15_bars)}, H1={len(h1_bars)}", flush=True)
    print()

    # -- 4-strategy blend --
    print("## Running 4-strategy blend (KZ + DualTF + Donchian + SRMR+)...")
    t0 = time.time()
    all_trades_4 = []
    per_strat_4 = {}
    for sid in blend_4:
        tf = STRATEGY_TIMEFRAMES[sid]
        bars = tf_bars[tf]
        regimes, adxs, sessions = tf_labels[tf]
        gate = GATES[sid]
        strat_trades = backtest_strategy_gated(
            sid,
            bars,
            gate,
            regimes,
            adxs,
            sessions,
            tf,
            spread_pips=spread,
            commission_per_lot=comm,
        )
        all_trades_4.extend(strat_trades)
        per_strat_4[sid] = strat_trades

    all_trades_4.sort(key=lambda t: t.entry_bar)
    m_4 = metrics(all_trades_4)
    print("\n### 4-strategy blend portfolio:")
    print(f"  Trades: {m_4['trades']}, PF: {m_4['pf']}, Net: ${m_4['net']}, DD: {m_4['dd_pct']}%, WR: {m_4['wr']}%")
    print(f"  Time: {time.time() - t0:.1f}s")

    # Per-strategy breakdown
    for sid in blend_4:
        trades = per_strat_4.get(sid, [])
        if trades:
            pnls = [t.pnl for t in trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            gw, gl = sum(wins), abs(sum(losses))
            pf = gw / gl if gl > 0 else 999
            wr = len(wins) / len(pnls) * 100
            net = sum(pnls)
            print(f"  {sid}: {len(trades)} trades, PF={pf:.3f}, Net=${net:.2f}, WR={wr:.1f}%")

    # -- 5-strategy blend --
    print("\n## Running 5-strategy blend (KZ + DualTF + Donchian + SRMR+ + LBO)...")
    t0 = time.time()
    all_trades_5 = []
    per_strat_5 = {}
    for sid in blend_5:
        tf = STRATEGY_TIMEFRAMES[sid]
        bars = tf_bars[tf]
        regimes, adxs, sessions = tf_labels[tf]
        gate = GATES[sid]
        strat_trades = backtest_strategy_gated(
            sid,
            bars,
            gate,
            regimes,
            adxs,
            sessions,
            tf,
            spread_pips=spread,
            commission_per_lot=comm,
        )
        all_trades_5.extend(strat_trades)
        per_strat_5[sid] = strat_trades

    all_trades_5.sort(key=lambda t: t.entry_bar)
    m_5 = metrics(all_trades_5)
    print("\n### 5-strategy blend portfolio:")
    print(f"  Trades: {m_5['trades']}, PF: {m_5['pf']}, Net: ${m_5['net']}, DD: {m_5['dd_pct']}%, WR: {m_5['wr']}%")
    print(f"  Time: {time.time() - t0:.1f}s")

    # -- Per-strategy contribution table --
    print("\n## Per-strategy contribution to 5-strategy blend:")
    print("| Strategy | TF | Trades | PF | Net $ | WR % |")
    print("|---|---|---:|---:|---:|---:|")
    for sid in blend_5:
        trades = per_strat_5.get(sid, [])
        if trades:
            pnls = [t.pnl for t in trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            gw, gl = sum(wins), abs(sum(losses))
            pf = gw / gl if gl > 0 else 999
            wr = len(wins) / len(pnls) * 100
            net = sum(pnls)
            tf = STRATEGY_TIMEFRAMES[sid]
            print(f"| {sid} | {tf} | {len(trades)} | {pf:.3f} | ${net:.2f} | {wr:.1f}% |")

    # -- Comparison --
    print("\n## Comparison: 4-strategy vs 5-strategy blend")
    print("| Metric | 4-strategy | 5-strategy | D |")
    print("|---|---:|---:|---:|")
    print(f"| Trades | {m_4['trades']} | {m_5['trades']} | {m_5['trades'] - m_4['trades']:+d} |")
    print(f"| PF | {m_4['pf']} | {m_5['pf']} | {m_5['pf'] - m_4['pf']:+.3f} |")
    print(f"| Net $ | ${m_4['net']} | ${m_5['net']} | ${m_5['net'] - m_4['net']:+.2f} |")
    print(f"| DD % | {m_4['dd_pct']}% | {m_5['dd_pct']}% | {m_5['dd_pct'] - m_4['dd_pct']:+.2f}% |")
    print(f"| WR % | {m_4['wr']}% | {m_5['wr']}% | {m_5['wr'] - m_4['wr']:+.1f}% |")

    # -- Original baseline reference --
    print("\n## Original baseline reference (gated_blend_results_2026-07-22.md):")
    print("| Metric | Original | 4-strategy (this run) | Match? |")
    print("|---|---:|---:|:---:|")
    print(f"| Trades | 170 | {m_4['trades']} | {'YES' if abs(m_4['trades'] - 170) <= 30 else 'NO'} |")
    print(f"| PF | 1.742 | {m_4['pf']} | {'YES' if abs(m_4['pf'] - 1.742) <= 0.3 else 'NO'} |")
    print(f"| Net | $2,820 | ${m_4['net']} | {'YES' if abs(m_4['net'] - 2820) <= 500 else 'NO'} |")
    print(f"| DD | 5.67% | {m_4['dd_pct']}% | {'YES' if abs(m_4['dd_pct'] - 5.67) <= 2.0 else 'NO'} |")

    # -- Annualized --
    bars_per_year = 96 * 365
    years_m15 = len(m15_bars) / bars_per_year
    print(f"\n## Annualized (assuming {years_m15:.1f} years of data):")
    print("| Blend | Trades/year |")
    print("|---|---:|")
    print(f"| 4-strategy | {m_4['trades'] / years_m15:.1f} |")
    print(f"| 5-strategy | {m_5['trades'] / years_m15:.1f} |")
    print(f"| D | +{m_5['trades'] / years_m15 - m_4['trades'] / years_m15:.1f} |")
    print(f"\n**Target:** 250/year. **5-strategy gap:** {max(0, 250 - m_5['trades'] / years_m15):.0f}/year")
    print(
        f"\n**5-strategy FTMO viability:** PF={m_5['pf']}, DD={m_5['dd_pct']}% (req DD<10%)",
        "PASS" if m_5["dd_pct"] < 10 else "FAIL",
    )


if __name__ == "__main__":
    main()
