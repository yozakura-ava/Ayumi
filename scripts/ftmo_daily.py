#!/usr/bin/env python3
"""FTMO daily tracker — produce a daily FTMO state report.

Per docs/design/hayate-checkpoint-design-2026-07-08.md §3.4 and the
Quest Phase 6 acceptance criteria. Reads from
``data/state/risk_guard_state.json`` (the live source for current
balance / peak / daily_start), computes:

  * Daily P&L vs the FTMO 1-Step daily limit (3%)
  * Total drawdown vs the FTMO 1-Step total limit (10%)
  * Best-day ratio vs the FTMO 1-Step 50% rule
  * Progress toward the +10% profit target

…and writes a markdown report to
``reports/ftmo-daily/YYYY-MM-DD.md``. With ``--activate-freeze`` the
script also fires the kill-switch global freeze when the +10% target
is reached (this is idempotent — re-running on an already-frozen
session just refreshes the audit trail).

FTMO 1-Step canonical values per Phase 0 of the FTMO Quest:
  * Daily loss limit: 3% of starting balance
  * Total drawdown limit: 10% of starting balance (peak-anchored)
  * Best-day cap: 50% (best day's profit vs total positive-day profit)
  * Starting balance: $100,000
  * Profit target: +10%
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Project-local imports (cwd-relative when run as a script).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "forex_bot"))
sys.path.insert(0, str(ROOT / "src"))

from reporting.equity_tracker import (  # noqa: E402, I001
    DEFAULT_PROFIT_TARGET_PCT,
    ProfitTargetResult,
    check_profit_target,
)

# ── FTMO 1-Step canonical values ───────────────────────────────────────────

DEFAULT_STATE_PATH = ROOT / "data" / "state" / "risk_guard_state.json"
DEFAULT_REPORTS_DIR = ROOT / "reports" / "ftmo-daily"

# FTMO 1-Step canonical values. These are intentionally independent of
# FTMOConfig (which is mode-dependent) so this script produces a
# consistent daily report regardless of which sub-account profile is
# currently selected on the engine.
FTMO_STARTING_BALANCE = 100_000.0
FTMO_DAILY_LIMIT_PCT = 0.03  # 3%
FTMO_TOTAL_LIMIT_PCT = 0.10  # 10%
FTMO_BEST_DAY_CAP_PCT = 0.50  # 50%
FTMO_PROFIT_TARGET_PCT = DEFAULT_PROFIT_TARGET_PCT  # 0.10


# ── Result dataclasses ─────────────────────────────────────────────────────


@dataclass
class FTMOMetrics:
    """Computed daily metrics derived from the live state file."""

    report_date: str  # YYYY-MM-DD
    starting_balance: float
    current_balance: float
    peak_balance: float
    daily_open_balance: float
    daily_pnl: float
    daily_loss_pct_of_start: float  # positive = loss vs starting balance
    daily_limit_pct: float
    daily_limit_pct_used: float  # daily_loss_pct / daily_limit_pct
    total_dd_pct: float  # peak-anchored drawdown
    total_limit_pct: float
    total_limit_pct_used: float  # total_dd_pct / total_limit_pct
    best_day_profit: Optional[float]
    total_positive_profit: Optional[float]
    best_day_ratio: Optional[float]
    best_day_cap_pct: float
    profit_target: ProfitTargetResult
    days_remaining_estimate: Optional[float]
    breach_status: str  # "OK" | "WARNING" | "BREACH"
    notes: list[str]


# ── State loader ───────────────────────────────────────────────────────────


def _load_state(path: str | Path = DEFAULT_STATE_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        return {
            "peak_balance": FTMO_STARTING_BALANCE,
            "current_balance": FTMO_STARTING_BALANCE,
            "daily_start_balance": FTMO_STARTING_BALANCE,
            "current_day": "",
            "total_trades": 0,
            "_missing": True,
        }
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "peak_balance": FTMO_STARTING_BALANCE,
            "current_balance": FTMO_STARTING_BALANCE,
            "daily_start_balance": FTMO_STARTING_BALANCE,
            "current_day": "",
            "total_trades": 0,
            "_missing": True,
            "_corrupt": True,
        }


# ── Best-day ratio loader (best-effort) ────────────────────────────────────


def _load_daily_pnl_history(data_dir: Path = ROOT / "data") -> list[dict]:
    """Load per-day P&L from the FTMO guard's persisted history if
    available. ``FTMOState`` writes daily_pnl_history via
    ``risk_guard_state.json.action_level_history`` style metadata; for
    the daily tracker we accept either that field OR fall back to the
    ``EquityTracker`` snapshots, whichever exists first.

    Returns an empty list if neither is present — the resulting report
    just shows the ratio as ``n/a``.
    """
    # Path 1: EquityTracker snapshots under data/forex/equity_snapshots.jsonl
    snaps_path = data_dir / "forex" / "equity_snapshots.jsonl"
    if snaps_path.exists():
        rows: list[dict] = []
        try:
            with snaps_path.open() as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            return []
        # Group by date
        per_day: dict[str, list[float]] = {}
        for r in rows:
            ts = r.get("timestamp", "")
            if not ts:
                continue
            day = ts[:10]
            bal = r.get("balance")
            if not isinstance(bal, (int, float)):
                continue
            per_day.setdefault(day, []).append(float(bal))
        out: list[dict] = []
        for day, balances in sorted(per_day.items()):
            if not balances:
                continue
            pnl = round(balances[-1] - balances[0], 2)
            out.append({"date": day, "pnl": pnl})
        return out
    return []


def _compute_best_day_ratio(
    history: list[dict],
) -> tuple[Optional[float], Optional[float]]:
    """Return ``(best_day_profit, total_positive_profit)``.

    Both are ``None`` if no positive-day history exists.
    """
    pos = [h["pnl"] for h in history if h.get("pnl", 0.0) > 0]
    if not pos:
        return None, None
    total_pos = sum(pos)
    best = max(pos)
    ratio = best / total_pos if total_pos > 0 else None
    return (
        round(best, 2),
        round(total_pos, 2),
        round(ratio, 4) if ratio is not None else None,
    )


# ── Core computation ───────────────────────────────────────────────────────


def compute_ftmo_metrics(
    state: dict | None = None,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    starting_balance: float = FTMO_STARTING_BALANCE,
    daily_limit_pct: float = FTMO_DAILY_LIMIT_PCT,
    total_limit_pct: float = FTMO_TOTAL_LIMIT_PCT,
    best_day_cap_pct: float = FTMO_BEST_DAY_CAP_PCT,
    profit_target_pct: float = FTMO_PROFIT_TARGET_PCT,
    daily_pnl_history: list[dict] | None = None,
    now: datetime | None = None,
) -> FTMOMetrics:
    """Compute the day's FTMO metrics.

    Parameters
    ----------
    state
        Pre-loaded state dict (skips the file read). When ``None`` we
        read from ``state_path``.
    state_path, starting_balance, *_pct
        Allow the values to be overridden in tests and dry-runs.
    daily_pnl_history
        Pre-loaded history (skips the snapshot scan).
    now
        Override for "report_date" (defaults to ``datetime.now(UTC)``).
    """
    now = now or datetime.now(timezone.utc)
    state = state if state is not None else _load_state(state_path)

    starting = float(starting_balance)
    peak = float(state.get("peak_balance") or starting_balance)
    current = float(state.get("current_balance") or starting_balance)
    daily_open = float(state.get("daily_start_balance") or starting_balance)
    daily_pnl = round(current - daily_open, 2)

    notes: list[str] = []
    if state.get("_missing"):
        notes.append("Risk guard state file missing — using $100K defaults.")
    if state.get("_corrupt"):
        notes.append("Risk guard state file was corrupt — using $100K defaults.")
    if peak != starting:
        notes.append(
            f"Peak balance (${peak:,.2f}) differs from canonical FTMO "
            f"starting balance (${starting:,.2f}). Phase 0 reconciliation is "
            f"still pending — daily/headroom figures use the canonical $100K starting."
        )

    # Daily loss vs starting balance (% of starting balance, NOT of
    # peak — this matches the Quest Phase 6 spec explicitly: "FTMO
    # 3% daily limit" is fractional of the *initial* account balance
    # in FTMO 1-Step).
    daily_loss_amt = max(0.0, daily_open - current)
    daily_loss_pct_of_start = round(daily_loss_amt / starting, 4) if starting > 0 else 0.0

    # Total drawdown (peak-anchored, expressed as % of peak).
    total_dd = 0.0
    if peak > 0:
        total_dd = max(0.0, (peak - current) / peak)
    total_dd_pct = round(total_dd, 4)

    # Best-day ratio
    history = daily_pnl_history
    if history is None:
        history = _load_daily_pnl_history()
    best_day, total_pos, best_ratio = (None, None, None)
    if history:
        result = _compute_best_day_ratio(history)
        if len(result) == 3:
            best_day, total_pos, best_ratio = result

    profit_target = check_profit_target(state_path, target_pct=profit_target_pct)

    # Days remaining estimate — straight-line based on current total
    # P&L. If we're losing or flat, mark ``None`` rather than a negative
    # count of days.
    days_remaining_estimate: Optional[float] = None
    total_pnl = current - starting
    if total_pnl > 0 and profit_target.required_balance > current:
        # assume the same per-day pace as a single-day average over the
        # active history. If history is empty, fall back to $1k/day as a
        # conservative placeholder (clearly flagged in notes).
        per_day = None
        if history:
            day_count = max(1, len(history))
            per_day = total_pnl / day_count
        if per_day is None or per_day <= 0:
            per_day = 1000.0
            notes.append("Day-pace estimate uses $1,000/day placeholder — no per-day history available.")
        remaining_dollars = profit_target.required_balance - current
        days_remaining_estimate = round(remaining_dollars / per_day, 1)

    # Composite breach status
    breach = "OK"
    if daily_loss_pct_of_start >= daily_limit_pct or total_dd_pct >= total_limit_pct:
        breach = "BREACH"
    elif (
        daily_loss_pct_of_start >= 0.5 * daily_limit_pct
        or total_dd_pct >= 0.5 * total_limit_pct
        or (best_ratio is not None and best_ratio >= 0.95 * best_day_cap_pct)
    ):
        breach = "WARNING"

    return FTMOMetrics(
        report_date=now.strftime("%Y-%m-%d"),
        starting_balance=round(starting, 2),
        current_balance=round(current, 2),
        peak_balance=round(peak, 2),
        daily_open_balance=round(daily_open, 2),
        daily_pnl=daily_pnl,
        daily_loss_pct_of_start=daily_loss_pct_of_start,
        daily_limit_pct=daily_limit_pct,
        daily_limit_pct_used=round(daily_loss_pct_of_start / daily_limit_pct, 4) if daily_limit_pct else 0.0,
        total_dd_pct=total_dd_pct,
        total_limit_pct=total_limit_pct,
        total_limit_pct_used=round(total_dd_pct / total_limit_pct, 4) if total_limit_pct else 0.0,
        best_day_profit=best_day,
        total_positive_profit=total_pos,
        best_day_ratio=best_ratio,
        best_day_cap_pct=best_day_cap_pct,
        profit_target=profit_target,
        days_remaining_estimate=days_remaining_estimate,
        breach_status=breach,
        notes=notes,
    )


# ── Markdown report writer ─────────────────────────────────────────────────


def render_report(m: FTMOMetrics) -> str:
    pt = m.profit_target
    pct_used_daily = m.daily_loss_pct_of_start / m.daily_limit_pct * 100 if m.daily_limit_pct else 0
    pct_used_total = m.total_dd_pct / m.total_limit_pct * 100 if m.total_limit_pct else 0
    progress_pct = (
        (m.current_balance - m.starting_balance) / (m.starting_balance * FTMO_PROFIT_TARGET_PCT) * 100
        if FTMO_PROFIT_TARGET_PCT
        else 0
    )

    lines: list[str] = []
    lines.append(f"# FTMO Daily Report — {m.report_date}")
    lines.append("")
    lines.append(f"**Status:** {m.breach_status}")
    if m.breach_status == "BREACH":
        lines.append("")
        lines.append("> ⚠️ **BREACH**: One or more FTMO limits exceeded. New positions must be frozen.")
    elif m.breach_status == "WARNING":
        lines.append("")
        lines.append("> ⚠️ **WARNING**: Within 50% of one or more FTMO limits — review before next session.")
    if pt.reached:
        lines.append("")
        lines.append("> 🎯 **Profit target reached.** New positions are frozen.")
    lines.append("")
    lines.append("## Equity")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Starting balance | ${m.starting_balance:,.2f} |")
    lines.append(f"| Current balance | ${m.current_balance:,.2f} |")
    lines.append(f"| Peak balance | ${m.peak_balance:,.2f} |")
    lines.append(f"| Daily open | ${m.daily_open_balance:,.2f} |")
    lines.append(f"| Daily P&L | ${m.daily_pnl:+,.2f} |")
    lines.append("")

    lines.append("## Daily Loss (FTMO 3% limit)")
    lines.append("")
    daily_remaining = max(0.0, m.daily_limit_pct - m.daily_loss_pct_of_start)
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Daily loss ($) | ${(m.daily_open_balance - m.current_balance):+,.2f} |")
    lines.append(f"| Daily loss (% of start) | {m.daily_loss_pct_of_start * 100:.2f}% |")
    lines.append(f"| Limit | {m.daily_limit_pct * 100:.2f}% (${m.starting_balance * m.daily_limit_pct:,.2f}) |")
    lines.append(f"| Used | {pct_used_daily:.1f}% of limit |")
    lines.append(
        f"| Remaining headroom | {daily_remaining * 100:.2f}% (${m.starting_balance * daily_remaining:,.2f}) |"
    )
    lines.append("")

    lines.append("## Total Drawdown (FTMO 10% limit)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| DD (%) | {m.total_dd_pct * 100:.2f}% |")
    lines.append(f"| DD ($) | ${(m.peak_balance - m.current_balance):,.2f} |")
    lines.append(f"| Limit | {m.total_limit_pct * 100:.2f}% (${m.peak_balance * m.total_limit_pct:,.2f}) |")
    lines.append(f"| Used | {pct_used_total:.1f}% of limit |")
    lines.append("")

    lines.append("## Best-Day Ratio (FTMO 50% cap)")
    lines.append("")
    if m.best_day_ratio is None:
        lines.append("- No positive-day history available yet (live snapshot stream not started).")
    else:
        ratio_pct = m.best_day_ratio * 100
        cap_pct = m.best_day_cap_pct * 100
        used = ratio_pct / cap_pct * 100 if cap_pct else 0
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Best day profit | ${m.best_day_profit:,.2f} |")
        lines.append(f"| Total positive-day profit | ${m.total_positive_profit:,.2f} |")
        lines.append(f"| Ratio | {ratio_pct:.2f}% |")
        lines.append(f"| Cap | {cap_pct:.2f}% |")
        lines.append(f"| Used | {used:.1f}% of cap |")
    lines.append("")

    lines.append("## Profit Target (+10%)")
    lines.append("")
    progress_dollars = m.current_balance - m.starting_balance
    target_dollars = m.starting_balance * FTMO_PROFIT_TARGET_PCT
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Progress to target | ${progress_dollars:+,.2f} / ${target_dollars:,.2f} ({progress_pct:+.1f}%) |")
    lines.append(f"| Required balance | ${pt.required_balance:,.2f} |")
    lines.append(f"| Reached | **{pt.reached}** |")
    if m.days_remaining_estimate is not None:
        lines.append(f"| Pace-based days remaining | {m.days_remaining_estimate:.1f} |")
    elif pt.reached:
        lines.append("| Pace-based days remaining | 0 (target reached) |")
    else:
        lines.append("| Pace-based days remaining | n/a (currently at or below starting balance) |")
    lines.append("")

    if m.notes:
        lines.append("## Notes")
        lines.append("")
        for n in m.notes:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"_Generated by scripts/ftmo_daily.py on {m.report_date}._")
    return "\n".join(lines) + "\n"


def write_report(
    metrics: FTMOMetrics,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
) -> Optional[Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{metrics.report_date}.md"
    path.write_text(render_report(metrics))
    return path


# ── Optional freeze activation ─────────────────────────────────────────────


def activate_freeze_if_target_reached(metrics: FTMOMetrics) -> bool:
    """Fire the kill-switch freeze when the +10% target is reached.

    Wrapped in a try/except so a missing import (e.g. older unit
    tests) doesn't kill the report generation. Returns True if the
    freeze was activated or already active.
    """
    if not metrics.profit_target.reached:
        return False
    try:
        from adapters.ctrader.kill_switch import KillSwitchManager

        km = KillSwitchManager()
        km.activate_profit_target_freeze(
            triggered_by="ftmo_daily.py",
            current_balance=metrics.current_balance,
            target_pct=metrics.profit_target.target_pct,
        )
        return True
    except Exception as exc:  # pragma: no cover - depends on runtime
        # The engine-side freeze is best-effort. The report itself
        # already records `pt.reached`, which the engine watches.
        print(f"WARN: kill-switch freeze unavailable: {exc}", file=sys.stderr)
        return False


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="FTMO daily tracker.")
    p.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Path to risk_guard_state.json",
    )
    p.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help="Output directory for the daily report",
    )
    p.add_argument(
        "--activate-freeze",
        action="store_true",
        help="Activate kill-switch global freeze when +10%% target is reached",
    )
    p.add_argument(
        "--no-write",
        action="store_true",
        help="Print the report to stdout instead of writing to disk",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    metrics = compute_ftmo_metrics(state_path=args.state_path)

    if args.no_write:
        sys.stdout.write(render_report(metrics))
    else:
        path = write_report(metrics, reports_dir=args.reports_dir)
        if path is None:
            print("Could not write report", file=sys.stderr)
            return 2
        print(f"Wrote {path}")

    if args.activate_freeze:
        if activate_freeze_if_target_reached(metrics):
            print("Profit target reached — kill-switch global FREEZE activated.")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
