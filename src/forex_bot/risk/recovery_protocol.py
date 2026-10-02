"""Recovery protocol for controlled strategy unfreeze (BQ-685b).

Executes a 4-step recovery sequence when a frozen strategy is ready to
resume trading:

  1. Position reconciliation — compare local state against broker state
  2. Margin verification — verify sufficient margin is available
  3. Gradual unfreeze at 50% risk — unfreeze with reduced risk multiplier
  4. Restore to 100% risk — after a 1-hour cooldown if stable

Designed to be called by the ForwardTestEngine or manual recovery tools.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ayumi.risk.recovery")


# ── Constants ─────────────────────────────────────────────────────────────────

COOLDOWN_DURATION_SEC = 3600  # 1 hour
COOLDOWN_RISK_MULTIPLIER = 0.5  # 50% risk during cooldown
FULL_RISK_MULTIPLIER = 1.0
MIN_MARGIN_LEVEL_PCT = 200.0  # Minimum margin level (%) to proceed with recovery


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class PositionInfo:
    """Position information for reconciliation."""

    symbol: str
    volume: float
    side: str  # "buy" or "sell"

    def key(self) -> str:
        """A comparison key for matching local and broker positions."""
        return f"{self.symbol}:{self.side}"


@dataclass
class ReconciliationResult:
    """Result of position reconciliation between local and broker state."""

    matched: bool
    local_positions: list[PositionInfo] = field(default_factory=list)
    broker_positions: list[PositionInfo] = field(default_factory=list)
    mismatches: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "matched": self.matched,
            "local_count": len(self.local_positions),
            "broker_count": len(self.broker_positions),
            "mismatches": self.mismatches,
        }


@dataclass
class MarginInfo:
    """Margin state from broker."""

    available_margin: float
    used_margin: float
    margin_level_pct: float


@dataclass
class CooldownState:
    """Tracks the cooldown period for a recovering strategy."""

    strategy_id: str
    started_at: float  # unix timestamp
    risk_multiplier: float = COOLDOWN_RISK_MULTIPLIER
    duration_sec: int = COOLDOWN_DURATION_SEC

    def is_expired(self, now: Optional[float] = None) -> bool:
        """Check if the cooldown period has elapsed."""
        ts = now if now is not None else time.time()
        return (ts - self.started_at) >= self.duration_sec

    def remaining_sec(self, now: Optional[float] = None) -> float:
        """Seconds remaining in cooldown (0 if expired)."""
        ts = now if now is not None else time.time()
        return max(0.0, self.duration_sec - (ts - self.started_at))

    def to_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "started_at": self.started_at,
            "risk_multiplier": self.risk_multiplier,
            "duration_sec": self.duration_sec,
            "remaining_sec": self.remaining_sec(),
        }


@dataclass
class RecoveryResult:
    """Outcome of a recovery attempt."""

    success: bool
    strategy_id: str
    step: str  # which step completed or failed
    reconciliation: Optional[ReconciliationResult] = None
    margin: Optional[MarginInfo] = None
    risk_multiplier: float = FULL_RISK_MULTIPLIER
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "strategy_id": self.strategy_id,
            "step": self.step,
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation else None,
            "margin_level_pct": self.margin.margin_level_pct if self.margin else None,
            "risk_multiplier": self.risk_multiplier,
            "message": self.message,
        }


# ── Recovery Protocol ────────────────────────────────────────────────────────


class RecoveryProtocol:
    """Controlled unfreeze protocol for frozen strategies.

    Executes a 4-step recovery sequence:

      1. Position reconciliation against broker state
      2. Margin verification
      3. Gradual unfreeze at 50% risk
      4. Restore to 100% risk after cooldown

    Usage::

        protocol = RecoveryProtocol(kill_switch_manager)
        result = protocol.execute_recovery(
            strategy_id="momentum_eurusd",
            broker_positions=[...],
            margin_info=MarginInfo(...),
            local_positions=[...],
        )
        if result.success:
            # Strategy is now in cooldown at 50% risk
            # Later, check if cooldown is complete:
            if protocol.check_cooldown_complete("momentum_eurusd"):
                protocol.restore_full_risk("momentum_eurusd")
    """

    def __init__(self, kill_switch_manager) -> None:
        self._ks = kill_switch_manager
        self._cooldowns: dict[str, CooldownState] = {}

    # ── Public API ─────────────────────────────────────────────────────────

    def execute_recovery(
        self,
        strategy_id: str,
        broker_positions: list[PositionInfo],
        margin_info: MarginInfo,
        local_positions: list[PositionInfo] | None = None,
    ) -> RecoveryResult:
        """Execute the full 4-step recovery sequence.

        Steps 1–3 run synchronously. Step 4 (restore full risk) is
        triggered later via :meth:`check_cooldown_complete` and
        :meth:`restore_full_risk`.

        Args:
            strategy_id: The frozen strategy to recover.
            broker_positions: Live positions from the broker.
            margin_info: Current margin state from the broker.
            local_positions: Locally tracked positions (if None, uses
                an empty list — reconciliation will report missing).

        Returns:
            RecoveryResult describing the outcome.
        """
        logger.info("Starting recovery for strategy '%s'", strategy_id)

        # ── Step 1: Position Reconciliation ───────────────────────────
        reconciliation = self._reconcile_positions(
            strategy_id,
            local_positions or [],
            broker_positions,
        )

        if not reconciliation.matched:
            logger.warning(
                "Recovery aborted for '%s': position reconciliation failed — %d mismatches",
                strategy_id,
                len(reconciliation.mismatches),
            )
            return RecoveryResult(
                success=False,
                strategy_id=strategy_id,
                step="reconciliation",
                reconciliation=reconciliation,
                message=(f"Position reconciliation failed: {len(reconciliation.mismatches)} mismatches detected"),
            )

        logger.info("Step 1 passed: positions reconciled for '%s'", strategy_id)

        # ── Step 2: Margin Verification ───────────────────────────────
        if not self._verify_margin(margin_info):
            logger.warning(
                "Recovery aborted for '%s': margin verification failed — margin level %.1f%% < %.1f%%",
                strategy_id,
                margin_info.margin_level_pct,
                MIN_MARGIN_LEVEL_PCT,
            )
            return RecoveryResult(
                success=False,
                strategy_id=strategy_id,
                step="margin_verification",
                reconciliation=reconciliation,
                margin=margin_info,
                message=(
                    f"Margin verification failed: "
                    f"margin level {margin_info.margin_level_pct:.1f}% "
                    f"< {MIN_MARGIN_LEVEL_PCT:.1f}%"
                ),
            )

        logger.info(
            "Step 2 passed: margin verified for '%s' (%.1f%%)",
            strategy_id,
            margin_info.margin_level_pct,
        )

        # ── Step 3: Gradual Unfreeze at 50% Risk ─────────────────────
        self._ks.unfreeze_strategy(strategy_id, reason="recovery_protocol")

        cooldown = CooldownState(
            strategy_id=strategy_id,
            started_at=time.time(),
            risk_multiplier=COOLDOWN_RISK_MULTIPLIER,
        )
        self._cooldowns[strategy_id] = cooldown

        logger.info(
            "Step 3 complete: strategy '%s' unfrozen at %.0f%% risk — cooldown %d seconds",
            strategy_id,
            COOLDOWN_RISK_MULTIPLIER * 100,
            COOLDOWN_DURATION_SEC,
        )

        return RecoveryResult(
            success=True,
            strategy_id=strategy_id,
            step="gradual_unfreeze",
            reconciliation=reconciliation,
            margin=margin_info,
            risk_multiplier=COOLDOWN_RISK_MULTIPLIER,
            message=(
                f"Strategy '{strategy_id}' recovered — unfrozen at "
                f"{COOLDOWN_RISK_MULTIPLIER * 100:.0f}% risk. "
                f"Cooldown: {COOLDOWN_DURATION_SEC}s."
            ),
        )

    def check_cooldown_complete(self, strategy_id: str) -> bool:
        """Check if the cooldown period has elapsed for a strategy.

        Returns True if the strategy is in cooldown and the cooldown
        has expired. Returns False if the strategy is not in cooldown
        or the cooldown is still active.
        """
        cooldown = self._cooldowns.get(strategy_id)
        if cooldown is None:
            return False
        return cooldown.is_expired()

    def restore_full_risk(self, strategy_id: str) -> bool:
        """Restore a strategy to 100% risk after cooldown completion.

        Removes the cooldown entry and returns True if successful.
        Returns False if the strategy is not in cooldown or the
        cooldown has not yet expired.
        """
        cooldown = self._cooldowns.get(strategy_id)
        if cooldown is None:
            logger.warning(
                "restore_full_risk: strategy '%s' not in cooldown",
                strategy_id,
            )
            return False

        if not cooldown.is_expired():
            remaining = cooldown.remaining_sec()
            logger.info(
                "restore_full_risk: cooldown still active for '%s' — %.0f seconds remaining",
                strategy_id,
                remaining,
            )
            return False

        del self._cooldowns[strategy_id]
        logger.info(
            "Step 4 complete: strategy '%s' restored to 100%% risk",
            strategy_id,
        )
        return True

    def get_risk_multiplier(self, strategy_id: str) -> float:
        """Return the current risk multiplier for a strategy.

        Returns 0.5 if in cooldown, 1.0 otherwise.
        """
        cooldown = self._cooldowns.get(strategy_id)
        if cooldown is not None and not cooldown.is_expired():
            return cooldown.risk_multiplier
        return FULL_RISK_MULTIPLIER

    def get_cooldown_state(self, strategy_id: str) -> Optional[CooldownState]:
        """Return the cooldown state for a strategy, if active."""
        cooldown = self._cooldowns.get(strategy_id)
        if cooldown is not None and not cooldown.is_expired():
            return cooldown
        return None

    def get_all_cooldowns(self) -> dict[str, CooldownState]:
        """Return all active cooldowns."""
        now = time.time()
        return {sid: cd for sid, cd in self._cooldowns.items() if not cd.is_expired(now)}

    # ── Internal Methods ───────────────────────────────────────────────────

    def _reconcile_positions(
        self,
        strategy_id: str,
        local_positions: list[PositionInfo],
        broker_positions: list[PositionInfo],
    ) -> ReconciliationResult:
        """Compare local positions against broker positions.

        Detects:
          - Missing positions: in local but not in broker
          - Unexpected positions: in broker but not in local
          - Volume mismatches: same symbol/side but different volume
        """
        mismatches: list[dict] = []

        local_map: dict[str, PositionInfo] = {p.key(): p for p in local_positions}
        broker_map: dict[str, PositionInfo] = {p.key(): p for p in broker_positions}

        all_keys = set(local_map.keys()) | set(broker_map.keys())

        for key in all_keys:
            local = local_map.get(key)
            broker = broker_map.get(key)

            if local and not broker:
                mismatches.append(
                    {
                        "type": "missing_in_broker",
                        "symbol": local.symbol,
                        "side": local.side,
                        "volume": local.volume,
                    }
                )
            elif broker and not local:
                mismatches.append(
                    {
                        "type": "unexpected_in_broker",
                        "symbol": broker.symbol,
                        "side": broker.side,
                        "volume": broker.volume,
                    }
                )
            elif local and broker:
                vol_diff = abs(local.volume - broker.volume)
                if vol_diff > 0.01:  # tolerance: 0.01 lot
                    mismatches.append(
                        {
                            "type": "volume_mismatch",
                            "symbol": local.symbol,
                            "side": local.side,
                            "local_volume": local.volume,
                            "broker_volume": broker.volume,
                        }
                    )

        return ReconciliationResult(
            matched=len(mismatches) == 0,
            local_positions=local_positions,
            broker_positions=broker_positions,
            mismatches=mismatches,
        )

    def _verify_margin(self, margin_info: MarginInfo) -> bool:
        """Verify that margin level is sufficient for recovery."""
        return margin_info.margin_level_pct >= MIN_MARGIN_LEVEL_PCT
