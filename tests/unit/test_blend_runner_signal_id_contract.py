"""Tests for BlendForwardTestRunner.make_signal_id() — the canonical
signal_id construction.

Background
----------
Phase 5 refactored SLPositionSizer to an identity-keyed API.
BlendForwardTestRunner.on_signal() registers positions with the sizer
using a ``signal_id`` constructed from the signal's strategy_id and
timestamp.  Callers (launcher, engine) need this exact id to cancel
or close the position.

Initially the launcher (scripts/launch_blend_forward_test.py) and the
engine (forward_test_engine.py) each constructed the signal_id
independently.  During live trading we observed the launcher's pattern
diverge from blend_runner's pattern, producing KeyError in cancel paths.

Fix: blend_runner exposes ``make_signal_id(signal)`` as the canonical
constructor.  All callers MUST delegate to it.

These tests lock the contract:
1. make_signal_id exists on BlendForwardTestRunner
2. It uses signal.strategy_id + "_" + str(timestamp.timestamp())
3. The launcher delegates to it (no local construction)
4. The engine delegates to it (no local construction)
5. AST check: no caller constructs an id like X + "_" + str(timestamp)
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

WORKSPACE = Path("$AYUMI_ROOT")
LAUNCHER = WORKSPACE / "scripts" / "launch_blend_forward_test.py"
ENGINE = WORKSPACE / "src" / "forex_bot" / "adapters" / "ctrader" / "forward_test_engine.py"


# ---------------------------------------------------------------------------
# 1. blend_runner.make_signal_id() exists and behaves correctly
# ---------------------------------------------------------------------------


def test_blend_runner_has_make_signal_id_method():
    """The canonical helper must exist on BlendForwardTestRunner."""
    sys.path.insert(0, str(WORKSPACE / "src" / "forex_bot"))
    from forward_test.blend_runner import BlendForwardTestRunner

    assert hasattr(BlendForwardTestRunner, "make_signal_id"), (
        "BlendForwardTestRunner must expose make_signal_id() so callers "
        "can construct the canonical signal_id without drifting."
    )


def test_blend_runner_make_signal_id_uses_strategy_id_and_timestamp():
    """make_signal_id must produce 'strategy_id' + '_' + str(timestamp)."""
    sys.path.insert(0, str(WORKSPACE / "src" / "forex_bot"))
    from datetime import datetime, timezone

    from adapters.ctrader.signal_adapter import CTraderTradeSignal
    from forward_test.blend_runner import BlendForwardTestRunner

    # Minimal construction — use __new__ to skip init.
    runner = BlendForwardTestRunner.__new__(BlendForwardTestRunner)
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

    sig_id = runner.make_signal_id(sig)
    # Contract: strategy_id + "_" + str(timestamp.timestamp())
    expected = "session_breakout_ny" + "_" + str(sig.timestamp.timestamp())
    assert sig_id == expected, (
        f"make_signal_id must produce exact 'strategy_id_timestamp'; got {sig_id!r}, expected {expected!r}"
    )


def test_blend_runner_on_signal_uses_make_signal_id():
    """The on_signal() registration MUST use make_signal_id() — otherwise
    the helper and the actual registration can drift.
    """
    src = (WORKSPACE / "src" / "forex_bot" / "forward_test" / "blend_runner.py").read_text()
    # Find the on_signal function body
    tree = ast.parse(src)
    on_signal_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "on_signal":
            on_signal_node = node
            break
    assert on_signal_node is not None, "Could not find on_signal in blend_runner.py"

    # Find calls to make_signal_id inside on_signal
    calls = [
        n
        for n in ast.walk(on_signal_node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "make_signal_id"
    ]
    assert calls, (
        "on_signal() must call self.make_signal_id() to build the signal_id "
        "it registers with the sizer.  If you construct the id locally, "
        "callers cannot reliably reference it."
    )


# ---------------------------------------------------------------------------
# 2. Launcher delegates — no local construction
# ---------------------------------------------------------------------------


def test_launcher_does_not_construct_signal_id_locally():
    """The launcher's _blend_signal_id() must delegate to blend_runner,
    not construct the id itself.
    """
    src = LAUNCHER.read_text()
    tree = ast.parse(src)

    # Find _blend_signal_id function
    helper_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_blend_signal_id":
            helper_node = node
            break
    assert helper_node is not None, "Launcher must have _blend_signal_id method"

    # The helper must contain a call to make_signal_id (the delegation).
    calls = [
        n
        for n in ast.walk(helper_node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "make_signal_id"
    ]
    assert calls, (
        "Launcher._blend_signal_id() must delegate to blend_runner.make_signal_id(). "
        "Local construction has caused pattern drift in the past (Phase 5 regression)."
    )


# ---------------------------------------------------------------------------
# 3. Engine callers also delegate (Phase 5 already did this — keep it locked)
# ---------------------------------------------------------------------------


def test_engine_does_not_construct_signal_id_locally():
    """The engine's cancel_risk call sites should also delegate to
    blend_runner.make_signal_id.  Phase 5 used a local pattern; if
    that ever returns, this test catches it.
    """
    src = ENGINE.read_text()
    # Look for the signature pattern: X + "_" + str(...) or similar
    # Note: this is a smoke test — the blend_runner delegation tests
    # are the primary guard.
    bad_pattern = re.compile(r"strategy_id\s*\+\s*[\"']_[\"']\s*\+\s*str\(")
    matches = []
    for i, line in enumerate(src.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if bad_pattern.search(line):
            matches.append((i, line.strip()))
    assert not matches, (
        f"Engine should NOT construct signal_id locally.  Use blend_runner.make_signal_id(signal) instead: {matches!r}"
    )


# ---------------------------------------------------------------------------
# 4. AST sanity: the launcher + engine each have _blend_signal_id or
#    delegate via make_signal_id
# ---------------------------------------------------------------------------


def test_launcher_and_engine_both_reference_make_signal_id():
    """Both files should reference the canonical helper at least once."""
    launcher_src = LAUNCHER.read_text()
    engine_src = ENGINE.read_text()
    assert "make_signal_id" in launcher_src, "Launcher must reference make_signal_id (delegation)."
    assert "make_signal_id" in engine_src, "Engine must reference make_signal_id (delegation)."
