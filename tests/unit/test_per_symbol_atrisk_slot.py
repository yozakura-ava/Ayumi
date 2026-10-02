"""Unit tests for the per-symbol at-risk slot policy (card 4083ac2d-...).

Background
----------
Card 4083ac2d-4a9e-4713-b5bf-33002d7b4f32 (Craig, 2026-09-08 14:35 EDT):
replace the single-slot-per-(symbol, direction) cap in ``CorrelationGate``
with **max 3 concurrent AT-RISK positions per symbol**. Risk-free positions
(SL >= entry for LONG, SL <= entry for SHORT) are EXEMPT.

The at-risk definition (verbatim):
    LONG  at-risk iff current SL <  entry.
    SHORT at-risk iff current SL >  entry.
    Risk-free iff LONG: SL >= entry  OR  SHORT: SL <= entry.

Acceptance criteria covered here:
  AC1 — 4th entry on a symbol with 3 at-risk positions -> rejected (LONG+SHORT).
  AC1 — Same entry accepted when one position's SL moves to breakeven/in-profit.
  AC2 — Slot-release event fires when SL transitions at-risk -> risk-free.
  AC3 — Per-symbol at-risk cap invariant (at_risk_count(symbol) never > 3).

These tests do NOT touch SL trailing itself. They drive the gate directly
and inspect ``at_risk_count``, ``slot_release_log``, and ``check`` return
values.
"""

from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE = Path("$AYUMI_ROOT")
LAUNCHER = WORKSPACE / "scripts" / "launch_blend_forward_test.py"

sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(WORKSPACE / "src" / "forex_bot"))

from launch_blend_forward_test import CorrelationGate  # noqa: E402  # isort: skip


# ── AC1: 4th entry rejected, accepted after SL→breakeven ─────────────────────


def test_lon_fourth_entry_rejected_with_clear_reason():
    """4th LONG on XAUUSD with 3 at-risk positions is rejected with a
    clear ``symbol_atrisk_cap`` reason (LONG case).
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    for i in range(3):
        ok, reason = gate.check(sym, "LONG", f"strat_{i}")
        assert ok, f"entry #{i + 1} should pass: {reason}"
        # Simulate fill with entry=2000, sl=1990 -> at-risk (LONG: 1990 < 2000).
        gate.attach_position(
            symbol=sym,
            strategy_id=f"strat_{i}",
            position_id=f"pos_{i}",
            direction="LONG",
            entry_price=2000.0,
            sl=1990.0,
        )

    # 4th entry must be rejected.
    ok, reason = gate.check(sym, "LONG", "strat_3")
    assert not ok, "4th LONG on at-risk-saturated symbol must be rejected"
    assert "symbol_atrisk_cap" in reason, f"reason should mention cap: {reason!r}"
    assert sym in reason, f"reason should mention symbol: {reason!r}"
    assert "3" in reason, f"reason should mention current count: {reason!r}"
    assert "max 3" in reason or "(max 3)" in reason or "max 3" in reason.lower(), (
        f"reason should mention cap of 3: {reason!r}"
    )

    # Invariant: at-risk count never exceeded 3.
    assert gate.at_risk_count(sym) == 3
    assert gate.at_risk_count(sym) <= gate.MAX_ATRISK_PER_SYMBOL


def test_short_fourth_entry_rejected_with_clear_reason():
    """4th SHORT on XAUUSD with 3 at-risk positions is rejected (SHORT case).
    SHORT at-risk iff SL > entry.
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    for i in range(3):
        ok, reason = gate.check(sym, "SHORT", f"strat_{i}")
        assert ok, f"entry #{i + 1} should pass: {reason}"
        # SHORT at-risk: SL > entry.  entry=2000, sl=2010.
        gate.attach_position(
            symbol=sym,
            strategy_id=f"strat_{i}",
            position_id=f"pos_{i}",
            direction="SHORT",
            entry_price=2000.0,
            sl=2010.0,
        )

    ok, reason = gate.check(sym, "SHORT", "strat_3")
    assert not ok, "4th SHORT on at-risk-saturated symbol must be rejected"
    assert "symbol_atrisk_cap" in reason, f"reason should mention cap: {reason!r}"
    assert gate.at_risk_count(sym) == 3


def test_lon_accepted_after_one_position_sl_breakeven():
    """After one position's SL moves to breakeven (LONG: SL == entry), the
    same 4th entry that was previously rejected is now accepted (LONG).
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    for i in range(3):
        ok, _ = gate.check(sym, "LONG", f"strat_{i}")
        assert ok
        gate.attach_position(
            symbol=sym,
            strategy_id=f"strat_{i}",
            position_id=f"pos_{i}",
            direction="LONG",
            entry_price=2000.0,
            sl=1990.0,
        )

    # Confirm 4th was rejected.
    ok_before, reason_before = gate.check(sym, "LONG", "strat_3")
    assert not ok_before, "sanity: 4th should be rejected when 3 are at-risk"

    # Release the 4th's pending reservation (since check() reserved it).
    gate.release_pending(sym, "strat_3")

    # Move one position's SL to breakeven (LONG: SL == entry -> risk-free).
    transitioned = gate.update_sl("pos_0", 2000.0)
    assert transitioned, "SL moving to breakeven should fire slot_release"

    assert gate.at_risk_count(sym) == 2, "one position now risk-free, 2 remain"

    # Same 4th entry should now be accepted.
    ok_after, reason_after = gate.check(sym, "LONG", "strat_3")
    assert ok_after, f"4th LONG should be accepted after slot release: {reason_after!r}"
    assert reason_after == "", f"empty reason on accept: {reason_after!r}"


def test_short_accepted_after_one_position_sl_breakeven():
    """SHORT case: after one position's SL moves to breakeven (SHORT: SL == entry),
    the previously-rejected 4th entry is accepted.
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    for i in range(3):
        ok, _ = gate.check(sym, "SHORT", f"strat_{i}")
        assert ok
        gate.attach_position(
            symbol=sym,
            strategy_id=f"strat_{i}",
            position_id=f"pos_{i}",
            direction="SHORT",
            entry_price=2000.0,
            sl=2010.0,  # SHORT at-risk: 2010 > 2000
        )

    ok_before, _ = gate.check(sym, "SHORT", "strat_3")
    assert not ok_before
    gate.release_pending(sym, "strat_3")

    # Move SL to breakeven (SHORT: SL == entry -> risk-free).
    transitioned = gate.update_sl("pos_0", 2000.0)
    assert transitioned

    assert gate.at_risk_count(sym) == 2

    ok_after, reason_after = gate.check(sym, "SHORT", "strat_3")
    assert ok_after, f"4th SHORT should be accepted: {reason_after!r}"


def test_lon_accepted_after_one_position_sl_in_profit():
    """After one position's SL moves into profit (LONG: SL > entry),
    the 4th entry is accepted.
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    for i in range(3):
        ok, _ = gate.check(sym, "LONG", f"strat_{i}")
        assert ok
        gate.attach_position(
            symbol=sym,
            strategy_id=f"strat_{i}",
            position_id=f"pos_{i}",
            direction="LONG",
            entry_price=2000.0,
            sl=1990.0,
        )

    gate.release_pending(sym, "strat_3")  # release from any prior failed check

    # First confirm 4th is rejected.
    ok_before, _ = gate.check(sym, "LONG", "strat_3")
    assert not ok_before
    gate.release_pending(sym, "strat_3")

    # Move SL into profit (LONG: 2010 > 2000 -> risk-free).
    transitioned = gate.update_sl("pos_0", 2010.0)
    assert transitioned, "LONG SL > entry should be risk-free (in-profit)"

    assert gate.at_risk_count(sym) == 2

    ok_after, reason_after = gate.check(sym, "LONG", "strat_3")
    assert ok_after, f"4th LONG should be accepted after in-profit SL: {reason_after!r}"


def test_short_accepted_after_one_position_sl_in_profit():
    """SHORT in-profit: SL < entry -> risk-free. 4th accepted.
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    for i in range(3):
        ok, _ = gate.check(sym, "SHORT", f"strat_{i}")
        assert ok
        gate.attach_position(
            symbol=sym,
            strategy_id=f"strat_{i}",
            position_id=f"pos_{i}",
            direction="SHORT",
            entry_price=2000.0,
            sl=2010.0,
        )

    # First check & reject the 4th.
    ok_before, _ = gate.check(sym, "SHORT", "strat_3")
    assert not ok_before
    gate.release_pending(sym, "strat_3")

    # SHORT in-profit: SL=1990 < entry=2000 -> risk-free.
    transitioned = gate.update_sl("pos_0", 1990.0)
    assert transitioned

    ok_after, reason_after = gate.check(sym, "SHORT", "strat_3")
    assert ok_after, f"4th SHORT should be accepted: {reason_after!r}"


# ── AC2: slot_release event fires on at-risk -> risk-free transition ─────────


def test_slot_release_event_fires_with_full_payload():
    """When SL transitions at-risk -> risk-free, a slot_release event fires
    with the expected payload fields.
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    ok, _ = gate.check(sym, "LONG", "strat_a")
    assert ok
    gate.attach_position(
        symbol=sym,
        strategy_id="strat_a",
        position_id="pos_a",
        direction="LONG",
        entry_price=2000.0,
        sl=1990.0,  # at-risk (LONG: 1990 < 2000)
    )

    transitioned = gate.update_sl("pos_a", 2000.0)  # -> breakeven
    assert transitioned

    log = gate.slot_release_log()
    assert len(log) == 1, f"expected 1 slot_release event, got {len(log)}: {log!r}"
    event = log[0]
    assert event["event"] == "slot_release"
    assert event["position_id"] == "pos_a"
    assert event["symbol"] == "XAUUSD"
    assert event["direction"] == "LONG"
    assert event["strategy_id"] == "strat_a"
    assert event["entry_price"] == 2000.0
    assert event["new_sl"] == 2000.0
    assert event["reason"] == "sl_to_breakeven_or_profit"
    assert "ts" in event and event["ts"]


def test_slot_release_callback_invoked():
    """Custom on_slot_release callback receives the event dict."""
    gate = CorrelationGate()
    captured: list[dict] = []
    gate.on_slot_release(lambda e: captured.append(e))

    ok, _ = gate.check("XAUUSD", "LONG", "s1")
    assert ok
    gate.attach_position(
        symbol="XAUUSD",
        strategy_id="s1",
        position_id="p1",
        direction="LONG",
        entry_price=2000.0,
        sl=1990.0,
    )
    gate.update_sl("p1", 2010.0)  # LONG in-profit -> risk-free

    assert len(captured) == 1
    assert captured[0]["position_id"] == "p1"
    assert captured[0]["direction"] == "LONG"


def test_slot_release_does_not_fire_when_still_at_risk():
    """An SL change that does NOT cross breakeven must not fire slot_release.
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    ok, _ = gate.check(sym, "LONG", "s1")
    assert ok
    gate.attach_position(
        symbol=sym,
        strategy_id="s1",
        position_id="p1",
        direction="LONG",
        entry_price=2000.0,
        sl=1990.0,  # at-risk
    )
    # SL still below entry (1995 < 2000) — still at-risk.
    transitioned = gate.update_sl("p1", 1995.0)
    assert not transitioned
    assert gate.slot_release_log() == []


def test_slot_release_does_not_fire_when_already_risk_free():
    """An SL change within the risk-free region must not fire slot_release
    (no transition occurred).
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    ok, _ = gate.check(sym, "LONG", "s1")
    assert ok
    gate.attach_position(
        symbol=sym,
        strategy_id="s1",
        position_id="p1",
        direction="LONG",
        entry_price=2000.0,
        sl=2000.0,  # already risk-free (breakeven)
    )
    assert gate.at_risk_count(sym) == 0, "SL == entry means risk-free"

    # Tighten SL further into profit — still risk-free.
    transitioned = gate.update_sl("p1", 2010.0)
    assert not transitioned, "no at-risk -> risk-free transition occurred"
    assert gate.slot_release_log() == []


# ── AC3: per-symbol at-risk invariant ────────────────────────────────────────


def test_per_symbol_atrisk_invariant_holds_across_sequence():
    """Drive a mixed sequence of LONG+SHORT entries on a symbol; at-risk
    count never exceeds 3.
    """
    gate = CorrelationGate()
    sym = "EURUSD"
    fills: list[tuple[str, str, float, float]] = []  # (pid, dir, entry, sl)
    pid = 0
    # Open positions alternating directions until rejected.
    while True:
        direction = "LONG" if pid % 2 == 0 else "SHORT"
        ok, reason = gate.check(sym, direction, f"s_{pid}")
        if not ok:
            # 4th should be rejected.
            assert "symbol_atrisk_cap" in reason
            break
        # Fill at-risk with direction-corrective SL.
        if direction == "LONG":
            entry, sl = 1.1000, 1.0990  # LONG at-risk: 1.0990 < 1.1000
        else:
            entry, sl = 1.1000, 1.1010  # SHORT at-risk: 1.1010 > 1.1000
        gate.attach_position(
            symbol=sym,
            strategy_id=f"s_{pid}",
            position_id=f"p_{pid}",
            direction=direction,
            entry_price=entry,
            sl=sl,
        )
        fills.append((f"p_{pid}", direction, entry, sl))
        pid += 1
        assert gate.at_risk_count(sym) <= gate.MAX_ATRISK_PER_SYMBOL, (
            f"invariant violated after {pid} fills"
        )

    # Should have stopped at 3 fills (3 at-risk positions max).
    assert len(fills) == 3, f"expected to stop at 3 at-risk, got {len(fills)}"

    # Now trail all 3 to breakeven and verify each fires slot_release.
    releases = 0
    for p, d, entry, _ in fills:
        if d == "LONG":
            ok = gate.update_sl(p, entry)  # breakeven
        else:
            ok = gate.update_sl(p, entry)  # breakeven
        if ok:
            releases += 1
    assert releases == 3, f"expected 3 slot_releases, got {releases}"
    assert gate.at_risk_count(sym) == 0

    # Now we should be able to enter 3 more at-risk positions.
    for i in range(3):
        direction = "LONG" if i % 2 == 0 else "SHORT"
        ok, reason = gate.check(sym, direction, f"second_{i}")
        assert ok, f"second-batch entry #{i + 1} should pass: {reason}"
        if direction == "LONG":
            entry, sl = 1.1000, 1.0990
        else:
            entry, sl = 1.1000, 1.1010
        gate.attach_position(
            symbol=sym,
            strategy_id=f"second_{i}",
            position_id=f"second_p_{i}",
            direction=direction,
            entry_price=entry,
            sl=sl,
        )
    assert gate.at_risk_count(sym) == 3


def test_release_position_frees_slot():
    """After ``release_position`` (position close), the slot is freed and
    the next entry is accepted.
    """
    gate = CorrelationGate()
    sym = "GBPUSD"
    for i in range(3):
        ok, _ = gate.check(sym, "LONG", f"s_{i}")
        assert ok
        gate.attach_position(
            symbol=sym,
            strategy_id=f"s_{i}",
            position_id=f"p_{i}",
            direction="LONG",
            entry_price=1.3000,
            sl=1.2990,
        )

    ok_before, _ = gate.check(sym, "LONG", "s_3")
    assert not ok_before
    gate.release_pending(sym, "s_3")

    # Close one position.
    gate.release_position("p_0")
    assert gate.at_risk_count(sym) == 2

    # Now the 4th entry should be accepted.
    ok_after, reason_after = gate.check(sym, "LONG", "s_3")
    assert ok_after, f"after close, 4th should pass: {reason_after!r}"


def test_pending_reservations_count_as_at_risk():
    """A pending reservation (not yet filled) counts as at-risk.  This is
    the conservative default and prevents overshoot when a signal is
    accepted but the trade hasn't filled yet.
    """
    gate = CorrelationGate()
    sym = "USDJPY"
    # 3 pending reservations (no fill).
    for i in range(3):
        ok, _ = gate.check(sym, "LONG", f"pending_{i}")
        assert ok
    # 4th rejected.
    ok, reason = gate.check(sym, "LONG", "pending_3")
    assert not ok
    assert "symbol_atrisk_cap" in reason


def test_release_pending_clears_unfilled_reservation():
    """``release_pending(symbol, strategy_id)`` drops only the matching
    pending reservation; other reservations remain.
    """
    gate = CorrelationGate()
    sym = "AUDUSD"
    gate.check(sym, "LONG", "keep")
    gate.check(sym, "LONG", "drop")

    assert gate.active_count == 2  # 2 pending slots
    gate.release_pending(sym, "drop")
    # Active count drops by 1 (the dropped pending slot).
    assert gate.active_count == 1


def test_release_legacy_does_not_clear_real_slots():
    """``release(symbol, direction)`` (no position_id) only clears PENDING
    reservations, never real (filled) slots.  This protects pre-existing
    positions from being cleared by a downstream signal reject.
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    # Open a real position (fills after attach_position).
    ok, _ = gate.check(sym, "LONG", "real")
    assert ok
    gate.attach_position(
        symbol=sym,
        strategy_id="real",
        position_id="real_pos",
        direction="LONG",
        entry_price=2000.0,
        sl=1990.0,
    )
    # Add a pending reservation.
    gate.check(sym, "LONG", "pending")

    # Legacy release — should clear only the pending, not the real slot.
    gate.release(sym, "LONG")
    # The real slot remains.
    assert gate.at_risk_count(sym) == 1
    # And we can still release the real slot by position_id.
    gate.release_position("real_pos")
    assert gate.at_risk_count(sym) == 0


# ── Regression: external API still works for downstream callers ──────────────


def test_active_count_back_compat():
    """The legacy ``active_count`` property returns total tracked slots.
    """
    gate = CorrelationGate()
    assert gate.active_count == 0
    gate.check("XAUUSD", "LONG", "a")
    assert gate.active_count == 1
    gate.check("XAUUSD", "SHORT", "b")
    assert gate.active_count == 2
    gate.release("XAUUSD", "LONG")
    # Pending LONG cleared; SHORRT remains.
    assert gate.active_count == 1


# ── REGRESSION (Rin rework iter2) ────────────────────────────────────────────
# Two findings addressed here:
#   HIGH  — production SL amendments now flow through gate.update_sl via the
#           slot_tracker callback wired into PositionMonitor._fire_ratchet.
#   MEDIUM — rejection-path release calls use strategy-specific
#           release_pending(symbol, strategy_id); legacy release() falls back
#           only on position close.


def test_slot_tracker_callback_fires_on_ratchet_to_breakeven():
    """HIGH (Rin iter2): PositionMonitor._fire_ratchet, after a successful
    broker amend, calls the slot_tracker with (position_id, new_sl).  The
    gate wired to that tracker fires slot_release exactly when SL crosses
    from at-risk to breakeven.

    This drives the real PositionMonitor._fire_ratchet with a stub
    market_feed that reports ``amend_sl_tp`` success, so we exercise the
    actual production path — not a parallel re-implementation.
    """
    from unittest.mock import MagicMock

    sys.path.insert(0, str(WORKSPACE / "src" / "forex_bot"))

    from adapters.ctrader.models import Position, PositionStatus, TradeDirection
    from adapters.ctrader.position_monitor import PositionMonitor

    gate = CorrelationGate()
    # Open a real LONG at-risk position via the canonical flow.
    ok, _ = gate.check("XAUUSD", "LONG", "kz_breakout")
    assert ok
    gate.attach_position(
        symbol="XAUUSD",
        strategy_id="kz_breakout",
        position_id="pos_real",
        direction="LONG",
        entry_price=2000.0,
        sl=1990.0,  # at-risk (LONG: 1990 < 2000)
    )
    assert gate.at_risk_count("XAUUSD") == 1

    # Construct a real PositionMonitor with the gate's update_sl as the
    # slot_tracker. Stub the order_manager + market_feed so the ratchet
    # path runs without an actual broker.
    monitor = PositionMonitor(
        order_manager=MagicMock(),
        slot_tracker=gate.update_sl,
        market_feed=MagicMock(),
    )
    monitor._market_feed.resolve_symbol_id.return_value = 12345
    monitor._market_feed.amend_sl_tp.return_value = True

    position = Position(
        position_id="pos_real",
        symbol="XAUUSD",
        direction=TradeDirection.LONG,
        volume=0.1,
        entry_price=2000.0,
        current_price=2005.0,
        stop_loss=1990.0,
        status=PositionStatus.OPEN,
    )

    # Simulate TP2 crossing — _fire_ratchet should move SL to breakeven
    # (entry) and call slot_tracker.  Returns TpRatchetAction with
    # amend_status='fired'.
    action = monitor._fire_ratchet(
        position,
        level=2,
        new_sl=2000.0,  # breakeven — LONG risk-free after this
        new_tp=2010.0,
        direction="LONG",
        entry_price=2000.0,
    )
    assert action.amend_status == "fired"
    assert position.stop_loss == 2000.0, "position.stop_loss must be updated to new SL"

    # The slot_tracker (gate.update_sl) should have fired slot_release
    # because the position transitioned at-risk -> risk-free.
    log = gate.slot_release_log()
    assert len(log) == 1
    assert log[0]["position_id"] == "pos_real"
    assert log[0]["new_sl"] == 2000.0
    assert log[0]["reason"] == "sl_to_breakeven_or_profit"
    assert gate.at_risk_count("XAUUSD") == 0, "slot freed on breakeven"


def test_slot_tracker_callback_not_called_on_amend_failure():
    """HIGH (Rin iter2): if the broker amend FAILS (returns False or
    raises), the slot_tracker must NOT be called — the position's SL is
    unchanged at the broker, so the gate's view should not change either.
    """
    from unittest.mock import MagicMock

    sys.path.insert(0, str(WORKSPACE / "src" / "forex_bot"))

    from adapters.ctrader.models import Position, PositionStatus, TradeDirection
    from adapters.ctrader.position_monitor import PositionMonitor

    gate = CorrelationGate()
    ok, _ = gate.check("XAUUSD", "LONG", "s1")
    assert ok
    gate.attach_position(
        symbol="XAUUSD",
        strategy_id="s1",
        position_id="pos_fail",
        direction="LONG",
        entry_price=2000.0,
        sl=1990.0,
    )
    initial_log_len = len(gate.slot_release_log())

    monitor = PositionMonitor(
        order_manager=MagicMock(),
        slot_tracker=gate.update_sl,
        market_feed=MagicMock(),
    )
    monitor._market_feed.resolve_symbol_id.return_value = 99
    monitor._market_feed.amend_sl_tp.return_value = False  # amend fails

    position = Position(
        position_id="pos_fail",
        symbol="XAUUSD",
        direction=TradeDirection.LONG,
        volume=0.1,
        entry_price=2000.0,
        current_price=2005.0,
        stop_loss=1990.0,
        status=PositionStatus.OPEN,
    )

    action = monitor._fire_ratchet(
        position,
        level=2,
        new_sl=2000.0,
        new_tp=2010.0,
        direction="LONG",
        entry_price=2000.0,
    )
    assert action.amend_status == "amend_failed"
    # No slot_release fired.
    assert len(gate.slot_release_log()) == initial_log_len
    # Gate still considers the position at-risk.
    assert gate.at_risk_count("XAUUSD") == 1


def test_slot_tracker_exception_does_not_break_ratchet():
    """HIGH (Rin iter2): if slot_tracker raises, the ratchet must still
    succeed (amend_status='fired', position.stop_loss updated) and the
    exception must be logged but swallowed.
    """
    from unittest.mock import MagicMock

    sys.path.insert(0, str(WORKSPACE / "src" / "forex_bot"))

    from adapters.ctrader.models import Position, PositionStatus, TradeDirection
    from adapters.ctrader.position_monitor import PositionMonitor

    def _explode(position_id: str, new_sl: float) -> None:
        raise RuntimeError("tracker exploded (test fixture)")

    monitor = PositionMonitor(
        order_manager=MagicMock(),
        slot_tracker=_explode,
        market_feed=MagicMock(),
    )
    monitor._market_feed.resolve_symbol_id.return_value = 7
    monitor._market_feed.amend_sl_tp.return_value = True

    position = Position(
        position_id="pos_x",
        symbol="XAUUSD",
        direction=TradeDirection.LONG,
        volume=0.1,
        entry_price=2000.0,
        current_price=2005.0,
        stop_loss=1990.0,
        status=PositionStatus.OPEN,
    )

    action = monitor._fire_ratchet(
        position,
        level=2,
        new_sl=2000.0,
        new_tp=2010.0,
        direction="LONG",
        entry_price=2000.0,
    )
    assert action.amend_status == "fired"
    assert position.stop_loss == 2000.0


def test_release_pending_only_clears_callers_reservation():
    """MEDIUM (Rin iter2): rejection path uses release_pending(symbol,
    strategy_id).  Strategy A's rejection must NOT remove Strategy B's
    pending reservation on the same symbol/direction.
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    # Two strategies hold pending reservations for the same symbol/direction.
    ok_a, _ = gate.check(sym, "LONG", "strat_a")
    ok_b, _ = gate.check(sym, "LONG", "strat_b")
    assert ok_a and ok_b
    assert gate.active_count == 2

    # Strategy A's signal is rejected — release only its pending reservation.
    gate.release_pending(sym, "strat_a")
    assert gate.active_count == 1, "B's reservation must remain after A's reject"

    # The remaining slot belongs to B — verify by direction/symbol counts.
    counts = gate.at_risk_counts()
    # Both pending reservations counted as at-risk (conservative default).
    # After A's release, only B remains.
    assert counts.get(sym, 0) == 1

    # Strategy B's reservation is still usable — can be promoted to a real slot.
    gate.attach_position(
        symbol=sym,
        strategy_id="strat_b",
        position_id="pos_b",
        direction="LONG",
        entry_price=2000.0,
        sl=1990.0,
    )
    assert gate.at_risk_count(sym) == 1
    assert gate.active_count == 1


def test_release_pending_no_op_when_no_matching_reservation():
    """MEDIUM (Rin iter2): release_pending with an unknown strategy_id is a
    no-op (does not raise, does not affect other reservations).
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    gate.check(sym, "LONG", "real_strat")
    assert gate.active_count == 1
    # No reservation for "unknown_strat" — should be a silent no-op.
    gate.release_pending(sym, "unknown_strat")
    assert gate.active_count == 1, "real_strat's reservation must remain"


def test_legacy_release_symbol_direction_drops_all_pending_for_pair():
    """Legacy release(symbol, direction) (used only in the on_position_closed
    fallback path now) drops ALL pending reservations for the (symbol,
    direction) pair AND cleans up orphan _pending entries.
    """
    gate = CorrelationGate()
    sym = "XAUUSD"
    gate.check(sym, "LONG", "strat_a")
    gate.check(sym, "LONG", "strat_b")
    gate.check(sym, "SHORT", "strat_c")
    assert gate.active_count == 3

    # Legacy release drops both LONG pending reservations but leaves SHORT.
    gate.release(sym, "LONG")
    assert gate.active_count == 1
    # Only the SHORT reservation remains.
    counts = gate.at_risk_counts()
    assert counts.get(sym, 0) == 1

    # Internal invariant: no orphan _pending entries whose slot is gone.
    # Verify by inspecting _pending dict directly.
    assert len(gate._pending) == 1, f"orphan _pending entries: {list(gate._pending.keys())}"


def test_blend_runner_reject_uses_strategy_specific_pending_cleanup():
    """MEDIUM (Rin iter2): end-to-end — BlendForwardTestEngine on blend
    reject must only clear the rejecting strategy's pending reservation,
    leaving other strategies' reservations on the same symbol/direction
    intact.

    This drives the real launcher source through BlendForwardTestEngine
    __new__ + _route_signal's blend-reject branch (line ~1218-1225) and
    asserts the gate ends up with one remaining pending reservation.
    """
    from launch_blend_forward_test import BlendForwardTestEngine  # noqa: I001

    engine = BlendForwardTestEngine.__new__(BlendForwardTestEngine)
    gate = CorrelationGate()
    engine._correlation_gate = gate
    engine._lock = MagicMock()  # type: ignore[assignment]
    engine._lock.__enter__ = lambda self: None
    engine._lock.__exit__ = lambda self, *args: None
    engine._health = MagicMock()
    engine._heartbeat = MagicMock()
    engine._strategy_id_map = {}

    # Pre-seed two strategies' pending reservations on XAUUSD LONG.
    gate.check("XAUUSD", "LONG", "strat_keep")
    gate.check("XAUUSD", "LONG", "strat_reject")
    assert gate.active_count == 2

    # Stub the blend_runner to reject the incoming signal so the
    # blend-reject branch fires.
    blend_runner = MagicMock()
    blend_runner.make_signal_id.side_effect = lambda signal: (
        signal.strategy_id + "_" + str(signal.timestamp.timestamp())
    )
    order = MagicMock()
    order.rejected = True
    order.rejection_reason = "blend_cap_reached"
    order.lots = 0.0
    order.risk_amount = 25.0
    blend_runner.on_signal.return_value = order
    engine._blend_runner = blend_runner

    # Build the signal with the rejecting strategy_id.
    from adapters.ctrader.signal_adapter import CTraderTradeSignal

    sig = CTraderTradeSignal(
        symbol="XAUUSD",
        direction="LONG",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=None,
        take_profit_3=None,
        volume=0.1,
        confidence=0.9,
        rationale="test",
        timestamp=datetime(2026, 9, 8, 14, 0, 0, tzinfo=timezone.utc),
        strategy_id="strat_reject",
    )

    # Reset mocks (record_signal will be called).
    engine._heartbeat.record_signal = MagicMock()

    engine._route_signal(sig, "strat_reject")

    # Only strat_reject's reservation should be gone; strat_keep's remains.
    assert gate.active_count == 1, (
        f"rejection must clear only caller's pending; got active_count={gate.active_count}"
    )
    # Verify via the at_risk_counts diagnostic that exactly 1 at-risk slot remains.
    assert gate.at_risk_counts().get("XAUUSD", 0) == 1


# ── Helpers / imports for the new tests ───────────────────────────────────────


from datetime import datetime, timezone  # noqa: E402  # isort: skip
from unittest.mock import MagicMock  # noqa: E402  # isort: skip

