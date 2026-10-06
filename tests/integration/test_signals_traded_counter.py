"""Tests for T2 — defer ``signals_traded`` counter until execution confirmed.

The launcher's ``_route_signal`` no longer bumps ``signals_traded`` on
"blend runner accepted"; it bumps it only after the order reaches the
broker with a FILLED outcome (or after a paper-mode ``process_signal``
returns success).  Three new counters distinguish the live-mode states:

* ``signals_accepted`` — blend runner accepted the signal
* ``signals_sent``     — order sent to cTrader, awaiting ack (live only)
* ``signals_pending``  — orders in flight (live only)
* ``signals_failed_live`` — REJECTED / TIMEOUT / NOT_CONNECTED / CANCELLED
* ``signals_traded``   — backwards-compatible alias for FILLED count

These tests cover the launcher-side branch only.  Per Rei's amendment
E7 the base-class ``_evaluate_strategies`` is dead code in the launcher
path and is intentionally left alone.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from adapters.ctrader.forward_test_engine import (
    ForwardTestConfig,
    ForwardTestHealth,
    LiveExecutionOutcome,
    LiveExecutionStatus,
)
from adapters.ctrader.models import TradeDirection


# We can't easily import the launcher file because it touches pid_guard,
# logging setup, etc. — too much side-effect for a unit test.  Instead we
# import the BlendForwardTestEngine class via the same module-level
# loader the launcher uses.
def _load_launcher_module():
    """Load scripts/launch_blend_forward_test.py without running main()."""
    import importlib.util
    import sys

    from _project_root import PROJECT_ROOT

    path = str(PROJECT_ROOT / "scripts" / "launch_blend_forward_test.py")
    spec = importlib.util.spec_from_file_location("launch_blend_forward_test", path)
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module. The launcher uses
    # ``from __future__ import annotations``, so its frozen dataclasses defer
    # type resolution and later ask sys.modules for their defining module's
    # namespace (dataclasses ``_is_type``). Unregistered, that lookup yields
    # None and the class body raises
    # ``AttributeError: 'NoneType' object has no attribute '__dict__'``
    # at import time on Python 3.12.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


launcher = _load_launcher_module()


def _build_blend_engine_with_route_signal_mock():
    """Construct a BlendForwardTestEngine and stub out the live execution path.

    Returns (engine, captured_outcome) where ``captured_outcome`` is what
    the engine's mocked ``_execute_signal_live`` will return.
    """
    cfg = ForwardTestConfig(live_mode=True)
    engine = launcher.BlendForwardTestEngine(config=cfg, strategies=[])
    # Provide the minimum attributes _route_signal touches
    engine._blend_runner = MagicMock()
    engine._correlation_gate = MagicMock()
    engine._correlation_gate.check.return_value = (True, "")
    engine._heartbeat = MagicMock()
    engine._strategy_id_map = {"TestStrat": "test_strat"}
    engine._paper_trader = MagicMock()
    # Stub blend runner order output
    order = MagicMock()
    order.rejected = False
    order.lots = 0.1
    order.risk_amount = 1.0
    engine._blend_runner.on_signal.return_value = order
    # Default: outcome will be injected by each test
    return engine


class TestSignalsTradedCounter:
    def test_signals_traded_does_not_increment_when_live_execution_returns_none(self):
        """If _execute_signal_live returns None, signals_traded stays put."""
        engine = _build_blend_engine_with_route_signal_mock()
        engine._execute_signal_live = MagicMock(return_value=None)

        sig = MagicMock()
        sig.symbol = "EURUSD"
        sig.direction = TradeDirection.LONG
        sig.entry_price = 1.1000
        sig.stop_loss = 1.0950
        sig.take_profit_1 = 1.1050
        sig.take_profit_2 = 1.1100
        sig.take_profit_3 = 1.1150
        sig.volume = 0.1
        sig.confidence = 0.8
        sig.rationale = "test"
        sig.strategy_id = "test_strat"

        baseline_traded = engine._health.signals_traded

        engine._route_signal(sig, "TestStrat")

        assert engine._health.signals_traded == baseline_traded
        # Blend runner should have its risk released + correlation gate released.
        # Production uses the two-arg cancel_risk(signal_id, risk_amount)
        # contract (see scripts/launch_blend_forward_test.py) and releases the
        # gate via release_pending(symbol, strategy_id) -- strategy_id resolves
        # through _strategy_id_map, so "TestStrat" maps to "test_strat".
        engine._blend_runner.cancel_risk.assert_called_once_with(
            engine._blend_runner.make_signal_id.return_value,
            engine._blend_runner.on_signal.return_value.risk_amount,
        )
        engine._correlation_gate.release_pending.assert_called_once_with(
            "EURUSD", "test_strat"
        )

    def test_signals_traded_increments_on_filled_outcome(self):
        """If _execute_signal_live returns FILLED, signals_traded += 1."""
        engine = _build_blend_engine_with_route_signal_mock()
        filled_outcome = LiveExecutionOutcome(
            status=LiveExecutionStatus.FILLED,
            order=MagicMock(order_id="ord_filled"),
            symbol="EURUSD",
            direction="LONG",
            strategy_id="test_strat",
        )
        engine._execute_signal_live = MagicMock(return_value=filled_outcome)

        sig = MagicMock()
        sig.symbol = "EURUSD"
        sig.direction = TradeDirection.LONG
        sig.entry_price = 1.1000
        sig.stop_loss = 1.0950
        sig.take_profit_1 = 1.1050
        sig.take_profit_2 = 1.1100
        sig.take_profit_3 = 1.1150
        sig.volume = 0.1
        sig.confidence = 0.8
        sig.rationale = "test"
        sig.strategy_id = "test_strat"

        baseline_traded = engine._health.signals_traded

        engine._route_signal(sig, "TestStrat")

        assert engine._health.signals_traded == baseline_traded + 1
        assert engine._live_fill_count == 1
        # Risk should NOT be released on fill
        engine._blend_runner.cancel_risk.assert_not_called()
        engine._correlation_gate.release.assert_not_called()

    def test_signals_traded_does_not_increment_on_sent_outcome(self):
        """If _execute_signal_live returns SENT, signals_traded stays put; signals_sent += 1."""
        engine = _build_blend_engine_with_route_signal_mock()
        sent_outcome = LiveExecutionOutcome(
            status=LiveExecutionStatus.SENT,
            order=MagicMock(order_id="ord_sent"),
            symbol="EURUSD",
            direction="LONG",
            strategy_id="test_strat",
        )
        engine._execute_signal_live = MagicMock(return_value=sent_outcome)

        sig = MagicMock()
        sig.symbol = "EURUSD"
        sig.direction = TradeDirection.LONG
        sig.entry_price = 1.1000
        sig.stop_loss = 1.0950
        sig.take_profit_1 = 1.1050
        sig.take_profit_2 = 1.1100
        sig.take_profit_3 = 1.1150
        sig.volume = 0.1
        sig.confidence = 0.8
        sig.rationale = "test"
        sig.strategy_id = "test_strat"

        engine._route_signal(sig, "TestStrat")

        assert engine._health.signals_sent == 1
        assert engine._health.signals_pending == 1
        assert getattr(engine, "_live_fill_count", 0) == 0
        # SENT does not yet release correlation gate (the broker has the order)
        engine._correlation_gate.release.assert_not_called()

    def test_signals_traded_does_not_increment_on_rejected_or_timeout(self):
        """FILTERED fail states don't bump the success counter; signals_failed_live += 1."""
        engine = _build_blend_engine_with_route_signal_mock()
        rej_outcome = LiveExecutionOutcome(
            status=LiveExecutionStatus.REJECTED,
            order=MagicMock(order_id="ord_rej"),
            symbol="EURUSD",
            direction="LONG",
            strategy_id="test_strat",
            reason="INVALID_PRICE",
        )
        engine._execute_signal_live = MagicMock(return_value=rej_outcome)

        sig = MagicMock()
        sig.symbol = "EURUSD"
        sig.direction = TradeDirection.LONG
        sig.entry_price = 1.1000
        sig.stop_loss = 1.0950
        sig.take_profit_1 = 1.1050
        sig.take_profit_2 = 1.1100
        sig.take_profit_3 = 1.1150
        sig.volume = 0.1
        sig.confidence = 0.8
        sig.rationale = "test"
        sig.strategy_id = "test_strat"

        baseline_traded = engine._health.signals_traded
        baseline_failed = engine._health.signals_failed_live

        engine._route_signal(sig, "TestStrat")

        assert engine._health.signals_traded == baseline_traded
        assert engine._health.signals_failed_live == baseline_failed + 1
        # Risk should be cancelled, correlation gate released -- same two-arg
        # cancel_risk(signal_id, risk_amount) + release_pending(symbol,
        # strategy_id) production contract as the None-outcome case above.
        engine._blend_runner.cancel_risk.assert_called_once_with(
            engine._blend_runner.make_signal_id.return_value,
            engine._blend_runner.on_signal.return_value.risk_amount,
        )
        engine._correlation_gate.release_pending.assert_called_once_with(
            "EURUSD", "test_strat"
        )

    def test_paper_path_still_increments_signals_traded_on_success(self):
        """In paper mode, signals_traded increments only when exec_result.success is True."""
        engine = _build_blend_engine_with_route_signal_mock()
        # Flip live_mode off
        engine._config.live_mode = False

        exec_result_success = MagicMock()
        exec_result_success.success = True
        engine._paper_trader.process_signal.return_value = exec_result_success

        sig = MagicMock()
        sig.symbol = "EURUSD"
        sig.direction = TradeDirection.LONG
        sig.entry_price = 1.1000
        sig.stop_loss = 1.0950
        sig.take_profit_1 = 1.1050
        sig.take_profit_2 = 1.1100
        sig.take_profit_3 = 1.1150
        sig.volume = 0.1
        sig.confidence = 0.8
        sig.rationale = "test"
        sig.strategy_id = "test_strat"

        baseline_traded = engine._health.signals_traded

        engine._route_signal(sig, "TestStrat")

        assert engine._health.signals_traded == baseline_traded + 1

    def test_health_dataclass_has_new_counter_fields(self):
        """ForwardTestHealth exposes the new counters as dataclass fields."""
        h = ForwardTestHealth()
        assert h.signals_sent == 0
        assert h.signals_failed_live == 0
        assert h.signals_pending == 0
        assert h.signals_cancelled == 0
        assert h.signals_accepted == 0
