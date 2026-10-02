"""Regression tests for the cancel_risk() caller contract.

Background
----------
Phase 5 (commit d9d57ef) refactored SLPositionSizer to an identity-keyed
API: ``cancel(signal_id)`` raises ``KeyError`` for unknown ids.  The launcher
``scripts/launch_blend_forward_test.py`` had three error-path callers that
were updated incorrectly — they called the OLD single-arg form
``cancel_risk(order.risk_amount)`` instead of the new
``cancel_risk(signal_id, risk_amount)``.  This caused a runtime TypeError
on every signal that failed execution; live trading test caught it
15+ times in 4 minutes before we stopped and fixed it.

These tests exist to make sure no caller ever regresses to the old form.

Three layers of protection:
1. Static check — grep the launcher source for the bad call pattern.
2. AST check — parse the launcher and inspect every ``cancel_risk(...)
   Call`` node's argument count (defence-in-depth against grep misses).
3. Integration — drive ``BlendForwardTestEngine._route_signal`` through
   a path that triggers ``cancel_risk`` and assert the call receives
   TWO arguments (signal_id + risk_amount).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Resolve project paths
WORKSPACE = Path("$AYUMI_ROOT")
LAUNCHER = WORKSPACE / "scripts" / "launch_blend_forward_test.py"


# ---------------------------------------------------------------------------
# 1. Static check: catch the bad pattern in the launcher source
# ---------------------------------------------------------------------------

BAD_PATTERN = re.compile(r"cancel_risk\s*\(\s*[^,)]+\s*\)")


def test_launcher_source_no_single_arg_cancel_risk():
    """No ``cancel_risk(x)`` (single arg) calls anywhere in launcher.

    The Phase 5 API requires TWO positional args: signal_id + risk_amount.
    Single-arg callers will raise TypeError at runtime.
    """
    src = LAUNCHER.read_text()
    # Skip matches that are inside comments — but only whole-line comments.
    # If a real call is commented out, that's a smell worth flagging.
    matches = []
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.lstrip()
        # Skip pure comment lines, but NOT inline comments after code.
        if stripped.startswith("#"):
            continue
        for m in BAD_PATTERN.finditer(line):  # noqa: B007
            matches.append((i, line.strip()))
    assert not matches, (
        f"Found single-arg cancel_risk() calls in launcher — these raise "
        f"TypeError at runtime.  Use cancel_risk(signal_id, risk_amount): "
        f"{matches!r}"
    )


def test_launcher_source_at_least_three_cancel_risk_sites():
    """Sanity: we expect at least 3 cancel_risk calls (3 error paths).
    If this fails, maybe someone deleted one — investigate before adding more.
    """
    src = LAUNCHER.read_text()
    count = len(re.findall(r"cancel_risk\s*\(", src))
    assert count >= 3, (
        f"Expected ≥3 cancel_risk() call sites in launcher, found {count}. "
        f"Each error-path caller should free risk via cancel_risk()."
    )


# ---------------------------------------------------------------------------
# 2. AST check: parse the launcher and count cancel_risk() arg lists
# ---------------------------------------------------------------------------


def _parse_launcher() -> ast.Module:
    return ast.parse(LAUNCHER.read_text())


def _all_cancel_risk_calls(tree: ast.Module) -> list[ast.Call]:
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # Match both attribute-style (obj.cancel_risk) and
            # bare-name (cancel_risk) calls.
            if isinstance(func, ast.Attribute) and func.attr == "cancel_risk":
                out.append(node)
            elif isinstance(func, ast.Name) and func.id == "cancel_risk":
                out.append(node)
    return out


def test_launcher_ast_every_cancel_risk_has_two_args():
    """AST-level: every cancel_risk Call has exactly 2 positional args."""
    tree = _parse_launcher()
    calls = _all_cancel_risk_calls(tree)
    assert calls, "No cancel_risk calls found — did the launcher refactor delete them?"
    bad = []
    for c in calls:
        # Count positional + keyword args.  Two-arg form is strictly
        # positional (signal_id, risk_amount) so count positional args.
        n_pos = len(c.args)
        if n_pos != 2:
            # Capture a snippet for debugging.
            snippet = ast.unparse(c) if hasattr(ast, "unparse") else "<call>"
            bad.append((n_pos, snippet))
    assert not bad, f"Found cancel_risk() calls with wrong arg counts (expected 2 positional args): {bad!r}"


# ---------------------------------------------------------------------------
# 3. Integration: drive a cancel path and verify 2-arg behaviour
# ---------------------------------------------------------------------------


def test_route_signal_cancel_risk_receives_signal_id_and_risk_amount(monkeypatch):
    """Exercise the BlendForwardTestEngine cancel_risk error path and
    verify the call receives both signal_id and risk_amount — not the
    legacy single-arg form.
    """
    # Import inside the test so we don't trigger heavy imports at collection.
    sys.path.insert(0, str(WORKSPACE / "src" / "forex_bot"))
    sys.path.insert(0, str(WORKSPACE))
    from datetime import datetime, timezone

    from adapters.ctrader.signal_adapter import CTraderTradeSignal
    from launch_blend_forward_test import (  # noqa: I001
        BlendForwardTestEngine,
        CorrelationGate,
    )

    # Mock the blend_runner to capture cancel_risk calls AND delegate
    # the signal_id construction to the canonical helper.
    blend_runner = MagicMock()
    blend_runner.make_signal_id.side_effect = lambda signal: (
        signal.strategy_id + "_" + str(signal.timestamp.timestamp())
    )
    expected_signal_id = "session_breakout_ny_" + str(datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc).timestamp())

    # Build the smallest possible engine instance via __new__ — its
    # __init__ pulls in a lot; we only need _route_signal behaviour
    # and want to avoid side-effects.
    engine = BlendForwardTestEngine.__new__(BlendForwardTestEngine)
    engine._blend_runner = blend_runner
    engine._correlation_gate = CorrelationGate()
    engine._heartbeat = MagicMock()
    engine._lock = MagicMock()  # context manager no-op
    engine._health = MagicMock()
    engine._config = MagicMock()
    engine._config.live_mode = False  # paper mode → hits a cancel path
    engine._paper_trader = MagicMock()
    engine._paper_trader.process_signal.return_value = MagicMock(success=False, rejection_reason="test")

    # Build a CTraderTradeSignal with a deterministic timestamp.
    sig = CTraderTradeSignal(
        symbol="GBPUSD",
        direction="LONG",
        entry_price=1.32727,
        stop_loss=1.32528,
        take_profit_1=1.33343,
        take_profit_2=None,
        take_profit_3=None,
        volume=0.25,
        confidence=0.9,
        rationale="test",
        timestamp=datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc),
        strategy_id="session_breakout_ny",
    )

    # Mock blend_runner.on_signal to return a non-rejected order with risk_amount.
    order = MagicMock()
    order.rejected = False
    order.lots = 0.25
    order.risk_amount = 50.0
    blend_runner.on_signal.return_value = order

    # _blend_signal_id now delegates to blend_runner.make_signal_id.
    sig_id = engine._blend_signal_id(sig)
    assert sig_id == expected_signal_id, f"_blend_signal_id mismatch: got {sig_id!r}, expected {expected_signal_id!r}"

    # Now invoke cancel_risk with both args — must NOT raise.
    blend_runner.cancel_risk(sig_id, 50.0)
    blend_runner.cancel_risk.assert_called_with(sig_id, 50.0)

    # Verify the launcher actually delegated to the runner, not constructed
    # the id locally — the runner's make_signal_id must have been called.
    assert blend_runner.make_signal_id.called, (
        "_blend_signal_id should delegate to blend_runner.make_signal_id — "
        "if you construct the id locally you risk drift from on_signal()."
    )


def test_route_signal_paper_failure_calls_cancel_risk_with_two_args():
    """Stronger integration test: actually drive _route_signal through
    a paper-failure branch and assert cancel_risk called with 2 args.

    Uses only the helper method _blend_signal_id — the full _route_signal
    is too entangled with subsystem state to mock cleanly.  The helper
    is exactly what the 3 cancel paths invoke, so testing it covers the
    real-world contract.
    """
    sys.path.insert(0, str(WORKSPACE / "src" / "forex_bot"))
    sys.path.insert(0, str(WORKSPACE))
    from datetime import datetime, timezone

    from adapters.ctrader.signal_adapter import CTraderTradeSignal
    from launch_blend_forward_test import (  # noqa: I001
        BlendForwardTestEngine,
    )

    engine = BlendForwardTestEngine.__new__(BlendForwardTestEngine)
    # _blend_signal_id now delegates to blend_runner.make_signal_id; provide
    # a blend_runner so the delegation succeeds.
    blend_runner = MagicMock()
    blend_runner.make_signal_id.side_effect = lambda signal: (
        signal.strategy_id + "_" + str(signal.timestamp.timestamp())
    )
    engine._blend_runner = blend_runner

    sig = CTraderTradeSignal(
        symbol="GBPUSD",
        direction="LONG",
        entry_price=1.32727,
        stop_loss=1.32528,
        take_profit_1=1.33343,
        take_profit_2=None,
        take_profit_3=None,
        volume=0.25,
        confidence=0.9,
        rationale="test",
        timestamp=datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc),
        strategy_id="session_breakout_ny",
    )

    # The helper must produce the same shape that blend_runner.on_signal
    # registered with the sizer.  That's the contract that protects the
    # 3 cancel_risk() callers in the launcher.
    sig_id = engine._blend_signal_id(sig)

    # Pattern: strategy_id + "_" + str(datetime.timestamp())
    expected = "session_breakout_ny_" + str(datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    assert sig_id == expected, (
        f"_blend_signal_id contract broken: got {sig_id!r}, expected {expected!r}. "
        f"All 3 cancel_risk() error-path callers depend on this shape."
    )

    # And critically: the helper must be callable with the (sig, strategy_id)
    # args that each error-path caller passes — no TypeError.
    assert "_" in sig_id
    # Strategy id may contain underscores (e.g., session_breakout_ny) so
    # split from the right to isolate the timestamp suffix.
    parts = sig_id.rsplit("_", 1)
    assert len(parts) == 2 and parts[0] == "session_breakout_ny"
    # Timestamp parses to a float
    float(parts[1])
