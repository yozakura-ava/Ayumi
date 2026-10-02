"""Tests for Phase 6 FTMO daily tracker (scripts/ftmo_daily.py).

These tests exercise the computation function, not the CLI, and use
canned state dicts to avoid touching the live state file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Import via path so we don't depend on package layout; ftmo_daily.py
# self-injects ``src/forex_bot`` and ``src`` into sys.path.
SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _import_ftmo():
    import importlib
    import sys

    spec = importlib.util.spec_from_file_location("ftmo_daily", SCRIPTS_DIR / "ftmo_daily.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register first so dataclass introspection of cls.__module__ works
    sys.modules["ftmo_daily"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ftmo():
    return _import_ftmo()


def test_compute_metrics_default_state_writes_balance_snapshot_report(ftmo, tmp_path):
    """With a fake state file in place, compute + write + read the report."""
    state_path = tmp_path / "risk_guard_state.json"
    state_path.write_text(
        json.dumps(
            {
                "peak_balance": 102_500.0,
                "current_balance": 103_200.0,
                "daily_start_balance": 102_800.0,
                "current_day": "2026-07-08",
                "total_trades": 4,
            }
        )
    )

    # Pass empty history so the best-day ratio doesn't push us into
    # WARNING territory via the equity snapshot stream.
    metrics = ftmo.compute_ftmo_metrics(state_path=state_path, daily_pnl_history=[])
    assert metrics.peak_balance == 102_500.0
    assert metrics.current_balance == 103_200.0
    assert metrics.daily_pnl == pytest.approx(400.0, rel=1e-3)
    assert metrics.daily_loss_pct_of_start == 0.0  # daily positive, no loss
    # Total DD should be 0 because current > peak is treated as no DD
    assert metrics.total_dd_pct == 0.0
    # Profit-target: peak * 1.1 = 112750, current 103200 → not reached
    assert metrics.profit_target.reached is False
    assert metrics.profit_target.required_balance == pytest.approx(112_750.0, rel=1e-3)
    assert metrics.breach_status == "OK"


def test_compute_metrics_breach_when_dd_over_limit(ftmo, tmp_path):
    """When total DD exceeds the FTMO 10% limit, status must be BREACH."""
    state_path = tmp_path / "risk_guard_state.json"
    # peak = 110_000, current = 95_000 → DD ~ 13.6%
    state_path.write_text(
        json.dumps(
            {
                "peak_balance": 110_000.0,
                "current_balance": 95_000.0,
                "daily_start_balance": 100_000.0,
                "current_day": "2026-07-08",
            }
        )
    )

    metrics = ftmo.compute_ftmo_metrics(state_path=state_path)
    assert metrics.total_dd_pct >= 0.10
    assert metrics.breach_status == "BREACH"


def test_compute_metrics_warning_near_daily_limit(ftmo, tmp_path):
    """At ~50% of the 3% daily limit (1.5%), status must be WARNING."""
    state_path = tmp_path / "risk_guard_state.json"
    # daily start = 100_000, current = 98_500 → daily loss = 1.5% of start
    state_path.write_text(
        json.dumps(
            {
                "peak_balance": 100_000.0,
                "current_balance": 98_500.0,
                "daily_start_balance": 100_000.0,
                "current_day": "2026-07-08",
            }
        )
    )

    metrics = ftmo.compute_ftmo_metrics(state_path=state_path)
    assert metrics.daily_loss_pct_of_start >= 0.015  # ≥ 1.5%
    assert metrics.daily_loss_pct_of_start < 0.03  # < 3% hard limit
    assert metrics.breach_status == "WARNING"


def test_profit_target_freeze(ftmo, tmp_path):
    """When balance >= peak * (1 + target_pct), freeze flag is set."""
    state_path = tmp_path / "risk_guard_state.json"
    # peak = 100_000, current = 111_000 → 11% profit, target hit at 110_000
    state_path.write_text(
        json.dumps(
            {
                "peak_balance": 100_000.0,
                "current_balance": 111_000.0,
                "daily_start_balance": 100_000.0,
                "current_day": "2026-07-08",
            }
        )
    )

    metrics = ftmo.compute_ftmo_metrics(state_path=state_path)
    assert metrics.profit_target.reached is True
    assert metrics.profit_target.freeze_new_positions is True


def test_write_report_creates_file(ftmo, tmp_path):
    """write_report() should produce a markdown file with the right date in the name."""
    state_path = tmp_path / "risk_guard_state.json"
    state_path.write_text(
        json.dumps(
            {
                "peak_balance": 100_498.0,
                "current_balance": 100_498.0,
                "daily_start_balance": 100_000.0,
                "current_day": "2026-07-05",
            }
        )
    )

    metrics = ftmo.compute_ftmo_metrics(state_path=state_path)
    out = ftmo.write_report(metrics, reports_dir=tmp_path / "reports")
    assert out is not None
    assert out.exists()
    text = out.read_text()
    assert metrics.report_date in text
    assert "FTMO Daily Report" in text
    assert "+10%" in text
    assert "Best-Day Ratio" in text


def test_missing_state_file_falls_back_to_defaults(ftmo, tmp_path):
    """When the state file doesn't exist, compute returns sensible defaults."""
    state_path = tmp_path / "missing.json"
    metrics = ftmo.compute_ftmo_metrics(state_path=state_path)
    assert metrics.current_balance == 100_000.0
    assert metrics.peak_balance == 100_000.0
    assert metrics.daily_loss_pct_of_start == 0.0
    # no notes by default? Actually one should be added for missing file
    notes = "\n".join(metrics.notes)
    assert "missing" in notes.lower()
