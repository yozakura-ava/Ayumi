"""Equity curve tracking with daily/weekly summary reporting.

Records periodic balance snapshots and generates markdown reports
for monitoring forward-test performance against FTMO-style drawdown limits.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Challenge-completion detector (Phase 6, Quest §6) ─────────────────────


@dataclass
class ProfitTargetResult:
    """Result of FTMO profit-target check.

    Attributes:
        reached: True if profit target has been hit.
        current_balance: Balance used in the check.
        peak_balance: Peak balance used as the target baseline.
        target_pct: Fractional profit target (default 0.10 for FTMO 1-Step).
        required_balance: Peak * (1 + target_pct) — the level we need to cross.
        distance_to_target: required - current (signed).
        freeze_new_positions: True once target is reached. Engine should stop
            opening new positions per FTMO challenge-completion semantics.
        target_date: ISO date string when the target was first reached, if
            known (persisted in risk_guard_state.json for idempotent freezes).
    """

    reached: bool
    current_balance: float
    peak_balance: float
    target_pct: float
    required_balance: float
    distance_to_target: float
    freeze_new_positions: bool = False
    target_date: Optional[str] = None


# Canonical FTMO 1-Step +10% profit target (Quest Phase 0).
DEFAULT_PROFIT_TARGET_PCT = 0.10


def check_profit_target(
    state_path: str | Path = "data/state/risk_guard_state.json",
    target_pct: float = DEFAULT_PROFIT_TARGET_PCT,
    fallback_starting_balance: float = 100_000.0,
) -> ProfitTargetResult:
    """Determine whether the FTMO profit target has been reached.

    Uses the on-disk ``risk_guard_state.json`` (peak_balance + current_balance)
    so we get a persistent signal across processes. If the state file is
    missing or incomplete, falls back to a fresh peak derived from
    ``starting_balance`` so the call never raises a KeyError during cron.

    Returns a ``ProfitTargetResult``. ``freeze_new_positions`` is set
    whenever ``reached`` is True; the caller should then invoke the
    kill-switch freeze via ``KillSwitchManager.activate_global_freeze``
    (or wait for ``equity_tracker`` wiring in Phase 6 to do it
    automatically).
    """
    state_path = Path(state_path)
    peak = fallback_starting_balance
    current = fallback_starting_balance
    target_date: Optional[str] = None

    if state_path.exists():
        try:
            with state_path.open() as f:
                state = json.load(f)
            if isinstance(state, dict):
                # risk_guard_state uses peak_balance / current_balance
                pk = state.get("peak_balance")
                if isinstance(pk, (int, float)) and pk > 0:
                    peak = float(pk)
                cb = state.get("current_balance")
                if isinstance(cb, (int, float)) and cb > 0:
                    current = float(cb)
                # Prefer explicit "target_reached_date" if present
                tdate = state.get("profit_target_reached_date")
                if isinstance(tdate, str):
                    target_date = tdate
        except (json.JSONDecodeError, OSError):
            # Bad state file — fall through with defaults.
            pass

    required = peak * (1.0 + target_pct)
    distance = round(required - current, 2)
    reached = current >= required

    return ProfitTargetResult(
        reached=reached,
        current_balance=round(current, 2),
        peak_balance=round(peak, 2),
        target_pct=target_pct,
        required_balance=round(required, 2),
        distance_to_target=distance,
        freeze_new_positions=reached,
        target_date=target_date,
    )


@dataclass
class EquitySnapshot:
    """Single point-in-time equity observation."""

    timestamp: str  # ISO-8601 UTC
    balance: float
    daily_pnl: float
    peak_balance: float
    drawdown_pct: float
    trade_count: int


@dataclass
class DailySummary:
    """Aggregated equity stats for one trading day."""

    date: str
    open_balance: float
    close_balance: float
    pnl: float
    pnl_pct: float
    max_dd: float
    trades: int
    ftmo_status: str


@dataclass
class WeeklySummary:
    """Aggregated equity stats for one week (Mon–Sun)."""

    week_of: str
    start_balance: float
    end_balance: float
    pnl: float
    pnl_pct: float
    best_day: Optional[str]
    worst_day: Optional[str]
    total_trades: int


class EquityTracker:
    """Tracks equity snapshots and produces daily/weekly reports.

    Parameters
    ----------
    data_dir
        Root data directory.  Snapshots are written to
        ``<data_dir>/forex/equity_snapshots.jsonl`` and reports to
        ``<data_dir>/forex/equity_reports/``.
    starting_balance
        Opening balance used to seed the peak and daily open.
    """

    def __init__(
        self,
        data_dir: str | Path = "data",
        starting_balance: float = 10_000.0,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._snapshots_path = self._data_dir / "forex" / "equity_snapshots.jsonl"
        self._reports_dir = self._data_dir / "forex" / "equity_reports"
        self._snapshots_path.parent.mkdir(parents=True, exist_ok=True)
        self._reports_dir.mkdir(parents=True, exist_ok=True)

        self._peak_balance = starting_balance
        self._daily_open = starting_balance
        self._current_day: Optional[str] = None
        self._trade_count_at_day_open = 0

    # ── Core recording ───────────────────────────────────────────────────

    def record(self, balance: float, trade_count: int) -> EquitySnapshot:
        """Record a single equity snapshot and append it to the JSONL log.

        Returns the snapshot that was recorded.

        Day rollover handling (card d8c2a10b): when the first snapshot of
        a new UTC day is recorded, the prior day's canonical export
        (``equity_reports/<prior_day>.md``) is written before the new
        snapshot is appended. This keeps the canonical export in sync with
        the operational feed (5-min snapshots) even when the forward
        test process never exits cleanly — the previous behaviour wrote
        the daily report only on process shutdown, leaving canonical
        exports stale for the entire continuous-run lifetime.
        """
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # Roll the day if needed
        if self._current_day is None:
            self._current_day = today
            self._daily_open = balance
            self._trade_count_at_day_open = trade_count
        elif self._current_day != today:
            # Day boundary crossed — flush the prior day's canonical
            # export BEFORE recording the first snapshot of the new day,
            # so the markdown reflects only the prior day's data.
            prior_day = self._current_day
            self._current_day = today
            self._daily_open = balance
            self._trade_count_at_day_open = trade_count
            try:
                _prior_report = self.write_daily_report(prior_day)
                if _prior_report is not None:
                    logger.info(
                        "[A8 Equity] Day rollover: wrote prior-day "
                        "canonical export %s for %s",
                        _prior_report,
                        prior_day,
                    )
            except Exception as _rollover_err:  # noqa: BLE001
                # Non-fatal: a failed rollover report must not break
                # the operational snapshot pipeline. The next explicit
                # write_daily_report() call (e.g. on exit or by cron)
                # will still surface the prior day.
                logger.warning(
                    "[A8 Equity] Day rollover report for %s failed "
                    "(non-fatal): %s",
                    prior_day,
                    _rollover_err,
                )

        if balance > self._peak_balance:
            self._peak_balance = balance

        dd_pct = 0.0
        if self._peak_balance > 0:
            dd_pct = round((self._peak_balance - balance) / self._peak_balance * 100, 4)

        daily_pnl = round(balance - self._daily_open, 2)

        snap = EquitySnapshot(
            timestamp=now.isoformat(),
            balance=round(balance, 2),
            daily_pnl=daily_pnl,
            peak_balance=round(self._peak_balance, 2),
            drawdown_pct=dd_pct,
            trade_count=trade_count,
        )

        # Append to JSONL
        with open(self._snapshots_path, "a") as f:
            f.write(json.dumps(asdict(snap)) + "\n")

        return snap

    # ── Snapshot loading ─────────────────────────────────────────────────

    def _load_snapshots(self) -> list[dict]:
        """Load all snapshots from the JSONL file."""
        if not self._snapshots_path.exists():
            return []
        rows: list[dict] = []
        with open(self._snapshots_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    @staticmethod
    def _snap_date(snap: dict) -> str:
        """Extract YYYY-MM-DD from an ISO timestamp."""
        return snap["timestamp"][:10]

    # ── Summaries ────────────────────────────────────────────────────────

    def daily_summary(self, target_date: Optional[str] = None) -> Optional[DailySummary]:
        """Return aggregated stats for *target_date* (YYYY-MM-DD).

        Defaults to the most recent date present in snapshots.
        """
        snaps = self._load_snapshots()
        if not snaps:
            return None

        if target_date is None:
            target_date = self._snap_date(snaps[-1])

        day_snaps = [s for s in snaps if self._snap_date(s) == target_date]
        if not day_snaps:
            return None

        open_bal = day_snaps[0]["balance"]
        close_bal = day_snaps[-1]["balance"]
        pnl = round(close_bal - open_bal, 2)
        pnl_pct = round(pnl / open_bal * 100, 2) if open_bal else 0.0
        max_dd = max(s["drawdown_pct"] for s in day_snaps)
        trades = day_snaps[-1]["trade_count"] - day_snaps[0]["trade_count"]
        # Absolute value in case of negative (shouldn't happen but guard)
        trades = abs(trades)

        # FTMO-style status: 10% max daily loss, 5% daily drawdown concern
        loss_pct = abs(min(pnl_pct, 0))
        if loss_pct >= 10:
            ftmo = "BREACH"
        elif loss_pct >= 5:
            ftmo = "WARNING"
        elif max_dd >= 5:
            ftmo = "WARNING"
        else:
            ftmo = "OK"

        return DailySummary(
            date=target_date,
            open_balance=round(open_bal, 2),
            close_balance=round(close_bal, 2),
            pnl=pnl,
            pnl_pct=pnl_pct,
            max_dd=round(max_dd, 2),
            trades=trades,
            ftmo_status=ftmo,
        )

    def weekly_summary(self, week_start: Optional[str] = None) -> Optional[WeeklySummary]:
        """Return aggregated stats for the ISO week containing *week_start*.

        Defaults to the most recent week present in snapshots.
        """
        snaps = self._load_snapshots()
        if not snaps:
            return None

        # Group by ISO week
        weeks: dict[str, list[dict]] = {}
        for s in snaps:
            dt = datetime.fromisoformat(s["timestamp"])
            iso_year, iso_week, _ = dt.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
            weeks.setdefault(week_key, []).append(s)

        if week_start is None:
            # Most recent week
            week_key = sorted(weeks.keys())[-1]
        else:
            # Accept "YYYY-WXX" format
            week_key = week_start

        week_snaps = weeks.get(week_key, [])
        if not week_snaps:
            return None

        start_bal = week_snaps[0]["balance"]
        end_bal = week_snaps[-1]["balance"]
        pnl = round(end_bal - start_bal, 2)
        pnl_pct = round(pnl / start_bal * 100, 2) if start_bal else 0.0
        total_trades = week_snaps[-1]["trade_count"] - week_snaps[0]["trade_count"]

        # Best/worst day
        daily_pnls: dict[str, float] = {}
        day_keys = sorted(set(self._snap_date(s) for s in week_snaps))
        for dk in day_keys:
            ds = self.daily_summary(dk)
            if ds:
                daily_pnls[dk] = ds.pnl

        best_day = max(daily_pnls, key=daily_pnls.get) if daily_pnls else None
        worst_day = min(daily_pnls, key=daily_pnls.get) if daily_pnls else None

        # Resolve the Monday of this ISO week for the label
        iso_year, iso_week_num = int(week_key[:4]), int(week_key.split("-W")[1])
        monday = date.fromisocalendar(iso_year, iso_week_num, 1)

        return WeeklySummary(
            week_of=monday.isoformat(),
            start_balance=round(start_bal, 2),
            end_balance=round(end_bal, 2),
            pnl=pnl,
            pnl_pct=pnl_pct,
            best_day=best_day,
            worst_day=worst_day,
            total_trades=abs(total_trades),
        )

    # ── Report writers ───────────────────────────────────────────────────

    def write_daily_report(self, target_date: Optional[str] = None) -> Optional[Path]:
        """Write a markdown daily report and return the file path."""
        ds = self.daily_summary(target_date)
        if ds is None:
            return None

        lines = [
            f"# Equity Daily Report — {ds.date}",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Open Balance | ${ds.open_balance:,.2f} |",
            f"| Close Balance | ${ds.close_balance:,.2f} |",
            f"| P&L | ${ds.pnl:+,.2f} |",
            f"| P&L % | {ds.pnl_pct:+.2f}% |",
            f"| Max Drawdown | {ds.max_dd:.2f}% |",
            f"| Trades | {ds.trades} |",
            f"| FTMO Status | {ds.ftmo_status} |",
            "",
        ]
        path = self._reports_dir / f"{ds.date}.md"
        path.write_text("\n".join(lines))
        return path

    def write_weekly_report(self, week_start: Optional[str] = None) -> Optional[Path]:
        """Write a markdown weekly report and return the file path."""
        ws = self.weekly_summary(week_start)
        if ws is None:
            return None

        lines = [
            f"# Equity Weekly Report — Week of {ws.week_of}",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Start Balance | ${ws.start_balance:,.2f} |",
            f"| End Balance | ${ws.end_balance:,.2f} |",
            f"| P&L | ${ws.pnl:+,.2f} |",
            f"| P&L % | {ws.pnl_pct:+.2f}% |",
            f"| Best Day | {ws.best_day or 'N/A'} |",
            f"| Worst Day | {ws.worst_day or 'N/A'} |",
            f"| Total Trades | {ws.total_trades} |",
            "",
        ]

        # Determine the ISO week key for the filename
        monday_dt = date.fromisoformat(ws.week_of)
        iso_year, iso_week, _ = monday_dt.isocalendar()
        path = self._reports_dir / f"week-{iso_year}-W{iso_week:02d}.md"
        path.write_text("\n".join(lines))
        return path
