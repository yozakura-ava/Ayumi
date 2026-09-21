"""Daily performance report generation from trade history."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass
class DailyPerformance:
    date: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    max_drawdown: float
    sniper_trades: int
    swarm_trades: int
    sniper_win_rate: float
    swarm_win_rate: float
    confidence_distribution: dict[str, int]  # "low"/"med"/"high" -> count
    gate_rejections: dict[str, int]  # gate_name -> count
    circuit_breaker_triggers: int
    daily_risk_used_pct: float
    per_strategy: dict[str, dict]


class DailyAnalytics:
    """Generates daily performance reports from trade history."""

    def __init__(
        self,
        trade_log_path: str = "logs/trades.jsonl",
        starting_balance: float = 10000.0,
        config: dict | None = None,
    ) -> None:
        self._trade_log = trade_log_path
        self._starting_balance = starting_balance
        self._config = config or {}
        self._tz = ZoneInfo("America/Toronto")

    def _load_trades(self, date: str | None = None) -> list[dict]:
        """Load trades from JSONL log, optionally filtered by date."""
        if not os.path.exists(self._trade_log):
            return []

        trades = []
        with open(self._trade_log) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = __import__("json").loads(line)
                except __import__("json").JSONDecodeError:
                    continue
                if date:
                    ts = t.get("timestamp", "")
                    if not ts.startswith(date):
                        continue
                trades.append(t)
        return trades

    def generate_report(self, date: str | None = None) -> DailyPerformance:
        """Generate daily report. Default: today."""
        if date is None:
            date = datetime.now(self._tz).strftime("%Y-%m-%d")

        trades = self._load_trades(date)
        return self._build_report(date, trades)

    def generate_summary(self, days: int = 7) -> list[DailyPerformance]:
        """Generate reports for last N days."""
        reports = []
        today = datetime.now(self._tz)
        for i in range(days):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            reports.append(self.generate_report(d))
        return reports

    def format_report(self, report: DailyPerformance) -> str:
        """Format report as human-readable text."""
        lines = [
            f"📊 Daily Performance — {report.date}",
            f"{'─' * 40}",
            f"Trades: {report.total_trades} (W: {report.winning_trades} / L: {report.losing_trades})",
            f"Win Rate: {report.win_rate:.1%}",
            f"PnL: ${report.total_pnl:+.2f}",
            f"Max Drawdown: ${report.max_drawdown:.2f}",
            "",
            f"Sniper: {report.sniper_trades} trades ({report.sniper_win_rate:.1%} WR)",
            f"Swarm: {report.swarm_trades} trades ({report.swarm_win_rate:.1%} WR)",
            "",
            f"Confidence: low={report.confidence_distribution.get('low', 0)} | "
            f"med={report.confidence_distribution.get('med', 0)} | "
            f"high={report.confidence_distribution.get('high', 0)}",
            f"Gate Rejections: {report.gate_rejections or 'none'}",
            f"Circuit Breakers: {report.circuit_breaker_triggers}",
            f"Risk Used: {report.daily_risk_used_pct:.1%}",
        ]
        if report.per_strategy:
            lines.append("")
            lines.append("Per Strategy:")
            for sid, stats in report.per_strategy.items():
                lines.append(f"  {sid}: {stats.get('trades', 0)} trades, ${stats.get('pnl', 0):+.2f}")
        return "\n".join(lines)

    def _build_report(self, date: str, trades: list[dict]) -> DailyPerformance:
        if not trades:
            return DailyPerformance(
                date=date,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=0.0,
                total_pnl=0.0,
                max_drawdown=0.0,
                sniper_trades=0,
                swarm_trades=0,
                sniper_win_rate=0.0,
                swarm_win_rate=0.0,
                confidence_distribution={"low": 0, "med": 0, "high": 0},
                gate_rejections={},
                circuit_breaker_triggers=0,
                daily_risk_used_pct=0.0,
                per_strategy={},
            )

        winning = [t for t in trades if t.get("pnl", 0) > 0]
        losing = [t for t in trades if t.get("pnl", 0) <= 0]
        win_rate = len(winning) / len(trades) if trades else 0.0
        total_pnl = sum(t.get("pnl", 0) for t in trades)

        # Max drawdown from running equity
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            equity += t.get("pnl", 0)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        # Profile stats
        sniper = [t for t in trades if t.get("profile") == "sniper"]
        swarm = [t for t in trades if t.get("profile") == "swarm"]
        sniper_wins = [t for t in sniper if t.get("pnl", 0) > 0]
        swarm_wins = [t for t in swarm if t.get("pnl", 0) > 0]

        # Confidence buckets
        conf_dist = {"low": 0, "med": 0, "high": 0}
        for t in trades:
            c = t.get("confidence", 0.5)
            if c < 0.6:
                conf_dist["low"] += 1
            elif c < 0.8:
                conf_dist["med"] += 1
            else:
                conf_dist["high"] += 1

        # Gate rejections
        gate_rej: dict[str, int] = {}
        for t in trades:
            for gate in t.get("gate_rejections", []):
                gate_rej[gate] = gate_rej.get(gate, 0) + 1

        # Per-strategy
        per_strat: dict[str, dict] = {}
        for t in trades:
            sid = t.get("strategy_id", "unknown")
            if sid not in per_strat:
                per_strat[sid] = {"trades": 0, "pnl": 0.0}
            per_strat[sid]["trades"] += 1
            per_strat[sid]["pnl"] += t.get("pnl", 0)

        total_risk = sum(t.get("risk_amount", 0) for t in trades)
        daily_risk_pct = total_risk / self._starting_balance if total_risk else 0.0

        return DailyPerformance(
            date=date,
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=win_rate,
            total_pnl=round(total_pnl, 2),
            max_drawdown=round(max_dd, 2),
            sniper_trades=len(sniper),
            swarm_trades=len(swarm),
            sniper_win_rate=len(sniper_wins) / len(sniper) if sniper else 0.0,
            swarm_win_rate=len(swarm_wins) / len(swarm) if swarm else 0.0,
            confidence_distribution=conf_dist,
            gate_rejections=gate_rej,
            circuit_breaker_triggers=sum(1 for t in trades if t.get("circuit_breaker")),
            daily_risk_used_pct=round(daily_risk_pct, 4),
            per_strategy={k: {"trades": v["trades"], "pnl": round(v["pnl"], 2)} for k, v in per_strat.items()},
        )


# ── CLI entry point ─────────────────────────────────────────────────────
#
# Cron-invoked. Writes a markdown report to
#   <reports_root>/daily/<YYYY-MM-DD>.md
#
# Defaults assume the project layout from $AYUMI_ROOT:
#   - trade log:      logs/trades.jsonl
#   - reports root:   data/forex/equity_reports/
# Both can be overridden via CLI flags for forward-test isolation.
#
# Resolution order (regression guard, card df5435e5):
#   1. $AYUMI_ROOT env var if set and non-empty (operator override).
#   2. File-relative parents[3] (analytics/daily_report.py → project root).
# Never produces a literal "$"-containing path — fixes 2026-09-20 env-var
# regression where unset $AYUMI_ROOT became Path("$AYUMI_ROOT") and writes
# landed at <cwd>/$AYUMI_ROOT/data/forex/equity_reports/.

DEFAULT_PROJECT_ROOT = Path(
    os.environ.get("AYUMI_ROOT") or Path(__file__).resolve().parents[3]
)


def _resolve_runtime_identity() -> tuple[int, int, str, str] | None:
    """Return (uid, gid, user, group) for the expected runtime owner.

    Mirrors ``remediation_actions._resolve_runtime_identity``: defaults to
    $USER:$USER, overridable via ``AYUMI_RUNTIME_USER`` /
    ``AYUMI_RUNTIME_GROUP``. Returns None if the user can't be resolved.
    """
    import grp as _grp
    import pwd as _pwd

    user_name = os.environ.get("AYUMI_RUNTIME_USER", "$USER")
    group_name = os.environ.get("AYUMI_RUNTIME_GROUP", user_name)
    try:
        uid = _pwd.getpwnam(user_name).pw_uid
        gid = _grp.getgrnam(group_name).gr_gid
    except KeyError:
        return None
    return uid, gid, user_name, group_name


def _self_heal_ownership(path: Path) -> None:
    """Chown a file/dir to the runtime user if it was written as root.

    Self-heal pattern for cron jobs running as root (see
    ``monitoring/remediation_actions.py:check_signal_stats_owner``). When a
    root-owned output is detected, fix ownership to the expected runtime
    user so downstream tools can read/write the file without ``PermissionError``.
    Silently no-ops if we can't resolve the runtime user or lack permission.
    """
    if not path.exists():
        return
    identity = _resolve_runtime_identity()
    if identity is None:
        return
    uid, gid, user_name, group_name = identity
    try:
        st = path.stat()
    except OSError:
        return
    if st.st_uid == uid and st.st_gid == gid:
        return
    try:
        os.chown(path, uid, gid)
        print(f"[self-heal] chown {path} -> {user_name}:{group_name}")
    except (PermissionError, OSError):
        # Not running as root, or path in a read-only mount — skip silently.
        pass


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    """Return (project_root, trade_log, daily_report_path)."""
    project_root = Path(args.project_root).resolve() if args.project_root else DEFAULT_PROJECT_ROOT
    # Regression guard (card df5435e5): fail-fast if a literal "$" sentinel
    # leaked into the path. Means $AYUMI_ROOT was set to a string containing
    # "$" or DEFAULT_PROJECT_ROOT computation fell through.
    if "$" in str(project_root):
        raise RuntimeError(
            f"project_root contains unexpanded env var sentinel: {project_root!r}. "
            "Set AYUMI_ROOT to an absolute path, or unset it for the "
            "file-relative fallback (parents[3])."
        )
    trade_log = Path(args.trade_log) if args.trade_log else project_root / "logs" / "trades.jsonl"
    reports_root = Path(args.reports_root) if args.reports_root else project_root / "data" / "forex" / "equity_reports"
    return project_root, trade_log, reports_root


def main(argv: list[str] | None = None) -> int:
    """Generate the daily performance markdown report. Returns exit code."""
    parser = argparse.ArgumentParser(
        description="Generate the daily forex performance report from the trade JSONL log."
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Target date in YYYY-MM-DD (default: today in America/Toronto).",
    )
    parser.add_argument(
        "--trade-log",
        default=None,
        help="Path to trades JSONL (default: <project>/logs/trades.jsonl).",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Project root used to resolve defaults (default: $AYUMI_ROOT env var, else file-relative parents[3]).",
    )
    parser.add_argument(
        "--reports-root",
        default=None,
        help="Reports root directory (default: <project>/data/forex/equity_reports).",
    )
    parser.add_argument(
        "--starting-balance",
        type=float,
        default=10_000.0,
        help="Starting balance used for risk-percentage metrics (default: 10000).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print the formatted report to stdout (for debugging).",
    )
    args = parser.parse_args(argv)

    _project_root, trade_log, reports_root = _resolve_paths(args)

    target_date = args.date or datetime.now(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d")

    analytics = DailyAnalytics(
        trade_log_path=str(trade_log),
        starting_balance=args.starting_balance,
    )
    report = analytics.generate_report(target_date)
    formatted = analytics.format_report(report)

    daily_dir = reports_root / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    _self_heal_ownership(daily_dir)  # fix parent dir if written as root
    out_path = daily_dir / f"{target_date}.md"
    out_path.write_text(formatted + "\n")
    _self_heal_ownership(out_path)

    if args.stdout:
        print(formatted)
    print(f"[daily_report] wrote {out_path} (trades={report.total_trades}, pnl=${report.total_pnl:+.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
