"""Unit tests for recovery protocol (BQ-685b).

Tests cover:
  1. Happy path: full 4-step recovery sequence
  2. Position reconciliation mismatch aborts recovery
  3. Margin verification failure aborts recovery
  4. Gradual unfreeze sets 50% risk multiplier
  5. Cooldown expiry restores full risk
  6. Risk multiplier queries during/after cooldown
  7. StatePersistence.reconcile_positions method
"""

import sys
import time
from pathlib import Path

import pytest

# Ensure src is on the path
src_dir = Path(__file__).resolve().parents[3] / "src" / "forex_bot"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from adapters.ctrader.kill_switch import KillSwitchManager  # noqa: E402
from risk.recovery_protocol import (  # noqa: E402, I001
    COOLDOWN_DURATION_SEC,
    COOLDOWN_RISK_MULTIPLIER,
    FULL_RISK_MULTIPLIER,
    MIN_MARGIN_LEVEL_PCT,
    CooldownState,
    MarginInfo,
    PositionInfo,
    RecoveryProtocol,
)
from risk.state_persistence import StatePersistence, StrategyTracker  # noqa: E402

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def ks_manager(tmp_path):
    """Fresh KillSwitchManager with isolated state directory."""
    mgr = KillSwitchManager(state_dir=str(tmp_path / "ks"))
    mgr._disabled = False
    return mgr


@pytest.fixture
def protocol(ks_manager):
    """RecoveryProtocol wired to a fresh KillSwitchManager."""
    return RecoveryProtocol(kill_switch_manager=ks_manager)


@pytest.fixture
def good_margin():
    """Margin info that passes verification."""
    return MarginInfo(
        available_margin=10000.0,
        used_margin=2000.0,
        margin_level_pct=300.0,
    )


@pytest.fixture
def bad_margin():
    """Margin info that fails verification (< MIN_MARGIN_LEVEL_PCT)."""
    return MarginInfo(
        available_margin=500.0,
        used_margin=2000.0,
        margin_level_pct=125.0,
    )


@pytest.fixture
def matching_positions():
    """Local and broker positions that reconcile cleanly."""
    local = [
        PositionInfo(symbol="EURUSD", volume=0.50, side="buy"),
        PositionInfo(symbol="GBPUSD", volume=0.30, side="sell"),
    ]
    broker = [
        PositionInfo(symbol="EURUSD", volume=0.50, side="buy"),
        PositionInfo(symbol="GBPUSD", volume=0.30, side="sell"),
    ]
    return local, broker


# ── Test 1: Happy Path — Full 4-Step Recovery ──────────────────────────────


def test_happy_path_recovery(protocol, ks_manager, good_margin, matching_positions):
    """Full recovery sequence succeeds when all checks pass."""
    local, broker = matching_positions

    # Setup: freeze the strategy first
    ks_manager.register_strategy("momentum_eurusd")
    ks_manager.freeze_strategy("momentum_eurusd", reason="test")
    assert ks_manager.is_strategy_frozen("momentum_eurusd")

    # Execute recovery
    result = protocol.execute_recovery(
        strategy_id="momentum_eurusd",
        broker_positions=broker,
        margin_info=good_margin,
        local_positions=local,
    )

    assert result.success is True
    assert result.step == "gradual_unfreeze"
    assert result.risk_multiplier == COOLDOWN_RISK_MULTIPLIER
    assert "recovered" in result.message
    assert ks_manager.is_strategy_frozen("momentum_eurusd") is False
    assert result.reconciliation is not None
    assert result.reconciliation.matched is True


# ── Test 2: Position Reconciliation Mismatch ────────────────────────────────


def test_reconciliation_mismatch_aborts(protocol, ks_manager, good_margin):
    """Recovery aborts when positions don't match."""
    local = [PositionInfo(symbol="EURUSD", volume=0.50, side="buy")]
    broker = [
        PositionInfo(symbol="EURUSD", volume=0.50, side="buy"),
        PositionInfo(symbol="GBPUSD", volume=0.30, side="sell"),  # unexpected
    ]

    ks_manager.register_strategy("scalper_gbpusd")
    ks_manager.freeze_strategy("scalper_gbpusd", reason="test")

    result = protocol.execute_recovery(
        strategy_id="scalper_gbpusd",
        broker_positions=broker,
        margin_info=good_margin,
        local_positions=local,
    )

    assert result.success is False
    assert result.step == "reconciliation"
    assert result.reconciliation is not None
    assert result.reconciliation.matched is False
    assert len(result.reconciliation.mismatches) == 1
    assert result.reconciliation.mismatches[0]["type"] == "unexpected_in_broker"
    # Strategy should remain frozen
    assert ks_manager.is_strategy_frozen("scalper_gbpusd") is True


def test_reconciliation_volume_mismatch(protocol, ks_manager, good_margin):
    """Volume mismatch is detected during reconciliation."""
    local = [PositionInfo(symbol="EURUSD", volume=0.50, side="buy")]
    broker = [PositionInfo(symbol="EURUSD", volume=0.75, side="buy")]

    ks_manager.register_strategy("grid_xauusd")
    ks_manager.freeze_strategy("grid_xauusd", reason="test")

    result = protocol.execute_recovery(
        strategy_id="grid_xauusd",
        broker_positions=broker,
        margin_info=good_margin,
        local_positions=local,
    )

    assert result.success is False
    assert result.step == "reconciliation"
    assert result.reconciliation.matched is False
    assert result.reconciliation.mismatches[0]["type"] == "volume_mismatch"


def test_reconciliation_missing_in_broker(protocol, ks_manager, good_margin):
    """Position in local but not in broker is flagged as missing."""
    local = [
        PositionInfo(symbol="EURUSD", volume=0.50, side="buy"),
        PositionInfo(symbol="XAUUSD", volume=0.10, side="sell"),
    ]
    broker = [PositionInfo(symbol="EURUSD", volume=0.50, side="buy")]

    ks_manager.register_strategy("breakout_usdjpy")
    ks_manager.freeze_strategy("breakout_usdjpy", reason="test")

    result = protocol.execute_recovery(
        strategy_id="breakout_usdjpy",
        broker_positions=broker,
        margin_info=good_margin,
        local_positions=local,
    )

    assert result.success is False
    assert result.reconciliation.mismatches[0]["type"] == "missing_in_broker"


# ── Test 3: Margin Verification Failure ─────────────────────────────────────


def test_margin_failure_aborts(protocol, ks_manager, bad_margin, matching_positions):
    """Recovery aborts when margin level is too low."""
    local, broker = matching_positions

    ks_manager.register_strategy("trend_eurusd")
    ks_manager.freeze_strategy("trend_eurusd", reason="test")

    result = protocol.execute_recovery(
        strategy_id="trend_eurusd",
        broker_positions=broker,
        margin_info=bad_margin,
        local_positions=local,
    )

    assert result.success is False
    assert result.step == "margin_verification"
    assert result.margin is not None
    assert result.margin.margin_level_pct < MIN_MARGIN_LEVEL_PCT
    assert "margin" in result.message.lower()
    # Strategy should remain frozen
    assert ks_manager.is_strategy_frozen("trend_eurusd") is True


# ── Test 4: Gradual Unfreeze Sets 50% Risk ──────────────────────────────────


def test_gradual_unfreeze_sets_half_risk(protocol, ks_manager, good_margin, matching_positions):
    """After successful recovery, risk multiplier is 50% during cooldown."""
    local, broker = matching_positions

    ks_manager.register_strategy("alpha")
    ks_manager.freeze_strategy("alpha", reason="test")

    result = protocol.execute_recovery(
        strategy_id="alpha",
        broker_positions=broker,
        margin_info=good_margin,
        local_positions=local,
    )

    assert result.success is True
    assert result.risk_multiplier == 0.5

    # Verify via get_risk_multiplier
    assert protocol.get_risk_multiplier("alpha") == COOLDOWN_RISK_MULTIPLIER

    # Cooldown state should be tracked
    cooldown = protocol.get_cooldown_state("alpha")
    assert cooldown is not None
    assert cooldown.risk_multiplier == COOLDOWN_RISK_MULTIPLIER
    assert cooldown.strategy_id == "alpha"


# ── Test 5: Cooldown Expiry Restores Full Risk ─────────────────────────────


def test_cooldown_expiry_restores_full_risk(protocol, ks_manager, good_margin, matching_positions):
    """After cooldown expires, risk is restored to 100%."""
    local, broker = matching_positions

    ks_manager.register_strategy("beta")
    ks_manager.freeze_strategy("beta", reason="test")

    # Recover
    protocol.execute_recovery(
        strategy_id="beta",
        broker_positions=broker,
        margin_info=good_margin,
        local_positions=local,
    )

    # Manually expire the cooldown
    cooldown = protocol._cooldowns["beta"]
    cooldown.started_at = time.time() - COOLDOWN_DURATION_SEC - 1

    # Check cooldown is complete
    assert protocol.check_cooldown_complete("beta") is True

    # Restore full risk
    restored = protocol.restore_full_risk("beta")
    assert restored is True

    # Risk multiplier should now be 1.0
    assert protocol.get_risk_multiplier("beta") == FULL_RISK_MULTIPLIER

    # Cooldown should be cleared
    assert protocol.get_cooldown_state("beta") is None


def test_restore_full_risk_before_cooldown_fails(protocol, ks_manager, good_margin, matching_positions):
    """restore_full_risk returns False if cooldown hasn't expired."""
    local, broker = matching_positions

    ks_manager.register_strategy("gamma")
    ks_manager.freeze_strategy("gamma", reason="test")

    protocol.execute_recovery(
        strategy_id="gamma",
        broker_positions=broker,
        margin_info=good_margin,
        local_positions=local,
    )

    # Immediately try to restore — should fail
    assert protocol.check_cooldown_complete("gamma") is False
    assert protocol.restore_full_risk("gamma") is False
    assert protocol.get_risk_multiplier("gamma") == COOLDOWN_RISK_MULTIPLIER


def test_restore_full_risk_not_in_cooldown(protocol):
    """restore_full_risk returns False for strategy not in cooldown."""
    assert protocol.restore_full_risk("nonexistent") is False


# ── Test 6: Risk Multiplier Queries ─────────────────────────────────────────


def test_risk_multiplier_no_cooldown(protocol):
    """get_risk_multiplier returns 1.0 when strategy is not in cooldown."""
    assert protocol.get_risk_multiplier("unknown_strategy") == FULL_RISK_MULTIPLIER


def test_get_all_cooldowns(protocol, ks_manager, good_margin, matching_positions):
    """get_all_cooldowns returns active cooldowns only."""
    local, broker = matching_positions

    for sid in ["strat_a", "strat_b"]:
        ks_manager.register_strategy(sid)
        ks_manager.freeze_strategy(sid, reason="test")
        protocol.execute_recovery(
            strategy_id=sid,
            broker_positions=broker,
            margin_info=good_margin,
            local_positions=local,
        )

    cooldowns = protocol.get_all_cooldowns()
    assert len(cooldowns) == 2
    assert "strat_a" in cooldowns
    assert "strat_b" in cooldowns

    # Expire one
    protocol._cooldowns["strat_a"].started_at = time.time() - COOLDOWN_DURATION_SEC - 1
    cooldowns = protocol.get_all_cooldowns()
    assert len(cooldowns) == 1
    assert "strat_b" in cooldowns


# ── Test 7: StatePersistence.reconcile_positions ────────────────────────────


def test_state_persistence_reconcile_positions_match(tmp_path):
    """StatePersistence.reconcile_positions succeeds when counts match."""
    state_file = str(tmp_path / "risk_state.json")
    persist = StatePersistence(state_path=state_file)

    tracker = StrategyTracker()
    tracker.register("alpha")
    tracker.update("alpha", open_positions=2)

    broker_positions = [
        {"symbol": "EURUSD", "volume": 0.5, "side": "buy"},
        {"symbol": "GBPUSD", "volume": 0.3, "side": "sell"},
    ]

    result = persist.reconcile_positions(tracker, "alpha", broker_positions)
    assert result["matched"] is True
    assert result["local_count"] == 2
    assert result["broker_count"] == 2
    assert len(result["mismatches"]) == 0


def test_state_persistence_reconcile_positions_count_mismatch(tmp_path):
    """reconcile_positions detects count mismatch."""
    state_file = str(tmp_path / "risk_state.json")
    persist = StatePersistence(state_path=state_file)

    tracker = StrategyTracker()
    tracker.register("beta")
    tracker.update("beta", open_positions=3)

    broker_positions = [
        {"symbol": "EURUSD", "volume": 0.5, "side": "buy"},
    ]

    result = persist.reconcile_positions(tracker, "beta", broker_positions)
    assert result["matched"] is False
    assert result["local_count"] == 3
    assert result["broker_count"] == 1
    assert result["mismatches"][0]["type"] == "count_mismatch"


def test_state_persistence_reconcile_positions_unregistered(tmp_path):
    """reconcile_positions handles unregistered strategy gracefully."""
    state_file = str(tmp_path / "risk_state.json")
    persist = StatePersistence(state_path=state_file)

    tracker = StrategyTracker()

    broker_positions = [
        {"symbol": "EURUSD", "volume": 0.5, "side": "buy"},
    ]

    result = persist.reconcile_positions(tracker, "unknown", broker_positions)
    assert result["matched"] is False
    assert result["local_count"] == 0
    assert result["broker_count"] == 1


# ── Test 8: CooldownState Unit Tests ────────────────────────────────────────


def test_cooldown_state_not_expired():
    """CooldownState correctly reports not-expired."""
    cd = CooldownState(
        strategy_id="test",
        started_at=time.time(),
        duration_sec=3600,
    )
    assert cd.is_expired() is False
    assert cd.remaining_sec() > 3500


def test_cooldown_state_expired():
    """CooldownState correctly reports expired."""
    cd = CooldownState(
        strategy_id="test",
        started_at=time.time() - 3700,
        duration_sec=3600,
    )
    assert cd.is_expired() is True
    assert cd.remaining_sec() == 0.0
