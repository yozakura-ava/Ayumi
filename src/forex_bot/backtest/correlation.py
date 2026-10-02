#!/usr/bin/env python3
"""Cross-strategy correlation matrix for backtest returns.

Computes an N×N Pearson correlation matrix from per-strategy return series.
Strategies with disparate trade counts are aligned on their overlapping
index range (min of the two lengths) to avoid fabricating data.

Usage::

    python -m backtest.correlation --input returns.json
    python -m backtest.correlation --input returns.json --output matrix.json

Input JSON format::

    {
      "strategy_a": [0.01, -0.02, 0.005, ...],
      "strategy_b": [0.02, -0.01, 0.008, ...],
      "strategy_c": [0.003, 0.001, -0.004, ...]
    }

Output (JSON to stdout)::

    {
      "strategies": ["strategy_a", "strategy_b", "strategy_c"],
      "matrix": [[1.0, 0.32, -0.15], [0.32, 1.0, 0.08], [-0.15, 0.08, 1.0]],
      "n_strategies": 3,
      "alignment": {"strategy_a__strategy_b": {"overlap": 42}, ...},
      "high_correlations": [
        {"pair": ["strategy_a", "strategy_b"], "correlation": 0.82}
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HIGH_CORRELATION_THRESHOLD = 0.7


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def pearson_correlation(x: Sequence[float], y: Sequence[float]) -> float:
    """Compute Pearson correlation coefficient between two series.

    Uses the overlapping portion (first ``min(len(x), len(y))`` elements)
    to avoid fabricating data points.

    Returns ``0.0`` when either series has zero variance or fewer than 2
    overlapping points.
    """
    n = min(len(x), len(y))
    if n < 2:
        return 0.0

    xs = x[:n]
    ys = y[:n]

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    var_x = sum((xi - mean_x) ** 2 for xi in xs)
    var_y = sum((yi - mean_y) ** 2 for yi in ys)

    denom = math.sqrt(var_x * var_y)
    if denom < 1e-15:
        return 0.0

    return cov / denom


def compute_correlation_matrix(
    returns_by_strategy: dict[str, Sequence[float]],
) -> dict:
    """Compute the full N×N correlation matrix across strategies.

    Parameters
    ----------
    returns_by_strategy
        Mapping of strategy name → sequence of per-trade or per-period returns.

    Returns
    -------
    dict with keys:
        ``strategies`` — list of strategy names (matrix axis order).
        ``matrix`` — N×N list of lists with correlation coefficients.
        ``n_strategies`` — number of strategies.
        ``alignment`` — dict of "nameA__nameB" → {"overlap": int} for each pair.
        ``high_correlations`` — list of pairs whose absolute correlation exceeds
            :data:`DEFAULT_HIGH_CORRELATION_THRESHOLD`.
    """
    strategies = sorted(returns_by_strategy.keys())
    n = len(strategies)

    if n == 0:
        return {
            "strategies": [],
            "matrix": [],
            "n_strategies": 0,
            "alignment": {},
            "high_correlations": [],
        }

    # Build matrix
    matrix: list[list[float]] = [[0.0] * n for _ in range(n)]
    alignment: dict[str, dict] = {}
    high_corrs: list[dict] = []

    for i in range(n):
        matrix[i][i] = 1.0  # diagonal is always 1.0
        for j in range(i + 1, n):
            name_a = strategies[i]
            name_b = strategies[j]
            rets_a = returns_by_strategy[name_a]
            rets_b = returns_by_strategy[name_b]
            overlap = min(len(rets_a), len(rets_b))

            key = f"{name_a}__{name_b}"
            alignment[key] = {"overlap": overlap}

            if overlap < 2:
                corr = 0.0
                alignment[key]["note"] = "insufficient_overlap"
            else:
                corr = pearson_correlation(rets_a, rets_b)

            matrix[i][j] = round(corr, 6)
            matrix[j][i] = round(corr, 6)

            if abs(corr) >= DEFAULT_HIGH_CORRELATION_THRESHOLD:
                high_corrs.append(
                    {
                        "pair": [name_a, name_b],
                        "correlation": round(corr, 6),
                    }
                )

    # Sort high correlations by absolute value descending
    high_corrs.sort(key=lambda c: abs(c["correlation"]), reverse=True)

    return {
        "strategies": strategies,
        "matrix": matrix,
        "n_strategies": n,
        "alignment": alignment,
        "high_correlations": high_corrs,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_returns(path: str) -> dict[str, list[float]]:
    """Load per-strategy returns from a JSON file.

    Expected format: ``{"strategy_name": [r1, r2, ...], ...}``
    """
    with open(path, "r") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object mapping strategy names to return lists, got {type(raw).__name__}")

    result: dict[str, list[float]] = {}
    for name, vals in raw.items():
        if not isinstance(vals, list):
            raise ValueError(f"Strategy '{name}' must map to a list of floats, got {type(vals).__name__}")
        result[name] = [float(v) for v in vals]

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backtest.correlation",
        description="Compute N×N correlation matrix from per-strategy returns.",
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        type=str,
        help="Path to JSON file mapping strategy names to return arrays.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Output file path (default: stdout).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_HIGH_CORRELATION_THRESHOLD,
        help=f"Absolute correlation threshold for flagging high pairs (default: {DEFAULT_HIGH_CORRELATION_THRESHOLD}).",
    )
    args = parser.parse_args(argv)

    try:
        returns = _load_returns(args.input)
    except FileNotFoundError:
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    result = compute_correlation_matrix(returns)

    # Override threshold-based filtering with CLI value if different
    if args.threshold != DEFAULT_HIGH_CORRELATION_THRESHOLD:
        high = []
        strategies = result["strategies"]
        matrix = result["matrix"]
        for i in range(len(strategies)):
            for j in range(i + 1, len(strategies)):
                if abs(matrix[i][j]) >= args.threshold:
                    high.append(
                        {
                            "pair": [strategies[i], strategies[j]],
                            "correlation": matrix[i][j],
                        }
                    )
        high.sort(key=lambda c: abs(c["correlation"]), reverse=True)
        result["high_correlations"] = high

    output = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
            f.write("\n")
    else:
        sys.stdout.write(output)
        sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
