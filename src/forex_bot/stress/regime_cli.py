"""CLI for synthetic regime generation — CSV I/O, multi-pair batch, and output.

Loads per-month or per-pair CSV files, fits GMM+HMM regime models via
:mod:`stress.regime_generator`, and writes synthetic price paths plus
regime distribution summaries.

Output format
-------------
- **Price CSV** — long-form with columns ``path_id, t, close``.
- **Summary JSON** — per-pair regime distribution counts and fitted params.

Usage examples
--------------
::

    # Single pair
    python -m stress.regime_cli single -i data/forex/historical/GBPUSD_M15.csv -o output/

    # All pairs in a directory (auto-discovers *_M*.csv)
    python -m stress.regime_cli all -i data/forex/historical/ -o output/

    # Custom parameters
    python -m stress.regime_cli single -i data.csv -o output/ --n-regimes 4 --n-paths 5000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from stress.regime_generator import (
    DEFAULT_N_REGIMES,
    DEFAULT_RANDOM_STATE,
    MIN_OBSERVATIONS,
    RegimeParams,
    bootstrap_synthetic_returns,
    synthesize_prices,
)

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_OUTPUT_DIR = "output/regime"
DEFAULT_PATHS = 500  # Lower default for CLI — full 10k is heavy
CLOSE_COLUMN_CANDIDATES = ("close", "Close", "CLOSE")
PRICE_COLUMN_CANDIDATES = ("close", "Close", "CLOSE", "price", "Price")


# ═══════════════════════════════════════════════════════════════════════════
# CSV I/O
# ═══════════════════════════════════════════════════════════════════════════


def load_price_csv(path: str | Path) -> pd.Series:
    """Load a CSV file and return the close-price series.

    Parameters
    ----------
    path : str or Path
        Path to the CSV file.  Must contain a recognised close-price column
        (``close``, ``Close``, or ``CLOSE``).

    Returns
    -------
    pd.Series
        Close prices as float64, indexed by row order.

    Raises
    ------
    ValueError
        If no recognised close-price column is found.
    """
    df = pd.read_csv(path)
    for col in CLOSE_COLUMN_CANDIDATES:
        if col in df.columns:
            return df[col].astype(float).dropna().reset_index(drop=True)
    raise ValueError(
        f"No close-price column found in {path}. "
        f"Looked for: {CLOSE_COLUMN_CANDIDATES}. "
        f"Available columns: {list(df.columns)}"
    )


def write_price_csv(
    synthetic_prices: np.ndarray,
    output_path: str | Path,
) -> int:
    """Write synthetic price paths to a long-form CSV.

    Output columns: ``path_id, t, close``.

    Parameters
    ----------
    synthetic_prices : ndarray, shape (n_paths, T)
        Synthetic price paths (including starting price at t=0).
    output_path : str or Path
        Destination CSV path.

    Returns
    -------
    int
        Number of rows written.
    """
    n_paths, T = synthetic_prices.shape
    rows = []
    for pid in range(n_paths):
        for t in range(T):
            rows.append((pid, t, float(synthetic_prices[pid, t])))
    df = pd.DataFrame(rows, columns=["path_id", "t", "close"])
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    return len(df)


# ═══════════════════════════════════════════════════════════════════════════
# Core pipeline
# ═══════════════════════════════════════════════════════════════════════════


def run_synthetic_generation(
    prices: np.ndarray | pd.Series,
    n_regimes: int = DEFAULT_N_REGIMES,
    n_paths: int = DEFAULT_PATHS,
    path_length: int | None = None,
    random_state: int = DEFAULT_RANDOM_STATE,
    start_price: float | None = None,
) -> tuple[np.ndarray, RegimeParams]:
    """Run the full synthetic generation pipeline on a single price series.

    Wraps :func:`bootstrap_synthetic_returns` and converts the output
    returns to price paths.

    Parameters
    ----------
    prices : array-like or pd.Series
        Input close prices.
    n_regimes : int
        Number of GMM/HMM regimes.
    n_paths : int
        Number of synthetic paths to generate.
    path_length : int or None
        Length of each path.  If ``None``, uses input return length.
    random_state : int
        Seed.
    start_price : float or None
        Starting price for synthetic paths.  If ``None``, uses the last
        observed close.

    Returns
    -------
    price_paths : ndarray, shape (n_paths, T+1)
        Synthetic price paths (T+1 includes the starting price).
    params : RegimeParams
        Fitted parameters.
    """
    price_arr = np.asarray(prices, dtype=float).ravel()
    if start_price is None:
        start_price = float(price_arr[-1])

    synthetic_returns, params = bootstrap_synthetic_returns(
        price_arr,
        n_regimes=n_regimes,
        n_paths=n_paths,
        path_length=path_length,
        random_state=random_state,
    )

    # Convert each return path to a price path
    n_paths_out, T = synthetic_returns.shape
    price_paths = np.empty((n_paths_out, T + 1), dtype=float)
    for i in range(n_paths_out):
        price_paths[i] = synthesize_prices(synthetic_returns[i], start_price=start_price)

    logger.info(
        "Generated %d synthetic price paths (T=%d, start=%.5f, regimes=%d)",
        n_paths_out,
        T,
        start_price,
        params.n_regimes,
    )
    return price_paths, params


def build_regime_summary(
    params: RegimeParams,
    pair_name: str,
    n_paths: int,
) -> dict[str, Any]:
    """Build a JSON-serialisable regime distribution summary.

    Parameters
    ----------
    params : RegimeParams
        Fitted parameters.
    pair_name : str
        Name of the pair (e.g. ``"GBPUSD"``).
    n_paths : int
        Number of paths generated.

    Returns
    -------
    dict
        Summary dictionary with per-regime counts and statistics.
    """
    regime_counts: dict[str, int] = {}
    for k in range(params.n_regimes):
        # Stationary probability × n_paths gives expected count
        expected = int(round(params.weights[k] * n_paths))
        regime_counts[f"regime_{k}"] = expected

    return {
        "pair": pair_name,
        "n_regimes": params.n_regimes,
        "n_paths": n_paths,
        "regime_distribution": regime_counts,
        "regime_means": [float(m) for m in params.means],
        "regime_stds": [float(s) for s in params.stds],
        "regime_weights": [float(w) for w in params.weights],
        "bic": float(params.bic),
        "transmat": [[float(v) for v in row] for row in params.transmat],
    }


def run_all_pairs(
    input_dir: str | Path,
    output_dir: str | Path,
    n_regimes: int = DEFAULT_N_REGIMES,
    n_paths: int = DEFAULT_PATHS,
    random_state: int = DEFAULT_RANDOM_STATE,
    pattern: str = "*_M*.csv",
) -> list[dict[str, Any]]:
    """Run synthetic generation for all pair CSVs in a directory.

    Parameters
    ----------
    input_dir : str or Path
        Directory containing input CSVs.
    output_dir : str or Path
        Directory for output files.
    n_regimes : int
        Number of regimes per pair.
    n_paths : int
        Paths per pair.
    random_state : int
        Seed.
    pattern : str
        Glob pattern for input file discovery.

    Returns
    -------
    list of dict
        Per-pair summary dictionaries.
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_files = sorted(input_dir.glob(pattern))
    if not csv_files:
        logger.warning("No CSV files matching '%s' in %s", pattern, input_dir)
        return []

    all_summaries: list[dict[str, Any]] = []

    for csv_path in csv_files:
        pair_name = csv_path.stem  # e.g. "GBPUSD_M15_2026"
        logger.info("Processing %s ...", pair_name)

        try:
            close_prices = load_price_csv(csv_path)
        except ValueError as exc:
            logger.error("Skip %s: %s", pair_name, exc)
            continue

        if len(close_prices) < MIN_OBSERVATIONS:
            logger.warning(
                "Skip %s: only %d prices (need ≥%d)",
                pair_name,
                len(close_prices),
                MIN_OBSERVATIONS,
            )
            continue

        try:
            price_paths, params = run_synthetic_generation(
                close_prices.to_numpy(),
                n_regimes=n_regimes,
                n_paths=n_paths,
                random_state=random_state,
            )
        except Exception:
            logger.exception("Generation failed for %s", pair_name)
            continue

        # Write price CSV
        price_out = output_dir / f"{pair_name}_synthetic.csv"
        write_price_csv(price_paths, price_out)

        # Build summary
        summary = build_regime_summary(params, pair_name, n_paths)
        all_summaries.append(summary)

    # Write combined JSON summary
    if all_summaries:
        summary_path = output_dir / "regime_summary.json"
        with open(summary_path, "w") as f:
            json.dump(all_summaries, f, indent=2)
        logger.info("Wrote summary for %d pairs → %s", len(all_summaries), summary_path)

    return all_summaries


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse CLI parser."""
    parser = argparse.ArgumentParser(
        prog="stress.regime_cli",
        description=(
            "Synthetic regime generator — fits GMM+HMM models to historical "
            "price data and generates synthetic price paths for stress testing."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── single ────────────────────────────────────────────────────────
    p_single = sub.add_parser(
        "single",
        help="Generate synthetic paths for a single CSV file.",
    )
    p_single.add_argument(
        "-i",
        "--input",
        required=True,
        help="Input CSV path (must contain a Close column).",
    )
    p_single.add_argument(
        "-o",
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    p_single.add_argument(
        "--n-regimes",
        type=int,
        default=DEFAULT_N_REGIMES,
        help=f"Number of regimes (default: {DEFAULT_N_REGIMES}).",
    )
    p_single.add_argument(
        "--n-paths",
        type=int,
        default=DEFAULT_PATHS,
        help=f"Number of synthetic paths (default: {DEFAULT_PATHS}).",
    )
    p_single.add_argument(
        "--path-length",
        type=int,
        default=None,
        help="Override synthetic path length (default: input length).",
    )
    p_single.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_RANDOM_STATE,
        help=f"Random seed (default: {DEFAULT_RANDOM_STATE}).",
    )

    # ── all ───────────────────────────────────────────────────────────
    p_all = sub.add_parser(
        "all",
        help="Generate synthetic paths for all CSV files in a directory.",
    )
    p_all.add_argument(
        "-i",
        "--input-dir",
        required=True,
        help="Directory containing input CSV files.",
    )
    p_all.add_argument(
        "-o",
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    p_all.add_argument(
        "--n-regimes",
        type=int,
        default=DEFAULT_N_REGIMES,
        help=f"Number of regimes per pair (default: {DEFAULT_N_REGIMES}).",
    )
    p_all.add_argument(
        "--n-paths",
        type=int,
        default=DEFAULT_PATHS,
        help=f"Number of synthetic paths per pair (default: {DEFAULT_PATHS}).",
    )
    p_all.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_RANDOM_STATE,
        help=f"Random seed (default: {DEFAULT_RANDOM_STATE}).",
    )
    p_all.add_argument(
        "--pattern",
        default="*_M*.csv",
        help="Glob pattern for file discovery (default: *_M*.csv).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Parameters
    ----------
    argv : list[str] or None
        Argument vector.  If ``None``, uses ``sys.argv[1:]``.

    Returns
    -------
    int
        Exit code (0 = success).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "single":
        return _cmd_single(args)
    elif args.command == "all":
        return _cmd_all(args)
    else:
        parser.print_help()
        return 1


def _cmd_single(args: argparse.Namespace) -> int:
    """Execute the ``single`` subcommand."""
    close_prices = load_price_csv(args.input)
    if len(close_prices) < MIN_OBSERVATIONS:
        logger.error(
            "Input has only %d rows (need ≥%d)",
            len(close_prices),
            MIN_OBSERVATIONS,
        )
        return 1

    price_paths, params = run_synthetic_generation(
        close_prices.to_numpy(),
        n_regimes=args.n_regimes,
        n_paths=args.n_paths,
        path_length=args.path_length,
        random_state=args.seed,
    )

    pair_name = Path(args.input).stem
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    price_out = output_dir / f"{pair_name}_synthetic.csv"
    n_rows = write_price_csv(price_paths, price_out)

    summary = build_regime_summary(params, pair_name, args.n_paths)
    summary_path = output_dir / f"{pair_name}_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    logger.info(
        "Done: %d rows → %s, summary → %s",
        n_rows,
        price_out,
        summary_path,
    )
    return 0


def _cmd_all(args: argparse.Namespace) -> int:
    """Execute the ``all`` subcommand."""
    summaries = run_all_pairs(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        n_regimes=args.n_regimes,
        n_paths=args.n_paths,
        random_state=args.seed,
        pattern=args.pattern,
    )

    if not summaries:
        logger.error("No pairs processed successfully.")
        return 1

    logger.info("Processed %d pairs.", len(summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
