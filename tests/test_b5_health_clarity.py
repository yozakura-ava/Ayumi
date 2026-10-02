"""Tests for card fca4715a-c478-44e7-b1c4-4a870561bdd5 — B5 Health log clarity.

Disambiguates the three dd-like numbers and the bars counter that previously
appeared as bare ``dd=`` / ``bars=`` in [B5 Health] and [Balance Sync] log lines.

AC1: B5 Health / [Balance Sync] lines label dd fields explicitly
     (``dd_from_start=``, ``dd_from_peak=``, ``daily_loss_from_day_start=``);
     no bare ``dd=`` token remains in those lines.
AC2: bars counter self-describing: ``bars_total=`` (cumulative since process start).
AC3: Consumers of the renamed tokens updated inside allowed_files; outside
     consumers reported in a card comment for orchestrator follow-up.
AC4: Zero functional change — formatting/string-only diff.

Scope (allowed_files):
    src/forex_bot/engine/health_monitor.py
    src/forex_bot/adapters/ctrader/risk_guard.py
    src/forex_bot/adapters/ctrader/forward_test_engine.py
    scripts/launch_blend_forward_test.py
    tests/test_b5_health_clarity.py    (this file)
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# HealthMonitor is importable via the engine/ package (no forex_brain helpers
# required for this narrow observability test).
from engine.health_monitor import HealthMonitor


# --------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[1]

HEALTH_MONITOR_PY = REPO_ROOT / "src" / "forex_bot" / "engine" / "health_monitor.py"
FORWARD_TEST_ENGINE_PY = (
    REPO_ROOT / "src" / "forex_bot" / "adapters" / "ctrader" / "forward_test_engine.py"
)
LAUNCH_BLEND_PY = REPO_ROOT / "scripts" / "launch_blend_forward_test.py"
RISK_GUARD_PY = REPO_ROOT / "src" / "forex_bot" / "adapters" / "ctrader" / "risk_guard.py"


# --------------------------------------------------------------------- #
# Capture handler (mirrors tests/integration/test_health_monitor.py)
# --------------------------------------------------------------------- #


class _LogCapture:
    """Minimal logging handler that stores records for assertions."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []

    def __call__(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def capture_logger():
    """Attach a capture handler to the health-monitor logger."""
    cap = _LogCapture()
    handler = logging.Handler()
    handler.emit = cap
    logger = logging.getLogger("ayumi.forward_test")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield cap
    logger.removeHandler(handler)


def _make_mock(**kwattrs):
    """Create a MagicMock with the given attribute values."""
    m = MagicMock()
    for k, v in kwattrs.items():
        setattr(m, k, v)
    return m


# --------------------------------------------------------------------- #
# AC1 / AC2 — runtime test for HealthMonitor._emit_health()
# --------------------------------------------------------------------- #


class TestHealthMonitorB5HealthLine:
    """Verify the [B5 Health] log line carries the disambiguated tokens."""

    def test_main_b5_health_line_uses_bars_total(self, capture_logger):
        """AC2 — 'bars=' is renamed to 'bars_total=' on the main B5 Health line."""
        mon = HealthMonitor(interval_seconds=999)
        mon._start_time = time.monotonic()
        mon._market_data_feed = _make_mock(
            ticks_received=100,
            ticks_per_second=2.5,
            bars_built=10,
            signals_generated=5,
            paper_trades=3,
            balance=10000.0,
        )
        mon._order_gateway = _make_mock(live_fills=2)

        mon._emit_health()

        b5_lines = [
            r for r in capture_logger.records
            if "[B5 Health]" in r.getMessage() and r.levelno == logging.INFO
        ]
        assert b5_lines, "expected at least one [B5 Health] INFO line"
        msg = b5_lines[0].getMessage()

        # AC2 (positive): the renamed token is present.
        assert "bars_total=10" in msg, (
            f"AC2: expected 'bars_total=10' in B5 Health line, got: {msg!r}"
        )

        # AC1 (negative): no bare 'bars=' remains on the main B5 Health line.
        # Use a regex that excludes 'bars_total=' so we don't false-positive on the
        # substring (Python's `in` would match 'bars_total=10' as containing 'bars_t').
        bare_bars = re.search(r"(?<!_)bars=\d", msg)
        assert bare_bars is None, (
            f"AC1: bare 'bars=' token must not appear in B5 Health line, got: {msg!r}"
        )

    def test_main_b5_health_line_keeps_other_field_names(self, capture_logger):
        """AC4 — other field names on the B5 Health line are unchanged."""
        mon = HealthMonitor(interval_seconds=999)
        mon._start_time = time.monotonic()
        mon._market_data_feed = _make_mock(
            ticks_received=200,
            ticks_per_second=1.5,
            bars_built=42,
            signals_generated=7,
            paper_trades=4,
            balance=9876.54,
        )
        mon._order_gateway = _make_mock(live_fills=1)

        mon._emit_health()

        b5_info = [
            r for r in capture_logger.records
            if "[B5 Health]" in r.getMessage() and r.levelno == logging.INFO
        ][0]
        msg = b5_info.getMessage()

        # Tokens that must still be present (unchanged semantics).
        assert "ticks=200" in msg
        assert "tps=1.50" in msg
        assert "signals=7" in msg
        assert "paper_trades=4" in msg
        assert "live_fills=1" in msg
        assert "balance=9876.54" in msg
        assert "uptime=" in msg


# --------------------------------------------------------------------- #
# AC1 — static test for forward_test_engine.py [Balance Sync] line
# --------------------------------------------------------------------- #


class TestBalanceSyncLineSource:
    """Static-test of forward_test_engine.py source for the renamed tokens."""

    def test_balance_sync_uses_dd_from_start(self):
        """AC1 — [Balance Sync] uses 'dd_from_start=' (not bare 'dd=')."""
        text = FORWARD_TEST_ENGINE_PY.read_text()

        # Extract the [Balance Sync] RiskGuard synced format string.
        match = re.search(
            r'"\[Balance Sync\] RiskGuard synced:[^"]+"',
            text,
        )
        assert match, "could not locate [Balance Sync] RiskGuard synced line"
        line = match.group(0)

        assert "dd_from_start=" in line, (
            f"AC1: [Balance Sync] must contain 'dd_from_start=', got: {line}"
        )

        # No bare 'dd=' on this specific line.  'dd_from_start' starts with 'dd_'
        # so a word-boundary check correctly excludes it.
        bare_dd = re.search(r"\bdd=\b", line)
        assert bare_dd is None, (
            f"AC1: bare 'dd=' must not appear on the [Balance Sync] line, got: {line}"
        )


# --------------------------------------------------------------------- #
# AC1 — static test for launch_blend_forward_test.py _ftmo_str / B5 Health / B5 Pipeline
# --------------------------------------------------------------------- #


class TestLaunchBlendB5HealthSource:
    """Static-test of launch_blend_forward_test.py for the renamed tokens."""

    def test_ftmo_str_uses_dd_from_peak(self):
        """AC1 — _ftmo_str in B5 Health line uses 'dd_from_peak='."""
        text = LAUNCH_BLEND_PY.read_text()

        match = re.search(
            r'_ftmo_str\s*=\s*\(\s*(?:f?"[^"]*"\s*)+\)',
            text,
            re.DOTALL,
        )
        assert match, "could not locate _ftmo_str assignment"
        ftmo_str = match.group(0)

        assert "dd_from_peak=" in ftmo_str, (
            f"AC1: _ftmo_str must contain 'dd_from_peak=', got: {ftmo_str}"
        )
        assert "daily_loss_from_day_start=" in ftmo_str, (
            f"AC1: _ftmo_str must contain 'daily_loss_from_day_start=', got: {ftmo_str}"
        )

        # No bare 'dd=' on _ftmo_str line (where current_dd_pct lives).
        bare_dd = re.search(r"\bdd=\b", ftmo_str)
        assert bare_dd is None, (
            f"AC1: bare 'dd=' must not appear in _ftmo_str, got: {ftmo_str}"
        )
        bare_daily_loss = re.search(r"\bdaily_loss=\b", ftmo_str)
        assert bare_daily_loss is None, (
            f"AC1: bare 'daily_loss=' must not appear in _ftmo_str, got: {ftmo_str}"
        )

    def test_main_b5_health_line_uses_bars_total(self):
        """AC1 / AC2 — main [B5 Health] log line uses 'bars_total='."""
        text = LAUNCH_BLEND_PY.read_text()

        # The main B5 Health emission starts with '[B5 Health] ticks=' and spans
        # multiple continuation lines.  Match the full literal expression.
        match = re.search(
            r'"\[B5 Health\] ticks=%d[^"]+"',
            text,
        )
        assert match, "could not locate main [B5 Health] log line"
        line = match.group(0)

        assert "bars_total=" in line, (
            f"AC2: main [B5 Health] line must contain 'bars_total=', got: {line}"
        )
        bare_bars = re.search(r"(?<!_)bars=\d", line)
        assert bare_bars is None, (
            f"AC1: bare 'bars=' must not appear on main B5 Health line, got: {line}"
        )

    def test_market_closed_pipeline_uses_bars_total(self):
        """AC1 / AC2 — [B5 Pipeline] Market closed line uses 'bars_total='."""
        text = LAUNCH_BLEND_PY.read_text()

        match = re.search(
            r'"\[B5 Pipeline\] Market closed — ticks=%d[^"]+"',
            text,
        )
        assert match, "could not locate [B5 Pipeline] Market closed line"
        line = match.group(0)

        assert "bars_total=" in line, (
            f"AC2: [B5 Pipeline] Market closed line must contain 'bars_total=', got: {line}"
        )
        bare_bars = re.search(r"(?<!_)bars=\d", line)
        assert bare_bars is None, (
            f"AC1: bare 'bars=' must not appear on [B5 Pipeline] Market closed line, got: {line}"
        )


# --------------------------------------------------------------------- #
# AC4 — zero functional change verification (RiskGuard math is unchanged)
# --------------------------------------------------------------------- #


class TestFunctionalInvariants:
    """Verify the dd / daily_loss math is unchanged (AC4)."""

    def test_risk_guard_dd_formula_sign_unchanged(self):
        """dd_pct is NEGATIVE when equity is ABOVE the starting balance.

        The audit (ayumi-groupchat-claims-verification-2026-10-02.md, Claim 1)
        shows the formula (starting - current) / starting is sign-correct: when
        current > starting, dd_pct is negative.  This is the very confusion
        the rename fixes — the LABEL now disambiguates, but the math is
        unchanged.
        """
        from adapters.ctrader.risk_guard import FTMOConfig, RiskGuard

        config = FTMOConfig(
            total_drawdown_limit_pct=0.10,
            daily_loss_limit_pct=0.03,
        )
        rg = RiskGuard(ftmo_config=config, starting_balance=10_000.0)
        rg._current_balance = 10_180.48  # equity above start
        rg._daily_start_balance = 10_180.48
        rg._daily_trade_count = 0  # gate daily_loss_pct calc

        # (starting - current) / starting = (10000 - 10180.48) / 10000 = -0.018
        dd_pct = rg.current_drawdown_pct
        assert dd_pct < 0, (
            f"dd_pct must be negative when equity above start, got {dd_pct}"
        )
        assert abs(dd_pct - (-0.018048)) < 1e-4, (
            f"dd_pct must equal the formula value, got {dd_pct}"
        )

    def test_risk_guard_dd_formula_positive_for_loss(self):
        """dd_pct is POSITIVE when equity is BELOW the starting balance."""
        from adapters.ctrader.risk_guard import FTMOConfig, RiskGuard

        config = FTMOConfig(
            total_drawdown_limit_pct=0.10,
            daily_loss_limit_pct=0.03,
        )
        rg = RiskGuard(ftmo_config=config, starting_balance=10_000.0)
        rg._current_balance = 9_241.07  # equity below start (historical min)
        rg._daily_start_balance = 9_241.07
        rg._daily_trade_count = 0

        dd_pct = rg.current_drawdown_pct
        # (10000 - 9241.07) / 10000 = 0.075893
        assert dd_pct > 0, (
            f"dd_pct must be positive when equity below start, got {dd_pct}"
        )
        assert abs(dd_pct - 0.075893) < 1e-4, (
            f"dd_pct must equal the formula value, got {dd_pct}"
        )

    def test_trip_condition_still_fires_only_on_losses(self):
        """The trip condition `>=` cannot fire when dd_pct is negative (gain)."""
        from adapters.ctrader.risk_guard import FTMOConfig, RiskGuard

        config = FTMOConfig(
            total_drawdown_limit_pct=0.10,
            daily_loss_limit_pct=0.03,
        )
        rg = RiskGuard(ftmo_config=config, starting_balance=10_000.0)
        rg._current_balance = 10_180.48  # equity above start (gain)
        rg._daily_start_balance = 10_180.48
        rg._daily_trade_count = 0

        dd_pct = rg.current_drawdown_pct
        assert not (dd_pct >= config.total_drawdown_limit_pct), (
            "trip condition must NOT fire on a gain (negative dd_pct < limit)"
        )


# --------------------------------------------------------------------- #
# AC3 — outside-consumer report (recorded for orchestrator follow-up)
# --------------------------------------------------------------------- #


class TestOutsideConsumersDocumented:
    """Sanity check on the report we'd attach to the card for orchestrator follow-up.

    The actual report is attached as a card comment at release time.  This test
    exists so that if a future rename breaks an outside consumer, we at least
    have a sentinel.
    """

    def test_outside_consumer_in_integration_health_monitor_test(self):
        """`tests/integration/test_health_monitor.py:81` asserts 'bars=' in msg.

        That assertion predates this card's rename to bars='' and will fail
        once the rename ships.  It is OUTSIDE allowed_files and must be
        updated by a follow-up card, not this one.
        """
        consumer_path = (
            REPO_ROOT / "tests" / "integration" / "test_health_monitor.py"
        )
        if not consumer_path.exists():
            pytest.skip("integration test file not present in this checkout")

        text = consumer_path.read_text()
        assert 'assert "bars=" in msg' in text, (
            "Expected outside-consumer assertion 'assert \"bars=\" in msg' "
            "to still be in tests/integration/test_health_monitor.py:81.  "
            "If it has been updated, remove this sentinel test."
        )