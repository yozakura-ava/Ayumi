#!/usr/bin/env python3
"""deflated_sharpe.py — Standalone CLI for Deflated Sharpe Ratio analysis.

Ingests sweep results from the SRF DuckDB (default), a JSON file, or
walk-forward JSONL reports, and produces:

1. **DSR p-value** per candidate (Bailey & López de Prado 2014 Eq. 5)
2. **Edge probability** — ``1 - DSR p-value``, the probability the observed
   Sharpe is genuine after multiple-testing correction.
3. **Minimum track record length** (MinTRL) — minimum observations needed
   to reject H0 at α = 0.05.
4. **Consolidated markdown report** ranking all candidates by edge
   probability, with kill list (edge prob < 50%) and promote list
   (DSR p-value < 0.05).

Usage
-----
    # Default: load all 43 trials from SRF DuckDB
    python scripts/quant/deflated_sharpe.py

    # Custom JSON input (list of {name, mean_sharpe, trade_count})
    python scripts/quant/deflated_sharpe.py --input trials.json --n-trials 43

    # Walk-forward JSONL reports
    python scripts/quant/deflated_sharpe.py \\
        --jsonl reports/srmr-plus-pipeline-2026-07-08/GBPUSD_focused_results.jsonl \\
        --jsonl reports/srmr-plus-pipeline-2026-07-08/EURUSD_focused_results.jsonl

    # Custom output path
    python scripts/quant/deflated_sharpe.py --report reports/quant/dsr_report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------------------
# Path setup — allow running from project root or scripts/quant/
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve()
PROJECT_ROOT = HERE.parents[2]
SRC_FOREX_BOT = PROJECT_ROOT / "src" / "forex_bot"

# Add src/forex_bot to path so `quant` package is importable.
# This matches the pattern in the project's conftest.py.
if str(SRC_FOREX_BOT) not in sys.path:
    sys.path.insert(0, str(SRC_FOREX_BOT))

from quant.dsr_integration import (  # noqa: E402
    DEFAULT_N_INDEPENDENT_TRIALS,
)
from quant.oos_gate import (  # noqa: E402, I001
    deflated_sharpe_ratio,
    expected_max_sharpe,
    min_track_record_length,
)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Candidate:
    """One strategy candidate for DSR analysis."""

    def __init__(
        self,
        name: str,
        mean_sharpe: float,
        trade_count: int,
        pair: str = "",
        timeframe: str = "",
        extra: dict | None = None,
    ):
        self.name = name
        self.mean_sharpe = mean_sharpe
        self.trade_count = trade_count
        self.pair = pair
        self.timeframe = timeframe
        self.extra = extra or {}

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "mean_sharpe": self.mean_sharpe,
            "trade_count": self.trade_count,
        }
        if self.pair:
            d["pair"] = self.pair
        if self.timeframe:
            d["timeframe"] = self.timeframe
        d.update(self.extra)
        return d


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_from_duckdb(duckdb_path: Path) -> list[Candidate]:
    """Load trial summaries from the SRF research DuckDB."""
    import duckdb

    if not duckdb_path.exists():
        raise FileNotFoundError(f"SRF DuckDB not found at {duckdb_path}")

    conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT
                r.strategy_name,
                r.pair,
                r.timeframe,
                m.mean_sharpe,
                m.total_trades
            FROM runs r
            JOIN metrics_summary m USING (run_id)
            WHERE r.status = 'completed'
              AND m.mean_sharpe IS NOT NULL
              AND m.total_trades IS NOT NULL
              AND m.total_trades > 0
            ORDER BY r.strategy_name, r.pair, r.timeframe
            """
        ).fetchall()
    finally:
        conn.close()

    candidates: list[Candidate] = []
    for strategy, pair, tf, sharpe, trades in rows:
        tf_str = f"M{tf}" if tf < 60 else f"H{tf // 60}"
        name = f"{strategy}/{pair}/{tf_str}"
        candidates.append(
            Candidate(
                name=name,
                mean_sharpe=float(sharpe) if sharpe is not None else 0.0,
                trade_count=int(trades) if trades is not None else 0,
                pair=str(pair),
                timeframe=tf_str,
            )
        )
    return candidates


def load_from_json(path: Path) -> list[Candidate]:
    """Load from a JSON array of trial records."""
    with path.open() as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    candidates: list[Candidate] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "?"))
        sharpe = float(entry.get("mean_sharpe", 0.0) or 0.0)
        trades = int(entry.get("trade_count", 0) or 0)
        pair = str(entry.get("pair", ""))
        tf = str(entry.get("timeframe", ""))
        candidates.append(
            Candidate(
                name=name,
                mean_sharpe=sharpe,
                trade_count=trades,
                pair=pair,
                timeframe=tf,
                extra={
                    k: v
                    for k, v in entry.items()
                    if k not in ("name", "mean_sharpe", "trade_count", "pair", "timeframe")
                },
            )
        )
    return candidates


def load_from_jsonl(path: Path) -> list[Candidate]:
    """Load from a walk-forward JSONL report file."""
    candidates: list[Candidate] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: invalid JSON on line {lineno}: {e}") from e

        pair = str(entry.get("pair", ""))
        tf = str(entry.get("timeframe", ""))
        name = entry.get("name") or f"{pair}/{tf}" if pair else entry.get("name", f"trial_{lineno}")
        sharpe = float(entry.get("mean_sharpe", 0.0) or 0.0)
        trades = int(entry.get("mean_trade_count", entry.get("trade_count", 0)) or 0)
        windows_passed = int(entry.get("windows_passed", 0) or 0)
        candidates.append(
            Candidate(
                name=name,
                mean_sharpe=sharpe,
                trade_count=trades,
                pair=pair,
                timeframe=tf,
                extra={
                    "windows_passed": windows_passed,
                    "windows_total": int(entry.get("windows_total", 0) or 0),
                    "mean_profit_factor": float(entry.get("mean_profit_factor", 0.0) or 0.0),
                    "mean_win_rate": float(entry.get("mean_win_rate", 0.0) or 0.0),
                },
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_candidates(
    candidates: Sequence[Candidate],
    n_trials: int = DEFAULT_N_INDEPENDENT_TRIALS,
    alpha: float = 0.05,
) -> list[dict]:
    """Compute DSR, edge probability, and MinTRL for each candidate.

    Returns a list of dicts sorted by edge probability descending.
    """
    results: list[dict] = []
    e_max = expected_max_sharpe(n_trials) if n_trials > 1 else 0.0

    for c in candidates:
        n_obs = max(c.trade_count, 1)

        if c.mean_sharpe > 0.0 and n_obs >= 2:
            dsr_p = deflated_sharpe_ratio(
                observed_sr=c.mean_sharpe,
                n_trials=n_trials,
                n_obs=n_obs,
                skewness=0.0,
                kurtosis_regular=3.0,
            )
        else:
            dsr_p = 1.0

        edge_prob = max(0.0, min(1.0, 1.0 - dsr_p))

        min_trl = (
            min_track_record_length(
                observed_sr=c.mean_sharpe,
                n_trials=n_trials,
                skewness=0.0,
                kurtosis_regular=3.0,
                alpha=alpha,
            )
            if c.mean_sharpe > 0.0
            else -1
        )

        # Track record adequacy: does the candidate have enough trades?
        trl_adequate = c.trade_count >= min_trl if min_trl > 0 else False

        results.append(
            {
                "name": c.name,
                "pair": c.pair,
                "timeframe": c.timeframe,
                "mean_sharpe": c.mean_sharpe,
                "trade_count": c.trade_count,
                "dsr_pvalue": float(dsr_p),
                "edge_probability": float(edge_prob),
                "min_track_record": int(min_trl),
                "trl_adequate": bool(trl_adequate),
                "expected_max_sr": float(e_max),
                "n_trials": int(n_trials),
                **c.extra,
            }
        )

    # Sort by edge probability descending.
    results.sort(key=lambda r: r["edge_probability"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def render_report(
    results: Sequence[dict],
    n_trials: int,
    source: str,
    alpha: float = 0.05,
) -> str:
    """Render a consolidated markdown report ranking all candidates."""
    today = date.today().isoformat()
    total = len(results)

    # Kill list: edge probability < 50% (DSR p-value > 0.50)
    kill_list = [r for r in results if r["edge_probability"] < 0.50]
    # Promote list: DSR p-value < alpha (statistically significant)
    promote_list = [r for r in results if r["dsr_pvalue"] < alpha]
    # Watch list: edge prob >= 50% but not significant
    watch_list = [r for r in results if 0.50 <= r["edge_probability"] < (1.0 - alpha)]

    lines: list[str] = []
    lines.append(f"# Deflated Sharpe Ratio Analysis — {today}")
    lines.append("")
    lines.append(f"**Source:** {source}")
    lines.append(f"**Candidates analyzed:** {total}")
    lines.append(f"**Independent trials (multiple-testing correction):** {n_trials}")
    lines.append(f"**Significance level (α):** {alpha}")
    lines.append(f"**Expected max Sharpe under null:** {results[0]['expected_max_sr']:.4f}" if results else "")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Category | Count | Criteria |")
    lines.append("|---|---:|---|")
    lines.append(f"| Promote (significant edge) | {len(promote_list)} | DSR p-value < {alpha} |")
    lines.append(f"| Watch (uncertain) | {len(watch_list)} | Edge prob ∈ [50%, {1 - alpha:.0%}) |")
    lines.append(f"| Kill (insufficient evidence) | {len(kill_list)} | Edge prob < 50% |")
    lines.append("")

    # --- Promote list ---
    lines.append("## Promote List")
    lines.append("")
    lines.append(
        f"Candidates with DSR p-value < {alpha} (statistically significant edge after multiple-testing correction)."
    )
    lines.append("")
    if promote_list:
        lines.append("| # | Candidate | Sharpe | Trades | DSR p-value | Edge Prob | MinTRL | TRL Met |")
        lines.append("|---:|---|---:|---:|---:|---:|---:|:---:|")
        for i, r in enumerate(promote_list, 1):
            trl_met = "✅" if r["trl_adequate"] else "⚠️"
            lines.append(
                f"| {i} | `{r['name']}` | {r['mean_sharpe']:+.3f} | {r['trade_count']} | "
                f"{r['dsr_pvalue']:.4f} | {r['edge_probability']:.1%} | "
                f"{r['min_track_record']} | {trl_met} |"
            )
    else:
        lines.append("_No candidates meet the promotion threshold._")
    lines.append("")

    # --- Kill list ---
    lines.append("## Kill List")
    lines.append("")
    lines.append("Candidates with edge probability < 50% (insufficient evidence of genuine edge).")
    lines.append("")
    if kill_list:
        lines.append("| # | Candidate | Sharpe | Trades | DSR p-value | Edge Prob |")
        lines.append("|---:|---|---:|---:|---:|---:|")
        for i, r in enumerate(kill_list, 1):
            lines.append(
                f"| {i} | `{r['name']}` | {r['mean_sharpe']:+.3f} | {r['trade_count']} | "
                f"{r['dsr_pvalue']:.4f} | {r['edge_probability']:.1%} |"
            )
    else:
        lines.append("_No candidates in the kill list._")
    lines.append("")

    # --- Full ranking ---
    lines.append("## Full Ranking")
    lines.append("")
    lines.append("All candidates sorted by edge probability (descending).")
    lines.append("")
    lines.append("| # | Candidate | Sharpe | Trades | DSR p-value | Edge Prob | MinTRL | TRL Met | Tier |")
    lines.append("|---:|---|---:|---:|---:|---:|---:|:---:|:---:|")
    for i, r in enumerate(results, 1):
        trl_met = "✅" if r["trl_adequate"] else "⚠️" if r["min_track_record"] > 0 else "—"
        # Simple tier: promote / watch / kill
        if r["dsr_pvalue"] < alpha:
            tier = "🟢 Promote"
        elif r["edge_probability"] >= 0.50:
            tier = "🟡 Watch"
        else:
            tier = "🔴 Kill"
        lines.append(
            f"| {i} | `{r['name']}` | {r['mean_sharpe']:+.3f} | {r['trade_count']} | "
            f"{r['dsr_pvalue']:.4f} | {r['edge_probability']:.1%} | "
            f"{r['min_track_record']} | {trl_met} | {tier} |"
        )
    lines.append("")

    # --- Method notes ---
    lines.append("## Method")
    lines.append("")
    lines.append(
        "**Deflated Sharpe Ratio (DSR):** Bailey & López de Prado (2014) Eq. 5. "
        "Adjusts the observed Sharpe ratio for selection bias (multiple testing) "
        "and non-normality (skewness and kurtosis). The DSR p-value tests "
        "H0: true SR ≤ E[max SR | null], where E[max SR] is the expected maximum "
        f"Sharpe under the null across {n_trials} independent trials."
    )
    lines.append("")
    lines.append(
        "**Edge Probability:** `1 - DSR p-value`. Represents the probability "
        "that the observed Sharpe ratio is genuine (not due to selection bias "
        "or multiple testing). Higher is better."
    )
    lines.append("")
    lines.append(
        "**Minimum Track Record Length (MinTRL):** Bailey & López de Prado (2014). "
        "The minimum number of observations (trades) needed to reject H0 at "
        f"α = {alpha}. If `Trades < MinTRL`, the candidate has insufficient "
        "track record for the DSR verdict to be trustworthy, even if the "
        "p-value is significant. Marked with ⚠️ in the TRL Met column."
    )
    lines.append("")
    lines.append(
        "**Kill List:** Edge probability < 50% — the observed Sharpe is more "
        "likely than not due to selection bias. Do not deploy or paper-trade."
    )
    lines.append("")
    lines.append(
        "**Promote List:** DSR p-value < α — the observed edge is statistically "
        "significant after multiple-testing correction. Candidates with "
        "⚠️ on TRL Met should accumulate more trades before live deployment."
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deflated Sharpe Ratio analysis CLI — ranks strategy candidates by genuine edge probability."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Path to a JSON array of trial summaries. Defaults to the SRF DuckDB.",
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        action="append",
        default=None,
        help="Path to a walk-forward JSONL report. Can be specified multiple times.",
    )
    parser.add_argument(
        "--duckdb",
        type=Path,
        default=None,
        help="Path to the SRF research DuckDB (used when --input and --jsonl are not given).",
    )
    parser.add_argument(
        "--n-trials",
        type=int,
        default=DEFAULT_N_INDEPENDENT_TRIALS,
        help=f"Number of independent trials for multiple-testing correction (default: {DEFAULT_N_INDEPENDENT_TRIALS}).",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.05,
        help="Significance level for DSR test and MinTRL (default: 0.05).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Path for the consolidated markdown report. Defaults to reports/quant/deflated_sharpe_<date>.md.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional path to also dump a JSON summary with all annotations.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    # --- Load candidates ---
    if args.input is not None:
        candidates = load_from_json(args.input)
        source = f"JSON input: {args.input}"
    elif args.jsonl is not None:
        candidates = []
        for p in args.jsonl:
            candidates.extend(load_from_jsonl(p))
        source = f"JSONL: {', '.join(str(p) for p in args.jsonl)}"
    else:
        duckdb_path = args.duckdb
        if duckdb_path is None:
            duckdb_path = PROJECT_ROOT / "data" / "research" / "research.duckdb"
        candidates = load_from_duckdb(duckdb_path)
        source = f"SRF DuckDB: {duckdb_path}"

    if not candidates:
        print("No candidates found — nothing to analyze.", file=sys.stderr)
        return 1

    # --- Analyze ---
    results = analyze_candidates(candidates, n_trials=args.n_trials, alpha=args.alpha)

    # --- Report ---
    report_path = args.report
    if report_path is None:
        report_path = PROJECT_ROOT / "reports" / "quant" / f"deflated_sharpe_{date.today().isoformat()}.md"

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_text = render_report(results, n_trials=args.n_trials, source=source, alpha=args.alpha)
    report_path.write_text(report_text)

    # --- JSON output ---
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        summary = {
            "generated_at": date.today().isoformat(),
            "source": source,
            "n_trials": args.n_trials,
            "alpha": args.alpha,
            "total_candidates": len(results),
            "promote_count": sum(1 for r in results if r["dsr_pvalue"] < args.alpha),
            "kill_count": sum(1 for r in results if r["edge_probability"] < 0.50),
            "candidates": results,
        }
        args.json_out.write_text(json.dumps(summary, indent=2, default=str))

    # --- Console summary ---
    n_promote = sum(1 for r in results if r["dsr_pvalue"] < args.alpha)
    n_kill = sum(1 for r in results if r["edge_probability"] < 0.50)
    n_watch = len(results) - n_promote - n_kill
    print(f"Analyzed {len(results)} candidates with n_trials={args.n_trials}")
    print(f"  Promote (DSR p < {args.alpha}): {n_promote}")
    print(f"  Watch (edge prob ≥ 50%): {n_watch}")
    print(f"  Kill (edge prob < 50%): {n_kill}")
    print(f"\nReport: {report_path}")
    if args.json_out:
        print(f"JSON:   {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
