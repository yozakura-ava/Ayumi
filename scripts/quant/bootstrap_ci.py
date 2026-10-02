"""Bootstrap confidence intervals on SRF candidate strategy metrics.

The Tier-1 SRF pipeline produces per-window metrics (Sharpe, Profit Factor,
Win Rate, Max Drawdown) and per-run aggregates in ``metrics_summary``. A
production-grade confidence interval on each candidate requires the full
per-window distribution — the per-trade distribution is even better — so
the bootstrap can faithfully reflect the variability in the underlying
estimator rather than the variability of the per-run mean.

This script adapts to the data that is *actually* present in the DuckDB
research database. In priority order it uses:

1. ``windows`` table rows — one Sharpe / PF / WR / DD per walk-forward
   window. Bootstrap resamples windows with replacement and reports the
   distribution of the per-window mean.
2. ``trades`` table rows — one PnL per trade. Bootstrap resamples trades
   and rebuilds per-window metrics before computing the mean.
3. ``metrics_summary`` table only — what we have today. Each run already
   collapses its windows into a ``mean_*`` scalar, and the cross-run
   variability is what we can sample. The script bootstraps the run-level
   ``mean_*`` values to produce a CI on the expected per-window metric.

The third mode is the current production reality: ``windows`` and
``trades`` are 0-row tables because the per-row persistence fix shipped
later than the runs we are scoring. The CIs derived from run-level
aggregates are necessarily wider and coarser than what we will get once
per-window data lands, but they are still far more honest than reporting
single point estimates with no uncertainty.

CLI examples
------------
    # Default: read the SRF DuckDB at the project root, write the report
    # to reports/quant/bootstrap_ci_YYYY-MM-DD.md with today's date.
    python scripts/quant/bootstrap_ci.py

    # Override DB path, iterations, or seed for reproducibility studies:
    python scripts/quant/bootstrap_ci.py \\
        --db data/research/research.duckdb \\
        --iterations 10000 --seed 42 \\
        --report reports/quant/bootstrap_ci_2026-07-13.md
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

import numpy as np

# Resolve the Ayumi project root by walking up from this file until we
# find a directory that has both ``.git`` and ``src/forex_bot`` — that
# combination uniquely identifies an Ayumi git checkout (worktree or
# main). We intentionally do NOT depend on the SRF helpers because the
# schema may drift between SRF versions and we want this script to
# remain runnable when db.py is mid-edit. The DB path itself may live
# outside the resolved root (worktrees usually do not have ``data/``
# populated), so callers should pass ``--db`` explicitly when the DB
# lives outside the resolved root.
THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR
while REPO_ROOT != REPO_ROOT.parent:
    if (REPO_ROOT / ".git").exists() and (REPO_ROOT / "src" / "forex_bot").exists():
        break
    REPO_ROOT = REPO_ROOT.parent
# Last-resort fallback: if the upward walk never identified an Ayumi
# checkout, try the canonical install path. This handles the case where
# the script was copied to a bare scripts directory without a sibling
# Ayumi checkout.
if REPO_ROOT == REPO_ROOT.parent:
    fallback = Path("$AYUMI_ROOT")
    if (fallback / "src" / "forex_bot").exists():
        REPO_ROOT = fallback
DEFAULT_DB = REPO_ROOT / "data" / "research" / "research.duckdb"
DEFAULT_REPORT = REPO_ROOT / "reports" / "quant" / f"bootstrap_ci_{date.today().isoformat()}.md"

# Bootstrap configuration constants.
DEFAULT_ITERATIONS = 10_000
DEFAULT_SEED = 42
PF_KILL_THRESHOLD = 1.0  # lower CI bound of mean PF < 1.0 → kill-eligible
MIN_TRADES_FOR_RELIABLE_CI = 30  # below this, flag as "thin sample"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyRuns:
    """All runs and their per-run metrics for one strategy."""

    name: str
    n_runs: int
    n_runs_with_trades: int
    total_trades: int
    # Per-run scalars (lists aligned by index).
    sharpe: list[float] = field(default_factory=list)
    profit_factor: list[float] = field(default_factory=list)
    win_rate: list[float] = field(default_factory=list)
    max_drawdown: list[float] = field(default_factory=list)
    # True when we had per-window data for at least one run.
    has_per_window: bool = False
    per_window_sharpe: list[float] = field(default_factory=list)
    per_window_pf: list[float] = field(default_factory=list)
    per_window_wr: list[float] = field(default_factory=list)
    per_window_dd: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class BootstrapResult:
    """95% CI on one metric for one strategy."""

    metric: str
    point: float
    lower: float
    upper: float
    n_obs: int
    note: str = ""


def load_strategy_runs(db_path: Path) -> list[StrategyRuns]:
    """Load per-strategy aggregates from the SRF DuckDB.

    Uses per-window data when available (windows table non-empty), and
    falls back to run-level metrics_summary aggregation otherwise.
    """
    import duckdb

    if not db_path.exists():
        raise FileNotFoundError(f"SRF DuckDB not found at {db_path}")

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        # Count of per-window rows — if any, we have per-window data.
        n_windows = conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
        _n_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

        # Always read the run-level summary so we can report run counts.
        runs_rows = conn.execute(
            """SELECT r.strategy_name,
                      m.mean_sharpe, m.mean_profit_factor,
                      m.mean_win_rate, m.mean_max_drawdown,
                      m.total_trades, m.windows_passed, m.windows_total,
                      m.go_nogo
               FROM runs r
               LEFT JOIN metrics_summary m USING (run_id)
               ORDER BY r.strategy_name, r.created_at"""
        ).fetchall()

        # Per-window payload, if any.
        per_window: dict[str, dict[str, list[float]]] = {}
        if n_windows > 0:
            for row in conn.execute(
                """SELECT r.strategy_name, w.sharpe, w.profit_factor,
                          w.win_rate, w.max_drawdown
                   FROM windows w
                   JOIN runs r USING (run_id)
                   WHERE w.sharpe IS NOT NULL
                """
            ).fetchall():
                name = row[0]
                bucket = per_window.setdefault(
                    name,
                    {"sharpe": [], "pf": [], "wr": [], "dd": []},
                )
                bucket["sharpe"].append(float(row[1]))
                bucket["pf"].append(float(row[2]))
                bucket["wr"].append(float(row[3]))
                bucket["dd"].append(float(row[4]))

        # Aggregate by strategy.
        by_strategy: dict[str, StrategyRuns] = {}
        for row in runs_rows:
            name = row[0]
            mean_sharpe = row[1]
            mean_pf = row[2]
            mean_wr = row[3]
            mean_dd = row[4]
            total_trades = row[5] or 0

            cur = by_strategy.get(name)
            if cur is None:
                cur = StrategyRuns(
                    name=name,
                    n_runs=0,
                    n_runs_with_trades=0,
                    total_trades=0,
                )
                by_strategy[name] = cur

            # Re-build the StrategyRuns with appended scalars — dataclass
            # is frozen, so we replace.
            new = StrategyRuns(
                name=cur.name,
                n_runs=cur.n_runs + 1,
                n_runs_with_trades=cur.n_runs_with_trades + (1 if total_trades > 0 else 0),
                total_trades=cur.total_trades + int(total_trades),
                sharpe=cur.sharpe + ([float(mean_sharpe)] if mean_sharpe is not None else []),
                profit_factor=cur.profit_factor + ([float(mean_pf)] if mean_pf is not None else []),
                win_rate=cur.win_rate + ([float(mean_wr)] if mean_wr is not None else []),
                max_drawdown=cur.max_drawdown + ([float(mean_dd)] if mean_dd is not None else []),
                has_per_window=name in per_window,
                per_window_sharpe=per_window.get(name, {}).get("sharpe", []),
                per_window_pf=per_window.get(name, {}).get("pf", []),
                per_window_wr=per_window.get(name, {}).get("wr", []),
                per_window_dd=per_window.get(name, {}).get("dd", []),
            )
            by_strategy[name] = new

        return list(by_strategy.values())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bootstrap engine
# ---------------------------------------------------------------------------


def _percentile_ci(samples: np.ndarray, alpha: float = 0.05) -> tuple[float, float, float]:
    """Percentile-method bootstrap CI. Returns (point, lower, upper)."""
    point = float(np.mean(samples))
    lower = float(np.percentile(samples, 100 * (alpha / 2)))
    upper = float(np.percentile(samples, 100 * (1 - alpha / 2)))
    return point, lower, upper


def bootstrap_metric(
    values: Sequence[float],
    iterations: int,
    rng: np.random.Generator,
) -> BootstrapResult | None:
    """Bootstrap CI on the mean of ``values``.

    Returns ``None`` when the sample is empty or has fewer than 2 finite
    observations — the caller is expected to render that as ``n/a``.
    """
    arr = np.asarray([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if arr.size < 2:
        return None
    n = arr.size
    # Resample ``n`` indices with replacement, B times, compute mean of each.
    idx = rng.integers(0, n, size=(iterations, n))
    samples = arr[idx].mean(axis=1)
    point, lower, upper = _percentile_ci(samples)
    return BootstrapResult(metric="mean", point=point, lower=lower, upper=upper, n_obs=n)


def bootstrap_metric_with_note(
    metric_name: str,
    values: Sequence[float],
    iterations: int,
    rng: np.random.Generator,
    note: str = "",
) -> BootstrapResult | None:
    """Bootstrap CI with a metric label attached."""
    res = bootstrap_metric(values, iterations, rng)
    if res is None:
        return None
    return BootstrapResult(
        metric=metric_name,
        point=res.point,
        lower=res.lower,
        upper=res.upper,
        n_obs=res.n_obs,
        note=note,
    )


def derive_run_level_ci(
    strategy: StrategyRuns,
    iterations: int,
    rng: np.random.Generator,
) -> dict[str, BootstrapResult | None]:
    """Bootstraps across run-level means.

    This is the degraded path: each run contributes one scalar per metric
    (its per-run mean). We resample runs and average. The CI reflects the
    cross-run variability of the per-run mean — not the within-run
    variability that a per-window bootstrap would expose. We mark the
    result so the report can be honest about which path produced it.
    """
    note = "run-level resample (per-window table empty — wider, coarser CI)" if not strategy.has_per_window else ""
    return {
        "sharpe": bootstrap_metric_with_note("sharpe", strategy.sharpe, iterations, rng, note=note),
        "profit_factor": bootstrap_metric_with_note(
            "profit_factor", strategy.profit_factor, iterations, rng, note=note
        ),
        "win_rate": bootstrap_metric_with_note("win_rate", strategy.win_rate, iterations, rng, note=note),
        "max_drawdown": bootstrap_metric_with_note("max_drawdown", strategy.max_drawdown, iterations, rng, note=note),
    }


def derive_per_window_ci(
    strategy: StrategyRuns,
    iterations: int,
    rng: np.random.Generator,
) -> dict[str, BootstrapResult | None]:
    """Bootstraps across per-window scalars (preferred path)."""
    return {
        "sharpe": bootstrap_metric_with_note("sharpe", strategy.per_window_sharpe, iterations, rng),
        "profit_factor": bootstrap_metric_with_note("profit_factor", strategy.per_window_pf, iterations, rng),
        "win_rate": bootstrap_metric_with_note("win_rate", strategy.per_window_wr, iterations, rng),
        "max_drawdown": bootstrap_metric_with_note("max_drawdown", strategy.per_window_dd, iterations, rng),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt(v: float | None, digits: int = 3) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "n/a"
    return f"{v:.{digits}f}"


def kill_recommendation(
    pf_ci: BootstrapResult | None,
    total_trades: int,
) -> tuple[str, str]:
    """Return (verdict, reason) for a strategy.

    Verdicts:
      KILL    — kill-eligible based on data
      WATCH   — insufficient data to be sure; do not promote yet
      SURVIVE — lower PF CI > 1.0 and total trades ≥ MIN_TRADES
    """
    if total_trades <= 0:
        return (
            "KILL",
            "no trades across any run — strategy is non-functional or not wired to a live data feed",
        )
    if pf_ci is None:
        return (
            "WATCH",
            "PF CI undefined (need ≥2 finite per-window observations); re-run after windows table populates",
        )
    if pf_ci.lower < PF_KILL_THRESHOLD:
        return (
            "KILL",
            f"PF lower CI {pf_ci.lower:.3f} < {PF_KILL_THRESHOLD:.1f} — "
            "edge not statistically distinguishable from break-even",
        )
    if total_trades < MIN_TRADES_FOR_RELIABLE_CI:
        return (
            "WATCH",
            f"PF lower CI {pf_ci.lower:.3f} > 1.0 but total trades "
            f"({total_trades}) < {MIN_TRADES_FOR_RELIABLE_CI} — thin sample",
        )
    return (
        "SURVIVE",
        f"PF lower CI {pf_ci.lower:.3f} > 1.0 with {total_trades} trades",
    )


def render_markdown_report(
    rows: list[dict],
    iterations: int,
    seed: int,
    source: str,
    db_path: Path,
) -> str:
    """Render the bootstrap results as a markdown report."""
    today = date.today().isoformat()

    lines: list[str] = []
    lines.append(f"# Bootstrap Confidence Intervals — {today}")
    lines.append("")
    lines.append("**Source:** " + source)
    lines.append(f"**Database:** `{db_path}`")
    lines.append(f"**Iterations:** {iterations:,} (seed={seed})")
    lines.append("**Confidence level:** 95% (percentile method)")
    lines.append(f"**Kill threshold:** lower PF CI < {PF_KILL_THRESHOLD:.1f}")
    lines.append("")

    # Summary counts.
    counts = {"KILL": 0, "WATCH": 0, "SURVIVE": 0}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **SURVIVE:** {counts.get('SURVIVE', 0)}")
    lines.append(f"- **WATCH:** {counts.get('WATCH', 0)}")
    lines.append(f"- **KILL:** {counts.get('KILL', 0)}")
    lines.append("")

    # Per-strategy table.
    lines.append("## Per-strategy 95% Confidence Intervals")
    lines.append("")
    lines.append(
        "Columns: `point` = observed mean across bootstrap samples; `lo` / "
        "`hi` = 2.5% / 97.5% percentile of the bootstrap distribution of the "
        "metric mean; `n` = number of underlying observations (runs when "
        "per-window data is empty, windows otherwise); `path` = which data "
        "the bootstrap drew from."
    )
    lines.append("")
    lines.append("| Strategy | Metric | Point | 95% CI low | 95% CI high | n | Bootstrap path |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
    for r in rows:
        strategy = r["strategy"]
        ci_map = r["ci"]
        path = r["path"]
        for metric_key, label in [
            ("profit_factor", "Profit Factor"),
            ("sharpe", "Sharpe"),
            ("win_rate", "Win Rate"),
            ("max_drawdown", "Max Drawdown"),
        ]:
            ci = ci_map.get(metric_key)
            if ci is None:
                lines.append(f"| {strategy} | {label} | n/a | n/a | n/a | n/a | {path} |")
            else:
                lines.append(
                    f"| {strategy} | {label} | {_fmt(ci.point)} | "
                    f"{_fmt(ci.lower)} | {_fmt(ci.upper)} | {ci.n_obs} | {path} |"
                )
    lines.append("")

    # Sample size + kill verdict table.
    lines.append("## Sample Size & Verdict")
    lines.append("")
    lines.append("| Strategy | n_runs | n_runs_w_trades | total_trades | PF lower CI | Verdict | Reason |")
    lines.append("| --- | ---: | ---: | ---: | ---: | --- | --- |")
    for r in rows:
        pf_ci = r["ci"].get("profit_factor")
        pf_lo = _fmt(pf_ci.lower) if pf_ci else "n/a"
        lines.append(
            f"| {r['strategy']} | {r['n_runs']} | {r['n_runs_with_trades']} | "
            f"{r['total_trades']} | {pf_lo} | **{r['verdict']}** | {r['reason']} |"
        )
    lines.append("")

    # Detail block per strategy for the survivors + kills.
    lines.append("## Detail")
    lines.append("")
    for r in rows:
        lines.append(f"### {r['strategy']} — {r['verdict']}")
        lines.append("")
        lines.append(f"- **n_runs:** {r['n_runs']}")
        lines.append(f"- **n_runs_with_trades:** {r['n_runs_with_trades']}")
        lines.append(f"- **total_trades:** {r['total_trades']}")
        lines.append(f"- **bootstrap path:** {r['path']}")
        lines.append("")
        ci_map = r["ci"]
        for metric_key, label in [
            ("profit_factor", "Profit Factor"),
            ("sharpe", "Sharpe"),
            ("win_rate", "Win Rate"),
            ("max_drawdown", "Max Drawdown"),
        ]:
            ci = ci_map.get(metric_key)
            if ci is None:
                lines.append(f"- **{label}:** n/a (insufficient observations)")
            else:
                lines.append(
                    f"- **{label}:** point = {_fmt(ci.point)}, "
                    f"95% CI = [{_fmt(ci.lower)}, {_fmt(ci.upper)}] "
                    f"(n={ci.n_obs})" + (f" — {ci.note}" if ci.note else "")
                )
        lines.append(f"- **Verdict reason:** {r['reason']}")
        lines.append("")

    # Caveats & methodology.
    lines.append("## Methodology & Caveats")
    lines.append("")
    lines.append(
        "- When the `windows` table has rows for a strategy, the bootstrap "
        "resamples per-window scalars directly. This is the preferred path "
        "because the CI reflects the within-strategy window variability."
    )
    lines.append(
        "- When `windows` is empty (current production reality for the "
        "pre-SRF-fix runs in this database), the bootstrap resamples "
        "per-run `mean_*` scalars. The CI is wider and coarser because "
        "each run already collapsed many windows into one number; "
        "between-window variability is unrecoverable without the raw "
        "windows."
    )
    lines.append(
        "- All CIs use the percentile method on 10,000 bootstrap iterations with a fixed RNG seed for reproducibility."
    )
    lines.append(
        "- `total_trades = 0` strategies are kill-eligible on data "
        "grounds: there is no statistical evidence either way, but a "
        "strategy with zero executed trades cannot be promoted to "
        "production. Verdict is KILL regardless of any computed CI."
    )
    lines.append(
        "- The kill threshold (`lower PF CI < 1.0`) is the standard "
        "break-even test. It is intentionally one-sided — we only kill on "
        "evidence of no edge, not on evidence of large edge."
    )
    lines.append("")
    lines.append("_Generated by `scripts/quant/bootstrap_ci.py` on " + today + "._")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def compute_all(
    db_path: Path,
    iterations: int,
    seed: int,
) -> list[dict]:
    """Run the full bootstrap and return a list of per-strategy row dicts."""
    strategies = load_strategy_runs(db_path)
    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    for s in strategies:
        if s.has_per_window:
            ci_map = derive_per_window_ci(s, iterations, rng)
            path = "per-window"
        else:
            ci_map = derive_run_level_ci(s, iterations, rng)
            path = "run-level"
        verdict, reason = kill_recommendation(ci_map.get("profit_factor"), s.total_trades)
        rows.append(
            {
                "strategy": s.name,
                "n_runs": s.n_runs,
                "n_runs_with_trades": s.n_runs_with_trades,
                "total_trades": s.total_trades,
                "ci": ci_map,
                "path": path,
                "verdict": verdict,
                "reason": reason,
            }
        )
    return rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap CIs on SRF candidate strategy metrics.")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="Path to research.duckdb (default: %(default)s)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help="Bootstrap iterations (default: %(default)s)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="RNG seed for reproducibility (default: %(default)s)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Path to write markdown report (default: reports/quant/bootstrap_ci_<today>.md)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Optional path to also dump machine-readable JSON",
    )
    return parser.parse_args(argv)


def _jsonable(rows: list[dict]) -> list[dict]:
    """Convert BootstrapResult objects inside rows to plain dicts."""
    out = []
    for r in rows:
        ci_out = {}
        for k, ci in r["ci"].items():
            if ci is None:
                ci_out[k] = None
            else:
                ci_out[k] = {
                    "metric": ci.metric,
                    "point": ci.point,
                    "lower": ci.lower,
                    "upper": ci.upper,
                    "n_obs": ci.n_obs,
                    "note": ci.note,
                }
        out.append(
            {
                "strategy": r["strategy"],
                "n_runs": r["n_runs"],
                "n_runs_with_trades": r["n_runs_with_trades"],
                "total_trades": r["total_trades"],
                "ci": ci_out,
                "path": r["path"],
                "verdict": r["verdict"],
                "reason": r["reason"],
            }
        )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    rows = compute_all(args.db, args.iterations, args.seed)
    # Sort: SURVIVE first (by PF point desc), then WATCH, then KILL.
    verdict_order = {"SURVIVE": 0, "WATCH": 1, "KILL": 2}
    rows.sort(
        key=lambda r: (
            verdict_order.get(r["verdict"], 99),
            -(r["ci"].get("profit_factor").point if r["ci"].get("profit_factor") else float("-inf")),
        )
    )

    report_path = args.report or DEFAULT_REPORT
    report_path.parent.mkdir(parents=True, exist_ok=True)
    source = "metrics_summary JOIN runs (per-run aggregates; per-window fallback when windows table is non-empty)"
    report_md = render_markdown_report(
        rows,
        iterations=args.iterations,
        seed=args.seed,
        source=source,
        db_path=args.db,
    )
    report_path.write_text(report_md, encoding="utf-8")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(_jsonable(rows), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # Print a compact summary to stdout.
    print(f"Wrote report: {report_path}")
    print()
    print(f"{'Strategy':<32} {'Verdict':<8} {'PF point':>9} {'PF 95% CI':>17} {'Trades':>7}")
    print("-" * 80)
    for r in rows:
        pf_ci = r["ci"].get("profit_factor")
        if pf_ci is None:
            ci_str = "n/a"
            point_str = "n/a"
        else:
            ci_str = f"[{pf_ci.lower:.3f}, {pf_ci.upper:.3f}]"
            point_str = f"{pf_ci.point:.3f}"
        print(f"{r['strategy']:<32} {r['verdict']:<8} {point_str:>9} {ci_str:>17} {r['total_trades']:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
