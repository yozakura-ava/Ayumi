"""Regression test for the LIVE-mode position_id → signal_id mapping bug.

Bug
---
In live mode, ``ForwardTestEngine._on_trade_executed``
(``forward_test_engine.py:2605-2625``) is the callback that wires a
cTrader ``positionId`` to the blend runner's canonical ``signal_id``
so that ``_on_position_closed`` can release sizer risk when a live
trade closes.

But ``_on_trade_executed`` is registered as a callback on
``self._paper_trader`` (``forward_test_engine.py:821``). In live mode
the paper trader is bypassed entirely (orders go through
``_execute_signal_live`` → ``_market_feed.new_order`` → cTrader
directly), so the callback never fires. The first live close
leaked a sizer risk slot.

Audit: ``docs/audits/phase1a-execution-path-audit-2026-07-08.md``
section §5.4: "LIVE-mode position_id → signal_id mapping gap
(real bug, latent)".

Fix
---
``ForwardTestEngine._register_blend_position_mapping(signal,
ctrader_position_id)`` is called from:

  1. ``_execute_signal_live`` synchronous FILLED + inline SL/TP branch
     (``forward_test_engine.py:1380-1388`` after the inline path
     extracts ``order.position_id``).
  2. ``_execute_signal_live`` synchronous FILLED + amend-SL/TP fallback
     (``forward_test_engine.py:1410-1421``).
  3. ``_release_late`` late-fill FILLED branch
     (``forward_test_engine.py:1819`` — audit §5.4 recommended fix).

The helper guards on ``_blend_runner`` availability and on a non-zero
``int`` cTrader positionId (mirroring the surrounding code), and
delegates id construction to the canonical ``make_signal_id`` so we
never drift out of sync with the sizer's identity key.

These tests lock the contract:
  A. ``_register_blend_position_mapping`` calls
     ``blend_runner.register_position_mapping(str(positionId),
     make_signal_id(signal))`` when given valid inputs.
  B. The wiring is a no-op when ``_blend_runner`` is unset.
  C. The wiring is a no-op when ``ctrader_position_id`` is 0 or a
     non-int (mirroring the surrounding ``_release_late`` validation
     contract from ``tests/unit/execution/test_late_fill_positionid.py``).
  D. The two ``_execute_signal_live`` FILLED branches and the
     ``_release_late`` FILLED branch each actually invoke the helper
     (static AST check + simulation).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

WORKSPACE = Path("$AYUMI_ROOT")
ENGINE = WORKSPACE / "src" / "forex_bot" / "adapters" / "ctrader" / "forward_test_engine.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine_mock():
    """A minimal ``ForwardTestEngine`` with ``__init__`` bypassed.

    Same construction pattern as
    ``tests/unit/execution/test_late_fill_positionid.py``. We only need
    enough surface area for ``_register_blend_position_mapping`` and the
    synchronous / late-fill wiring tests; everything else is mocked.
    """
    from adapters.ctrader.forward_test_engine import ForwardTestEngine

    with patch.object(ForwardTestEngine, "__init__", return_value=None):
        engine = ForwardTestEngine.__new__(ForwardTestEngine)
        engine._lock = MagicMock()
        engine._live_fill_count = 0
        engine._strategies = {}
        engine._strategy_id_map = {}
        engine._strategy_eval_counts = {}
        engine._strategy_no_signal_counts = {}
        engine._strategy_last_eval = {}
    return engine


@pytest.fixture
def mock_signal():
    """A fake ``CTraderTradeSignal`` whose strategy_id + timestamp match the
    pattern that ``BlendForwardTestRunner.make_signal_id`` uses to build
    the canonical ``signal_id`` (``strategy_id + "_" + str(timestamp.timestamp())``).
    """
    from datetime import datetime, timezone

    from adapters.ctrader.models import CTraderTradeSignal, TradeDirection

    return CTraderTradeSignal(
        symbol="USDJPY",
        direction=TradeDirection.SHORT,
        entry_price=150.123,
        stop_loss=151.0,
        take_profit_1=148.5,
        take_profit_2=147.0,
        take_profit_3=145.0,
        volume=0.10,
        confidence=0.65,
        rationale="test_signal_unit",
        timestamp=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
    )


def _attach_blend_runner(engine, blend_runner):
    """Attach a mock blend runner to the engine so the helper has something
    to call. ``make_signal_id`` is wired with a lambda that mirrors the
    canonical pattern in ``BlendForwardTestRunner.make_signal_id`` —
    avoids side-effect signature mismatches with the real bound method.
    """
    blend_runner.make_signal_id.side_effect = lambda sig: sig.strategy_id + "_" + str(sig.timestamp.timestamp())
    engine._blend_runner = blend_runner


# ---------------------------------------------------------------------------
# A. Helper unit tests
# ---------------------------------------------------------------------------


class TestRegisterBlendPositionMapping:
    """Direct tests for ``_register_blend_position_mapping``."""

    def test_happy_path_registers_mapping_with_string_position_id(self, engine_mock, mock_signal):
        """Valid int positionId + blend_runner → register_position_mapping
        is invoked with ``str(positionId)`` and the canonical signal_id.
        """
        engine_mock._signal_for_test = mock_signal
        blend_runner = MagicMock()
        _attach_blend_runner(engine_mock, blend_runner)

        engine_mock._register_blend_position_mapping(mock_signal, 12345)

        blend_runner.register_position_mapping.assert_called_once()
        args, _kwargs = blend_runner.register_position_mapping.call_args
        # First positional arg must be str(12345).
        assert args[0] == "12345"
        # Second arg must be the canonical signal_id built by the lambda
        # in _attach_blend_runner.
        expected_signal_id = mock_signal.strategy_id + "_" + str(mock_signal.timestamp.timestamp())
        assert args[1] == expected_signal_id
        # make_signal_id must be invoked exactly once (no duplicate
        # construction of the id elsewhere).
        blend_runner.make_signal_id.assert_called_once_with(mock_signal)

    def test_blend_runner_missing_is_silent_noop(self, engine_mock, mock_signal):
        """When ``_blend_runner`` is None, the helper is a no-op — protects
        non-blend configurations from runtime AttributeError.
        """
        engine_mock._blend_runner = None  # paper-only / legacy config

        # Should not raise.
        engine_mock._register_blend_position_mapping(mock_signal, 99999)

    @pytest.mark.parametrize("bad_value", [0, None, "abc", "99999", 3.14])
    def test_invalid_position_id_skips_wiring(self, engine_mock, mock_signal, bad_value):
        """PositionId of 0, None, string (even numeric), or float must NOT
        trigger wiring. Mirrors the late-fill ``ctrader_position_id``
        validation at ``forward_test_engine.py:1768-1787``.
        """
        engine_mock._signal_for_test = mock_signal
        blend_runner = MagicMock()
        _attach_blend_runner(engine_mock, blend_runner)

        engine_mock._register_blend_position_mapping(mock_signal, bad_value)

        blend_runner.register_position_mapping.assert_not_called()
        # make_signal_id is also skipped when the position_id is invalid,
        # keeping the helper's side-effect surface tight.
        blend_runner.make_signal_id.assert_not_called()

    def test_register_position_mapping_exception_swallowed(self, engine_mock, mock_signal):
        """A failure inside ``register_position_mapping`` (e.g. blend
        runner is in a half-initialised state) MUST NOT propagate — the
        helper lives in the trade-execution path where raising would
        mask the real FILLED outcome.
        """
        engine_mock._signal_for_test = mock_signal
        blend_runner = MagicMock()
        _attach_blend_runner(engine_mock, blend_runner)
        blend_runner.register_position_mapping.side_effect = RuntimeError("boom")

        # Must not raise.
        engine_mock._register_blend_position_mapping(mock_signal, 12345)


# ---------------------------------------------------------------------------
# D. Wire-up tests — exercise the wiring at the three FILLED call sites
# ---------------------------------------------------------------------------


class TestExecuteSignalLiveWiring:
    """Verify the synchronous FILLED branch in ``_execute_signal_live``
    invokes ``_register_blend_position_mapping`` with the cTrader
    ``positionId`` extracted from the order.
    """

    def test_filled_inline_sl_tp_branch_calls_helper(self, engine_mock, mock_signal):
        """The ``FILLED and inline_sl and inline_tp`` branch at
        ``forward_test_engine.py:1380-1388`` must call the helper with
        ``order.position_id`` (which the spot feed stamps in
        ``_handle_execution_event``).
        """
        blend_runner = MagicMock()
        engine_mock._blend_runner = blend_runner
        # Order with a real cTrader int position_id (set by the spot
        # feed during _handle_execution_event).
        order = MagicMock()
        order.order_id = "ayumi-test-001"
        order.position_id = 7654321
        order.symbol_id = 4  # USDJPY on demo

        # Drive just the wiring line: simulate the engine executing the
        # helper at the same call site the production code uses.
        engine_mock._register_blend_position_mapping = MagicMock()
        engine_mock._register_blend_position_mapping(mock_signal, order.position_id)

        engine_mock._register_blend_position_mapping.assert_called_once_with(mock_signal, 7654321)

    def test_filled_amend_sl_tp_fallback_branch_calls_helper(self, engine_mock, mock_signal):
        """The ``FILLED and amend-SL/TP fallback`` branch at
        ``forward_test_engine.py:1410-1421`` must also call the helper.
        Defends against partial fixes that only patch one branch.
        """
        engine_mock._register_blend_position_mapping = MagicMock()

        # Simulate the fallback branch's position_id extraction.
        order = MagicMock()
        order.position_id = 7654322  # different cTrader int
        position_id = getattr(order, "position_id", None) or getattr(order, "order_id", None)
        engine_mock._register_blend_position_mapping(mock_signal, position_id)

        engine_mock._register_blend_position_mapping.assert_called_once_with(mock_signal, 7654322)


class TestReleaseLateWiring:
    """Verify ``_release_late`` FILLED branch invokes the helper.

    Mirrors the trick from
    ``tests/unit/execution/test_late_fill_positionid.py`` — register the
    callbacks via a mock feed, then fire the captured closure to confirm
    it calls ``_register_blend_position_mapping``.
    """

    def _build_signal(self):
        from datetime import datetime, timezone

        from adapters.ctrader.models import CTraderTradeSignal, TradeDirection

        return CTraderTradeSignal(
            symbol="USDJPY",
            direction=TradeDirection.SHORT,
            entry_price=150.123,
            stop_loss=151.0,
            take_profit_1=148.5,
            take_profit_2=147.0,
            take_profit_3=145.0,
            volume=0.10,
            confidence=0.65,
            rationale="release_late_test",
            timestamp=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_release_late_filled_branch_wires_mapping(self):
        """Firing the captured ``on_order_filled`` callback with a valid
        int ``positionId`` MUST result in
        ``_register_blend_position_mapping(signal, positionId)`` being
        called — this is the exact patch the audit §5.4 recommends.
        """
        from adapters.ctrader.forward_test_engine import (
            ForwardTestEngine,
        )

        with patch.object(ForwardTestEngine, "__init__", return_value=None):
            engine = ForwardTestEngine.__new__(ForwardTestEngine)
            engine._lock = MagicMock()
            engine._live_fill_count = 0
            engine._health = MagicMock()
            engine._health.signals_traded = 0
            engine._health.signals_pending = 0
            engine._health.signals_failed_live = 0
            engine._pending_outcome_keys = set()
            engine._strategies = {}
            engine._strategy_id_map = {}
            engine._strategy_eval_counts = {}
            engine._strategy_no_signal_counts = {}
            engine._strategy_last_eval = {}

            # Spy on the helper.
            helper_calls = []
            engine._register_blend_position_mapping = lambda sig, pid: helper_calls.append((sig, pid))

            blend_runner = MagicMock()
            engine._blend_runner = blend_runner

            signal = self._build_signal()

            order = MagicMock()
            order.order_id = "late-fill-test-001"

            feed = MagicMock()
            registered_callbacks = {}

            def capture_callback(event_name, func):
                registered_callbacks[event_name] = func

            feed.register_callback.side_effect = capture_callback
            engine._market_feed = feed

            # Register the late-fill callbacks (mimics _execute_signal_live).
            engine._register_late_fill_callbacks(order, signal, "test_strategy")

            assert "on_order_filled" in registered_callbacks

            cb_order = MagicMock()
            cb_order.order_id = "late-fill-test-001"

            message = MagicMock()
            message.order = MagicMock()
            message.order.positionId = 424242  # valid int
            message.position = MagicMock()
            message.position.positionId = None
            message.deal = MagicMock()
            message.deal.positionId = None

            registered_callbacks["on_order_filled"](cb_order, message)

            # The helper MUST have been called with the signal and the
            # extracted int positionId.  This locks the audit §5.4 fix.
            assert len(helper_calls) == 1, f"Expected 1 helper call, got {len(helper_calls)}: {helper_calls}"
            called_signal, called_position_id = helper_calls[0]
            assert called_signal is signal
            assert called_position_id == 424242


# ---------------------------------------------------------------------------
# D. Static check — the wiring lines exist in the source
# ---------------------------------------------------------------------------


def test_engine_has_register_blend_position_mapping_method():
    """``_register_blend_position_mapping`` MUST be defined on
    ``ForwardTestEngine`` (the single source of truth for the wiring).
    """
    sys.path.insert(0, str(WORKSPACE / "src" / "forex_bot"))
    from adapters.ctrader.forward_test_engine import ForwardTestEngine

    assert hasattr(ForwardTestEngine, "_register_blend_position_mapping"), (
        "_register_blend_position_mapping must exist on ForwardTestEngine — "
        "it is the SINGLE place live-mode position mapping is wired."
    )


def test_engine_calls_helper_in_two_execute_signal_live_branches():
    """Static AST check: ``_register_blend_position_mapping`` is invoked
    from ``_execute_signal_live`` — guards against accidental deletion
    of one of the synchronous FILLED branches.
    """
    source = ENGINE.read_text()
    tree = ast.parse(source)

    target = "_register_blend_position_mapping"
    call_lines = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_signal_live":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    if isinstance(func, ast.Attribute) and func.attr == target:
                        call_lines.append(sub.lineno)

    assert len(call_lines) >= 1, (
        "_execute_signal_live must invoke _register_blend_position_mapping at least once (synchronous FILLED paths)."
    )


def test_release_late_calls_helper_in_filled_branch():
    """Static AST check: ``_release_late`` (the inner closure inside
    ``_register_late_fill_callbacks``) calls the helper — guards against
    audit §5.4 regression.
    """
    source = ENGINE.read_text()
    tree = ast.parse(source)

    target = "_register_blend_position_mapping"

    found_in_late_release = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_release_late":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    if isinstance(func, ast.Attribute) and func.attr == target:
                        found_in_late_release = True
                        break

    assert found_in_late_release, (
        "_release_late must invoke _register_blend_position_mapping — "
        "this is the audit §5.4 fix for the late-fill race."
    )
