"""SRF Go/No-Go criteria evaluation."""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Phase 1 thresholds (placeholders until Phase 2 adds DSR) ─────────────
MIN_PROFIT_FACTOR = 1.3
MIN_WINDOWS_PASSED = 3  # out of 5
MAX_PARAM_STABILITY_CV = 0.3
MAX_OOS_SHARPE_DECAY = 0.5  # OOS/IS ratio, so < 0.5 means >50% decay
MIN_TRADES_PER_WINDOW = 20


@dataclass
class GoNogoResult:
    decision: str  # "go", "watch", "no-go"
    checks: dict[str, bool]
    detail: str


def evaluate_go_nogo(
    windows: list[dict],
    *,
    min_pf: float = MIN_PROFIT_FACTOR,
    min_windows: int = MIN_WINDOWS_PASSED,
    min_trades: int = MIN_TRADES_PER_WINDOW,
) -> GoNogoResult:
    """Evaluate go/no-go criteria against walk-forward window results.

    Each window dict should have: profit_factor, win_rate, trade_count,
    sharpe, max_drawdown, total_pnl.
    """
    checks = {}
    reasons = []

    if not windows:
        return GoNogoResult("no-go", {"has_windows": False}, "No windows to evaluate")

    checks["has_windows"] = True

    # ── 1. Profit factor per window ──────────────────────────────────────
    pf_passing = sum(
        1 for w in windows if w.get("profit_factor", 0) >= min_pf and w.get("trade_count", 0) >= min_trades
    )
    checks["windows_passed"] = pf_passing >= min_windows
    if not checks["windows_passed"]:
        reasons.append(f"Only {pf_passing}/{len(windows)} windows pass PF>{min_pf}")

    # ── 2. Minimum trades across all windows ─────────────────────────────
    total_trades = sum(w.get("trade_count", 0) for w in windows)
    checks["sufficient_trades"] = total_trades >= min_trades * len(windows) * 0.5
    if not checks["sufficient_trades"]:
        reasons.append(f"Only {total_trades} total trades (need ~{min_trades * len(windows)})")

    # ── 3. PF consistency (std not too high) ─────────────────────────────
    pfs = [w.get("profit_factor", 0) for w in windows if w.get("trade_count", 0) > 0]
    if pfs and len(pfs) > 1:
        pf_mean = statistics.mean(pfs)
        pf_std = statistics.stdev(pfs)
        pf_cv = pf_std / pf_mean if pf_mean > 0 else 1.0
        checks["pf_stability"] = pf_cv < 0.5
        if not checks["pf_stability"]:
            reasons.append(f"PF CV={pf_cv:.2f} — too inconsistent across windows")
    else:
        checks["pf_stability"] = False
        reasons.append("Insufficient windows for stability check")

    # ── 4. No catastrophic window (PF < 0.5) ─────────────────────────────
    catastrophic = [w for w in windows if w.get("profit_factor", 1) < 0.5]
    checks["no_catastrophe"] = len(catastrophic) == 0
    if catastrophic:
        reasons.append(f"{len(catastrophic)} windows with PF<0.5")

    # ── Decision ─────────────────────────────────────────────────────────
    all_pass = all(checks.values())
    near_pass = checks["windows_passed"] and checks["sufficient_trades"]

    if all_pass:
        decision = "go"
        detail = "All go/no-go criteria met"
    elif near_pass:
        decision = "watch"
        detail = "Core criteria met but stability/consistency concerns: " + "; ".join(reasons)
    else:
        decision = "no-go"
        detail = "; ".join(reasons)

    return GoNogoResult(decision, checks, detail)
