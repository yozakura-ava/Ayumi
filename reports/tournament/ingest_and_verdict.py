#!/usr/bin/env python3
"""Ingest the Sprint E matrix scorecards and evaluate the pre-registered FTMO gate.

Card 37078a83-2af3-4e5c-9b6e-0ad419192c72 (G4 ingestion + G6 verdict).

Reads every ``scorecard_stage1_XAUUSD_<TF>.json`` (multi-strategy per timeframe)
AND ``scorecard_stage1_XAUUSD_<TF>__<strategy>.json`` (one process per cell - the
G4 execution fix), evaluates pre-registered thresholds T1-T5 from
``docs/reports/ftmo-gate-preregistration-2026-10-13.md`` (frozen at commit
03bdc36b), and emits a verdict payload + grid.

T6 (DSR/BH-FDR selection-set survival) is reported as UNEVALUABLE unless a per-bar
return-series capture is supplied -- see docs/reports/g5-dsr-blocker-finding-2026-10-13.md.
T6 is NEVER silently waived: viability is T1..T6 simultaneously.

Usage::

    python3 ingest_and_verdict.py --dir /opt/ayumi-tournament/ayumi/data/tournament/sprintE --staged
    python3 ingest_and_verdict.py --dir ... --returns returns.json   # T6 capable

Exit codes: 0 = payload + grid written, 2 = usage/IO error.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

# ── Pre-registered thresholds (03bdc36b). Do NOT edit without voiding registration. ──
T1_MIN_RETURN_PCT = 10.0
T2_MAX_DAILY_DD_PCT = 3.0
T3_MAX_DAILY_BREACHES = 0
T4_MIN_TIMEFRAMES = 2
T5_MIN_TRADES = 30  # accepted by Ava pre-G4; amendment after launch is void
ALPHA_NOTE = "T6 DSR/BH-FDR selection-set pass (alpha=0.05)"

TIMEFRAME_ORDER = ["M3", "M5", "M15", "M30", "H1"]
# scorecard_stage1_XAUUSD_M30.json  |  scorecard_stage1_XAUUSD_M30__srmr_plus.json
NAME_RE = re.compile(
    r"^scorecard_(?:stage1|full)_XAUUSD_(M3|M5|M15|M30|H1)(?:__(?P<strat>.+))?\.json$"
)


def load_scorecards(directory: str) -> dict:
    """Return {timeframe: [scorecard, ...]} for every stage/full scorecard found.

    A timeframe can map to several cards: one multi-strategy card (legacy
    one-process-per-timeframe launch) or N per-strategy cards (the G4 split
    launch). Both are merged downstream; a cell is keyed by
    (strategy_id, timeframe) so a duplicate would be visible rather than
    silently double-counted into N.
    """
    out: dict[str, list] = {}
    for path in sorted(glob.glob(os.path.join(directory, "scorecard_*.json"))):
        m = NAME_RE.match(os.path.basename(path))
        if not m:
            continue
        tf = m.group(1)
        try:
            with open(path) as fh:
                card = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARN unreadable scorecard {path}: {exc}", file=sys.stderr)
            continue
        out.setdefault(tf, []).append({"path": path, "card": card})
    return out


def evaluate_cells(scorecards: dict) -> list:
    """Flatten every scorecard row into a per-cell record with T1-T5 verdicts."""
    cells = []
    for tf, entries in sorted(scorecards.items()):
        for entry in entries:
            card = entry["card"]
            meta = card.get("meta", {}) or {}
            run_meta = meta.get("run_meta", {}) or {}
            harness_meta = meta.get("harness_meta", {}) or {}
            bars_loaded = harness_meta.get("bars_loaded")
            for row in card.get("rows", []) or []:
                sid = row.get("strategy_id")
                ret = float(row.get("return_pct", 0.0))
                dd = float(row.get("max_dd_pct", 0.0))
                breaches = int(row.get("daily_dd_breaches", 0))
                trades = int(row.get("trade_count", 0))
                rm = run_meta.get(sid, {}) or {}
                cells.append(
                    {
                        "strategy_id": sid,
                        "timeframe": tf,
                        "symbol": row.get("symbol"),
                        "return_pct": ret,
                        "max_dd_pct": dd,
                        "daily_dd_breaches": breaches,
                        "total_dd_breaches": int(row.get("total_dd_breaches", 0)),
                        "trade_count": trades,
                        "skipped": bool(rm.get("skipped", False)),
                        "skip_reason": rm.get("reason"),
                        "bars_processed": rm.get("bars_processed", bars_loaded),
                        "signals": rm.get("signals"),
                        "first_bar_utc": harness_meta.get("first_bar_utc"),
                        "last_bar_utc": harness_meta.get("last_bar_utc"),
                        "source_file": os.path.basename(entry["path"]),
                        "T1_return": ret >= T1_MIN_RETURN_PCT,
                        "T2_daily_dd": dd <= T2_MAX_DAILY_DD_PCT,
                        "T3_breaches": breaches <= T3_MAX_DAILY_BREACHES,
                        "T5_trades": trades >= T5_MIN_TRADES,
                    }
                )
    return cells


def triage_skipped(scorecards: dict) -> list:
    """Per-cell triage for skipped/zero-signal strategies (card requirement)."""
    out = []
    for tf, entries in sorted(scorecards.items()):
        for entry in entries:
            card = entry["card"]
            run_meta = (card.get("meta", {}) or {}).get("run_meta", {}) or {}
            hmeta = (card.get("meta", {}) or {}).get("harness_meta", {}) or {}
            for sid, rm in sorted(run_meta.items()):
                if rm.get("skipped"):
                    out.append(
                        {
                            "timeframe": tf,
                            "strategy_id": sid,
                            "bars_processed": rm.get("bars_processed", hmeta.get("bars_loaded")),
                            "first_bar_utc": hmeta.get("first_bar_utc"),
                            "last_bar_utc": hmeta.get("last_bar_utc"),
                            "signals": rm.get("signals", 0),
                            "skip_reason": rm.get("reason", "unknown"),
                            "error": rm.get("error"),
                            "source_file": os.path.basename(entry["path"]),
                            "diagnosis": (
                                "Zero signals over the FULL frozen window. Per the "
                                "2026-09-21 runbook this is a harness/window fault "
                                "until proven otherwise, NOT evidence of 'no edge'. "
                                "Counts as failing T5."
                            ),
                        }
                    )
    return out


def apply_t4(cells: list) -> dict:
    """A strategy passes T4 if >=2 TFs clear T1,T2,T3,T5 on that TF."""
    by_strategy: dict[str, dict] = {}
    seen: set[tuple] = set()
    for c in cells:
        if c["skipped"]:
            continue
        key = (c["strategy_id"], c["timeframe"])
        if key in seen:
            raise SystemExit(
                f"DUPLICATE CELL {key} -- two scorecards claim the same "
                "(strategy, timeframe). Refusing to double-count it into N."
            )
        seen.add(key)
        s = by_strategy.setdefault(
            c["strategy_id"], {"timeframes": [], "passing_tfs": [], "cells": []}
        )
        s["timeframes"].append(c["timeframe"])
        s["cells"].append(c)
        if c["T1_return"] and c["T2_daily_dd"] and c["T3_breaches"] and c["T5_trades"]:
            s["passing_tfs"].append(c["timeframe"])
    for s in by_strategy.values():
        s["T4_timeframes"] = len(s["passing_tfs"]) >= T4_MIN_TIMEFRAMES
        s["t4_detail"] = (
            f"{len(s['passing_tfs'])}/{len(s['timeframes'])} TFs clear T1-T3+T5"
        )
    return by_strategy


def render_grid(payload: dict) -> str:
    """Markdown grid of EVERY cell, including losers and zero-signal cells."""
    order = {tf: i for i, tf in enumerate(TIMEFRAME_ORDER)}
    cells = sorted(
        payload["all_cells"], key=lambda c: (c["strategy_id"], order.get(c["timeframe"], 99))
    )
    lines = [
        "# Sprint E — Staged Matrix Grid (4 strategies x 5 timeframes)",
        "",
        f"Generated: {payload['generated_at']}  ·  selection set N = {payload['selection_set_N']}",
        f"Thresholds frozen at commit `03bdc36b` — `docs/reports/ftmo-gate-preregistration-2026-10-13.md`",
        "",
        "Rows are per (strategy, timeframe). `T1..T5` are the pre-registered gates;",
        "`T6` (DSR/BH-FDR) is **UNEVALUABLE** for this run — see the G5 blocker finding.",
        "",
        "| Strategy | TF | Return% | MaxDD% | DailyBr | Trades | T1 | T2 | T3 | T5 | Notes |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cells:
        if c["skipped"]:
            notes = f"SKIPPED: {c.get('skip_reason')}"
        else:
            notes = ""
        yn = lambda b: "PASS" if b else "FAIL"  # noqa: E731
        lines.append(
            f"| {c['strategy_id']} | {c['timeframe']} | {c['return_pct']:.2f} | "
            f"{c['max_dd_pct']:.2f} | {c['daily_dd_breaches']} | {c['trade_count']} | "
            f"{yn(c['T1_return'])} | {yn(c['T2_daily_dd'])} | {yn(c['T3_breaches'])} | "
            f"{yn(c['T5_trades'])} | {notes} |"
        )
    if payload["skipped_cells"]:
        lines += ["", "## Zero-signal / skipped cells (triage)", ""]
        for s in payload["skipped_cells"]:
            lines.append(
                f"- **{s['strategy_id']} / {s['timeframe']}** — bars_processed="
                f"{s['bars_processed']}, first_bar={s['first_bar_utc']}, "
                f"last_bar={s['last_bar_utc']}, signals={s['signals']}, "
                f"reason=`{s['skip_reason']}`"
            )
    lines.append("")
    return "\n".join(lines)


def decide_verdict(payload: dict, candidates: list, t6_state: str) -> str:
    """GO / NO-GO / INCONCLUSIVE — honest about what the data can support.

    * GO requires a strategy clearing T1-T6 **simultaneously**. T6 unevaluable
      therefore forbids GO (it is never waived).
    * NO-GO is definitive when NO strategy clears T1-T5 on >=2 timeframes (T4):
      the economic gate already rejects the entire selection set, so T6 cannot
      rescue it and the verdict stands on its own.
    * INCONCLUSIVE is for the middle case - a candidate clears T1-T5 on >=2 TFs
      but T6 could not be evaluated, so we genuinely cannot say.
    """
    if any(c["verdict_eligible"] for c in candidates):
        return "GO"
    if not candidates:
        return "NO-GO"
    if t6_state != "COMPUTED":
        return "INCONCLUSIVE"
    return "NO-GO"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sprint E matrix ingest + FTMO verdict")
    ap.add_argument("--dir", required=True, help="tournament output dir with scorecards")
    ap.add_argument("--staged", action="store_true", help="staged 4x5 subset (N=20)")
    ap.add_argument("--returns", default=None, help="optional per-bar returns JSON for T6")
    ap.add_argument("--out", default=None, help="payload output path (.json)")
    ap.add_argument("--grid-out", default=None, help="grid markdown output path")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.dir):
        print(f"ERROR: not a directory: {args.dir}", file=sys.stderr)
        return 2

    scorecards = load_scorecards(args.dir)
    if not scorecards:
        print(
            f"ERROR: no scorecards matched in {args.dir}. Per the runbook, judge "
            "completion by scorecard FILES, not by log tail (stdout is buffered).",
            file=sys.stderr,
        )
        return 2

    cells = evaluate_cells(scorecards)
    skipped = triage_skipped(scorecards)
    by_strategy = apply_t4(cells)

    n_cells = len(cells)
    n_expected = 20 if args.staged else 85
    tfs_present = sorted(scorecards, key=lambda tf: TIMEFRAME_ORDER.index(tf))

    # ── T6 ────────────────────────────────────────────────────────────────────
    t6_state = "UNEVALUABLE"
    t6_note = (
        "Not computed. The scorecard persists aggregates only (no per-bar/per-trial "
        "return series), and rank_selection_set_with_dsr requires a TrialReturnStore "
        "matrix [T, N_trials]. See docs/reports/g5-dsr-blocker-finding-2026-10-13.md."
    )
    t6_per_strategy = {}
    if args.returns:
        try:
            from forex_bot.factory.risk_adjusted_ranking import rank_selection_set_with_dsr
            from forex_bot.factory.validation_runner import TrialReturnStore
            import numpy as np

            with open(args.returns) as fh:
                entries = json.load(fh)
            store = TrialReturnStore()
            for entry in entries:
                key = TrialReturnStore.cell_key(
                    "XAUUSD-grid", entry["symbol"], entry["timeframe"]
                )
                store.record(key, np.asarray(entry["returns"], dtype=float))
            ranking = rank_selection_set_with_dsr(
                store, n_trials_selection=n_cells, bar_period_minutes=60
            )
            for _cell_key, scores in ranking.cells.items():
                for sc in scores:
                    t6_per_strategy[sc.candidate_id] = {
                        "dsr_pvalue": sc.dsr_pvalue,
                        "is_discovery": sc.is_discovery,
                        "n_trials_cell": sc.n_trials_cell,
                        "n_trials_selection": sc.n_trials_selection,
                    }
            t6_state = "COMPUTED"
            t6_note = f"Computed over N={n_cells} trials."
        except Exception as exc:  # noqa: BLE001
            t6_note = f"Attempted and FAILED: {type(exc).__name__}: {exc}"
            t6_state = "FAILED"

    # ── candidates: cleared T1-T5 on >=2 TFs ─────────────────────────────────
    candidates = []
    for sid, s in sorted(by_strategy.items()):
        if not s["T4_timeframes"]:
            continue
        best = sorted(
            (c for c in s["cells"] if c["timeframe"] in s["passing_tfs"]),
            key=lambda c: (-c["return_pct"], c["max_dd_pct"]),
        )[0]
        t6 = t6_per_strategy.get(sid)
        t6_ok = bool(t6 and t6.get("is_discovery")) if t6_state == "COMPUTED" else False
        candidates.append(
            {
                "strategy_id": sid,
                "passing_tfs": s["passing_tfs"],
                "T4": True,
                "T6": t6_ok,
                "T6_detail": t6,
                "best_cell": best,
                "verdict_eligible": t6_ok,
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection_set_N": n_cells,
        "expected_cells": n_expected,
        "complete": n_cells >= n_expected,
        "timeframes_present": tfs_present,
        "thresholds": {
            "T1_min_return_pct": T1_MIN_RETURN_PCT,
            "T2_max_daily_dd_pct": T2_MAX_DAILY_DD_PCT,
            "T3_max_daily_breaches": T3_MAX_DAILY_BREACHES,
            "T4_min_timeframes": T4_MIN_TIMEFRAMES,
            "T5_min_trades": T5_MIN_TRADES,
            "T6": ALPHA_NOTE,
        },
        "T6_state": t6_state,
        "T6_note": t6_note,
        "cell_count": n_cells,
        "skipped_cells": skipped,
        "candidates_T4_pass": candidates,
        "all_cells": cells,
        "by_strategy": {
            k: {
                "timeframes": v["timeframes"],
                "passing_tfs": v["passing_tfs"],
                "T4_timeframes": v["T4_timeframes"],
                "t4_detail": v["t4_detail"],
                "best_return_pct": max((c["return_pct"] for c in v["cells"]), default=None),
            }
            for k, v in sorted(by_strategy.items())
        },
    }
    payload["verdict"] = decide_verdict(payload, candidates, t6_state)

    out_path = args.out or os.path.join(args.dir, "ftmo_verdict_payload.json")
    with open(out_path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, default=str)

    grid_path = args.grid_out
    if grid_path:
        with open(grid_path, "w") as fh:
            fh.write(render_grid(payload))

    print(f"VERDICT={payload['verdict']}  N={n_cells}  T6={t6_state}")
    print(f"cells={n_cells}/{n_expected}  complete={payload['complete']}  tfs={tfs_present}")
    print(f"candidates clearing T1-T5 on >=2 TFs: {[c['strategy_id'] for c in candidates]}")
    print(f"payload -> {out_path}")
    if grid_path:
        print(f"grid    -> {grid_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())