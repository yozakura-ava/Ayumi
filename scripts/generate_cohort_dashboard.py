#!/usr/bin/env python3
"""
generate_cohort_dashboard.py — Roll up walk-forward + DSR + ICIR metrics into
a single per-stream JSON + human-readable table.

Reads
-----
* ``reports/srmr-plus-pipeline-2026-07-08/{PAIR}_focused_results.jsonl``
  — one entry per (pair, timeframe). Aggregate-only metrics
  (PF / Sharpe / WR / MaxDD / total PnL / windows_passed).
* ``reports/srmr-plus-pipeline-2026-07-08/dsr_annotated_results.json``
  — Phase 10b output. Adds DSR p-value + tier to each stream. If missing,
  DSR columns are reported as ``null`` and a note is included.
* ``reports/multi-strategy-wf-2026-07-08/results.jsonl``
  — adds ``strategy`` column where present.

ICIR is computed via :func:`quant.icir.evaluate_icir`. With aggregate-only
WF data it is reported as ``null`` (confidence='low', note='no per-trade
confidence/R-multiple available') per the research doc §7.4 — this is the
expected behaviour for the current pipeline.

Outputs
-------
* ``reports/cohort_dashboard_2026-07-08.json`` — full per-stream rollup.
* Human-readable table on stdout.

Usage
-----
::

    source .venv/bin/activate
    export PYTHONPATH=src/forex_bot
    python scripts/generate_cohort_dashboard.py \\
        --reports-dir reports/srmr-plus-pipeline-2026-07-08 \\
        --output reports/cohort_dashboard_2026-07-08.json

CLI flags are optional; defaults match the Ayumi project layout as of
2026-07-08.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add repo src to path so ``quant.*`` imports work without install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "forex_bot"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from quant.icir import evaluate_icir  # noqa: E402, I001

# ---------------------------------------------------------------------------
# Defaults — paths under the Ayumi repo layout as of 2026-07-08.
# ---------------------------------------------------------------------------

DEFAULT_REPORTS_DIR = _REPO_ROOT / "reports" / "srmr-plus-pipeline-2026-07-08"
DEFAULT_DSR_FILE = DEFAULT_REPORTS_DIR / "dsr_annotated_results.json"
DEFAULT_STRATEGY_FILE = _REPO_ROOT / "reports" / "multi-strategy-wf-2026-07-08" / "results.jsonl"
DEFAULT_OUTPUT = _REPO_ROOT / "reports" / "cohort_dashboard_2026-07-08.json"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a JSONL file. Returns [] if missing. Skips blank / bad lines."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _load_dsr_annotated(
    path: Path,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any] | None]:
    """Load the Phase 10b DSR annotated report into a (pair, timeframe) → entry index.

    The annotated file groups entries by tier (``tier_a``, ``tier_b``,
    ``tier_c``, ``rejected``) — we flatten them. Returns the index plus
    the top-level ``summary`` if present (used for diagnostic metadata).
    """
    if not path.exists():
        return ({}, None)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return ({}, None)

    per_stream: dict[tuple[str, str], dict[str, Any]] = {}
    combined = data.get("combined") or {}
    for tier_key in ("tier_a", "tier_b", "tier_c", "rejected"):
        bucket = combined.get(tier_key)
        if not isinstance(bucket, list):
            continue
        for entry in bucket:
            if not isinstance(entry, dict):
                continue
            pair = entry.get("pair")
            timeframe = entry.get("timeframe")
            if pair is None or timeframe is None:
                continue
            per_stream[(str(pair), str(timeframe))] = entry
    return per_stream, data.get("summary")


def _load_strategy_index(path: Path) -> dict[tuple[str, str], str]:
    """Map (pair, timeframe) → strategy name from the multi-strategy WF log."""
    if not path.exists():
        return {}
    out: dict[tuple[str, str], str] = {}
    for entry in _load_jsonl(path):
        pair = entry.get("pair")
        timeframe = entry.get("timeframe")
        strategy = entry.get("strategy")
        if pair is None or timeframe is None or not strategy:
            continue
        out[(str(pair), str(timeframe))] = str(strategy)
    return out


# ---------------------------------------------------------------------------
# Row build
# ---------------------------------------------------------------------------


def _round(v: Any, ndigits: int = 4) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, ndigits)
    return v


def _build_stream_row(
    pair: str,
    timeframe: str,
    wf_entry: dict[str, Any],
    dsr_entry: dict[str, Any] | None,
    strategy: str | None,
) -> dict[str, Any]:
    """Build one cohort-dashboard row for a (pair, timeframe) stream.

    ``wf_entry`` is the source-of-truth for the realized metrics — DSR
    and ICIR are additive columns that may be null.
    """
    # ICIR computation for the aggregate-only WF JSONL shape (current
    # Ayumi report format). Evaluate_icir returns confidence='low' with
    # a diagnostic note when no per-trade confidence/R-multiple data is
    # present — which is the case for every entry, since the JSONL only
    # carries window-aggregated metrics. We pass an empty list to
    # surface that fact in the note and the ICIR columns stay null.
    icir_summary = evaluate_icir([])

    row: dict[str, Any] = {
        "strategy": strategy,
        "symbol": pair,
        "timeframe": timeframe,
        "status": wf_entry.get("status"),
        "pf": _round(wf_entry.get("mean_profit_factor")),
        "win_rate": _round(wf_entry.get("mean_win_rate")),
        "sharpe": _round(wf_entry.get("mean_sharpe")),
        "max_drawdown": _round(wf_entry.get("mean_max_drawdown")),
        "total_pnl": _round(wf_entry.get("mean_total_pnl")),
        "trade_count": _round(wf_entry.get("mean_trade_count")),
        "windows_passed": wf_entry.get("windows_passed"),
        "windows_total": wf_entry.get("windows_total"),
        "go_nogo": wf_entry.get("go_nogo"),
        "dsr_pvalue": None,
        "dsr_n_obs": None,
        "dsr_expected_max_sr": None,
        "icir": _round(icir_summary.get("icir")),
        "icir_mean": _round(icir_summary.get("mean_ic")),
        "icir_std": _round(icir_summary.get("std_ic")),
        "icir_n_windows": icir_summary.get("n_windows"),
        "icir_confidence": icir_summary.get("confidence"),
        "icir_note": icir_summary.get("note"),
        "tier": None,
    }

    if dsr_entry:
        row["dsr_pvalue"] = _round(dsr_entry.get("dsr_pvalue"))
        row["dsr_n_obs"] = dsr_entry.get("dsr_n_obs")
        row["dsr_expected_max_sr"] = _round(dsr_entry.get("dsr_expected_max_sr"))
        row["tier"] = dsr_entry.get("tier")

    return row


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def build_dashboard(
    reports_dir: Path,
    dsr_file: Path,
    strategy_file: Path,
) -> dict[str, Any]:
    """Compose the per-stream dashboard.

    Reads every ``{PAIR}_focused_results.jsonl`` file in ``reports_dir``,
    cross-references DSR + strategy annotations, and returns a JSON-able
    dict ready for ``json.dump``.
    """
    if not reports_dir.exists():
        # Produce an empty-but-valid dashboard with a note so downstream
        # tooling never crashes on missing inputs.
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reports_dir": str(reports_dir),
            "source_reports": [],
            "streams": [],
            "summary": {
                "n_streams": 0,
                "n_complete": 0,
                "n_in_dsr": 0,
                "n_icir_computed": 0,
                "tier_counts": {"A": 0, "B": 0, "C": 0, "REJECT": 0, "NONE": 0},
                "note": f"reports directory not found: {reports_dir}",
            },
        }

    dsr_index, dsr_summary = _load_dsr_annotated(dsr_file)
    strategy_index = _load_strategy_index(strategy_file)

    source_files: list[str] = []
    streams_by_pair_tf: dict[tuple[str, str], dict[str, Any]] = {}

    for jsonl_path in sorted(reports_dir.glob("*_focused_results.jsonl")):
        source_files.append(jsonl_path.name)
        for entry in _load_jsonl(jsonl_path):
            pair = entry.get("pair")
            timeframe = entry.get("timeframe")
            if not pair or not timeframe:
                continue
            key = (str(pair), str(timeframe))
            streams_by_pair_tf[key] = entry

    # Build one row per (pair, timeframe). Sort for deterministic output.
    rows: list[dict[str, Any]] = []
    for key in sorted(streams_by_pair_tf.keys()):
        pair, timeframe = key
        wf_entry = streams_by_pair_tf[key]
        dsr_entry = dsr_index.get(key)
        strategy = strategy_index.get(key)
        rows.append(_build_stream_row(pair, timeframe, wf_entry, dsr_entry, strategy))

    # Top-level summary.
    tier_counts: dict[str, int] = {"A": 0, "B": 0, "C": 0, "REJECT": 0, "NONE": 0}
    n_complete = 0
    n_in_dsr = 0
    n_icir_computed = 0
    for row in rows:
        if row["status"] == "complete":
            n_complete += 1
        if row["dsr_pvalue"] is not None:
            n_in_dsr += 1
        if row["icir"] is not None:
            n_icir_computed += 1
        tier_key = row["tier"] if row["tier"] in {"A", "B", "C"} else ("REJECT" if row["tier"] == "REJECT" else "NONE")
        tier_counts[tier_key] += 1

    summary: dict[str, Any] = {
        "n_streams": len(rows),
        "n_complete": n_complete,
        "n_in_dsr": n_in_dsr,
        "n_icir_computed": n_icir_computed,
        "tier_counts": tier_counts,
    }
    if dsr_summary is not None:
        summary["dsr_report_summary_keys"] = sorted(dsr_summary.keys())
    summary["note"] = (
        "ICIR requires per-trade confidence/R-multiple per WF window; the "
        "current aggregate-only WF reports do not provide this. ICIR "
        "columns are reported as null with confidence='low'. See "
        "docs/research/icir-research-2026-07-08.md §7.4 for the panel "
        "construction recipe that would make ICIR computable here."
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reports_dir": str(reports_dir),
        "source_reports": source_files,
        "streams": rows,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------


def print_human_readable(dashboard: dict[str, Any]) -> None:
    """Print a compact human-readable table for stdout."""
    streams = dashboard.get("streams", [])
    summary = dashboard.get("summary", {})
    print("=" * 100)
    print("Ayumi Cohort Dashboard")
    print(f"  generated_at: {dashboard.get('generated_at')}")
    print(f"  reports_dir:  {dashboard.get('reports_dir')}")
    print(
        f"  streams: {summary.get('n_streams', 0)} | complete: "
        f"{summary.get('n_complete', 0)} | in_dsr: "
        f"{summary.get('n_in_dsr', 0)} | icir: "
        f"{summary.get('n_icir_computed', 0)}"
    )
    tier_counts = summary.get("tier_counts", {})
    print(
        "  tier_counts: A={A} B={B} C={C} REJECT={R} NONE={N}".format(
            A=tier_counts.get("A", 0),
            B=tier_counts.get("B", 0),
            C=tier_counts.get("C", 0),
            R=tier_counts.get("REJECT", 0),
            N=tier_counts.get("NONE", 0),
        )
    )
    print("=" * 100)

    if not streams:
        print("(no streams to display)")
        return

    header = (
        f"{'Stream':<28} {'Status':<10} {'PF':>7} {'WR%':>7} "
        f"{'Sharpe':>9} {'MaxDD':>8} {'Trades':>8} {'Wins':>6} "
        f"{'DSR_p':>10} {'ICIR':>8} {'Tier':>6}"
    )
    print(header)
    print("-" * len(header))
    for row in streams:
        sym = row.get("symbol") or "?"
        tf = row.get("timeframe") or "?"
        strat = row.get("strategy") or "-"
        stream_label = f"{strat}/{sym}/{tf}"[:28]
        pf = row.get("pf")
        wr_pct = f"{row['win_rate'] * 100:.1f}" if row.get("win_rate") is not None else "-"
        max_dd_pct = f"{row['max_drawdown'] * 100:.2f}" if row.get("max_drawdown") is not None else "-"
        trades_v = row.get("trade_count")
        trades_str = f"{trades_v:.1f}" if trades_v is not None else "-"
        wins_v = row.get("windows_passed")
        wins_str = f"{wins_v}/{row.get('windows_total', '?')}"
        pf_str = f"{pf:.2f}" if pf is not None else "-"
        sharpe_str = f"{row['sharpe']:.2f}" if row.get("sharpe") is not None else "-"
        dsr_str = f"{row['dsr_pvalue']:.4f}" if row.get("dsr_pvalue") is not None else "-"
        icir_str = f"{row['icir']:.2f}" if row.get("icir") is not None else "-"
        tier = row.get("tier") or "-"
        print(
            f"{stream_label:<28} "
            f"{(row.get('status') or '?'):<10} "
            f"{pf_str:>7} {wr_pct:>7} {sharpe_str:>9} "
            f"{max_dd_pct:>8} {trades_str:>8} {wins_str:>6} "
            f"{dsr_str:>10} {icir_str:>8} {tier:>6}"
        )
    print("-" * len(header))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Roll up walk-forward, DSR, and ICIR metrics into a single per-stream dashboard JSON + stdout table."
        )
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help=(f"Directory containing {{PAIR}}_focused_results.jsonl files. Default: {DEFAULT_REPORTS_DIR}"),
    )
    parser.add_argument(
        "--dsr-file",
        type=Path,
        default=DEFAULT_DSR_FILE,
        help=("Path to the DSR annotated results JSON (Phase 10b). Optional — if missing, DSR columns are null."),
    )
    parser.add_argument(
        "--strategy-file",
        type=Path,
        default=DEFAULT_STRATEGY_FILE,
        help=(f"Optional path to multi-strategy WF results.jsonl. Default: {DEFAULT_STRATEGY_FILE}"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=(f"Output JSON path. Default: {DEFAULT_OUTPUT}"),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the stdout table (only write the JSON file).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    dashboard = build_dashboard(
        reports_dir=args.reports_dir,
        dsr_file=args.dsr_file,
        strategy_file=args.strategy_file,
    )

    # Always write the JSON (idiomatic for cron / aggregator pipelines).
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        json.dump(dashboard, fh, indent=2, sort_keys=False)
        fh.write("\n")

    if not args.quiet:
        print_human_readable(dashboard)
        print(f"Wrote {len(dashboard.get('streams', []))} streams to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
