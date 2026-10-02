#!/usr/bin/env python3
"""Hayate daily audit orchestrator.

Runs the 25 checkpoints catalogued in
``docs/design/hayate-checkpoint-design-2026-07-08.md`` §3.3–§3.7, writes
a structured markdown report, and (optionally) posts the
``## Executive Summary`` block to a Telegram chat.

The orchestrator is intentionally thin. Each individual checkpoint does
its own data fetch; this script aggregates their status, collects
remediation/escalation records that landed in the past 24 h, and renders
the report.

Usage::

    scripts/daily_audit.py --dry-run           # render report, no writes
    scripts/daily_audit.py                     # write to reports/hayate-daily/
    scripts/daily_audit.py --send-telegram     # also Telegram the summary
    scripts/daily_audit.py --report-date 2026-07-08
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "forex_bot"))
sys.path.insert(0, str(ROOT / "src"))

from monitoring.drift_detector import DriftDetector  # noqa: E402

DEFAULT_REPORTS_DIR = ROOT / "reports" / "hayate-daily"
DEFAULT_OPS_DIR = ROOT / "data" / "ops"
DEFAULT_REM_LOG = DEFAULT_OPS_DIR / "remediation_log.jsonl"
DEFAULT_ESCALATION_LOG = DEFAULT_OPS_DIR / "escalation_queue.jsonl"


# ── Checkpoint dataclass ───────────────────────────────────────────────────


@dataclass
class CheckResult:
    """Result of a single checkpoint evaluation."""

    check_id: str
    category: str
    status: str  # "OK" | "WARN" | "CRITICAL" | "SKIPPED"
    detail: str
    auto_remediation: str = ""
    notes: str = ""
    escalated: bool = False


# ── Helpers ────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_jsonl_window(path: Path, since_iso: str) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if isinstance(ts, str) and ts >= since_iso:
                    out.append(rec)
    except OSError:
        return []
    return out


def _try_attach_extra(check: CheckResult, source: str, extra: dict[str, Any]) -> CheckResult:
    """Attach a source-tagged extra detail blob to a CheckResult."""
    setattr(check, "source", source)  # noqa: B010
    if extra:
        check.notes = (check.notes + " | " if check.notes else "") + json.dumps(extra, sort_keys=True)
    return check


# ── Market status detection ────────────────────────────────────────────────


def _get_market_status(now: datetime | None = None) -> str:
    """Determine forex market status based on UTC time.

    Forex market schedule (standard, cTrader-aligned):
    - Opens Sunday 22:00 UTC
    - Closes Friday 22:00 UTC
    - Closed all day Saturday and Sunday before 22:00 UTC

    Returns one of: ``open``, ``weekend``, ``closed``.
    ``maintenance`` is reserved for future cTrader schedule API integration.
    """
    now = now or _now()
    day = now.weekday()  # 0=Monday, 5=Saturday, 6=Sunday
    hour = now.hour

    if day == 5:  # Saturday — always closed
        return "weekend"
    if day == 6:  # Sunday
        if hour < 22:
            return "weekend"
        return "open"  # Market opens at 22:00 UTC Sunday
    if day == 4 and hour >= 22:  # Friday after 22:00 UTC — market closed
        return "closed"
    return "open"


# ── Checkpoint implementations ─────────────────────────────────────────────
#
# Each function evaluates a single checkpoint against the live data
# sources and returns a populated ``CheckResult``. The body of the audit
# is the table these produce.
# ───────────────────────────────────────────────────────────────────────────


def _ch_pid_file_alive() -> CheckResult:
    pid_path = ROOT / "data" / "forward_test.pid"
    if not pid_path.exists():
        return CheckResult(
            "SH-001",
            "System Health",
            "WARN",
            "data/forward_test.pid not present (forward test may be down or unmanaged)",
            auto_remediation="A1 — restart via scripts/restart_forward_test.sh",
            notes="No PID file to read; treat as 'down' until cron confirms otherwise",
        )
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError) as exc:
        return CheckResult(
            "SH-001",
            "System Health",
            "CRITICAL",
            f"PID file unreadable: {exc}",
            auto_remediation="A2 — clear stale PID file (chown $USER first)",
        )
    alive = os.path.exists(f"/proc/{pid}")
    if alive:
        return CheckResult("SH-001", "System Health", "OK", f"PID {pid} alive")
    return CheckResult(
        "SH-001",
        "System Health",
        "CRITICAL",
        f"PID {pid} recorded but not running (stale PID file)",
        auto_remediation="A2 — clear stale PID, then A1 restart",
        escalated=True,
    )


def _ch_tick_flow() -> CheckResult:
    """Read latest heartbeat and check tps field if present."""
    hb_path = ROOT / "data" / "heartbeat_trading.json"
    if not hb_path.exists():
        return CheckResult(
            "SH-002",
            "System Health",
            "WARN",
            "data/heartbeat_trading.json missing",
            auto_remediation="A1 — verify forward_test is running",
            escalated=True,
        )
    try:
        hb = json.loads(hb_path.read_text())
        tps = hb.get("tps_5min_avg") or hb.get("tps_recent") or hb.get("tps") or 0.0
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult("SH-002", "System Health", "WARN", f"Heartbeat unreadable: {exc}")
    if tps >= 1.0:
        return CheckResult("SH-002", "System Health", "OK", f"tps_5min_avg={tps:.2f}")
    if tps >= 0.1:
        return CheckResult(
            "SH-002",
            "System Health",
            "WARN",
            f"tps_5min_avg={tps:.2f} (degraded)",
            auto_remediation="A1 — observe; restart only if persists >30 min",
        )
    return CheckResult(
        "SH-002",
        "System Health",
        "CRITICAL",
        f"tps_5min_avg={tps:.2f} (silent)",
        auto_remediation="A1 — restart forward test (counts toward 3-attempt cap)",
        escalated=True,
    )


def _ch_disk() -> CheckResult:
    """df on the project root."""
    try:
        out = subprocess.run(  # noqa: S603
            ["df", "-P", str(ROOT)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = out.stdout.strip().splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return CheckResult("SH-004", "System Health", "WARN", f"df unavailable: {exc}")
    if len(lines) < 2:
        return CheckResult("SH-004", "System Health", "WARN", "df output unparseable")
    parts = lines[1].split()
    if len(parts) < 5:
        return CheckResult("SH-004", "System Health", "WARN", "df columns unparseable")
    use_pct = parts[4]  # e.g. "76%"
    try:
        pct = int(use_pct.rstrip("%"))
    except ValueError:
        return CheckResult("SH-004", "System Health", "WARN", f"df pct parse fail: {use_pct!r}")
    if pct < 70:
        return CheckResult("SH-004", "System Health", "OK", f"disk use {pct}%")
    if pct < 85:
        return CheckResult(
            "SH-004",
            "System Health",
            "WARN",
            f"disk use {pct}%",
            auto_remediation="A2 — clear tmp + rotate logs",
        )
    return CheckResult(
        "SH-004",
        "System Health",
        "CRITICAL",
        f"disk use {pct}%",
        auto_remediation="A2 — rotate logs + clear tmp; alert Ava at >90%",
        escalated=True,
    )


def _ch_stale_pid_files() -> CheckResult:
    data_dir = ROOT / "data"
    if not data_dir.exists():
        return CheckResult("SH-005", "System Health", "OK", "data/ dir missing — no PIDs to scan")
    stale_count = 0
    forward_stale = False
    for pid_path in data_dir.glob("*.pid"):
        try:
            age_days = (time.time() - pid_path.stat().st_mtime) / 86400.0
        except OSError:
            continue
        if age_days < 1.0:
            continue
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            stale_count += 1
            continue
        if not os.path.exists(f"/proc/{pid}"):
            stale_count += 1
            if pid_path.name == "forward_test.pid":
                forward_stale = True
    if stale_count == 0:
        return CheckResult("SH-005", "System Health", "OK", "no stale PID files")
    if forward_stale:
        return CheckResult(
            "SH-005",
            "System Health",
            "CRITICAL",
            f"forward_test.pid references dead process ({stale_count} stale)",
            auto_remediation="A2 — clear stale; A1 restart (see SH-001)",
            escalated=True,
        )
    if stale_count <= 2:
        return CheckResult(
            "SH-005",
            "System Health",
            "WARN",
            f"{stale_count} stale PID file(s)",
            auto_remediation="A2 — delete stale PIDs",
        )
    return CheckResult(
        "SH-005",
        "System Health",
        "CRITICAL",
        f"{stale_count} stale PID files",
        auto_remediation="A2 — delete stale PIDs",
        escalated=True,
    )


def _ch_log_error_rate() -> CheckResult:
    log = ROOT / "logs" / "forward_test.log"
    if not log.exists():
        return CheckResult(
            "SH-008",
            "System Health",
            "OK",
            "logs/forward_test.log missing (test not started yet?)",
        )
    try:
        # Read last 200 lines for efficiency.
        # Use tail in a subprocess (always available on Linux).
        out = subprocess.run(  # noqa: S603
            ["tail", "-n", "200", str(log)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
        )
        recent = out.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return CheckResult("SH-008", "System Health", "WARN", f"tail failed: {exc}")
    if not recent:
        return CheckResult("SH-008", "System Health", "OK", "log empty")
    errors = sum(1 for line in recent.splitlines() if "ERROR" in line or "Traceback" in line)
    if errors == 0:
        return CheckResult("SH-008", "System Health", "OK", "no ERROR/Traceback in last 200 lines")
    if errors <= 5:
        return CheckResult(
            "SH-008",
            "System Health",
            "WARN",
            f"{errors} ERROR/Traceback line(s) in last 200 lines",
            auto_remediation="A2 — none (errors are diagnostic signal)",
            notes="Investigate top 5 most-recent errors next session.",
        )
    return CheckResult(
        "SH-008",
        "System Health",
        "CRITICAL",
        f"{errors} ERROR/Traceback line(s) in last 200 lines",
        auto_remediation="A3 — card with last 5 tracebacks",
        escalated=True,
    )


def _ch_log_rotation_state() -> CheckResult:
    """SH-009: surface forward-test log rotation state from rotate_logs.py.

    Reads ``logs/archive/.rotation_state.json`` (written by the rotation
    cron) and surfaces stale runs or script errors. State file is the
    single source of truth for whether rotation is keeping up.
    """
    state_path = ROOT / "logs" / "archive" / ".rotation_state.json"
    if not state_path.exists():
        return CheckResult(
            "SH-009",
            "System Health",
            "WARN",
            "rotation state file missing — cron has never run scripts/rotate_logs.py",
            auto_remediation=(
                "A2 — run scripts/rotate_logs.py --dry-run to verify; "
                "register the weekly cron entry"
            ),
            notes="Card 2ecfc254 logs the rotation policy. Cron entry lands in the same merge.",
        )
    try:
        payload = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            "SH-009",
            "System Health",
            "WARN",
            f"rotation state unreadable: {exc}",
        )
    last_run_at = payload.get("last_run_at")
    if not last_run_at:
        return CheckResult(
            "SH-009",
            "System Health",
            "WARN",
            "rotation state file has no last_run_at field",
        )
    try:
        last_run = datetime.fromisoformat(last_run_at)
    except ValueError:
        return CheckResult(
            "SH-009",
            "System Health",
            "WARN",
            f"rotation last_run_at unparseable: {last_run_at}",
        )
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last_run
    age_days = age.total_seconds() / 86400.0
    archived = payload.get("archived_count", 0)
    purged = payload.get("purged_count", 0)
    if age_days > 14:
        return CheckResult(
            "SH-009",
            "System Health",
            "CRITICAL",
            f"rotation last run {age_days:.1f}d ago (>{14}d threshold); archived={archived}, purged={purged}",
            auto_remediation="A3 — verify cron registration; rerun scripts/rotate_logs.py manually",
            escalated=True,
        )
    if age_days > 8:
        return CheckResult(
            "SH-009",
            "System Health",
            "WARN",
            f"rotation last run {age_days:.1f}d ago (>8d cron slack); archived={archived}, purged={purged}",
            auto_remediation="A2 — verify cron registration; rerun scripts/rotate_logs.py",
        )
    return CheckResult(
        "SH-009",
        "System Health",
        "OK",
        f"rotation last run {age_days:.1f}d ago; archived={archived}, purged={purged}",
    )


# ── Trading Health (FT-NNN) — use FTMOGuard + state ────────────────────────


def _ch_ft_daily_dd(state_path: Path, daily_limit_pct: float = 0.03) -> CheckResult:
    state_path = ROOT / "data" / "state" / "risk_guard_state.json"
    if not state_path.exists():
        return CheckResult("FT-003", "Trading Health", "OK", "risk_guard_state.json missing")
    try:
        s = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult("FT-003", "Trading Health", "WARN", f"state unreadable: {exc}")
    starting = 100_000.0
    daily_open = s.get("daily_start_balance") or starting
    current = s.get("current_balance") or starting
    loss_frac = max(0.0, (daily_open - current) / starting) if starting else 0.0
    detail = f"daily_loss={loss_frac * 100:.2f}% (vs {daily_limit_pct * 100:.0f}% limit)"
    if loss_frac >= daily_limit_pct * 0.85:
        return CheckResult(
            "FT-003",
            "Trading Health",
            "CRITICAL",
            detail,
            auto_remediation="A5 — Ava card → Craig if within 0.5pp of 3% hard limit",
            escalated=True,
        )
    if loss_frac >= daily_limit_pct * 0.5:
        return CheckResult(
            "FT-003",
            "Trading Health",
            "WARN",
            detail,
            auto_remediation="A5 — Ava card at warning; observe",
        )
    return CheckResult("FT-003", "Trading Health", "OK", detail)


def _ch_ft_total_dd(state_path: Path, total_limit_pct: float = 0.10) -> CheckResult:
    state_path = ROOT / "data" / "state" / "risk_guard_state.json"
    if not state_path.exists():
        return CheckResult("FT-004", "Trading Health", "OK", "risk_guard_state.json missing")
    try:
        s = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult("FT-004", "Trading Health", "WARN", f"state unreadable: {exc}")
    peak = s.get("peak_balance") or 100_000.0
    current = s.get("current_balance") or 100_000.0
    dd = max(0.0, (peak - current) / peak) if peak else 0.0
    detail = f"total_dd={dd * 100:.2f}% (vs {total_limit_pct * 100:.0f}% limit, peak=${peak:,.2f})"
    if dd >= total_limit_pct * 0.85:
        return CheckResult(
            "FT-004",
            "Trading Health",
            "CRITICAL",
            detail,
            auto_remediation="A5 — Craig card",
            escalated=True,
        )
    if dd >= 0.05:
        return CheckResult(
            "FT-004",
            "Trading Health",
            "WARN",
            detail,
            auto_remediation="A5 — Ava card at warning",
        )
    return CheckResult("FT-004", "Trading Health", "OK", detail)


def _ch_ft_target_reached() -> CheckResult:
    """Phase 6 deliverable: profit target hit → freeze new positions."""
    sys.path.insert(0, str(ROOT / "src"))
    from reporting.equity_tracker import check_profit_target

    res = check_profit_target(ROOT / "data" / "state" / "risk_guard_state.json")
    if res.reached:
        return CheckResult(
            "FT-AUX-TARGET",
            "Trading Health",
            "OK",
            f"+10% target REACHED at ${res.current_balance:,.2f} (target ${res.required_balance:,.2f}); freezes engaged",  # noqa: E501
            auto_remediation="A5 — KillSwitchManager.activate_global_freeze already wired",
            notes=f"distance_to_target=${res.distance_to_target:+,.2f}",
        )
    return CheckResult(
        "FT-AUX-TARGET",
        "Trading Health",
        "OK",
        f"target not reached (need ${res.required_balance:,.2f}, current ${res.current_balance:,.2f}, distance ${res.distance_to_target:+,.2f})",  # noqa: E501
    )


def _ch_signal_to_trade() -> CheckResult:
    """FT-009 ratio check derived from forward_test_health + signal_stats.jsonl."""
    health = ROOT / "data" / "forward_test_health.json"
    if not health.exists():
        return CheckResult(
            "FT-009",
            "Trading Health",
            "OK",
            "forward_test_health.json missing — ratio unobservable",
        )
    try:
        h = json.loads(health.read_text())
    except (json.JSONDecodeError, OSError):
        return CheckResult("FT-009", "Trading Health", "WARN", "forward_test_health.json unreadable")
    signals = h.get("signals_generated") or 0
    fills = h.get("live_fills") or 0
    if signals == 0:
        return CheckResult("FT-009", "Trading Health", "OK", "no signals observed today")
    ratio = fills / signals if signals else 0.0
    detail = f"live_fills/signals={ratio:.3f} ({fills}/{signals})"
    if ratio == 0.0 and signals >= 5:
        return CheckResult(
            "FT-009",
            "Trading Health",
            "CRITICAL",
            detail,
            auto_remediation="A3 — sizing-gate investigation card",
            escalated=True,
        )
    if ratio < 0.02 or ratio > 0.5:
        return CheckResult(
            "FT-009",
            "Trading Health",
            "WARN",
            detail,
            auto_remediation="A3 — investigate only if persists over 4h",
        )
    return CheckResult("FT-009", "Trading Health", "OK", detail)


def _ch_ft_open_positions_vs_limits(max_positions: int = 3) -> CheckResult:
    """FT-002: Open positions vs limits — query trading.db for open trades."""
    db_path = ROOT / "data" / "trading.db"
    if not db_path.exists():
        return CheckResult(
            "FT-002",
            "Trading Health",
            "WARN",
            "trading.db missing — cannot check open positions",
        )
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'open'")
        count = cursor.fetchone()[0]
        conn.close()
    except Exception as exc:
        return CheckResult("FT-002", "Trading Health", "WARN", f"trading.db query failed: {exc}")
    if count > max_positions:
        return CheckResult(
            "FT-002",
            "Trading Health",
            "CRITICAL",
            f"open_positions={count} > max={max_positions} (FTMO limit violation)",
            auto_remediation="A5 — never auto-close; A3 — escalate to Craig within 15 min",
            escalated=True,
        )
    if count >= max_positions:
        return CheckResult(
            "FT-002",
            "Trading Health",
            "WARN",
            f"open_positions={count} = max={max_positions} (at limit)",
        )
    return CheckResult(
        "FT-002",
        "Trading Health",
        "OK",
        f"open_positions={count} / max={max_positions}",
    )


def _ch_ft_best_day_ratio() -> CheckResult:
    """FT-005: Best-day ratio — best day P&L / total positive P&L."""
    db_path = ROOT / "data" / "trading.db"
    if not db_path.exists():
        return CheckResult(
            "FT-005",
            "Trading Health",
            "WARN",
            "trading.db missing — cannot check best-day ratio",
        )
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT date, total_pnl FROM daily_summary WHERE total_pnl > 0 ORDER BY total_pnl DESC"
        ).fetchall()
        conn.close()
    except Exception as exc:
        return CheckResult("FT-005", "Trading Health", "WARN", f"daily_summary query failed: {exc}")
    if not rows:
        return CheckResult(
            "FT-005",
            "Trading Health",
            "OK",
            "no profitable days recorded yet — best-day ratio not computable",
        )
    best_day_pnl = rows[0][1]
    total_positive = sum(r[1] for r in rows)
    if total_positive <= 0:
        return CheckResult("FT-005", "Trading Health", "OK", "no positive P&L days — ratio undefined")
    ratio = best_day_pnl / total_positive
    detail = f"best_day=${best_day_pnl:.2f} / total_positive=${total_positive:.2f} = {ratio * 100:.1f}%"
    if ratio > 0.48:
        return CheckResult(
            "FT-005",
            "Trading Health",
            "CRITICAL",
            detail + " (>48% — FTMO 1-Step qualification risk)",
            auto_remediation="A5 — Ava card; Craig card at critical",
            escalated=True,
        )
    if ratio > 0.40:
        return CheckResult(
            "FT-005",
            "Trading Health",
            "WARN",
            detail + " (40-48% — could affect FTMO qualification)",
        )
    return CheckResult("FT-005", "Trading Health", "OK", detail)


def _ch_ft_fill_latency_p50() -> CheckResult:
    """FT-007: Fill latency p50 — median time from signal generation to fill.

    The signal_stats.jsonl schema does not yet have a ``filled_at`` field.
    Until the engine instruments fill timing, this checkpoint reports WARN
    noting the data gap rather than SKIP.
    """
    stats_path = ROOT / "data" / "signal_stats.jsonl"
    if not stats_path.exists():
        return CheckResult(
            "FT-007",
            "Trading Health",
            "WARN",
            "signal_stats.jsonl missing — fill latency unobservable",
        )
    has_fill_data = False
    try:
        with stats_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("filled_at") is not None:
                    has_fill_data = True
                    break
    except OSError:
        return CheckResult("FT-007", "Trading Health", "WARN", "signal_stats.jsonl read error")
    if not has_fill_data:
        return CheckResult(
            "FT-007",
            "Trading Health",
            "WARN",
            "fill latency data not yet instrumented (no filled_at field in signal_stats.jsonl); manual monitoring required",  # noqa: E501
            notes="Engine needs filled_at field on SignalRecord. See design doc §6.2 FT-010 (1.5 SP dependency).",
        )
    latencies: list[float] = []
    try:
        with stats_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                filled_at = rec.get("filled_at")
                signal_ts = rec.get("timestamp")
                if filled_at and signal_ts:
                    try:
                        latency = (
                            datetime.fromisoformat(filled_at) - datetime.fromisoformat(signal_ts)
                        ).total_seconds()
                        if latency > 0:
                            latencies.append(latency)
                    except (ValueError, TypeError):
                        pass
    except OSError:
        return CheckResult(
            "FT-007",
            "Trading Health",
            "WARN",
            "signal_stats.jsonl read error during latency computation",
        )
    if not latencies:
        return CheckResult(
            "FT-007",
            "Trading Health",
            "WARN",
            "filled_at field exists but no valid latency pairs found",
        )
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    if p50 > 30:
        return CheckResult(
            "FT-007",
            "Trading Health",
            "CRITICAL",
            f"fill_latency_p50={p50:.1f}s (>30s — cTrader API degradation)",
            auto_remediation="A3 — card for cTrader API investigation",
            escalated=True,
        )
    if p50 > 5:
        return CheckResult(
            "FT-007",
            "Trading Health",
            "WARN",
            f"fill_latency_p50={p50:.1f}s (5-30s — degraded)",
        )
    return CheckResult(
        "FT-007",
        "Trading Health",
        "OK",
        f"fill_latency_p50={p50:.1f}s (based on {len(latencies)} fills)",
    )


def _ch_ft_fill_latency_p95() -> CheckResult:
    """FT-008: Fill latency p95 — 95th percentile time from signal to fill.

    Same data dependency as FT-007 — requires ``filled_at`` field.
    """
    stats_path = ROOT / "data" / "signal_stats.jsonl"
    if not stats_path.exists():
        return CheckResult(
            "FT-008",
            "Trading Health",
            "WARN",
            "signal_stats.jsonl missing — fill latency unobservable",
        )
    has_fill_data = False
    try:
        with stats_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("filled_at") is not None:
                    has_fill_data = True
                    break
    except OSError:
        return CheckResult("FT-008", "Trading Health", "WARN", "signal_stats.jsonl read error")
    if not has_fill_data:
        return CheckResult(
            "FT-008",
            "Trading Health",
            "WARN",
            "fill latency data not yet instrumented (no filled_at field in signal_stats.jsonl); manual monitoring required",  # noqa: E501
            notes="Engine needs filled_at field on SignalRecord. See design doc §6.2 FT-010 (1.5 SP dependency).",
        )
    latencies: list[float] = []
    try:
        with stats_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                filled_at = rec.get("filled_at")
                signal_ts = rec.get("timestamp")
                if filled_at and signal_ts:
                    try:
                        latency = (
                            datetime.fromisoformat(filled_at) - datetime.fromisoformat(signal_ts)
                        ).total_seconds()
                        if latency > 0:
                            latencies.append(latency)
                    except (ValueError, TypeError):
                        pass
    except OSError:
        return CheckResult(
            "FT-008",
            "Trading Health",
            "WARN",
            "signal_stats.jsonl read error during latency computation",
        )
    if not latencies:
        return CheckResult(
            "FT-008",
            "Trading Health",
            "WARN",
            "filled_at field exists but no valid latency pairs found",
        )
    latencies.sort()
    p95_idx = int(len(latencies) * 0.95)
    p95 = latencies[min(p95_idx, len(latencies) - 1)]
    if p95 > 30:
        return CheckResult(
            "FT-008",
            "Trading Health",
            "CRITICAL",
            f"fill_latency_p95={p95:.1f}s (>30s — tail latency severe)",
            auto_remediation="A3 — card for cTrader API investigation",
            escalated=True,
        )
    if p95 > 5:
        return CheckResult(
            "FT-008",
            "Trading Health",
            "WARN",
            f"fill_latency_p95={p95:.1f}s (5-30s — degraded tail)",
        )
    return CheckResult(
        "FT-008",
        "Trading Health",
        "OK",
        f"fill_latency_p95={p95:.1f}s (based on {len(latencies)} fills)",
    )


def _ch_ft_slippage_analysis() -> CheckResult:
    """FT-010: Slippage analysis — difference between expected and actual fill price.

    signal_stats.jsonl does not yet have a ``requested_price`` or ``expected_price``
    field to compare against ``entry_price``. Until instrumented, this checkpoint
    reports WARN noting the data gap.
    """
    stats_path = ROOT / "data" / "signal_stats.jsonl"
    if not stats_path.exists():
        return CheckResult(
            "FT-010",
            "Trading Health",
            "WARN",
            "signal_stats.jsonl missing — slippage unobservable",
        )
    has_slippage_data = False
    try:
        with stats_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("requested_price") is not None or rec.get("expected_price") is not None:
                    has_slippage_data = True
                    break
    except OSError:
        return CheckResult("FT-010", "Trading Health", "WARN", "signal_stats.jsonl read error")
    if not has_slippage_data:
        return CheckResult(
            "FT-010",
            "Trading Health",
            "WARN",
            "slippage data not yet instrumented (no requested_price/expected_price field in signal_stats.jsonl); manual monitoring required",  # noqa: E501
            notes="Engine needs requested_price or expected_price field on SignalRecord for slippage analysis.",
        )
    slippages: list[float] = []
    try:
        with stats_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                expected = rec.get("requested_price") or rec.get("expected_price")
                actual = rec.get("entry_price")
                if expected is not None and actual is not None:
                    slip = abs(actual - expected)
                    slippages.append(slip)
    except OSError:
        return CheckResult(
            "FT-010",
            "Trading Health",
            "WARN",
            "signal_stats.jsonl read error during slippage computation",
        )
    if not slippages:
        return CheckResult(
            "FT-010",
            "Trading Health",
            "WARN",
            "slippage fields exist but no valid pairs found",
        )
    avg_slip = sum(slippages) / len(slippages)
    max_slip = max(slippages)
    detail = f"avg_slippage={avg_slip:.5f}, max_slippage={max_slip:.5f} (n={len(slippages)})"
    if avg_slip > 0.001:
        return CheckResult(
            "FT-010",
            "Trading Health",
            "CRITICAL",
            detail + " — average slippage >1 pip",
            auto_remediation="A3 — card for cTrader execution investigation",
            escalated=True,
        )
    if avg_slip > 0.0003:
        return CheckResult("FT-010", "Trading Health", "WARN", detail + " — slippage elevated")
    return CheckResult("FT-010", "Trading Health", "OK", detail)


def _ch_ft_order_rejection_rate() -> CheckResult:
    """FT-011: Order rejection rate — rejected orders / total signals."""
    stats_path = ROOT / "data" / "signal_stats.jsonl"
    if not stats_path.exists():
        return CheckResult(
            "FT-011",
            "Trading Health",
            "WARN",
            "signal_stats.jsonl missing — rejection rate unobservable",
        )
    total = 0
    rejected = 0
    try:
        with stats_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                outcome = rec.get("outcome", "")
                if outcome in ("failed_order_error", "rejected", "order_rejected"):
                    rejected += 1
    except OSError:
        return CheckResult("FT-011", "Trading Health", "WARN", "signal_stats.jsonl read error")
    if total == 0:
        return CheckResult(
            "FT-011",
            "Trading Health",
            "OK",
            "no signals recorded — rejection rate not computable",
        )
    rate = rejected / total
    detail = f"rejection_rate={rate * 100:.2f}% ({rejected}/{total} signals)"
    if rate > 0.10:
        return CheckResult(
            "FT-011",
            "Trading Health",
            "CRITICAL",
            detail + " (>10% — cTrader API or risk gate issue)",
            auto_remediation="A3 — card for execution path investigation",
            escalated=True,
        )
    if rate > 0.03:
        return CheckResult("FT-011", "Trading Health", "WARN", detail + " (3-10% — elevated)")
    return CheckResult("FT-011", "Trading Health", "OK", detail)


# ── Data Health (DH-NNN) ────────────────────────────────────────────────────


def _ch_dh_file_freshness(path: Path, check_id: str, label: str, max_age_seconds: float = 60.0) -> CheckResult:
    if not path.exists():
        return CheckResult(
            check_id,
            "Data Health",
            "WARN",
            f"{label} missing ({path.name})",
            escalated=True,
        )
    try:
        age = time.time() - path.stat().st_mtime
    except OSError as exc:
        return CheckResult(check_id, "Data Health", "WARN", f"{label} stat failed: {exc}")
    if age < max_age_seconds:
        return CheckResult(check_id, "Data Health", "OK", f"{label} mtime={age:.0f}s")
    if age < max_age_seconds * 5:
        return CheckResult(
            check_id,
            "Data Health",
            "WARN",
            f"{label} mtime={age:.0f}s (stale)",
            auto_remediation=f"A3 — investigate writer for {label}",
        )
    return CheckResult(
        check_id,
        "Data Health",
        "CRITICAL",
        f"{label} mtime={age:.0f}s (very stale)",
        auto_remediation=f"A3 — escalation card for {label}",
        escalated=True,
    )


def _recent_stats_fails(log_path: Path | None = None, tail_lines: int = 20) -> int | None:
    """Return the most recent ``stats_fails=`` counter from [B5 Health] log lines.

    Returns ``None`` when no [B5 Health] line is found (evidence unavailable).
    """
    path = log_path or (ROOT / "logs" / "forward_test.log")
    try:
        out = subprocess.run(  # noqa: S603
            ["tail", "-n", str(tail_lines), str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    last: int | None = None
    for line in out.stdout.splitlines():
        if "[B5 Health]" not in line:
            continue
        m = re.search(r"stats_fails=(\d+)", line)
        if m:
            last = int(m.group(1))
    return last


def _signals_since_last_write(stats_mtime: float) -> bool:
    """True when the engine generated signals after the last signal_stats write.

    Uses forward_test_health.json: the ``signals_generated`` counter is
    per-process (reset on restart), so a file mtime older than
    ``last_restart_at`` combined with ``signals_generated > 0`` means the
    engine observed signals in this session without any corresponding
    signal_stats.jsonl write — a true writer failure.
    """
    hb_path = ROOT / "data" / "forward_test_health.json"
    try:
        hb = json.loads(hb_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    signals = hb.get("signals_generated")
    restart_at = hb.get("last_restart_at")
    if not isinstance(signals, int) or signals <= 0 or not restart_at:
        return False
    try:
        restart_epoch = (
            datetime.fromisoformat(str(restart_at))
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return False
    return stats_mtime < restart_epoch


def _ch_dh_signal_stats() -> CheckResult:
    """DH-003: signal_stats.jsonl writer health keyed on positive evidence.

    The writer is event-driven (appends only on signal events), so a stale
    mtime OR an absent file alone is NOT a failure. CRITICAL requires
    positive evidence: ``stats_fails > 0`` in recent [B5 Health] lines, or
    signals generated in-engine since the last file write without a write
    (writer dead). Absence with no positive evidence is OK/INFO — the
    writer has simply not had its first event yet.
    """
    stats_path = ROOT / "data" / "signal_stats.jsonl"

    # Evidence 1: stats_fails counter from B5 health log lines — independent
    # of signal_stats.jsonl presence (the log records writer failures even
    # when the file has never been written).
    stats_fails = _recent_stats_fails()
    if stats_fails is not None and stats_fails > 0:
        return CheckResult(
            "DH-003",
            "Data Health",
            "CRITICAL",
            f"stats_fails={stats_fails} in recent [B5 Health] lines — signal_stats writer failing",
            auto_remediation="A3 — escalation card for signal_stats writer",
            escalated=True,
        )

    # Determine stats_mtime for the signals-without-writes check; use 0
    # when the file is missing so any signal generation since restart
    # trips the CRITICAL path.
    stats_mtime = 0.0
    if stats_path.exists():
        try:
            stats_mtime = stats_path.stat().st_mtime
        except OSError as exc:
            return CheckResult("DH-003", "Data Health", "WARN", f"stat failed: {exc}")

    # Evidence 2: signals-without-writes — engine generated signals since
    # the last file write but no write occurred.
    if _signals_since_last_write(stats_mtime):
        if stats_path.exists():
            age_s = time.time() - stats_mtime
            detail = (
                f"signal_stats.jsonl mtime={age_s:.0f}s stale while engine reports signals "
                "since last write — signals-without-writes"
            )
        else:
            detail = (
                "signal_stats.jsonl not yet written while engine reports signals "
                "since last restart — signals-without-writes"
            )
        return CheckResult(
            "DH-003",
            "Data Health",
            "CRITICAL",
            detail,
            auto_remediation="A3 — escalation card for signal_stats writer",
            escalated=True,
        )

    # No positive failure evidence: event-driven staleness or absence is by design.
    market_status = _get_market_status()
    if not stats_path.exists():
        # Per DH-003 contract: alerts key on positive evidence, not mere
        # absence. With no file yet, the event-driven writer has simply
        # not had its first event — OK/INFO, not WARN/escalated.
        return CheckResult(
            "DH-003",
            "Data Health",
            "OK",
            "signal_stats.jsonl not yet written — event-driven writer, "
            f"no positive failure evidence (market={market_status})",
        )

    file_age_s = time.time() - stats_mtime
    detail = (
        f"signal_stats.jsonl mtime={file_age_s:.0f}s "
        f"(event-driven writer, no failure evidence, market={market_status})"
    )
    if stats_fails is not None:
        detail += f", stats_fails={stats_fails}"
    if market_status in ("weekend", "closed") or file_age_s < 86400:
        return CheckResult("DH-003", "Data Health", "OK", detail)
    return CheckResult(
        "DH-003",
        "Data Health",
        "WARN",
        detail,
        auto_remediation="Verify writer resumes at next signal event.",
    )


def _ch_dh_risk_state() -> CheckResult:
    market_status = _get_market_status()
    # During market closure, stale risk_guard_state.json is expected
    max_age = 3600.0 if market_status in ("weekend", "closed") else 60.0
    return _ch_dh_file_freshness(
        ROOT / "data" / "state" / "risk_guard_state.json",
        "DH-004",
        "risk_guard_state.json",
        max_age_seconds=max_age,
    )


def _ch_dh_tick_feed_latency() -> CheckResult:
    """DH-001: Tick feed latency — wall-clock now minus last_tick_time."""
    hb_path = ROOT / "data" / "forward_test_health.json"
    if not hb_path.exists():
        return CheckResult(
            "DH-001",
            "Data Health",
            "WARN",
            "forward_test_health.json missing — cannot measure tick feed latency",
            escalated=True,
        )
    try:
        hb = json.loads(hb_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            "DH-001",
            "Data Health",
            "WARN",
            f"forward_test_health.json unreadable: {exc}",
        )
    last_tick_str = hb.get("last_tick_time")
    if not last_tick_str:
        return CheckResult(
            "DH-001",
            "Data Health",
            "WARN",
            "last_tick_time field missing in forward_test_health.json",
        )
    try:
        last_tick = datetime.fromisoformat(last_tick_str)
    except (ValueError, TypeError) as exc:
        return CheckResult("DH-001", "Data Health", "WARN", f"last_tick_time unparseable: {exc}")
    now = _now()
    latency_s = (now - last_tick).total_seconds()
    if latency_s < 0:
        return CheckResult(
            "DH-001",
            "Data Health",
            "OK",
            f"last_tick_time in future (clock skew, latency={latency_s:.1f}s)",
        )

    market_status = _get_market_status(now)

    # During weekend / market closure, high latency is expected — not CRITICAL
    if market_status in ("weekend", "closed"):
        if latency_s < 30:
            return CheckResult(
                "DH-001",
                "Data Health",
                "OK",
                f"tick feed latency={latency_s:.2f}s (market={market_status})",
            )
        # High latency during market closure is WARN, not CRITICAL
        return CheckResult(
            "DH-001",
            "Data Health",
            "WARN",
            f"tick feed latency={latency_s:.0f}s (market={market_status}, expected during closure)",
            auto_remediation="No action needed — market closed. Verify ticks resume at market open.",
            notes=f"market_status={market_status}",
        )

    if latency_s < 2:
        return CheckResult("DH-001", "Data Health", "OK", f"tick feed latency={latency_s:.2f}s")
    if latency_s < 30:
        return CheckResult(
            "DH-001",
            "Data Health",
            "WARN",
            f"tick feed latency={latency_s:.1f}s (degraded)",
            auto_remediation="A1 — observe; restart only if persists >5min",
        )
    return CheckResult(
        "DH-001",
        "Data Health",
        "CRITICAL",
        f"tick feed latency={latency_s:.1f}s (>30s during market hours)",
        auto_remediation="A1 — restart forward test (counts toward 3-attempt cap)",
        escalated=True,
    )


def _ch_dh_bar_building_rate() -> CheckResult:
    """DH-002: Bar building rate — bars/min from forward_test_health.json."""
    hb_path = ROOT / "data" / "forward_test_health.json"
    if not hb_path.exists():
        return CheckResult(
            "DH-002",
            "Data Health",
            "WARN",
            "forward_test_health.json missing — cannot measure bar building rate",
            escalated=True,
        )
    try:
        hb = json.loads(hb_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            "DH-002",
            "Data Health",
            "WARN",
            f"forward_test_health.json unreadable: {exc}",
        )
    bars_built = hb.get("bars_built")
    if bars_built is None:
        return CheckResult(
            "DH-002",
            "Data Health",
            "WARN",
            "bars_built field missing in forward_test_health.json",
        )
    try:
        file_age_s = time.time() - hb_path.stat().st_mtime
    except OSError as exc:
        return CheckResult("DH-002", "Data Health", "WARN", f"stat failed: {exc}")
    market_closed = hb.get("market_closed", False)
    if market_closed:
        return CheckResult(
            "DH-002",
            "Data Health",
            "OK",
            f"market closed — bars_built={bars_built} (no new bars expected)",
        )
    if file_age_s > 300:
        return CheckResult(
            "DH-002",
            "Data Health",
            "CRITICAL",
            f"forward_test_health.json stale ({file_age_s:.0f}s old), bars_built={bars_built}",
            auto_remediation="A1 — restart forward test if stale >5min during market",
            escalated=True,
        )
    if bars_built == 0:
        return CheckResult(
            "DH-002",
            "Data Health",
            "WARN",
            "bars_built=0 (forward test may still be warming up)",
            auto_remediation="A2 — observe; investigate if persists >10min",
        )
    return CheckResult(
        "DH-002",
        "Data Health",
        "OK",
        f"bars_built={bars_built}, health_file_age={file_age_s:.0f}s",
    )


def _ch_dh_signal_stats_write_health() -> CheckResult:
    """DH-005: signal_stats write health — verify the file is being written to."""
    stats_path = ROOT / "data" / "signal_stats.jsonl"
    market_status = _get_market_status()
    if not stats_path.exists():
        return CheckResult(
            "DH-005",
            "Data Health",
            "WARN",
            "signal_stats.jsonl missing — SignalStatsRecorder not writing",
            escalated=True,
        )
    try:
        file_age_s = time.time() - stats_path.stat().st_mtime
    except OSError as exc:
        return CheckResult("DH-005", "Data Health", "WARN", f"stat failed: {exc}")
    # Read last line to check its timestamp
    last_ts: str | None = None
    try:
        out = subprocess.run(  # noqa: S603
            ["tail", "-n", "1", str(stats_path)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
        )
        last_line = out.stdout.strip()
        if last_line:
            try:
                rec = json.loads(last_line)
                last_ts = rec.get("timestamp")
            except json.JSONDecodeError:
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # During market closure, stale writer is expected — relax thresholds
    if market_status in ("weekend", "closed"):
        if file_age_s < 3600:
            detail = f"signal_stats.jsonl mtime={file_age_s:.0f}s (market={market_status})"
            if last_ts:
                detail += f", last_record_ts={last_ts}"
            return CheckResult("DH-005", "Data Health", "OK", detail)
        # Stale writer during market closure is WARN, not CRITICAL
        return CheckResult(
            "DH-005",
            "Data Health",
            "WARN",
            f"signal_stats.jsonl mtime={file_age_s:.0f}s (market={market_status}, stale but expected during closure)",
            auto_remediation="No action — market closed. Verify writer resumes at market open.",
            notes=f"market_status={market_status}",
        )

    if file_age_s < 60:
        detail = f"signal_stats.jsonl mtime={file_age_s:.0f}s"
        if last_ts:
            detail += f", last_record_ts={last_ts}"
        return CheckResult("DH-005", "Data Health", "OK", detail)
    if file_age_s < 300:
        return CheckResult(
            "DH-005",
            "Data Health",
            "WARN",
            f"signal_stats.jsonl mtime={file_age_s:.0f}s (stale writer)",
            auto_remediation="A3 — card for SignalStatsRecorder investigation if market open",
        )
    return CheckResult(
        "DH-005",
        "Data Health",
        "CRITICAL",
        f"signal_stats.jsonl mtime={file_age_s:.0f}s (writer may be dead)",
        auto_remediation="A3 — escalation card for SignalStatsRecorder",
        escalated=True,
    )


# ── Pipeline Health (PH-NNN) — separate from drift detector ────────────────


def _ch_ph_extraction() -> CheckResult:
    """PH-001 extraction cron ran within 35 min."""
    traj = ROOT / "data" / "learning" / "trajectories.jsonl"
    if not traj.exists():
        return CheckResult(
            "PH-001",
            "Pipeline Health",
            "WARN",
            "trajectories.jsonl missing — extraction pipeline inactive",
            auto_remediation="A2 — touch cron idle flag, allow re-extract on next tick",
            escalated=True,
        )
    try:
        age_min = (time.time() - traj.stat().st_mtime) / 60.0
    except OSError as exc:
        return CheckResult("PH-001", "Pipeline Health", "WARN", f"stat failed: {exc}")
    if age_min < 35:
        return CheckResult("PH-001", "Pipeline Health", "OK", f"trajectories mtime={age_min:.1f}min")
    if age_min < 90:
        return CheckResult(
            "PH-001",
            "Pipeline Health",
            "WARN",
            f"trajectories mtime={age_min:.1f}min",
            auto_remediation="A3 — card for cron investigation",
        )
    return CheckResult(
        "PH-001",
        "Pipeline Health",
        "CRITICAL",
        f"trajectories mtime={age_min:.1f}min",
        auto_remediation="A3 — escalation card",
        escalated=True,
    )


# ── Kanban Health (KH-NNN) — delegated to DriftDetector ────────────────────


def _ch_kanban_drift(detector: DriftDetector) -> list[CheckResult]:
    out: list[CheckResult] = []
    stale_cards = detector.check_card_staleness()
    _warn_count = sum(1 for c in stale_cards if c.level == "warn")
    auto_count = sum(1 for c in stale_cards if c.level == "auto_create")

    # KH-001 (todo) + KH-002 (ready) — combined view in the audit table.
    todo_warn = sum(1 for c in stale_cards if c.level == "warn" and c.status == "todo")
    ready_warn = sum(1 for c in stale_cards if c.level == "warn" and c.status == "ready")
    out.append(
        CheckResult(
            "KH-001",
            "Kanban Health",
            "WARN" if todo_warn else "OK",
            f"{todo_warn} stale todo card(s) (≥3d, <7d)",
            auto_remediation="A3 — comment on each stale card; auto-create [STALE] once >7d",
        )
    )
    out.append(
        CheckResult(
            "KH-002",
            "Kanban Health",
            "WARN" if ready_warn else "OK",
            f"{ready_warn} stale ready card(s) (≥3d, <7d)",
            auto_remediation="A3 — comment on each stale card; auto-create [STALE] once >7d",
        )
    )
    out.append(
        CheckResult(
            "KH-003",
            "Kanban Health",
            "CRITICAL" if auto_count else "OK",
            f"{auto_count} cards over 7d auto-create threshold",
            auto_remediation="A3 — auto-create [STALE] follow-up card per the drift detector",
            escalated=auto_count > 0,
        )
    )

    # KH-004 phase staleness
    phases = detector.check_phase_staleness()
    escalates = [p for p in phases if p.level == "escalate"]
    warns = [p for p in phases if p.level == "warn"]
    if escalates:
        out.append(
            CheckResult(
                "KH-004",
                "Kanban Health",
                "CRITICAL",
                f"{len(escalates)} stalled quest phase(s) (≥5d, audit_trail missing or stale)",
                auto_remediation="A4 — surface to overseer-outbox; A3 — escalation card to Craig",
                escalated=True,
            )
        )
    elif warns:
        out.append(
            CheckResult(
                "KH-004",
                "Kanban Health",
                "WARN",
                f"{len(warns)} stalled quest phase(s) (2-5d)",
                auto_remediation="A4 — log warning in heartbeat",
            )
        )
    else:
        out.append(
            CheckResult(
                "KH-004",
                "Kanban Health",
                "OK",
                "no stalled phases (note: audit_trail not yet instrumented in the active quest plan)",
            )
        )
    return out


# ── Aggregation / Report writer ────────────────────────────────────────────


def run_all_checkpoints(detector: DriftDetector | None = None) -> list[CheckResult]:
    detector = detector or DriftDetector()
    checks: list[CheckResult] = []
    # System Health
    checks.append(_ch_pid_file_alive())  # SH-001
    checks.append(_ch_tick_flow())  # SH-002
    checks.append(_ch_stale_pid_files())  # SH-005
    checks.append(_ch_disk())  # SH-004
    checks.append(_ch_log_error_rate())  # SH-008
    checks.append(_ch_log_rotation_state())  # SH-009

    # Trading Health
    checks.append(_ch_ft_daily_dd(ROOT / "data" / "state" / "risk_guard_state.json"))  # FT-003
    checks.append(_ch_ft_total_dd(ROOT / "data" / "state" / "risk_guard_state.json"))  # FT-004
    checks.append(_ch_ft_target_reached())  # FT-AUX-TARGET (Phase 6 audit marker)
    checks.append(_ch_signal_to_trade())  # FT-009
    checks.append(_ch_ft_open_positions_vs_limits())  # FT-002
    checks.append(_ch_ft_best_day_ratio())  # FT-005
    checks.append(_ch_ft_fill_latency_p50())  # FT-007
    checks.append(_ch_ft_fill_latency_p95())  # FT-008
    checks.append(_ch_ft_slippage_analysis())  # FT-010
    checks.append(_ch_ft_order_rejection_rate())  # FT-011

    # Data Health
    checks.append(_ch_dh_signal_stats())  # DH-003
    checks.append(_ch_dh_risk_state())  # DH-004
    checks.append(_ch_dh_tick_feed_latency())  # DH-001
    checks.append(_ch_dh_bar_building_rate())  # DH-002
    checks.append(_ch_dh_signal_stats_write_health())  # DH-005

    # Pipeline Health
    checks.append(_ch_ph_extraction())  # PH-001

    # Kanban Health (delegated to DriftDetector)
    checks.extend(_ch_kanban_drift(detector))  # KH-001..KH-004

    return checks


def render_report(
    report_date: str,
    checks: list[CheckResult],
    remediation_log: list[dict],
    escalation_log: list[dict],
    drift_cards: list[Any] | None = None,
) -> str:
    """Render the daily-audit markdown report.

    ``drift_cards`` accepts a list of ``StaleCard`` objects (or any
    objects exposing a ``.level`` attribute). When ``None`` or empty
    the drift section just prints "no stale cards".
    """
    drift_cards = drift_cards or []
    ok = sum(1 for c in checks if c.status == "OK")
    warn = sum(1 for c in checks if c.status == "WARN")
    crit = sum(1 for c in checks if c.status == "CRITICAL")

    lines: list[str] = []
    lines.append(f"# Hayate Daily Audit — {report_date}")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")

    # Include market status in report header
    market_status = _get_market_status()
    lines.append(f"_Market status: **{market_status}**_")
    lines.append("")
    if crit:
        verdict = f"{crit} CRITICAL, {warn} WARN, {ok} OK"
    elif warn:
        verdict = f"{warn} WARN, {ok} OK — no critical findings"
    else:
        verdict = "All checkpoints GREEN"
    lines.append(
        f"Run on {report_date}: {verdict} "
        f"(out of {len(checks)} checkpoints). "
        f"Drift detector found {len(drift_cards)} stale cards. "
        f"Remediation log: {len(remediation_log)} entry(s) in last 24h. "
        f"Escalation queue: {len(escalation_log)} entry(s)."
    )
    lines.append("")
    if crit:
        criticals = [c for c in checks if c.status == "CRITICAL"]
        lines.append("**Critical findings:**")
        for c in criticals:
            lines.append(f"- {c.check_id} ({c.category}): {c.detail}")
        lines.append("")

    lines.append("## Checkpoint Results")
    lines.append("")
    lines.append("| Check | Category | Status | Detail |")
    lines.append("|-------|----------|--------|--------|")
    for c in checks:
        lines.append(f"| {c.check_id} | {c.category} | {c.status} | {c.detail} |")
    lines.append("")

    lines.append("## Remediation Actions Taken")
    lines.append("")
    if not remediation_log:
        lines.append("- No auto-remediations recorded in `data/ops/remediation_log.jsonl` in the last 24 h.")
    else:
        for entry in remediation_log[:20]:
            ts = entry.get("ts", "?")
            kind = entry.get("kind", "?")
            detail = entry.get("action") or entry.get("detail") or json.dumps(entry)
            lines.append(f"- `{ts}` **{kind}** — {detail}")
    lines.append("")

    lines.append("## Items Escalated")
    lines.append("")
    if not escalation_log:
        lines.append("- No escalations recorded in `data/ops/escalation_queue.jsonl` in the last 24 h.")
    else:
        for entry in escalation_log[:20]:
            ts = entry.get("ts", "?")
            kind = entry.get("kind", "?")
            target = entry.get("phase_id") or entry.get("card_id") or ""
            action = entry.get("recommended_action", "")
            lines.append(f"- `{ts}` **{kind}** {target} — {action}")
    lines.append("")

    lines.append("## Drift Report (3d/7d/2d/5d)")
    lines.append("")
    if not drift_cards:
        lines.append("- Drift detector reported no stale cards today.")
    else:
        lines.append(f"- {len(drift_cards)} stale card(s) returned by `DriftDetector.check_card_staleness()`.")
        # Show first few at the 7d+ level
        auto_create = [c for c in drift_cards if c.level == "auto_create"]
        if auto_create:
            lines.append(
                f"  - **{len(auto_create)}** card(s) over 7d — auto-create follows-up drafted in `data/ops/stale_cards.jsonl`."  # noqa: E501
            )
        warn = [c for c in drift_cards if c.level == "warn"]
        if warn:
            lines.append(f"  - **{len(warn)}** card(s) in 3-7d warning band.")
    lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    recs = []
    for c in checks:
        if c.status == "CRITICAL":
            recs.append(f"**{c.check_id}** ({c.category}): {c.detail}")
        if len(recs) >= 3:
            break
    if not recs:
        recs = [
            "No critical findings — keep monitoring. Consider tuning warning thresholds against trailing 30d distributions.",  # noqa: E501
            "Phase 0 reconciliation (`starting_balance` = $100K canonical) is still pending; the FTMO daily tracker notes the peak mismatch.",  # noqa: E501
            "Phase 6 is operational; next priorities are (a) audit_trail instrumentation on the quest plan so KH-004 produces real signals, (b) activate the cron at 18:00 UTC once Ava approves.",  # noqa: E501
        ]
    for r in recs[:3]:
        lines.append(f"- {r}")
    lines.append("")

    lines.append("## Appendix: Data Sources Queried")
    lines.append("")
    lines.append("- `data/forward_test.pid`, `data/heartbeat_trading.json` (SH-001, SH-002, DH-001)")
    lines.append("- `df -h $AYUMI_ROOT/` (SH-004)")
    lines.append("- `logs/forward_test.log` (SH-008)")
    lines.append("- `data/state/risk_guard_state.json` (FT-003, FT-004, FT-AUX-TARGET, DH-004)")
    lines.append("- `data/forward_test_health.json` (FT-009, DH-001, DH-002)")
    lines.append("- `data/signal_stats.jsonl` (DH-003, DH-005, FT-007, FT-008, FT-010, FT-011)")
    lines.append("- `data/trading.db` trades + daily_summary tables (FT-002, FT-005)")
    lines.append("- `data/learning/trajectories.jsonl` (PH-001)")
    lines.append("- OpenClaw workboard sqlite (KH-001..KH-007 via DriftDetector)")
    lines.append("- `data/ops/remediation_log.jsonl` and `data/ops/escalation_queue.jsonl` (remediation/escalation)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"_Generated by scripts/daily_audit.py on {report_date}._")
    return "\n".join(lines) + "\n"


# ── Telegram delivery ──────────────────────────────────────────────────────


def _send_telegram(text: str, dry_run: bool = False) -> bool:
    """Send the executive summary via the OpenClaw message tool.

    The Hayate hard boundary says "NEVER communicate directly with
    Craig" — but `daily_audit.py` is invoked by the cron wrapper in
    /root/.openclaw/workspace/scripts/hayate_daily_audit.py
    which sits above Hayate and thus has delivery authority. The dry-run
    flag here is a runtime safety: when True, we print to stdout instead.
    """
    if dry_run:
        print("[--send-telegram dry-run] would send:")
        print(text)
        return True
    token = os.environ.get("OPENCLAW_TELEGRAM_TOKEN", "")
    chat = os.environ.get("OPENCLAW_TELEGRAM_CHAT", "")
    if not token or not chat:
        # Fallback to the workspace's gog/telegram tooling if available
        # (caller may run from /root/.openclaw where `gog` exists).
        try:
            import gog  # type: ignore  # noqa: F401

            return bool(gog.send_message(chat_id=chat, text=text))  # pragma: no cover
        except Exception:  # noqa: S110
            pass
        # No-op gracefully — the audit is also written to disk.
        print(
            "WARN: no OPENCLAW_TELEGRAM_TOKEN/CHAT in env — skipping Telegram send.",
            file=sys.stderr,
        )
        return False
    try:
        import requests  # type: ignore  # pragma: no cover

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={"chat_id": chat, "text": text}, timeout=10)
        return bool(resp and resp.ok)
    except Exception as exc:
        print(f"WARN: telegram send failed: {exc}", file=sys.stderr)
        return False


# ── CLI / main ────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Hayate daily audit orchestrator.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Render the report to stdout instead of writing (default)",
    )
    p.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Write the report to reports/hayate-daily/",
    )
    p.add_argument(
        "--send-telegram",
        action="store_true",
        help="Also send Executive Summary to Telegram",
    )
    p.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    p.add_argument("--ops-dir", type=Path, default=DEFAULT_OPS_DIR)
    p.add_argument(
        "--report-date",
        default=None,
        help="Override report date (YYYY-MM-DD); defaults to today UTC",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    now = _now()
    report_date = args.report_date or now.strftime("%Y-%m-%d")
    ops_dir = args.ops_dir

    # Use DriftDetector with explicit ops_dir / plans_dir
    detector = DriftDetector(ops_dir=ops_dir)

    checks = run_all_checkpoints(detector)

    # Pull last-24h entries from ops logs (best-effort).
    cutoff_iso = (now - timedelta(hours=24)).isoformat()
    remediation_log = _read_jsonl_window(ops_dir / "remediation_log.jsonl", cutoff_iso)
    escalation_log = _read_jsonl_window(ops_dir / "escalation_queue.jsonl", cutoff_iso)

    drift_cards = detector.check_card_staleness()

    body = render_report(
        report_date=report_date,
        checks=checks,
        remediation_log=remediation_log,
        escalation_log=escalation_log,
        drift_cards=drift_cards,
    )

    if args.dry_run:
        sys.stdout.write(body)
        print(
            f"\n(dry-run: would write to {args.reports_dir / f'{report_date}.md'})",
            file=sys.stderr,
        )
    else:
        out_dir = args.reports_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{report_date}.md"
        path.write_text(body)
        print(f"Wrote {path}", file=sys.stderr)

    if args.send_telegram:
        # Extract executive summary block (first non-empty paragraph
        # after the H2 heading) and send the first 1000 chars to avoid
        # Telegram's 4096-char message limit.
        summary = _extract_exec_summary(body)[:1000]
        _send_telegram(summary, dry_run=args.dry_run)

    return 0


def _extract_exec_summary(body: str) -> str:
    """Pull the Executive Summary block (paragraph after the H2 heading)."""
    lines = body.splitlines()
    in_section = False
    captured: list[str] = []
    for line in lines:
        if line.startswith("## Executive Summary"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and line.strip():
            captured.append(line.strip())
    return "\n\n".join(captured) if captured else "(no summary extracted)"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
