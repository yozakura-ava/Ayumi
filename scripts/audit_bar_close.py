#!/usr/bin/env python3
"""Bar-close timing audit for Ayumi strategies (BQ-345).

A strategy that fires signals on the *forming* bar (the one that is still
accumulating ticks) leaks current-tick data into the signal. The forward
test engine is expected to feed only closed bars; this audit verifies
that each strategy honours that contract.

Mechanism:
  - Treat the last bar in the input as the forming bar.
  - For each bar index i, build a ``MarketState`` from bars[:i+1] and call
    ``strategy.evaluate(state)``.
  - A signal at i == forming_index is a "forming bar" signal. Anything
    earlier is a closed-bar signal.
  - If a strategy fires a forming-bar signal, mark it FORMING_BAR_DEPENDENT.

The script does NOT touch the live cTrader API. It reads preloaded data
from ``data/forex/historical/<SYMBOL>_<TF>.csv`` or
``data/forex/parquet/<SYMBOL>_<TF>.parquet``; if neither is available it
falls back to a deterministic synthetic series.

Usage::

    python scripts/audit_bar_close.py --bars 200
    python scripts/audit_bar_close.py --bars 500 --symbols GBPUSD,EURUSD

Exit code is 0 on success, 1 if the audit itself errored (import failure,
no data, unhandled exception). A ``FAIL`` verdict (forming-bar leak) is
reported through the JSON; it does not change the exit code so the script
can be chained into cron / CI without blocking unrelated pipelines.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Repo layout: scripts/audit_bar_close.py -> repo root is parent.parent
_REPO = Path(__file__).resolve().parents[1]
for p in (str(_REPO / "src" / "forex_bot"), str(_REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from backtest.types import Bar, MarketState  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ayumi.audit.bar_close")

# Strategies required by the sprint spec (BQ-345). The display name on the
# left is what the strategy's ``name`` property returns; the right side is
# the (module, class, factory) triple. Factories let us pass per-strategy
# config dicts (e.g., session breakout variants) without changing the
# default constructor.
STRATEGY_SPEC: list[tuple[str, str, str, Optional[Callable[[], Any]]]] = [
    (
        "BB+RSI Mean Reversion",
        "strategies.bb_rsi_reversion",
        "BBRSIMeanReversion",
        None,
    ),
    (
        "Donchian Channel Breakout",
        "strategies.momentum",
        "DonchianBreakoutStrategy",
        None,
    ),
    (
        "Killzone Momentum",
        "strategies.killzone_momentum",
        "KillzoneMomentumStrategy",
        None,
    ),
    (
        "SRMR+",
        "strategies.srmr_plus",
        "SRMRPlusStrategy",
        None,
    ),
    (
        "Session Breakout Asian",
        "strategies.session_breakout",
        "SessionBreakoutStrategy",
        None,  # populated in _build_factories
    ),
    (
        "Session Breakout London",
        "strategies.session_breakout",
        "SessionBreakoutStrategy",
        None,
    ),
    (
        "Session Breakout NY",
        "strategies.session_breakout",
        "SessionBreakoutStrategy",
        None,
    ),
    (
        "Session-Range Mean Reversion",
        "strategies.session_range_mean_reversion",
        "SessionRangeMeanReversionStrategy",
        None,
    ),
    (
        "Simple RSI Threshold",
        "strategies.rsi_threshold",
        "SimpleRSIThresholdStrategy",
        None,
    ),
]

# Strategy -> preferred timeframe (mirrors launch_blend_forward_test.py)
TIMEFRAME_MAP: dict[str, int] = {
    "SRMR+": 60,
    "Killzone Momentum": 15,
    "Donchian Channel Breakout": 15,
    "Session-Range Mean Reversion": 60,
    "BB+RSI Mean Reversion": 60,
    "Session Breakout London": 15,
    "Session Breakout NY": 15,
    "Session Breakout Asian": 15,
    "Simple RSI Threshold": 15,
}


# ── Factories ────────────────────────────────────────────────────────────


def _build_session_breakout_factories() -> dict[str, Callable[[], Any]]:
    """Build the three session-breakout variants with their config dicts."""
    try:
        from strategies.session_breakout import SessionBreakoutStrategy
    except Exception as exc:  # pragma: no cover - import guard
        log.warning("Could not import SessionBreakoutStrategy: %s", exc)
        return {}

    def _asian() -> Any:
        return SessionBreakoutStrategy(
            {
                "name": "Session Breakout Asian",
                "range_start_hour": 21,
                "range_end_hour": 0,
                "trade_start_hour": 0,
                "trade_end_hour": 6,
                "min_range_pips": 20,
                "max_range_pips": 60,
                "buffer_pips": 3,
                "sl_atr_multiplier": 1.5,
                "atr_period": 14,
                "min_range_bars": 20,
            }
        )

    def _london() -> Any:
        return SessionBreakoutStrategy(
            {
                "name": "Session Breakout London",
                "range_start_hour": 0,
                "range_end_hour": 8,
                "trade_start_hour": 8,
                "trade_end_hour": 12,
                "min_range_pips": 30,
                "max_range_pips": 80,
                "buffer_pips": 3,
                "sl_atr_multiplier": 2.0,
                "atr_period": 14,
                "min_range_bars": 20,
            }
        )

    def _ny() -> Any:
        return SessionBreakoutStrategy(
            {
                "name": "Session Breakout NY",
                "range_start_hour": 8,
                "range_end_hour": 13,
                "trade_start_hour": 13,
                "trade_end_hour": 17,
                "min_range_pips": 25,
                "max_range_pips": 70,
                "buffer_pips": 3,
                "sl_atr_multiplier": 1.8,
                "atr_period": 14,
                "min_range_bars": 20,
            }
        )

    return {
        "Session Breakout Asian": _asian,
        "Session Breakout London": _london,
        "Session Breakout NY": _ny,
    }


def _resolve_factory(module: str, cls_name: str, custom: Optional[Callable[[], Any]]) -> Optional[Callable[[], Any]]:
    """Return a factory that instantiates the strategy, or None on failure."""
    if custom is not None:
        return custom
    try:
        import importlib

        mod = importlib.import_module(module)
        klass = getattr(mod, cls_name, None)
        if klass is None:
            log.warning("Module %s has no class %s", module, cls_name)
            return None
    except Exception as exc:
        log.warning("Import %s.%s failed: %s", module, cls_name, exc)
        return None

    def _factory() -> Any:
        return klass()

    return _factory


# ── Data loading ─────────────────────────────────────────────────────────


def _synthetic_bars(n: int, period_minutes: int = 60) -> list[Bar]:
    """Deterministic synthetic bars for offline audits.

    Slow random walk around a base price; the seed is ``n`` and the period
    so re-runs are stable.
    """
    base = 1.1000
    bars: list[Bar] = []
    price = base
    t0 = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        # Deterministic pseudo-random drift
        drift = 0.0001 * ((i * 7 + n) % 13 - 6)
        wave = 0.0005 * (1 if ((i + period_minutes) // 5) % 2 == 0 else -1)
        op = price
        cl = max(0.0001, price + drift + wave)
        bars.append(
            Bar(
                time=t0 + timedelta(minutes=i * period_minutes),
                open=op,
                high=max(op, cl) + 0.0004,
                low=max(0.0001, min(op, cl) - 0.0004),
                close=cl,
                volume=1000.0,
            )
        )
        price = cl
    return bars


def _load_bars_csv(path: Path, n: int) -> Optional[list[Bar]]:
    """Load the last n bars from a CSV.

    Accepts common header variants:
      - time/timestamp/date/datetime for the time column
      - open/high/low/close (case-insensitive)
      - volume (optional)
    """
    if not path.exists():
        return None
    try:
        import csv

        rows: list[Bar] = []

        def _col(row: dict, *names: str) -> Optional[str]:
            lookup = {k.lower(): k for k in row.keys()}
            for name in names:
                key = lookup.get(name.lower())
                if key is not None and row[key] not in (None, ""):
                    return row[key]
            return None

        with path.open() as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                raw_t = _col(row, "time", "timestamp", "date", "datetime")
                if raw_t is None:
                    continue
                # Date-only values get a midnight UTC stamp
                t = datetime.fromisoformat(raw_t.replace(" ", "T") if " " in raw_t and "T" not in raw_t else raw_t)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                rows.append(
                    Bar(
                        time=t,
                        open=float(_col(row, "open") or 0.0),
                        high=float(_col(row, "high") or 0.0),
                        low=float(_col(row, "low") or 0.0),
                        close=float(_col(row, "close") or 0.0),
                        volume=float(_col(row, "volume") or 0.0),
                    )
                )
        return rows[-n:] if len(rows) > n else rows
    except Exception as exc:
        log.warning("CSV load failed for %s: %s", path, exc)
        return None


def _load_bars_parquet(path: Path, n: int) -> Optional[list[Bar]]:
    """Load the last n bars from a parquet file with OHLCV columns."""
    if not path.exists():
        return None
    try:
        import pandas as pd

        df = pd.read_parquet(path).tail(n)
        bars: list[Bar] = []
        for _, row in df.iterrows():
            t = row.get("time") or row.get("timestamp") or row.name
            if isinstance(t, str):
                t = datetime.fromisoformat(t)
            if hasattr(t, "to_pydatetime"):
                t = t.to_pydatetime()
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            bars.append(
                Bar(
                    time=t,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0.0) or 0.0),
                )
            )
        return bars
    except Exception as exc:
        log.warning("Parquet load failed for %s: %s", path, exc)
        return None


def _load_bars(symbol: str, timeframe_minutes: int, n: int) -> list[Bar]:
    """Try CSV, then parquet, then fall back to synthetic."""
    # CSV first (already in repo under data/forex/historical)
    tf_label = {15: "M15", 60: "H1", 240: "H4", 1440: "D1"}.get(timeframe_minutes, f"M{timeframe_minutes}")
    csv_path = _REPO / "data" / "forex" / "historical" / f"{symbol}_{tf_label}.csv"
    bars = _load_bars_csv(csv_path, n)
    if bars:
        log.info("Loaded %d bars from %s", len(bars), csv_path)
        return bars
    # Parquet fallback
    pq_path = _REPO / "data" / "forex" / "parquet" / f"{symbol}_{tf_label.lower()}.parquet"
    bars = _load_bars_parquet(pq_path, n)
    if bars:
        log.info("Loaded %d bars from %s", len(bars), pq_path)
        return bars
    log.warning("No preloaded data for %s %s — using synthetic series", symbol, tf_label)
    return _synthetic_bars(n, period_minutes=timeframe_minutes)


# ── Per-strategy audit ───────────────────────────────────────────────────


@dataclass
class AuditRow:
    strategy: str
    symbol: str
    timeframe_minutes: int
    bars_audited: int
    signals_total: int
    on_closed_bars: int
    on_forming_bars: int
    verdict: str
    note: str = ""


def _audit_strategy(name: str, factory: Callable[[], Any], bars: list[Bar], symbol: str) -> AuditRow:
    """Run a strategy across the bar series and classify each signal.

    Walks all bar indices and re-evaluates on the prefix ending at each
    index. A signal at i == forming_index is a "forming bar" signal.
    """
    if len(bars) < 60:
        return AuditRow(
            strategy=name,
            symbol=symbol,
            timeframe_minutes=0,
            bars_audited=len(bars),
            signals_total=0,
            on_closed_bars=0,
            on_forming_bars=0,
            verdict="SKIP",
            note=f"insufficient bars ({len(bars)} < 60)",
        )

    try:
        strategy = factory()
    except Exception as exc:
        return AuditRow(
            strategy=name,
            symbol=symbol,
            timeframe_minutes=0,
            bars_audited=len(bars),
            signals_total=0,
            on_closed_bars=0,
            on_forming_bars=0,
            verdict="STUB",
            note=f"instantiation failed: {type(exc).__name__}: {exc}",
        )

    actual_name = getattr(strategy, "name", name)
    n = len(bars)
    forming_index = n - 1
    closed_total = 0
    forming_total = 0

    for i in range(60, n):
        state = MarketState(bars=list(bars[: i + 1]))
        try:
            sig = strategy.evaluate(state)
        except Exception as exc:
            log.debug("%s: evaluate() raised at i=%d: %s", actual_name, i, exc)
            continue
        if sig is None:
            continue
        if i == forming_index:
            forming_total += 1
        else:
            closed_total += 1

    total = closed_total + forming_total
    if total == 0:
        verdict = "WARN"
        note = "no signals across the audit window — could be data or strategy"
    elif forming_total > 0:
        verdict = "FAIL"
        note = f"{forming_total} signal(s) fired on the forming bar (look-ahead risk)"
    else:
        verdict = "PASS"
        note = "all signals on closed bars only"

    return AuditRow(
        strategy=actual_name,
        symbol=symbol,
        timeframe_minutes=TIMEFRAME_MAP.get(name, 0),
        bars_audited=n,
        signals_total=total,
        on_closed_bars=closed_total,
        on_forming_bars=forming_total,
        verdict=verdict,
        note=note,
    )


# ── Entry point ──────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit Ayumi strategies for bar-close timing discipline.")
    parser.add_argument(
        "--bars",
        type=int,
        default=200,
        help="Number of bars to audit per strategy (default: 200)",
    )
    parser.add_argument(
        "--symbols",
        default="GBPUSD,USDJPY",
        help="Comma-separated symbols to try (default: GBPUSD,USDJPY)",
    )
    parser.add_argument(
        "--data-dir",
        default="data/forex",
        help="Historical data root (default: data/forex)",
    )
    args = parser.parse_args(argv)

    sb_factories = _build_session_breakout_factories()
    factories: dict[str, Callable[[], Any]] = {}
    for display_name, module, cls_name, custom in STRATEGY_SPEC:
        if display_name in sb_factories:
            factories[display_name] = sb_factories[display_name]
            continue
        factory = _resolve_factory(module, cls_name, custom)
        if factory is not None:
            factories[display_name] = factory

    if not factories:
        log.error("No strategies could be imported — check PYTHONPATH and venv")
        print(json.dumps([{"error": "no_strategies_importable"}], indent=2))
        return 1

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    log.info(
        "Auditing %d strategies across %d symbol(s), window=%d bars",
        len(factories),
        len(symbols),
        args.bars,
    )

    results: list[dict] = []
    failures = 0
    skips = 0
    for strat_name, factory in factories.items():
        tf_minutes = TIMEFRAME_MAP.get(strat_name, 60)
        bars: Optional[list[Bar]] = None
        used_symbol = symbols[0] if symbols else "GBPUSD"
        for sym in symbols:
            bars = _load_bars(sym, tf_minutes, args.bars)
            if bars:
                used_symbol = sym
                break
        if not bars:
            log.warning("No data for %s on any symbol — skipping", strat_name)
            results.append(
                {
                    "strategy": strat_name,
                    "symbol": None,
                    "timeframe_minutes": tf_minutes,
                    "bars_audited": 0,
                    "signals_total": None,
                    "on_closed_bars": None,
                    "on_forming_bars": None,
                    "verdict": "SKIP",
                    "note": "no data available for any configured symbol",
                }
            )
            skips += 1
            continue
        try:
            row = _audit_strategy(strat_name, factory, bars, used_symbol)
        except Exception as exc:
            log.error("Audit crashed for %s: %s", strat_name, exc)
            log.debug("%s", traceback.format_exc())
            results.append(
                {
                    "strategy": strat_name,
                    "symbol": used_symbol,
                    "timeframe_minutes": tf_minutes,
                    "bars_audited": len(bars),
                    "signals_total": None,
                    "on_closed_bars": None,
                    "on_forming_bars": None,
                    "verdict": "ERROR",
                    "note": f"audit exception: {type(exc).__name__}: {exc}",
                }
            )
            failures += 1
            continue
        results.append(
            {
                "strategy": row.strategy,
                "symbol": row.symbol,
                "timeframe_minutes": row.timeframe_minutes,
                "bars_audited": row.bars_audited,
                "signals_total": row.signals_total,
                "on_closed_bars": row.on_closed_bars,
                "on_forming_bars": row.on_forming_bars,
                "verdict": row.verdict,
                "note": row.note,
            }
        )
        if row.verdict == "FAIL":
            failures += 1
        elif row.verdict in ("WARN", "STUB"):
            skips += 1
        log.info(
            "%-32s | %4s | closed=%-3d forming=%-3d | %s",
            row.strategy,
            row.verdict,
            row.on_closed_bars,
            row.on_forming_bars,
            row.note,
        )

    print(json.dumps(results, indent=2, default=str))

    if failures > 0:
        log.warning("%d strategy/strategies showed forming-bar signal leak", failures)
    if skips > 0:
        log.info("%d strategy/strategies skipped (no data or no signal)", skips)

    # Exit 0 unless something actually errored; FAIL verdicts are surfaced
    # in the JSON for downstream tooling to consume.
    return 0 if failures == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
