"""
dsr_integration.py — Wire the Deflated Sharpe Ratio (DSR) gate into the
existing walk-forward (WF) evaluation pipeline as a post-WF filter.

This module is a thin adapter on top of :mod:`quant.oos_gate`. It accepts
the JSONL WF report format already produced by the SRMR+ Optuna pipeline
(``reports/srmr-plus-pipeline-2026-07-08/{PAIR}_focused_results.jsonl``)
and:

1. Re-derives a DSR p-value using the aggregated per-window metrics in each
   entry (``mean_sharpe``, ``mean_trade_count``, ``windows_passed``).
2. Ranks each entry into Tier ``A`` (production), ``B`` (demo), ``C``
   (paper), or ``REJECT`` using the tier thresholds defined in
   :mod:`quant.oos_gate`.
3. Writes a single annotated JSON report covering all streams.

Design notes
------------
* The aggregate-only JSONL format does not carry per-trade returns, so we
  cannot replay :func:`quant.oos_gate.evaluate_oos_gate` exactly (its
  Defect-4 fix requires per-trade returns). Instead we call
  :func:`quant.oos_gate.deflated_sharpe_ratio` directly with:

    - ``observed_sr`` = ``mean_sharpe`` (already an annualized per-window
      average from the WF runner; matches what the production gate would
      estimate when fed per-window aggregate PnLs only — see oos_gate.py
      docstring "If only per-window aggregate PnL is available…").
    - ``n_obs`` = ``mean_trade_count * windows_passed`` (estimated total
      trades across the windows that actually traded).
    - ``skewness`` = 0.0, ``kurtosis_regular`` = 3.0 (normal-distribution
      fallback; the data isn't available to estimate these).
    - ``n_trials`` = 160 by default (conservative multiple-testing count
      from the canonical config in :class:`GateConfig`).

* Tier ranking uses the same :class:`TierConfig` thresholds as the
  production gate (``TIER_A_PRODUCTION``, ``TIER_B_DEMO``,
  ``TIER_C_PAPER``) but does so via a direct aggregate-based check rather
  than rebuilding synthetic per-window returns. The three checks per tier
  are: ``windows_passed >= min_windows_passed``,
  ``mean_sharpe >= min_aggregate_sharpe``, and
  ``dsr_pvalue < dsr_alpha``.

Constraints
-----------
* Does **not** import or modify ``walk_forward.py`` / ``walk_forward_runner.py``
  — DSR runs strictly as a post-WF filter on already-computed JSONL.
* Does **not** modify ``oos_gate.py`` or ``go_nogo_criteria.py``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .oos_gate import (
    TIER_A_PRODUCTION,
    TIER_B_DEMO,
    TIER_C_PAPER,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    min_track_record_length,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default multiple-testing correction factor. Matches the canonical
#: ``GateConfig`` default (``10 strategies × 4 pairs × 4 timeframes = 160``
#: conservative count from the oos-gate research doc §6).
DEFAULT_N_INDEPENDENT_TRIALS = 160

#: Conservative sanity floor for estimated total OOS trades. We only use
#: ``windows_passed`` worth of windows to estimate total trades, but we
#: never let ``n_obs`` fall below this floor — prevents DSR p-values
#: collapsing to ~0 from tiny sample sizes.
_MIN_TRADES_FLOOR = 30


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DSRTierThresholds:
    """Per-tier gating thresholds for the post-WF DSR filter.

    Mirrors :class:`oos_gate.TierConfig` but stays self-contained (no
    dependency on the ``oos_gate`` config dataclass shape) so the JSON
    writer doesn't choke on dataclass serialization.
    """

    tier: str  # "A", "B", "C", or "REJECT"
    min_windows_passed: int
    min_aggregate_sharpe: float
    dsr_alpha: float


#: Tier definitions re-exported in a JSON-serializable form.
TIER_THRESHOLDS: tuple[DSRTierThresholds, ...] = (
    DSRTierThresholds(
        tier=TIER_A_PRODUCTION.name,
        min_windows_passed=TIER_A_PRODUCTION.min_windows_passed,
        min_aggregate_sharpe=TIER_A_PRODUCTION.min_aggregate_sharpe,
        dsr_alpha=TIER_A_PRODUCTION.dsr_alpha,
    ),
    DSRTierThresholds(
        tier=TIER_B_DEMO.name,
        min_windows_passed=TIER_B_DEMO.min_windows_passed,
        min_aggregate_sharpe=TIER_B_DEMO.min_aggregate_sharpe,
        dsr_alpha=TIER_B_DEMO.dsr_alpha,
    ),
    DSRTierThresholds(
        tier=TIER_C_PAPER.name,
        min_windows_passed=TIER_C_PAPER.min_windows_passed,
        min_aggregate_sharpe=TIER_C_PAPER.min_aggregate_sharpe,
        dsr_alpha=TIER_C_PAPER.dsr_alpha,
    ),
)


# ---------------------------------------------------------------------------
# Pure helpers (no I/O; trivially unit-testable)
# ---------------------------------------------------------------------------


def _safe_get(entry: dict, *keys: str, default: Any = None) -> Any:
    """Return ``entry[k1]`` if present, else ``entry[k2]``, else ``default``."""
    for k in keys:
        if k in entry and entry[k] is not None:
            return entry[k]
    return default


def estimate_n_obs(entry: dict) -> int:
    """Estimate the total number of OOS trades for one WF entry.

    Computed as ``mean_trade_count * windows_passed`` (round to int),
    floored at :data:`_MIN_TRADES_FLOOR` so DSR p-values don't collapse to
    near-zero from tiny samples.

    Returns 0 when both inputs are missing or zero.
    """
    mean_trades = float(_safe_get(entry, "mean_trade_count", default=0.0))
    windows_passed = int(_safe_get(entry, "windows_passed", default=0))
    if mean_trades <= 0 or windows_passed <= 0:
        return 0
    estimated = int(round(mean_trades * windows_passed))
    return max(estimated, _MIN_TRADES_FLOOR) if estimated > 0 else 0


def _determine_tier(
    *,
    windows_passed: int,
    aggregate_sharpe: float,
    dsr_pvalue: float,
) -> tuple[str, str]:
    """Map a single stream to its tier (strictest first).

    Three checks per tier (in strictest-to-loosest order A → B → C):
    1. ``windows_passed >= tier.min_windows_passed``
    2. ``aggregate_sharpe >= tier.min_aggregate_sharpe``
    3. ``dsr_pvalue < tier.dsr_alpha``

    Returns ``(tier, reason)``. The first tier to pass all three wins.
    If no tier passes, returns ``("REJECT", reason)``.

    Streams with zero trades (``windows_passed == 0``) are immediately
    rejected before the tier loop — DSR cannot evaluate them meaningfully.
    """
    if windows_passed <= 0:
        return ("REJECT", "no windows passed WF")

    for tier in TIER_THRESHOLDS:
        windows_ok = windows_passed >= tier.min_windows_passed
        sharpe_ok = aggregate_sharpe >= tier.min_aggregate_sharpe
        dsr_ok = dsr_pvalue < tier.dsr_alpha
        if windows_ok and sharpe_ok and dsr_ok:
            reason = (
                f"windows_passed={windows_passed}>="
                f"{tier.min_windows_passed}, "
                f"aggregate_sharpe={aggregate_sharpe:.3f}>="
                f"{tier.min_aggregate_sharpe:.2f}, "
                f"dsr_p={dsr_pvalue:.4f}<{tier.dsr_alpha:.2f}"
            )
            return (tier.tier, reason)

    # Diagnostic reason: walk the strictest tier to surface why we failed.
    top = TIER_THRESHOLDS[0]
    return (
        "REJECT",
        (
            f"no tier passed: "
            f"sharpe={aggregate_sharpe:.3f} "
            f"(top required {top.min_aggregate_sharpe:.2f}), "
            f"dsr_p={dsr_pvalue:.4f}, "
            f"windows_passed={windows_passed} "
            f"(top required {top.min_windows_passed})"
        ),
    )


def annotate_wf_results_with_dsr(
    wf_results: Sequence[dict],
    n_trials: int = DEFAULT_N_INDEPENDENT_TRIALS,
) -> list[dict]:
    """Annotate a list of WF JSONL entries with DSR p-values and tiers.

    The returned list is a new list of shallow-copied dicts; the input
    dicts are not mutated.

    For each entry the following fields are added:

    - ``dsr_pvalue`` (float) — one-sided DSR p-value against ``n_trials``.
    - ``dsr_n_obs`` (int) — estimated total OOS trades used as ``n_obs``.
    - ``dsr_n_trials`` (int) — number of independent trials used.
    - ``dsr_expected_max_sr`` (float) — ``E[max SR | null]`` for ``n_trials``.
    - ``tier`` (str) — ``"A"`` / ``"B"`` / ``"C"`` / ``"REJECT"``.
    - ``tier_reason`` (str) — short explanation of the tier decision.
    - ``dsr_viable`` (bool) — ``True`` iff ``n_obs > 0``.

    Args:
        wf_results: list of JSONL-decoded dicts (one per WF run).
        n_trials: multiple-testing correction factor (default 160).

    Returns:
        A new list of dicts, preserving the original ``wf_results`` order.
    """
    annotated: list[dict] = []
    for entry in wf_results:
        # Extract aggregate metrics (multiple aliases for backward compat).
        mean_sharpe = float(_safe_get(entry, "mean_sharpe", default=0.0))
        mean_trades = float(_safe_get(entry, "mean_trade_count", default=0.0))
        windows_passed = int(_safe_get(entry, "windows_passed", default=0))

        n_obs = estimate_n_obs(entry)
        dsr_viable = n_obs > 0

        if dsr_viable and mean_sharpe > 0.0:
            dsr_p = deflated_sharpe_ratio(
                observed_sr=mean_sharpe,
                n_trials=n_trials,
                n_obs=n_obs,
                skewness=0.0,
                kurtosis_regular=3.0,
            )
        else:
            # Don't run DSR on non-viable streams — p=1.0 is the safe
            # "not significant" answer and prevents spurious REJECT
            # signals from the same condition.
            dsr_p = 1.0

        tier, tier_reason = _determine_tier(
            windows_passed=windows_passed,
            aggregate_sharpe=mean_sharpe,
            dsr_pvalue=dsr_p,
        )

        # Edge probability: 1 - DSR p-value, clamped to [0, 1].
        # Represents the probability that the observed Sharpe is genuine
        # (not due to selection bias / multiple testing).
        edge_prob = max(0.0, min(1.0, 1.0 - dsr_p))

        # Minimum track record length (Bailey & López de Prado 2014).
        # Only meaningful for positive-SR streams.
        min_trl = -1
        if dsr_viable and mean_sharpe > 0.0:
            min_trl = min_track_record_length(
                observed_sr=mean_sharpe,
                n_trials=n_trials,
                skewness=0.0,
                kurtosis_regular=3.0,
                alpha=0.05,
            )

        # Annotate a copy so the caller's dicts are untouched.
        annotated_entry = dict(entry)
        annotated_entry.update(
            {
                "dsr_pvalue": float(dsr_p),
                "edge_probability": float(edge_prob),
                "min_track_record": int(min_trl),
                "dsr_n_obs": int(n_obs),
                "dsr_n_trials": int(n_trials),
                "dsr_expected_max_sr": float(expected_max_sharpe(n_trials) if n_trials > 1 else 0.0),
                "tier": tier,
                "tier_reason": tier_reason,
                "dsr_viable": bool(dsr_viable),
                # Echo metrics for downstream consumers without re-parsing.
                "_aggregate_sharpe": float(mean_sharpe),
                "_mean_trade_count": float(mean_trades),
            }
        )
        annotated.append(annotated_entry)

    return annotated


def generate_tier_ranking(annotated_results: Sequence[dict]) -> dict:
    """Group annotated WF results into a tier ranking.

    Args:
        annotated_results: output of :func:`annotate_wf_results_with_dsr`
            (or anything with a ``tier`` field).

    Returns:
        A dict with keys ``tier_a``, ``tier_b``, ``tier_c``, ``rejected`` —
        each a list of annotated entries — plus ``summary``:

        .. code-block:: python

            {
                "tier_a":   [<entry>, ...],
                "tier_b":   [<entry>, ...],
                "tier_c":   [<entry>, ...],
                "rejected": [<entry>, ...],
                "summary":  {
                    "total": <int>,
                    "tier_a_count": <int>,
                    "tier_b_count": <int>,
                    "tier_c_count": <int>,
                    "rejected_count": <int>,
                    "viable_count": <int>,
                }
            }
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    viable_count = 0

    for entry in annotated_results:
        tier = str(entry.get("tier", "REJECT"))
        if tier not in ("A", "B", "C", "REJECT"):
            tier = "REJECT"
        buckets[tier].append(entry)
        if entry.get("dsr_viable", False):
            viable_count += 1

    return {
        "tier_a": buckets["A"],
        "tier_b": buckets["B"],
        "tier_c": buckets["C"],
        "rejected": buckets["REJECT"],
        "summary": {
            "total": len(annotated_results),
            "tier_a_count": len(buckets["A"]),
            "tier_b_count": len(buckets["B"]),
            "tier_c_count": len(buckets["C"]),
            "rejected_count": len(buckets["REJECT"]),
            "viable_count": viable_count,
        },
    }


# ---------------------------------------------------------------------------
# JSONL I/O
# ---------------------------------------------------------------------------


def load_wf_report(report_path: str | Path) -> list[dict]:
    """Load a WF report JSONL file and return a list of entry dicts.

    Skips blank lines. Raises :class:`ValueError` if any non-empty line
    fails to parse.
    """
    path = Path(report_path)
    entries: list[dict] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: invalid JSON on line {lineno}: {e}") from e
    return entries


def run_dsr_gate_on_report(
    report_path: str | Path,
    n_trials: int = DEFAULT_N_INDEPENDENT_TRIALS,
) -> dict:
    """Load a WF JSONL report, annotate it with DSR fields, return wrapper.

    The returned dict has keys ``source_path``, ``n_trials``, ``entries``
    (annotated list), and ``tier_ranking`` (output of
    :func:`generate_tier_ranking`).
    """
    path = Path(report_path)
    entries = load_wf_report(path)
    annotated = annotate_wf_results_with_dsr(entries, n_trials=n_trials)
    ranking = generate_tier_ranking(annotated)
    return {
        "source_path": str(path),
        "n_trials": int(n_trials),
        "entries": annotated,
        "tier_ranking": ranking,
    }


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------


def run_dsr_gate_on_reports(
    report_paths: Iterable[str | Path],
    n_trials: int = DEFAULT_N_INDEPENDENT_TRIALS,
) -> list[dict]:
    """Run :func:`run_dsr_gate_on_report` over multiple paths.

    Returns a list (one entry per path) in the same order.
    """
    return [run_dsr_gate_on_report(p, n_trials=n_trials) for p in report_paths]


def write_combined_annotated_report(
    report_paths: Sequence[str | Path],
    output_path: str | Path,
    n_trials: int = DEFAULT_N_INDEPENDENT_TRIALS,
) -> Path:
    """Annotate all reports, write a combined JSON, and return the path.

    The output schema is:

    .. code-block:: python

        {
            "generated_at": "<ISO-8601 UTC>",
            "phase": "10b",
            "n_independent_trials": <int>,
            "source_reports": [str, ...],
            "per_report": [...],
            "combined": {<tier_ranking>},
            "tier_definitions": [...],
        }
    """
    paths = [Path(p) for p in report_paths]
    per_report = [run_dsr_gate_on_report(p, n_trials=n_trials) for p in paths]

    combined_entries: list[dict] = []
    for r in per_report:
        combined_entries.extend(r["entries"])

    combined_ranking = generate_tier_ranking(combined_entries)

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "10b",
        "n_independent_trials": int(n_trials),
        "source_reports": [str(p) for p in paths],
        "per_report": [
            {
                "source_path": r["source_path"],
                "n_trials": r["n_trials"],
                "tier_ranking": r["tier_ranking"],
            }
            for r in per_report
        ],
        "combined": combined_ranking,
        "tier_definitions": [
            {
                "tier": t.tier,
                "min_windows_passed": t.min_windows_passed,
                "min_aggregate_sharpe": t.min_aggregate_sharpe,
                "dsr_alpha": t.dsr_alpha,
            }
            for t in TIER_THRESHOLDS
        ],
    }

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump(doc, f, indent=2)
    return out


__all__ = [
    "DEFAULT_N_INDEPENDENT_TRIALS",
    "TIER_THRESHOLDS",
    "DSRTierThresholds",
    "estimate_n_obs",
    "annotate_wf_results_with_dsr",
    "generate_tier_ranking",
    "load_wf_report",
    "run_dsr_gate_on_report",
    "run_dsr_gate_on_reports",
    "write_combined_annotated_report",
]
