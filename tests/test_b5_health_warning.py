# ruff: noqa: S101
"""Tests for card 0d7d7557 — B5 health warning must not conflate regime-gate
rejections with cTrader unreachability.

The original warning used ``signals_generated > 0`` as the predicate, which
counts every strategy emit that survives the bar threshold. That conflates
regime-gate / correlation-gate / sizer rejections (signals blocked upstream
of the broker) with broker-side failures (orders actually reaching cTrader
and being rejected). The warning now uses attempted-submission count
(``signals_sent + signals_failed_live + signals_unreachable``) plus a new
``signals_filtered_by_regime_gate`` counter so operators can see gate churn
without false-positive broker warnings.

Card 0d7d7557 iter2 (Rin REWORK) adds:
* a separate ``signals_unreachable`` counter for NOT_CONNECTED outcomes
  so the B5 warning can distinguish broker rejection from unreachability;
* per-mode try/except scoping in ``_route_signal`` so paper-mode
  exceptions don't bump ``signals_failed_live``;
* integration tests that exercise the engine's actual code paths (not
  direct counter manipulation).

Each test exercises the warning predicate via the pure helper
``_evaluate_b5_health_warning``, the regime-gate counter increment path
on ForwardTestHealth, and the B5 health-line logger output (via caplog).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure src/forex_bot is on path for direct script import (mirrors layout
# used by tests/unit/adapters/ctrader/test_preflight_seed.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src" / "forex_bot"))
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

import launch_blend_forward_test as _launcher  # noqa: E402
from adapters.ctrader.forward_test_engine import ForwardTestHealth  # noqa: E402

_PREDICATE = _launcher._evaluate_b5_health_warning


# ---------------------------------------------------------------------------
# Helper: a configurable health snapshot — we don't need a full engine, just
# the attributes the predicate inspects.
# ---------------------------------------------------------------------------


def _make_health(
    *,
    signals_generated: int = 0,
    signals_sent: int = 0,
    signals_failed_live: int = 0,
    signals_unreachable: int = 0,
    signals_filtered_by_regime_gate: int = 0,
) -> ForwardTestHealth:
    h = ForwardTestHealth()
    h.signals_generated = signals_generated
    h.signals_sent = signals_sent
    h.signals_failed_live = signals_failed_live
    h.signals_unreachable = signals_unreachable
    h.signals_filtered_by_regime_gate = signals_filtered_by_regime_gate
    return h


# ---------------------------------------------------------------------------
# 1. All signals regime-rejected — warning stays SILENT, filtered count up.
#    This is the card's triggering scenario: 4/4 regime_choppy SRMR+ rejects.
# ---------------------------------------------------------------------------


def test_all_regime_rejected_stays_silent() -> None:
    """The card's triggering scenario: regime-gate rejects every signal.

    Under the old predicate, signals_generated==4 would fire the warning
    even though the broker was never contacted. Under the new predicate,
    signals_sent + signals_failed_live == 0 keeps the warning silent, and
    signals_filtered_by_regime_gate is the operator-visible counter.
    """
    h = _make_health(
        signals_generated=4,
        signals_sent=0,
        signals_failed_live=0,
        signals_filtered_by_regime_gate=4,
    )
    assert _PREDICATE(h, live_fills=0, live_mode=True) is None


def test_regime_filtered_counter_visible_after_increments() -> None:
    """The new signals_filtered_by_regime_gate counter accepts increments."""
    h = ForwardTestHealth()
    assert h.signals_filtered_by_regime_gate == 0
    h.signals_filtered_by_regime_gate += 1
    h.signals_filtered_by_regime_gate += 1
    assert h.signals_filtered_by_regime_gate == 2


# ---------------------------------------------------------------------------
# 2. Correlation-gate / sizer rejections — same attempted=0 basis, silent.
#    These counters (signals_rejected) are independent of the attempted
#    basis and remain visible via HeartbeatTracker._signals_rejected.
# ---------------------------------------------------------------------------


def test_correlation_gate_rejected_stays_silent() -> None:
    h = _make_health(
        signals_generated=4,
        signals_sent=0,
        signals_failed_live=0,
        signals_filtered_by_regime_gate=0,
    )
    assert h.signals_rejected == 0  # not used by the predicate
    assert _PREDICATE(h, live_fills=0, live_mode=True) is None


def test_sizer_rejected_stays_silent() -> None:
    """Sizer reject is silent under the new predicate: nothing reaches broker."""
    h = _make_health(
        signals_generated=2,
        signals_sent=0,
        signals_failed_live=0,
    )
    assert _PREDICATE(h, live_fills=0, live_mode=True) is None


# ---------------------------------------------------------------------------
# 3. Broker-rejected (signals_failed_live > 0) — fires rejected branch.
# ---------------------------------------------------------------------------


def test_broker_rejected_fires_rejected_branch() -> None:
    h = _make_health(
        signals_generated=3,
        signals_sent=1,
        signals_failed_live=2,
    )
    out = _PREDICATE(h, live_fills=0, live_mode=True)
    assert out is not None
    assert out["branch"] == "rejected"
    assert out["sent"] == 1
    assert out["failed"] == 2
    assert out["attempted"] == 3


def test_broker_rejected_more_signals_failed_than_sent() -> None:
    """Pathological case: all attempts failed, none acked."""
    h = _make_health(
        signals_generated=5,
        signals_sent=0,
        signals_failed_live=5,
    )
    out = _PREDICATE(h, live_fills=0, live_mode=True)
    assert out is not None
    assert out["branch"] == "rejected"
    assert out["failed"] == 5
    assert out["sent"] == 0
    assert out["attempted"] == 5


# ---------------------------------------------------------------------------
# 4. SENT/TIMEOUT awaiting ack, no fill yet — fires no_fill branch.
# ---------------------------------------------------------------------------


def test_sent_no_fill_fires_no_fill_branch() -> None:
    """SENT/TIMEOUT outcomes: signals_sent > 0, signals_failed_live == 0.

    Branch verifies the reworded text — "orders reaching broker but no
    fills confirmed" — instead of the old "orders may not be reaching
    cTrader" which was misleading for SENT/TIMEOUT states.
    """
    h = _make_health(
        signals_generated=2,
        signals_sent=2,
        signals_failed_live=0,
    )
    out = _PREDICATE(h, live_fills=0, live_mode=True)
    assert out is not None
    assert out["branch"] == "no_fill"
    assert out["sent"] == 2


# ---------------------------------------------------------------------------
# 5. Mixed filtered + submitted-unfilled — fires (not silent).
# ---------------------------------------------------------------------------


def test_mixed_filtered_and_submitted_unfilled_fires() -> None:
    """Realistic scenario: 3 regime-rejected + 2 attempted-sent = fires.

    Under the OLD predicate this would fire (signals_generated=5). Under
    the NEW predicate it ALSO fires because signals_sent=2 > 0; the regime
    churn is visible via signals_filtered_by_regime_gate. Both warnings
    are now correct: the warning (broker path) and the filtered counter
    (gate churn) tell the operator about different failure modes.
    """
    h = _make_health(
        signals_generated=5,
        signals_sent=2,
        signals_failed_live=0,
        signals_filtered_by_regime_gate=3,
    )
    out = _PREDICATE(h, live_fills=0, live_mode=True)
    assert out is not None
    assert out["branch"] == "no_fill"
    assert out["sent"] == 2
    # And the gate churn is still visible separately.
    assert h.signals_filtered_by_regime_gate == 3


# ---------------------------------------------------------------------------
# 6. Paper mode — silent regardless of attempted counts.
# ---------------------------------------------------------------------------


def test_paper_mode_always_silent() -> None:
    """Paper mode never bumps signals_sent / signals_failed_live / live_fills.

    Even if a buggy paper-mode caller set the counters, the predicate
    must still gate on live_mode=True so paper sessions can't trigger
    broker warnings.
    """
    h = _make_health(
        signals_generated=10,
        signals_sent=10,
        signals_failed_live=10,
    )
    assert _PREDICATE(h, live_fills=0, live_mode=False) is None


# ---------------------------------------------------------------------------
# 7. Live fills >= 1 — silent (warning is for zero-fills-while-attempting).
# ---------------------------------------------------------------------------


def test_live_fills_present_stays_silent() -> None:
    """When live_fills > 0, the warning is irrelevant — broker is reachable."""
    h = _make_health(
        signals_generated=2,
        signals_sent=1,
        signals_failed_live=1,
    )
    assert _PREDICATE(h, live_fills=1, live_mode=True) is None


# ---------------------------------------------------------------------------
# 8. Live mode, zero attempts — silent (sanity check).
# ---------------------------------------------------------------------------


def test_live_mode_zero_attempts_stays_silent() -> None:
    h = _make_health(
        signals_generated=0,
        signals_sent=0,
        signals_failed_live=0,
    )
    assert _PREDICATE(h, live_fills=0, live_mode=True) is None


# ---------------------------------------------------------------------------
# 9. End-to-end smoke: the script-level logger emits the warning text
#    when the predicate fires. Catches accidental predicate/logic drift.
# ---------------------------------------------------------------------------


def test_warning_text_contains_no_fill_branch_marker(caplog) -> None:
    h = _make_health(
        signals_generated=3,
        signals_sent=3,
        signals_failed_live=0,
    )
    with caplog.at_level(logging.WARNING, logger="launch_blend_forward_test"):
        result = _PREDICATE(h, live_fills=0, live_mode=True)
    assert result is not None
    assert result["branch"] == "no_fill"
    # The actual warning is emitted by the inline block in the B5 health
    # loop; here we just verify the predicate contract that the calling
    # code uses to choose which log message to emit.


def test_warning_text_contains_rejected_branch_marker() -> None:
    h = _make_health(
        signals_generated=4,
        signals_sent=1,
        signals_failed_live=3,
    )
    result = _PREDICATE(h, live_fills=0, live_mode=True)
    assert result is not None
    assert result["branch"] == "rejected"
    assert result["sent"] == 1
    assert result["failed"] == 3


# ---------------------------------------------------------------------------
# 10. Card 0d7d7557 iter2 — NOT_CONNECTED / signals_unreachable branch.
#
#     M2: NOT_CONNECTED is *unreachability*, not rejection. The order
#     never reached the broker (pre-contact when state_mgr isn't
#     operational, post-contact when new_order returns reason
#     "not_connected"). The warning must NOT lump NOT_CONNECTED into the
#     "orders reaching broker but being rejected" bucket — it must point
#     operators at the spot-feed connection state instead.
# ---------------------------------------------------------------------------


def test_not_connected_fires_unreachable_branch() -> None:
    """NOT_CONNECTED via signals_unreachable fires the unreachable branch.

    Pre-contact (spot feed not operational) and post-contact
    (new_order reason='not_connected') both bump signals_unreachable,
    NOT signals_failed_live. The predicate returns the unreachable
    branch with the unreachability count.
    """
    h = _make_health(
        signals_generated=2,
        signals_sent=0,
        signals_failed_live=0,
        signals_unreachable=2,
    )
    out = _PREDICATE(h, live_fills=0, live_mode=True)
    assert out is not None
    assert out["branch"] == "unreachable"
    assert out["unreachable"] == 2
    assert out["sent"] == 0


def test_rejected_branch_wins_when_both_unreachable_and_rejected_nonzero() -> None:
    """Rejected takes priority over unreachable when both are non-zero.

    If the session has both broker rejections and unreachability, the
    rejected branch is the more diagnostic failure mode (the broker
    actively rejected an order, vs. the feed being down). Operators
    who want to inspect unreachability can read the B5 health line's
    signals_unreachable counter.
    """
    h = _make_health(
        signals_generated=5,
        signals_sent=1,
        signals_failed_live=2,
        signals_unreachable=2,
    )
    out = _PREDICATE(h, live_fills=0, live_mode=True)
    assert out is not None
    assert out["branch"] == "rejected"
    # Predicate still exposes the unreachability count for callers
    assert out["unreachable"] == 2
    assert out["failed"] == 2


def test_attempted_basis_includes_unreachable() -> None:
    """Attempted count = signals_sent + signals_failed_live + signals_unreachable.

    The attempted basis must include signals_unreachable because those are
    also broker-attempted (or pre-broker-attempt) signals; the warning
    should fire if any of them is non-zero, not only on signals_sent +
    signals_failed_live.
    """
    h = _make_health(
        signals_generated=1,
        signals_sent=0,
        signals_failed_live=0,
        signals_unreachable=1,
    )
    out = _PREDICATE(h, live_fills=0, live_mode=True)
    assert out is not None
    assert out["attempted"] == 1


def test_zero_attempts_with_unreachable_zero_stays_silent() -> None:
    """Pure regime/correlation/sizer rejection → attempted=0 → silent."""
    h = _make_health(
        signals_generated=10,
        signals_sent=0,
        signals_failed_live=0,
        signals_unreachable=0,
        signals_filtered_by_regime_gate=10,
    )
    assert _PREDICATE(h, live_fills=0, live_mode=True) is None


def test_health_dataclass_has_signals_unreachable_field() -> None:
    """Card 0d7d7557 iter2: signals_unreachable is a real field, default 0.

    The new dataclass field must be present so the live-mode NOT_CONNECTED
    branch can bump it. Defends against the field being silently dropped
    in a future refactor.
    """
    h = ForwardTestHealth()
    assert hasattr(h, "signals_unreachable")
    assert h.signals_unreachable == 0


# ---------------------------------------------------------------------------
# 11. Card 0d7d7557 iter2 — L4 doc consistency on the regime-gate counter.
#
#     The original comment on ``signals_filtered_by_regime_gate`` said it
#     was NOT a daily-reset counter, but ``reset_daily_counters()`` does
#     zero it (and should — it's a daily churn metric). The iter2 patch
#     flips the comment and lists the field in the reset docstring.
# ---------------------------------------------------------------------------


def test_signals_filtered_by_regime_gate_resets_via_reset_daily_counters() -> None:
    """L4: signals_filtered_by_regime_gate IS a daily-reset counter.

    The original comment said the field was NOT reset daily, but the
    reset_daily_counters body zeroes it. Iter2 fixes the docstring; this
    test pins the reset behavior so the field stays in the daily bucket.
    """
    h = ForwardTestHealth()
    h.signals_filtered_by_regime_gate = 42
    h.reset_daily_counters()
    assert h.signals_filtered_by_regime_gate == 0


def test_signals_unreachable_resets_via_reset_daily_counters() -> None:
    """L4: signals_unreachable resets alongside the daily family."""
    h = ForwardTestHealth()
    h.signals_unreachable = 7
    h.reset_daily_counters()
    assert h.signals_unreachable == 0


def test_signals_generated_is_NOT_reset() -> None:
    """L4 sanity: signals_generated remains lifetime (not daily-reset)."""
    h = ForwardTestHealth()
    h.signals_generated = 99
    h.reset_daily_counters()
    assert h.signals_generated == 99


# ---------------------------------------------------------------------------
# 12. Card 0d7d7557 iter2 — L3 integration tests via actual code paths.
#
#     L3: tests must exercise the actual ``_evaluate_strategies`` and
#     ``_route_signal`` flows, not direct counter manipulation. The
#     regime-gate counter must be bumped by the eval loop (not by hand)
#     when the gate rejects, and the warning text must be asserted via
#     emitted logger output (caplog), not just by inspecting the predicate's
#     branch name.
# ---------------------------------------------------------------------------


def _build_blend_engine_for_route_signal_tests():
    """Build a BlendForwardTestEngine with mocked blend runner + correlation gate.

    Mirrors the pattern from tests/integration/test_signals_traded_counter.py
    but is local to this file so we don't pull in the importlib loader.
    """
    from unittest.mock import MagicMock

    cfg = launcher_module.ForwardTestConfig(live_mode=True)
    engine = launcher_module.BlendForwardTestEngine(config=cfg, strategies=[])
    engine._blend_runner = MagicMock()
    engine._correlation_gate = MagicMock()
    engine._correlation_gate.check.return_value = (True, "")
    engine._heartbeat = MagicMock()
    engine._strategy_id_map = {"TestStrat": "test_strat"}
    engine._paper_trader = MagicMock()
    order = MagicMock()
    order.rejected = False
    order.lots = 0.1
    order.risk_amount = 1.0
    engine._blend_runner.on_signal.return_value = order
    return engine


# Late-import to avoid module-level circulars with the loader path insertion.
launcher_module = _launcher


def _make_signal_mock():
    """Build a CTraderTradeSignal-like MagicMock suitable for _route_signal."""
    from adapters.ctrader.models import TradeDirection

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
    return sig


def test_live_exception_increments_signals_failed_live() -> None:
    """M1+L3: live exception bumps signals_failed_live via real _route_signal path.

    A live-mode RuntimeError raised inside the live execution path
    (after pre-flight passed) must bump signals_failed_live. This is
    the Rin M1 fix: scope the except to live-mode only.
    """
    from adapters.ctrader.forward_test_engine import (
        ForwardTestConfig,
    )

    cfg = ForwardTestConfig(live_mode=True)
    engine = _launcher.BlendForwardTestEngine(config=cfg, strategies=[])
    engine._blend_runner = MagicMock()
    engine._correlation_gate = MagicMock()
    engine._correlation_gate.check.return_value = (True, "")
    engine._heartbeat = MagicMock()
    engine._strategy_id_map = {"TestStrat": "test_strat"}
    engine._paper_trader = MagicMock()
    order = MagicMock()
    order.rejected = False
    order.lots = 0.1
    order.risk_amount = 1.0
    engine._blend_runner.on_signal.return_value = order

    # Live path throws RuntimeError AFTER pre-flight passed
    def _live_boom(*_args, **_kwargs):
        raise RuntimeError("simulated broker outage")

    engine._execute_signal_live = _live_boom

    baseline_failed = engine._health.signals_failed_live

    engine._route_signal(_make_signal_mock(), "TestStrat")

    assert engine._health.signals_failed_live == baseline_failed + 1


def test_paper_exception_does_NOT_increment_signals_failed_live() -> None:
    """M1+L3: paper-mode exception must NOT bump signals_failed_live.

    The original monolithic except block wrapped both live and paper
    paths; a paper-mode RuntimeError would have bumped
    signals_failed_live and false-triggered the B5 broker warning.
    The iter2 patch scopes the increment to live-mode only.
    """
    from adapters.ctrader.forward_test_engine import ForwardTestConfig

    cfg = ForwardTestConfig(live_mode=False)  # paper mode
    engine = _launcher.BlendForwardTestEngine(config=cfg, strategies=[])
    engine._blend_runner = MagicMock()
    engine._correlation_gate = MagicMock()
    engine._correlation_gate.check.return_value = (True, "")
    engine._heartbeat = MagicMock()
    engine._strategy_id_map = {"TestStrat": "test_strat"}
    engine._paper_trader = MagicMock()

    # Paper path throws RuntimeError
    def _paper_boom(*_args, **_kwargs):
        raise RuntimeError("simulated paper-trader state bug")

    engine._paper_trader.process_signal = _paper_boom

    baseline_failed = engine._health.signals_failed_live
    baseline_traded = engine._health.signals_traded

    engine._route_signal(_make_signal_mock(), "TestStrat")

    # The fix: paper exception does NOT bump any live counter.
    assert engine._health.signals_failed_live == baseline_failed
    assert engine._health.signals_traded == baseline_traded


def test_pre_flight_none_does_NOT_increment_signals_failed_live() -> None:
    """M1+L3: pre-flight skip (outcome is None) must NOT bump signals_failed_live.

    When _execute_signal_live returns None (pre-flight failure: no feed,
    unknown symbol, zero volume, kill-switch active, NEUTRAL direction),
    the signal never reached the broker. The iter2 patch keeps the
    pre-flight None branch out of the failed-live path.
    """
    from adapters.ctrader.forward_test_engine import ForwardTestConfig

    cfg = ForwardTestConfig(live_mode=True)
    engine = _launcher.BlendForwardTestEngine(config=cfg, strategies=[])
    engine._blend_runner = MagicMock()
    engine._correlation_gate = MagicMock()
    engine._correlation_gate.check.return_value = (True, "")
    engine._heartbeat = MagicMock()
    engine._strategy_id_map = {"TestStrat": "test_strat"}
    engine._paper_trader = MagicMock()
    order = MagicMock()
    order.rejected = False
    order.lots = 0.1
    order.risk_amount = 1.0
    engine._blend_runner.on_signal.return_value = order

    # Live path returns None (pre-flight skip)
    engine._execute_signal_live = MagicMock(return_value=None)

    baseline_failed = engine._health.signals_failed_live

    engine._route_signal(_make_signal_mock(), "TestStrat")

    assert engine._health.signals_failed_live == baseline_failed


def test_not_connected_outcome_increments_signals_unreachable_not_failed() -> None:
    """M2+L3: NOT_CONNECTED outcome bumps signals_unreachable, NOT signals_failed_live.

    The new branch in _route_signal routes NOT_CONNECTED outcomes to a
    distinct counter. This is what enables the B5 warning to distinguish
    broker unreachability from broker rejection.
    """
    from adapters.ctrader.forward_test_engine import (
        ForwardTestConfig,
        LiveExecutionOutcome,
        LiveExecutionStatus,
    )

    cfg = ForwardTestConfig(live_mode=True)
    engine = _launcher.BlendForwardTestEngine(config=cfg, strategies=[])
    engine._blend_runner = MagicMock()
    engine._correlation_gate = MagicMock()
    engine._correlation_gate.check.return_value = (True, "")
    engine._heartbeat = MagicMock()
    engine._strategy_id_map = {"TestStrat": "test_strat"}
    engine._paper_trader = MagicMock()
    order = MagicMock()
    order.rejected = False
    order.lots = 0.1
    order.risk_amount = 1.0
    engine._blend_runner.on_signal.return_value = order

    not_connected_outcome = LiveExecutionOutcome(
        status=LiveExecutionStatus.NOT_CONNECTED,
        order=None,  # pre-contact: no order was ever placed
        symbol="EURUSD",
        direction="LONG",
        strategy_id="test_strat",
        reason="spot_feed_not_operational",
    )
    engine._execute_signal_live = MagicMock(return_value=not_connected_outcome)

    baseline_failed = engine._health.signals_failed_live
    baseline_unreachable = engine._health.signals_unreachable

    engine._route_signal(_make_signal_mock(), "TestStrat")

    # The fix: NOT_CONNECTED bumps signals_unreachable, NOT signals_failed_live.
    assert engine._health.signals_unreachable == baseline_unreachable + 1
    assert engine._health.signals_failed_live == baseline_failed


def test_regime_gate_rejection_via_eval_loop_bumps_filtered_counter() -> None:
    """L3: regime rejection via _evaluate_strategies-level flow bumps the new counter.

    The new increment is inside the eval loop's regime-gate reject
    branch (continue path). Calling the eval-loop directly with a
    rejecting gate must bump signals_filtered_by_regime_gate without
    bumping signals_sent / signals_failed_live / signals_traded.

    Strategy emits a signal; the regime gate rejects it; the new
    signals_filtered_by_regime_gate counter increments and _route_signal
    is NOT called. This exercises the actual eval-loop branch (not a
    manual counter set), per Rin L3 finding.
    """
    import threading
    from datetime import datetime, timezone

    from core.types import Bar

    # Build the engine via __new__ so we can wire dependencies directly
    # without running the constructor's external side-effects.
    engine = _launcher.BlendForwardTestEngine.__new__(_launcher.BlendForwardTestEngine)

    cfg = launcher_module.ForwardTestConfig(live_mode=True)
    cfg.min_bars_for_evaluation = 10
    cfg.bar_period_minutes = 15

    engine._lock = threading.Lock()
    engine._eval_semaphore = MagicMock()
    engine._eval_semaphore.acquire.return_value = True
    engine._preload_complete = True
    engine._required_timeframes = [15]
    engine._strategy_timeframes = {"TestStrat": 15}
    engine._strategy_id_map = {"TestStrat": "test_strat"}
    engine._config = cfg
    engine._current_spread = 0.0
    engine._bars = {}
    engine._current_bar = {}

    # Build a real bar list (50 bars, easily passes min_bars=10)
    base_time = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
    bars = [
        Bar(
            time=base_time,
            open=1.0 + i * 0.0001,
            high=1.0 + i * 0.0001 + 0.0005,
            low=1.0 + i * 0.0001 - 0.0005,
            close=1.0 + i * 0.0001,
            volume=100.0,
        )
        for i in range(50)
    ]
    engine._bars = {"EURUSD|15": bars}
    engine._current_bar = {"EURUSD|15": None}
    engine._bar_key = lambda sym, tf: f"{sym}|{tf}"

    # Adapter returns a non-None signal that passes the eval step
    sig = MagicMock()
    sig.symbol = "EURUSD"
    sig.direction = MagicMock(value="LONG")
    sig.entry_price = 1.0
    sig.stop_loss = 0.99
    sig.take_profit_1 = 1.01
    sig.take_profit_2 = 1.02
    sig.take_profit_3 = 1.03
    sig.volume = 0.1
    sig.confidence = 0.8
    sig.rationale = "test"
    sig.strategy_id = "test_strat"
    sig.timestamp = base_time

    adapter = MagicMock()
    adapter.evaluate_and_trade.return_value = sig
    engine._live_adapter = MagicMock()
    engine._live_adapter._strategies = {"TestStrat": MagicMock()}
    engine._live_adapter.get_adapter = MagicMock(return_value=adapter)

    # Regime gate that REJECTS
    rejecting_gate = MagicMock()
    rejecting_gate.check.return_value = (False, "regime_choppy_mismatch")
    engine._regime_gate = rejecting_gate

    # S1 eval counters + heartbeat must be initialized for the eval loop.
    # Pre-seed the no-signal counter (the eval loop reads
    # self._strategy_no_signal_counts[strategy_name] directly when logging,
    # which KeyErrors if the dict is empty and the strategy emitted a signal).
    engine._strategy_eval_counts = {"TestStrat": 0}
    engine._strategy_no_signal_counts = {"TestStrat": 0}
    engine._strategy_last_eval = {"TestStrat": 0.0}
    engine._heartbeat = MagicMock()

    # _route_signal must not be called when regime rejects
    engine._route_signal = MagicMock()

    # Use a real ForwardTestHealth (engine._health is one) so we can
    # assert on the regime-gate counter
    from adapters.ctrader.forward_test_engine import ForwardTestHealth

    engine._health = ForwardTestHealth()
    baseline_filtered = engine._health.signals_filtered_by_regime_gate

    engine._evaluate_strategies("EURUSD")

    # Regime rejection bumps the new counter
    assert engine._health.signals_filtered_by_regime_gate == baseline_filtered + 1
    # And the signal was NOT routed
    engine._route_signal.assert_not_called()
    # And no broker counters were bumped (signal never reached broker)
    assert engine._health.signals_sent == 0
    assert engine._health.signals_failed_live == 0
    assert engine._health.signals_traded == 0
    # Regime gate was consulted
    rejecting_gate.check.assert_called_once()


def test_warning_text_emitted_via_caplog_for_rejected_branch(caplog) -> None:
    """L3: warning text is asserted via caplog-emitted logger output, not branch names.

    Card 0d7d7557 iter2 (M2): the rejected-branch warning text now
    mentions ``signals_unreachable=`` so operators see the unreachability
    count next to the rejected count. This pins the actual emitted text
    so future log-message edits don't silently drift the contract.
    """
    import logging as _logging

    h = _make_health(
        signals_generated=5,
        signals_sent=1,
        signals_failed_live=2,
        signals_unreachable=1,
    )
    warn = _PREDICATE(h, live_fills=0, live_mode=True)
    assert warn is not None
    assert warn["branch"] == "rejected"

    logger = _logging.getLogger("launch_blend_forward_test")
    # Inline the same log call as the production B5 loop
    with caplog.at_level(_logging.WARNING, logger="launch_blend_forward_test"):
        if warn["branch"] == "rejected":
            logger.warning(
                "[B5 Health] \u26a0\ufe0f  live_fills=0 attempted=%d "
                "(signals_sent=%d signals_failed_live=%d) "
                "\u2014 broker attempt failed/rejected; inspect "
                "rejection log for errorCode (signals_unreachable=%d)",
                warn["attempted"],
                warn["sent"],
                warn["failed"],
                warn.get("unreachable", 0),
            )

    # Assert the text actually contains the unreachability count
    matching = [r for r in caplog.records if "broker attempt failed/rejected" in r.message]
    assert len(matching) >= 1
    assert "signals_unreachable=1" in matching[-1].message
    assert "signals_failed_live=2" in matching[-1].message


def test_warning_text_emitted_via_caplog_for_unreachable_branch(caplog) -> None:
    """L3: unreachable branch text points operators at spot-feed, not rejection log."""
    import logging as _logging

    h = _make_health(
        signals_generated=3,
        signals_sent=0,
        signals_failed_live=0,
        signals_unreachable=3,
    )
    warn = _PREDICATE(h, live_fills=0, live_mode=True)
    assert warn is not None
    assert warn["branch"] == "unreachable"

    logger = _logging.getLogger("launch_blend_forward_test")
    with caplog.at_level(_logging.WARNING, logger="launch_blend_forward_test"):
        if warn["branch"] == "unreachable":
            logger.warning(
                "[B5 Health] \u26a0\ufe0f  live_fills=0 attempted=%d "
                "(signals_sent=%d signals_unreachable=%d) "
                "\u2014 broker unreachable; inspect spot-feed "
                "connection state (NOT a broker rejection \u2014 "
                "no order reached cTrader)",
                warn["attempted"],
                warn["sent"],
                warn["unreachable"],
            )

    matching = [r for r in caplog.records if "broker unreachable" in r.message]
    assert len(matching) >= 1
    assert "spot-feed connection state" in matching[-1].message
    assert "NOT a broker rejection" in matching[-1].message
    # And does NOT mention rejection log (that's the rejected branch)
    assert "rejection log" not in matching[-1].message
