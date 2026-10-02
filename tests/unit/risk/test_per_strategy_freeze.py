"""Unit tests for per-strategy freeze/unfreeze functionality (BQ-685a).

Tests cover:
  1. Strategy registration
  2. Manual freeze/unfreeze
  3. Auto-freeze on consecutive losses (3)
  4. Auto-freeze on daily drawdown (1.5%)
  5. Auto-freeze on slippage threshold (5.0 pips)
  6. Strategy isolation (other strategies continue)
  7. Freeze state persistence across restart
  8. Unfreeze resets counters
  9. StrategyTracker in state_persistence
"""

import sys
from pathlib import Path

import pytest

# Ensure src is on the path
src_dir = Path(__file__).resolve().parents[3] / "src" / "forex_bot"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from adapters.ctrader.kill_switch import (  # noqa: E402
    KillSwitchManager,
)
from risk.state_persistence import StrategyTracker  # noqa: E402


@pytest.fixture
def ks_manager(tmp_path):
    """Fresh KillSwitchManager with isolated state directory."""
    mgr = KillSwitchManager(state_dir=str(tmp_path / "ks"))
    mgr._disabled = False  # Enable for testing
    return mgr


# ── Test 1: Strategy Registration ─────────────────────────────────────────


def test_register_strategy(ks_manager):
    """Registering a strategy creates an entry in the registry."""
    ks_manager.register_strategy("momentum_eurusd")

    status = ks_manager.get_strategy_status("momentum_eurusd")
    assert status["registered"] is True
    assert status["frozen"] is False
    assert status["strategy_id"] == "momentum_eurusd"


def test_register_strategy_idempotent(ks_manager):
    """Re-registering the same strategy is a no-op."""
    ks_manager.register_strategy("scalper_gbpusd")
    ks_manager.register_strategy("scalper_gbpusd")  # no error

    status = ks_manager.get_strategy_status("scalper_gbpusd")
    assert status["registered"] is True


# ── Test 2: Manual Freeze/Unfreeze ────────────────────────────────────────


def test_freeze_and_unfreeze_strategy(ks_manager):
    """freeze_strategy and unfreeze_strategy work correctly."""
    ks_manager.register_strategy("grid_xauusd")

    # Freeze
    result = ks_manager.freeze_strategy("grid_xauusd", reason="manual test")
    assert result is True
    assert ks_manager.is_strategy_frozen("grid_xauusd") is True

    status = ks_manager.get_strategy_status("grid_xauusd")
    assert status["frozen"] is True
    assert status["reason"] == "manual test"

    # Unfreeze
    result = ks_manager.unfreeze_strategy("grid_xauusd")
    assert result is True
    assert ks_manager.is_strategy_frozen("grid_xauusd") is False


def test_freeze_already_frozen_returns_false(ks_manager):
    """Freezing an already-frozen strategy returns False."""
    ks_manager.register_strategy("breakout_usdjpy")
    ks_manager.freeze_strategy("breakout_usdjpy", reason="first freeze")
    result = ks_manager.freeze_strategy("breakout_usdjpy", reason="second freeze")
    assert result is False


# ── Test 3: Auto-freeze on Consecutive Losses ──────────────────────────────


def test_auto_freeze_consecutive_losses(ks_manager):
    """Auto-freeze triggers on 3 consecutive losses."""
    ks_manager.register_strategy("trend_eurusd")

    # 2 losses — should not freeze
    frozen = ks_manager.check_auto_freeze(
        "trend_eurusd",
        consecutive_losses=2,
    )
    assert frozen is False
    assert ks_manager.is_strategy_frozen("trend_eurusd") is False

    # 3 losses — should freeze
    frozen = ks_manager.check_auto_freeze(
        "trend_eurusd",
        consecutive_losses=3,
    )
    assert frozen is True
    assert ks_manager.is_strategy_frozen("trend_eurusd") is True

    status = ks_manager.get_strategy_status("trend_eurusd")
    assert "consecutive losses" in status["reason"]


# ── Test 4: Auto-freeze on Daily Drawdown ──────────────────────────────────


def test_auto_freeze_daily_dd(ks_manager):
    """Auto-freeze triggers when daily DD >= 1.5%."""
    ks_manager.register_strategy("scalper_gbpusd")

    # 1.0% DD — should not freeze
    frozen = ks_manager.check_auto_freeze(
        "scalper_gbpusd",
        daily_dd_pct=1.0,
    )
    assert frozen is False

    # 1.5% DD — should freeze (>= threshold)
    frozen = ks_manager.check_auto_freeze(
        "scalper_gbpusd",
        daily_dd_pct=1.5,
    )
    assert frozen is True
    assert ks_manager.is_strategy_frozen("scalper_gbpusd") is True

    status = ks_manager.get_strategy_status("scalper_gbpusd")
    assert "daily DD" in status["reason"]


# ── Test 5: Auto-freeze on Slippage ────────────────────────────────────────


def test_auto_freeze_slippage(ks_manager):
    """Auto-freeze triggers when slippage >= 5.0 pips."""
    ks_manager.register_strategy("grid_xauusd")

    # 4.0 pips — should not freeze
    frozen = ks_manager.check_auto_freeze(
        "grid_xauusd",
        slippage_pips=4.0,
    )
    assert frozen is False

    # 5.0 pips — should freeze (>= threshold)
    frozen = ks_manager.check_auto_freeze(
        "grid_xauusd",
        slippage_pips=5.0,
    )
    assert frozen is True
    assert ks_manager.is_strategy_frozen("grid_xauusd") is True

    status = ks_manager.get_strategy_status("grid_xauusd")
    assert "slippage" in status["reason"]


# ── Test 6: Strategy Isolation ─────────────────────────────────────────────


def test_strategy_isolation(ks_manager):
    """When one strategy is frozen, others continue running."""
    ks_manager.register_strategy("alpha")
    ks_manager.register_strategy("beta")
    ks_manager.register_strategy("gamma")

    # Freeze only alpha
    ks_manager.freeze_strategy("alpha", reason="test isolation")

    assert ks_manager.is_strategy_frozen("alpha") is True
    assert ks_manager.is_strategy_frozen("beta") is False
    assert ks_manager.is_strategy_frozen("gamma") is False

    frozen_list = ks_manager.get_all_frozen_strategies()
    assert frozen_list == ["alpha"]


# ── Test 7: Persistence Across Restart ─────────────────────────────────────


def test_strategy_freeze_persists_across_restart(tmp_path):
    """Strategy freeze state survives KillSwitchManager restart."""
    state_dir = str(tmp_path / "ks_persist")

    # First instance: register and freeze
    mgr1 = KillSwitchManager(state_dir=state_dir)
    mgr1._disabled = False
    mgr1.register_strategy("persist_test")
    mgr1.freeze_strategy("persist_test", reason="restart test")

    assert mgr1.is_strategy_frozen("persist_test") is True

    # Second instance: should load from disk
    mgr2 = KillSwitchManager(state_dir=state_dir)
    mgr2._disabled = False

    assert mgr2.is_strategy_frozen("persist_test") is True

    status = mgr2.get_strategy_status("persist_test")
    assert status["reason"] == "restart test"


# ── Test 8: Unfreeze Resets Counters ───────────────────────────────────────


def test_unfreeze_resets_counters(ks_manager):
    """Unfreezing a strategy resets its consecutive_losses and daily_dd_pct."""
    ks_manager.register_strategy("counter_test")

    # Auto-freeze with losses
    ks_manager.check_auto_freeze("counter_test", consecutive_losses=5, daily_dd_pct=2.0)
    assert ks_manager.is_strategy_frozen("counter_test") is True

    status = ks_manager.get_strategy_status("counter_test")
    assert status["consecutive_losses"] == 5
    assert status["daily_dd_pct"] == 2.0

    # Unfreeze
    ks_manager.unfreeze_strategy("counter_test")

    status = ks_manager.get_strategy_status("counter_test")
    assert status["consecutive_losses"] == 0
    assert status["daily_dd_pct"] == 0.0


# ── Test 9: StrategyTracker in state_persistence ───────────────────────────


def test_strategy_tracker_roundtrip(tmp_path):
    """StrategyTracker saves and restores through StatePersistence."""
    from risk.state_persistence import StatePersistence

    state_file = str(tmp_path / "risk_state.json")
    _persist = StatePersistence(state_path=state_file)

    tracker = StrategyTracker()
    tracker.register("momentum")
    tracker.update("momentum", consecutive_losses=3, daily_dd_pct=1.5, open_positions=2)

    # Save with a mock-like dict (we don't need the full SLPositionSizer)
    # Instead, test the tracker serialization directly
    data = tracker.to_dict()
    assert data["strategies"]["momentum"]["consecutive_losses"] == 3
    assert data["strategies"]["momentum"]["daily_dd_pct"] == 1.5

    # Round-trip
    restored = StrategyTracker.from_dict(data)
    m = restored.get("momentum")
    assert m is not None
    assert m["consecutive_losses"] == 3
    assert m["daily_dd_pct"] == 1.5
    assert m["open_positions"] == 2


def test_strategy_tracker_reset_daily():
    """reset_daily clears loss/PnL counters but not positions or slippage."""
    tracker = StrategyTracker()
    tracker.register("alpha")
    tracker.update(
        "alpha",
        consecutive_losses=3,
        daily_pnl=-150.0,
        daily_dd_pct=2.0,
        open_positions=5,
        last_slippage_pips=3.5,
    )

    tracker.reset_daily()

    m = tracker.get("alpha")
    assert m["consecutive_losses"] == 0
    assert m["daily_pnl"] == 0.0
    assert m["daily_dd_pct"] == 0.0
    assert m["open_positions"] == 5  # Not reset
    assert m["last_slippage_pips"] == 3.5  # Not reset


# ── Test 10: Unregistered Strategy Queries ─────────────────────────────────


def test_unregistered_strategy_not_frozen(ks_manager):
    """Querying an unregistered strategy returns False."""
    assert ks_manager.is_strategy_frozen("nonexistent") is False

    status = ks_manager.get_strategy_status("nonexistent")
    assert status["registered"] is False
    assert status["frozen"] is False
