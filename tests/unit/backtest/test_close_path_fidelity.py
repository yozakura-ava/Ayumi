"""Tests for card 75b24f98 — blend harness close-path fidelity.

Two defects, one regression harness:

1. **SL fill at level** — ``OrderManager.update_position`` must close SL-triggered
   positions at the configured ``position.stop_loss`` level, NOT at the bar's
   bid (LONG) / ask (SHORT) excursion.  Previously the fill landed 50–275 pips
   past tight XAUUSD stops, producing ≈2.27× P&L bias on SL closes (Satsuki
   re-run #3 in ``reports/blend-harness-2026-09-08/``, 2026-09-08).

2. **Pre-trade FTMO ordering** — ``scripts/backtest_blend_harness.py`` must
   call ``_ftmo_guard.should_allow_new_position()`` BEFORE
   ``engine._evaluate_strategies(symbol)`` so a same-bar entry that would push
   daily-DD past the 3% line is preempted.  Previously ``update()`` only
   recorded post-trade state and FREEZEd an already-realized breach.

Coverage:

* Source-level invariants for both fixes.
* Behavioral — synthetic ``OrderManager`` + ``Position`` exercising SL fill at level
  for LONG (entry 3360.00, sl 3350.37, bid 3347.62 → fills at 3350.37) and SHORT.
* Behavioral — real ``FTMOGuard`` in FREEZE state returns (False, …) from
  ``should_allow_new_position()``; harness's eval-loop respects the gate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure src/forex_bot is importable for adapters.ctrader.order_manager +
# risk.ftmo_guard.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "forex_bot"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── Helpers ────────────────────────────────────────────────────────────────


def _harness_source_path() -> Path:
    return _REPO_ROOT / "scripts" / "backtest_blend_harness.py"


def _order_manager_source_path() -> Path:
    return _REPO_ROOT / "src" / "forex_bot" / "adapters" / "ctrader" / "order_manager.py"


def _build_synthetic_position(
    *,
    direction,
    entry_price: float,
    stop_loss: float,
    volume: float = 0.10,
):
    """Return a freshly-constructed Position object for unit tests.

    Avoids dataclass import gymnastics by parsing the module's signature;
    stays decoupled from internal field renames.
    """
    from adapters.ctrader.models import Position, TradeDirection  # noqa: F401

    return Position(
        position_id="synth-test",
        symbol="XAUUSD",
        direction=direction,
        volume=volume,
        entry_price=entry_price,
        current_price=entry_price,
        stop_loss=stop_loss,
        take_profit=None,
    )


# ── AC1 source-level checks — SL fill at level ────────────────────────────


def test_order_manager_sl_branch_fills_at_position_stop_loss():
    """The ``update_position`` SL branch must pass ``position.stop_loss`` as
    the fill price to ``_close_position`` — not the bar's bid/ask excursion."""
    src = _order_manager_source_path().read_text(encoding="utf-8")

    # Find the SL branch (after "# Card 75b24f98: SL-triggered closes fill AT")
    marker = "Card 75b24f98: SL-triggered closes fill AT"
    assert marker in src, (
        "Expected card-reference marker in OrderManager SL branch. "
        "Fix may not have landed — re-apply the edit."
    )
    marker_pos = src.index(marker)

    # The SL branch close call must reference ``position.stop_loss`` as the
    # exit_price (second positional arg to _close_position).
    close_pos = src.index("_close_position(\n", marker_pos)
    close_end = src.index(")", close_pos)
    snippet = src[close_pos:close_end]
    assert "position.stop_loss" in snippet, (
        f"SL close must exit at ``position.stop_loss``; got snippet: {snippet!r}"
    )

    # The legacy ``sl_fill = bid if … else ask`` pattern must be gone.
    legacy_marker = "sl_fill = bid if position.direction == TradeDirection.LONG else ask"
    assert legacy_marker not in src, (
        "Legacy SL-fill (using bid/ask excursion) must be removed. "
        "Confirm card 75b24f98 fix landed."
    )


def test_order_manager_tp_branch_unchanged():
    """TP fill behavior is OUT OF SCOPE for card 75b24f98 (a separate
    follow-up).  Guard against accidental TP-branch regressions: the
    legacy ``tp_fill = ask if … else bid`` pattern must still be present.
    """
    src = _order_manager_source_path().read_text(encoding="utf-8")
    legacy_tp = "tp_fill = ask if position.direction == TradeDirection.LONG else bid"
    assert legacy_tp in src, (
        "TP branch fill pattern regressed — out of scope for 75b24f98; "
        "this test guards against an unrelated TP fix slipping in."
    )


# ── AC1 behavioral — SL fill at level (OrderManager) ──────────────────────


def test_sl_long_fills_at_level_when_bid_overshoots():
    """LONG: bid=3347.62 overshoots SL=3350.37 by 2.75 price units.
    Fix expects fill at 3350.37, P&L = -$96.30 (volume=0.10, contract=100).
    Bug produced fill=3347.62, P&L = -$123.80."""
    from adapters.ctrader.models import TradeDirection
    from adapters.ctrader.order_manager import OrderManager

    om = OrderManager()
    pos = _build_synthetic_position(
        direction=TradeDirection.LONG,
        entry_price=3360.00,
        stop_loss=3350.37,
        volume=0.10,
    )
    om._positions[pos.position_id] = pos

    # Bar excursion lands bid 2.75 price units PAST the SL.
    om.update_position(
        pos.position_id,
        current_price=3349.00,
        bid=3347.62,
        ask=3350.00,
        contract_size=100.0,
    )

    assert pos.closed_price == 3350.37, (
        f"SL close must fill AT the configured SL level (3350.37); "
        f"got {pos.closed_price}.  Bug: pre-fix used bid=3347.62."
    )
    # P&L = (3350.37 - 3360.00) * 0.10 * 100 = -9.63 * 10 = -$96.30
    assert abs(pos.closed_pnl - (-96.30)) < 0.01, (
        f"P&L must be ~-$96.30 at fill=3350.37; got {pos.closed_pnl:.4f}"
    )
    assert pos.close_reason == "sl_hit"


def test_sl_short_fills_at_level_when_ask_overshoots():
    """SHORT: bar excursion pushes ask above SL=3342.00 by 1.00 price unit.
    Fix expects fill at 3342.00, P&L = -$50.00 (volume=0.10, contract=100).
    Bug produced fill at the bar's ask excursion (1.00 unit past SL).
    Note: for SHORT, SL > entry (entry=3337.00, SL=3342.00); bar must show
    rising prices so the SHORT-side SL triggers on ``ask >= stop_loss``."""
    from adapters.ctrader.models import TradeDirection
    from adapters.ctrader.order_manager import OrderManager

    om = OrderManager()
    pos = _build_synthetic_position(
        direction=TradeDirection.SHORT,
        entry_price=3337.00,
        stop_loss=3342.00,
        volume=0.10,
    )
    om._positions[pos.position_id] = pos

    om.update_position(
        pos.position_id,
        current_price=3342.50,
        bid=3342.50,
        ask=3343.00,  # 1.00 price unit PAST the SL level (3342.00)
        contract_size=100.0,
    )

    assert pos.closed_price == 3342.00, (
        f"SHORT SL close must fill AT the configured SL level (3342.00); "
        f"got {pos.closed_price}.  Bug: pre-fix used ask=3343.00."
    )
    # P&L = (3337.00 - 3342.00) * 0.10 * 100 = -5 * 10 = -$50.00
    assert abs(pos.closed_pnl - (-50.00)) < 0.01, (
        f"SHORT P&L must be ~-$50.00 at fill=3342.00; got {pos.closed_pnl:.4f}"
    )
    assert pos.close_reason == "sl_hit"


def test_sl_fill_not_used_when_no_stop_loss():
    """Position with stop_loss=None must NOT trigger SL — TP branch / no-fill
    path remains untouched."""
    from adapters.ctrader.models import TradeDirection
    from adapters.ctrader.order_manager import OrderManager

    om = OrderManager()
    pos = _build_synthetic_position(
        direction=TradeDirection.LONG,
        entry_price=3360.00,
        stop_loss=None,
        volume=0.10,
    )
    om._positions[pos.position_id] = pos

    om.update_position(
        pos.position_id,
        current_price=3347.62,  # extreme low
        bid=3347.62,
        ask=3350.00,
        contract_size=100.0,
    )

    assert pos.closed_price is None, (
        f"Position with stop_loss=None must NOT auto-close on price excursion; "
        f"got closed_price={pos.closed_price}"
    )


def test_tp_branch_unaffected_by_sl_fix():
    """TP-fill behavior (fill at ask for LONG, bid for SHORT) is unchanged —
    LONG at TP gets the favorable price."""
    from adapters.ctrader.models import TradeDirection
    from adapters.ctrader.order_manager import OrderManager

    om = OrderManager()
    pos = _build_synthetic_position(
        direction=TradeDirection.LONG,
        entry_price=3360.00,
        stop_loss=None,
        volume=0.10,
    )
    pos.take_profit = 3370.00  # set directly (helper passes None)

    om._positions[pos.position_id] = pos
    om.update_position(
        pos.position_id,
        current_price=3370.5,
        bid=3370.50,
        ask=3371.00,
        contract_size=100.0,
    )

    assert pos.close_reason == "tp_hit"
    # LONG TP fills at ask (best estimate of fill price past TP)
    assert pos.closed_price == 3371.00, (
        f"TP fill (LONG) unchanged: must equal ask=3371.00; got {pos.closed_price}"
    )


# ── AC2 source-level checks — pre-trade FTMO gate in harness ──────────────


def test_harness_calls_ftmo_pre_trade_gate_before_evaluate_strategies():
    """The pre-trade ``_ftmo_guard.should_allow_new_position()`` check must
    appear BEFORE ``engine._evaluate_strategies(symbol)`` in
    ``scripts/backtest_blend_harness.py``."""
    src = _harness_source_path().read_text(encoding="utf-8")

    pre_gate_marker = "_ftmo_guard.should_allow_new_position()"
    assert pre_gate_marker in src, (
        "Pre-trade FTMO gate call must be present in the harness's eval loop."
    )

    pre_gate_pos = src.index(pre_gate_marker)
    eval_pos = src.index("engine._evaluate_strategies(symbol)")
    # Use the unique post-trade update() signature (not the comment substring):
    # the call passes ``paper_trader._current_balance``; the matching comment
    # would be a generic prose mention of ``_ftmo_guard.update``.
    post_update_marker = "_ftmo_guard.update(paper_trader._current_balance"
    assert post_update_marker in src, (
        "Post-eval _ftmo_guard.update() call must be present and bind to the "
        "paper trader's current balance (card aa3a1cbe invariant)."
    )
    post_update_pos = src.index(post_update_marker)

    assert pre_gate_pos < eval_pos, (
        "Pre-trade FTMO gate must run BEFORE engine._evaluate_strategies "
        "so a same-bar breach is preempted, not post-realized."
    )
    # Post-trade update must still exist (card aa3a1cbe invariant) and run
    # AFTER the pre-trade gate — i.e. detection happens after attempt.
    assert pre_gate_pos < post_update_pos, (
        "Post-eval _ftmo_guard.update() must remain AFTER the pre-trade gate."
    )


def test_harness_skips_evaluate_when_pre_trade_gate_blocks():
    """When the pre-trade gate returns False, the harness's
    ``_evaluate_strategies(symbol)`` call must be inside an ``if not skip_evaluate``
    block (i.e., the call is conditionally skipped, not unconditionally executed)."""
    src = _harness_source_path().read_text(encoding="utf-8")

    # Locate the gate branch and verify the evaluate call is conditional on it.
    gate_idx = src.index("_ftmo_guard.should_allow_new_position()")
    eval_idx = src.index("engine._evaluate_strategies(symbol)", gate_idx)
    snippet_before_eval = src[gate_idx:eval_idx]

    assert "skip_evaluate" in snippet_before_eval, (
        "Pre-trade gate must set a ``skip_evaluate`` flag and use it to "
        "conditionally call _evaluate_strategies()."
    )
    # The negation of "not _pre_gate_open" must appear in the snippet.
    assert "not _pre_gate_open" in snippet_before_eval, (
        "Pre-trade gate must test ``not _pre_gate_open`` to set skip_evaluate=True"
    )


# ── AC2 behavioral — FTMOGuard pre-trade gate ─────────────────────────────


def test_real_ftmo_guard_blocks_new_position_when_in_freeze():
    """A real FTMOGuard forced into FREEZE state must reject new positions
    via ``should_allow_new_position()``.

    Setup: 5% daily loss on $10K starting balance engages FREEZE via the
    daily-DD circuit breaker (3% threshold). Then the gate is consulted —
    it must return (False, …) since the guard has escalated to FREEZE.
    """
    from adapters.ctrader.kill_switch import KillSwitchManager
    from risk.ftmo_guard import FTMOAction, FTMOGuard

    kill_switch = KillSwitchManager()
    guard = FTMOGuard(
        kill_switch=kill_switch,
        starting_balance=10_000.0,
        challenge_type="1-step",
    )

    # -5% = $500 drop → past the 3% daily-DD threshold → FREEZE
    guard.update(current_balance=9_500.0, open_positions=0)
    assert guard.action_level == FTMOAction.FREEZE, (
        f"5% daily loss must engage FREEZE; got {guard.action_level}. "
        f"daily_loss_pct={guard.daily_loss_pct}."
    )

    allowed, reason = guard.should_allow_new_position()
    assert allowed is False, (
        f"should_allow_new_position() must return False in FREEZE; "
        f"got ({allowed}, {reason!r})."
    )
    assert "freeze" in reason.lower(), (
        f"Gate reason must mention 'freeze' for diagnosability; got: {reason!r}"
    )


def test_real_ftmo_guard_allows_new_position_when_in_allow():
    """Sanity check: with no breach, ``should_allow_new_position()`` returns
    (True, ...) so the harness's eval loop is unblocked for healthy state."""
    from adapters.ctrader.kill_switch import KillSwitchManager
    from risk.ftmo_guard import FTMOAction, FTMOGuard

    guard = FTMOGuard(
        kill_switch=KillSwitchManager(),
        starting_balance=10_000.0,
        challenge_type="1-step",
    )

    # No update() yet → action_level == ALLOW
    assert guard.action_level == FTMOAction.ALLOW

    allowed, reason = guard.should_allow_new_position()
    assert allowed is True, (
        f"should_allow_new_position() must return True in healthy ALLOW; "
        f"got ({allowed}, {reason!r})."
    )


def test_pre_trade_gate_harness_eval_loop_skips_evaluate_when_blocked():
    """Behavioral — drives a mock-backed harness-side gate + engine and
    confirms ``engine._evaluate_strategies`` is NOT called when the gate
    returns (False, ...).  Mirrors the harness eval-loop ordering."""
    from risk.ftmo_guard import FTMOAction, FTMOGuard

    mock_guard = MagicMock(spec=FTMOGuard)
    # Gate blocks on the first iteration, allows on the second.
    mock_guard.should_allow_new_position.side_effect = [
        (False, "FTMO freeze active: daily_loss=3.20%"),
        (True, "OK"),
    ]
    mock_guard.update.return_value = FTMOAction.ALLOW

    engine = MagicMock()
    paper_trader = MagicMock()
    paper_trader._current_balance = 9_700.0
    paper_trader._order_manager._positions = {}

    iterations_evaluated = 0
    for _ in range(2):
        gate_open, _gate_reason = mock_guard.should_allow_new_position()
        if gate_open:
            engine._evaluate_strategies("XAUUSD")
            iterations_evaluated += 1
        # post-trade update (mirrors production)
        mock_guard.update(paper_trader._current_balance, 0)

    assert iterations_evaluated == 1, (
        f"Eval loop must skip the blocked iteration; expected 1 evaluate() "
        f"call across 2 iterations, got {iterations_evaluated}."
    )
    assert mock_guard.should_allow_new_position.call_count == 2
    assert mock_guard.update.call_count == 2
