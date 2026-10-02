"""Tests for card aa3a1cbe — Blend harness FTMO guard integration (Bug 1 fix).

The harness drives ``engine._evaluate_strategies(symbol)`` directly per bar and
previously failed to call ``FTMOGuard.update()``, allowing one losing trade to
drain -$123,700 (12.37× loss) on a $10K starting balance with the 3% daily-DD
circuit breaker never engaging. These tests guard against regression of the
fix.

The fix instantiates ``FTMOGuard(kill_switch=engine._kill_switch, ...)`` after
``_build_components()`` and calls ``_ftmo_guard.update(balance, open_n)`` after
every ``_evaluate_strategies(symbol)``. On FREEZE/KILL the eval loop halts by
setting ``engine._running = False`` and breaking.

Coverage:

* Source-level invariants — FTMOGuard is imported at module level, construction
  is anchored after ``_build_components()``, ``update()`` is called once per
  iteration in the eval loop, and FREEZE/KILL triggers halt.
* Synthetic 5-iteration eval loop with ``MagicMock``-backed FTMOGuard —
  asserts update-call parity and halt semantics on FREEZE / KILL / ALLOW.
* Real ``FTMOGuard`` object integration — confirms a 5% daily loss engages
  FREEZE while a 2% loss remains ALLOW.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure src/forex_bot is importable for risk.ftmo_guard + adapters.ctrader
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "forex_bot"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── Helpers ────────────────────────────────────────────────────────────────


def _harness_source_path() -> Path:
    return _REPO_ROOT / "scripts" / "backtest_blend_harness.py"


def _run_harness_eval_iteration(
    engine: MagicMock,
    paper_trader: MagicMock,
    ftmo_guard,
) -> bool:
    """Replicate the per-iteration halt decision from run_backtest().

    Returns True if the loop should halt (FREEZE/KILL was returned).
    """
    # Trigger evaluation (mirroring the harness eval loop)
    engine._evaluate_strategies("XAUUSD")

    # Mirror the harness's halt-check verbatim
    open_n = len(paper_trader._order_manager._positions)
    _ftmo_action = ftmo_guard.update(paper_trader._current_balance, open_n)
    if _ftmo_action.value in ("freeze", "kill"):
        engine._running = False
        return True
    return False


# ── AC3 static source checks ───────────────────────────────────────────────


def test_harness_imports_ftmo_guard_at_top_level():
    """FTMOGuard must be imported at module level (not lazy/local)."""
    src = _harness_source_path().read_text(encoding="utf-8")
    assert "from risk.ftmo_guard import FTMOGuard" in src, (
        "FTMOGuard must be a top-level import so the guard instance is "
        "constructed once when run_backtest executes."
    )


def test_harness_constructs_ftmo_guard_after_build_components():
    """FTMOGuard must be built AFTER ``engine._build_components()`` and bound
    to ``engine._kill_switch`` with starting_balance=$10K, 1-step challenge,
    trailing DD enabled."""
    src = _harness_source_path().read_text(encoding="utf-8")

    bc_pos = src.index("engine._build_components()")
    ftmo_pos = src.index("_ftmo_guard = FTMOGuard(")
    assert bc_pos < ftmo_pos, "FTMOGuard must be built AFTER _build_components()"

    snippet = src[ftmo_pos : ftmo_pos + 600]
    assert "kill_switch=engine._kill_switch" in snippet
    assert "starting_balance=10_000.0" in snippet
    assert 'challenge_type="1-step"' in snippet
    assert "trailing_dd=True" in snippet


def test_harness_calls_ftmo_guard_update_in_eval_loop():
    """FTMOGuard.update() must run after ``_evaluate_strategies(symbol)`` and
    before the next ``bars_processed += 1`` increment — i.e. once per bar."""
    src = _harness_source_path().read_text(encoding="utf-8")

    eval_pos = src.index("engine._evaluate_strategies(symbol)")
    update_pos = src.index("_ftmo_guard.update(")
    bars_pos = src.index("bars_processed += 1")
    assert eval_pos < update_pos < bars_pos, (
        "FTMOGuard.update() must be called after _evaluate_strategies() and "
        "before bars_processed increment so every iteration is guarded."
    )


def test_harness_halts_on_freeze_or_kill_action():
    """FREEZE/KILL must flip ``engine._running = False`` and break the loop."""
    src = _harness_source_path().read_text(encoding="utf-8")
    assert 'if _ftmo_action.value in ("freeze", "kill"):' in src
    assert "engine._running = False" in src
    halt_block_idx = src.index('if _ftmo_action.value in ("freeze", "kill"):')
    snippet_after = src[halt_block_idx : halt_block_idx + 600]
    assert "break" in snippet_after


# ── AC3 behavioral test — synthetic 5-iteration loop ─────────────────────


def test_ftmo_guard_update_invoked_once_per_iteration():
    """Mock FTMOGuard returning ALLOW: 5-iteration loop calls update() exactly 5 times."""
    from risk.ftmo_guard import FTMOAction, FTMOGuard

    mock_guard = MagicMock(spec=FTMOGuard)
    mock_guard.update.return_value = FTMOAction.ALLOW

    paper_trader = MagicMock()
    paper_trader._current_balance = 10_000.0
    paper_trader._order_manager._positions = {"p1": object(), "p2": object()}
    engine = MagicMock()
    engine._running = True

    iterations = 5
    halted = False
    for _ in range(iterations):
        if _run_harness_eval_iteration(engine, paper_trader, mock_guard):
            halted = True
            break

    assert mock_guard.update.call_count == iterations, (
        f"Expected one update() per iteration ({iterations}), "
        f"got {mock_guard.update.call_count}."
    )
    assert engine._running is True, "Eval loop should still be running (ALLOW action)."
    assert halted is False
    # First call positional args: (balance, open_count)
    first_call_args = mock_guard.update.call_args_list[0].args
    assert first_call_args == (10_000.0, 2), (
        f"First update call should pass (balance=10000.0, open_n=2); "
        f"got {first_call_args}."
    )


def test_ftmo_guard_halt_on_freeze_breaks_loop_early():
    """Mock guard returning FREEZE on iteration 2: loop halts at iteration 2."""
    from risk.ftmo_guard import FTMOAction, FTMOGuard

    mock_guard = MagicMock(spec=FTMOGuard)
    # Iteration 1: ALLOW; Iteration 2+: FREEZE
    mock_guard.update.side_effect = [
        FTMOAction.ALLOW,
        FTMOAction.FREEZE,
        FTMOAction.FREEZE,
        FTMOAction.FREEZE,
        FTMOAction.FREEZE,
    ]

    paper_trader = MagicMock()
    paper_trader._current_balance = 9_700.0
    paper_trader._order_manager._positions = {"p1": object()}
    engine = MagicMock()
    engine._running = True

    iterations_run = 0
    halted = False
    for _ in range(5):
        iterations_run += 1
        if _run_harness_eval_iteration(engine, paper_trader, mock_guard):
            halted = True
            break

    assert iterations_run == 2, f"Loop must halt at iteration 2 (FREEZE), ran {iterations_run}."
    assert mock_guard.update.call_count == 2
    assert engine._running is False
    assert halted is True


def test_ftmo_guard_halt_on_kill_on_first_iteration():
    """Mock guard returning KILL on first iteration: loop halts immediately."""
    from risk.ftmo_guard import FTMOAction, FTMOGuard

    mock_guard = MagicMock(spec=FTMOGuard)
    mock_guard.update.return_value = FTMOAction.KILL

    paper_trader = MagicMock()
    paper_trader._current_balance = 8_500.0
    paper_trader._order_manager._positions = {}
    engine = MagicMock()
    engine._running = True

    iterations_run = 0
    halted = False
    for _ in range(5):
        iterations_run += 1
        if _run_harness_eval_iteration(engine, paper_trader, mock_guard):
            halted = True
            break

    assert iterations_run == 1, "KILL must halt on first iteration."
    assert mock_guard.update.call_count == 1
    assert engine._running is False
    assert halted is True


def test_ftmo_guard_does_not_halt_on_reduce_50():
    """REDUCE_50 is informational, not a halt. Engine must keep running."""
    from risk.ftmo_guard import FTMOAction, FTMOGuard

    mock_guard = MagicMock(spec=FTMOGuard)
    mock_guard.update.return_value = FTMOAction.REDUCE_50

    paper_trader = MagicMock()
    paper_trader._current_balance = 9_200.0
    paper_trader._order_manager._positions = {"p1": object()}
    engine = MagicMock()
    engine._running = True

    halted = False
    for _ in range(5):
        if _run_harness_eval_iteration(engine, paper_trader, mock_guard):
            halted = True
            break

    assert halted is False
    assert engine._running is True
    assert mock_guard.update.call_count == 5


# ── Real-FTMOGuard integration evidence ───────────────────────────────────


def test_real_ftmo_guard_engages_freeze_on_5pct_daily_loss():
    """A real FTMOGuard returns FREEZE when balance drops 5% in one day vs
    $10K starting balance. The harness relies on ``action_level`` and
    ``daily_loss_pct`` reflecting the breach; the kill-switch side-effect
    is intentionally suppressed in production (KillSwitchManager._disabled
    defaults True per Craig directive Jun 27).
    """
    from adapters.ctrader.kill_switch import KillSwitchManager
    from risk.ftmo_guard import FTMOAction, FTMOGuard

    kill_switch = KillSwitchManager()
    real_guard = FTMOGuard(
        kill_switch=kill_switch,
        starting_balance=10_000.0,
        challenge_type="1-step",
    )

    # -5% = $500 drop → past the 3% daily-DD threshold (must FREEZE)
    action = real_guard.update(current_balance=9_500.0, open_positions=0)
    assert action == FTMOAction.FREEZE, (
        f"Expected FREEZE on 5% loss, got {action}. "
        f"daily_loss_pct={real_guard.daily_loss_pct}."
    )
    # FTMOGuard self-state must reflect the breach so the harness's
    # _ftmo_action.value check fires on FREEZE/KILL.
    assert real_guard.action_level == FTMOAction.FREEZE
    assert real_guard.daily_loss_pct >= 5.0, (
        f"daily_loss_pct must reflect the $500/$10K loss, "
        f"got {real_guard.daily_loss_pct}."
    )

    # Optional side-channel: production KillSwitchManager is administratively
    # disabled so is_globally_frozen() returns False; verify that contract
    # holds (so the test still documents the integration wiring).
    assert kill_switch.is_globally_frozen() is False, (
        "Production KillSwitchManager._disabled is True; activation is "
        "intentionally suppressed. This is a Craig directive (Jun 27)."
    )


def test_real_ftmo_guard_stays_allow_on_2pct_daily_loss():
    """A real FTMOGuard returns ALLOW when balance is within the 3% daily-DD band."""
    from risk.ftmo_guard import FTMOAction, FTMOGuard

    real_guard = FTMOGuard(starting_balance=10_000.0, challenge_type="1-step")

    # -2% = $200 drop → under 3% threshold (must ALLOW)
    action = real_guard.update(current_balance=9_800.0, open_positions=0)
    assert action == FTMOAction.ALLOW, (
        f"Expected ALLOW on 2% loss, got {action}. "
        f"daily_loss_pct={real_guard.daily_loss_pct}."
    )
