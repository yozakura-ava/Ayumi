"""Tests for equity curve tracking (A8)."""

import json
import sys
from pathlib import Path

import pytest

# Ensure src is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from reporting.equity_tracker import EquityTracker


@pytest.fixture
def tracker(tmp_path):
    """Fresh EquityTracker pointing at a temp directory."""
    return EquityTracker(data_dir=tmp_path, starting_balance=10_000.0)


# ── Test 1: record() appends to JSONL file ────────────────────────────────


def test_record_appends_to_jsonl(tracker, tmp_path):
    """Each call to record() appends one JSON line to the snapshots file."""
    tracker.record(10_100.0, 1)
    tracker.record(10_050.0, 2)
    tracker.record(10_200.0, 3)

    snapshots_file = tmp_path / "forex" / "equity_snapshots.jsonl"
    assert snapshots_file.exists(), "Snapshots JSONL file should be created"

    lines = snapshots_file.read_text().strip().split("\n")
    assert len(lines) == 3, "Should have 3 snapshot lines"

    first = json.loads(lines[0])
    assert first["balance"] == 10_100.0
    assert first["trade_count"] == 1
    assert "timestamp" in first
    assert "drawdown_pct" in first
    assert "peak_balance" in first
    assert "daily_pnl" in first


# ── Test 2: daily_summary() computes correct P&L ──────────────────────────


def test_daily_summary_computes_pnl(tracker):
    """daily_summary() should compute open/close/P&L from same-day snapshots."""
    # All snapshots on the same day
    tracker.record(10_000.0, 0)
    tracker.record(10_200.0, 2)
    tracker.record(10_150.0, 3)

    ds = tracker.daily_summary()
    assert ds is not None
    assert ds.open_balance == 10_000.0
    assert ds.close_balance == 10_150.0
    assert ds.pnl == 150.0
    assert ds.pnl_pct == 1.5
    assert ds.trades == 3
    assert ds.ftmo_status == "OK"


def test_daily_summary_ftmo_warning_on_large_loss(tracker):
    """A >5% loss should flag FTMO WARNING."""
    tracker.record(10_000.0, 0)
    tracker.record(9_400.0, 1)  # -6%

    ds = tracker.daily_summary()
    assert ds.ftmo_status == "WARNING"


# ── Test 3: weekly_summary() aggregates days correctly ────────────────────


def test_weekly_summary_aggregates(tracker):
    """weekly_summary() should aggregate across the full week."""
    # Simulate a week of data using direct JSONL injection
    snapshots_file = tracker._snapshots_path
    lines = []
    bal = 10_000.0
    trades = 0
    for day_offset in range(5):  # Mon-Fri
        d = f"2025-01-{6 + day_offset:02d}"
        for hour in range(9, 17, 2):
            trades += 1
            lines.append(
                json.dumps(
                    {
                        "timestamp": f"{d}T{hour:02d}:00:00+00:00",
                        "balance": round(bal, 2),
                        "daily_pnl": 50.0,
                        "peak_balance": round(bal, 2),
                        "drawdown_pct": 0.0,
                        "trade_count": trades,
                    }
                )
            )
            bal += 50.0
    snapshots_file.write_text("\n".join(lines) + "\n")

    ws = tracker.weekly_summary()
    assert ws is not None
    assert ws.start_balance == 10_000.0
    # 20 snapshots, first at 10000, each +50 → last at 10950
    assert ws.end_balance == 10_950.0
    assert ws.pnl == 950.0
    # trade_count delta: last(20) - first(1) = 19
    assert ws.total_trades == 19
    assert ws.best_day is not None
    assert ws.worst_day is not None


# ── Test 4: drawdown_pct calculated correctly from peak ───────────────────


def test_drawdown_calculated_from_peak(tracker):
    """drawdown_pct should reflect drop from peak, not from day open."""
    # Rise to peak then drawdown
    tracker.record(10_000.0, 0)
    tracker.record(10_500.0, 1)  # new peak
    tracker.record(10_200.0, 2)  # drawdown from peak

    snapshots_file = tracker._snapshots_path
    lines = snapshots_file.read_text().strip().split("\n")
    last = json.loads(lines[-1])

    # Drawdown = (10500 - 10200) / 10500 * 100 = 2.857%
    expected_dd = round((10_500 - 10_200) / 10_500 * 100, 4)
    assert last["drawdown_pct"] == expected_dd
    assert last["peak_balance"] == 10_500.0


def test_drawdown_zero_when_at_peak(tracker):
    """drawdown_pct should be 0 when balance equals or exceeds peak."""
    tracker.record(10_000.0, 0)
    tracker.record(10_500.0, 1)

    snapshots_file = tracker._snapshots_path
    lines = snapshots_file.read_text().strip().split("\n")
    last = json.loads(lines[-1])
    assert last["drawdown_pct"] == 0.0


# ── Test 5: write_daily_report produces markdown ──────────────────────────


def test_write_daily_report(tracker, tmp_path):
    """write_daily_report should produce a markdown file."""
    tracker.record(10_000.0, 0)
    tracker.record(10_200.0, 2)

    report_path = tracker.write_daily_report()
    assert report_path is not None
    assert report_path.exists()
    content = report_path.read_text()
    assert "Equity Daily Report" in content
    assert "P&L" in content
    assert "FTMO" in content


# ── Test 6: day rollover flushes prior-day canonical export (card d8c2a10b) ─


def test_record_writes_prior_day_canonical_on_rollover(tracker, tmp_path):
    """When record() detects a UTC day rollover, it must write the prior
    day's canonical export (equity_reports/<prior_day>.md) before appending
    the first snapshot of the new day.

    This prevents the canonical export from drifting behind the
    operational feed while the forward-test process keeps running.
    """
    from datetime import datetime, timedelta, timezone

    reports_dir = tmp_path / "forex" / "equity_reports"

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_iso = f"{yesterday}T12:00:00+00:00"

    # Pre-seed the JSONL with snapshots dated YESTERDAY so the
    # rollover flush has data to summarize.
    snapshots_file = tmp_path / "forex" / "equity_snapshots.jsonl"
    snapshots_file.parent.mkdir(parents=True, exist_ok=True)
    snapshots_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": f"{yesterday}T09:00:00+00:00",
                        "balance": 10_000.00,
                        "daily_pnl": 0.0,
                        "peak_balance": 10_000.00,
                        "drawdown_pct": 0.0,
                        "trade_count": 0,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": f"{yesterday}T16:00:00+00:00",
                        "balance": 10_250.00,
                        "daily_pnl": 250.0,
                        "peak_balance": 10_250.00,
                        "drawdown_pct": 0.0,
                        "trade_count": 2,
                    }
                ),
                "",  # trailing newline
            ]
        )
    )

    # Force the tracker to think it last wrote on YESTERDAY.
    tracker._current_day = yesterday
    tracker._daily_open = 10_000.00
    tracker._trade_count_at_day_open = 0
    tracker._peak_balance = 10_250.00

    # First record() of "today" must trigger the rollover flush.
    tracker.record(10_250.00, 2)

    prior_report = reports_dir / f"{yesterday}.md"
    assert prior_report.exists(), (
        f"Day rollover should write {prior_report} for {yesterday}"
    )
    content = prior_report.read_text()
    assert yesterday in content
    assert "Equity Daily Report" in content
    # Prior day closed at 10250.00, opened at 10000.00 → P&L +250.00.
    assert "+250" in content or "250" in content

    # The new-day snapshot must still be appended AFTER the rollover flush,
    # so the JSONL reflects operational continuity.
    # (2 seeded prior-day + 1 new-day = 3 total — the rollover flush
    # writes a markdown file, it does NOT add a JSONL line.)
    lines = [ln for ln in snapshots_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3, (
        f"Expected 3 lines after rollover (2 prior-day + 1 new day), "
        f"got {len(lines)}"
    )

    # Today's snapshot must reference a date > yesterday's.
    last_snap = json.loads(lines[-1])
    last_day = last_snap["timestamp"][:10]
    assert last_day > yesterday, (
        f"Last snapshot should be on the new day, got {last_day}"
    )


def test_record_no_rollover_report_on_same_day(tracker, tmp_path):
    """Repeated record() calls on the same UTC day must NOT emit
    extra canonical-export writes — the rollover flush fires only on
    the first snapshot of a new day.
    """
    tracker.record(10_000.00, 0)
    tracker.record(10_100.00, 1)
    tracker.record(10_050.00, 2)

    # No explicit write_daily_report() was called → reports dir empty.
    reports_dir = tmp_path / "forex" / "equity_reports"
    if reports_dir.exists():
        files = list(reports_dir.glob("*.md"))
        assert files == [], (
            f"Same-day record() should not write canonical exports; "
            f"found {files}"
        )


def test_record_rollover_failure_does_not_break_snapshots(tracker, tmp_path):
    """A failed prior-day report write on rollover must not break the
    operational snapshot pipeline — the new-day snapshot must still
    append and the process must keep running.
    """
    from datetime import datetime, timedelta, timezone

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    snapshots_file = tmp_path / "forex" / "equity_snapshots.jsonl"
    snapshots_file.parent.mkdir(parents=True, exist_ok=True)
    snapshots_file.write_text(
        json.dumps(
            {
                "timestamp": f"{yesterday}T10:00:00+00:00",
                "balance": 10_000.00,
                "daily_pnl": 0.0,
                "peak_balance": 10_000.00,
                "drawdown_pct": 0.0,
                "trade_count": 0,
            }
        )
        + "\n"
    )

    tracker._current_day = yesterday
    tracker._daily_open = 10_000.00
    tracker._trade_count_at_day_open = 0

    # Sabotage: make write_daily_report raise.
    def _boom(_target_date=None):  # noqa: ANN001
        raise OSError("simulated fs error")

    tracker.write_daily_report = _boom  # type: ignore[method-assign]

    # Must not raise — operational pipeline continues.
    snap = tracker.record(10_050.00, 1)
    assert snap.balance == 10_050.00

    # JSONL must still record the new-day snapshot despite the rollover
    # report failure.
    lines = [ln for ln in snapshots_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[-1])["balance"] == 10_050.00
