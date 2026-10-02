"""State persistence for risk system — saves/restores sizer state across restarts.

Also handles per-strategy position tracking for the per-strategy freeze
subsystem (BQ-685a).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from risk.sl_position_sizer import SLPositionSizer

if TYPE_CHECKING:
    pass

logger = logging.getLogger("ayumi.risk")


# ── Per-Strategy State Schema ─────────────────────────────────────────────────

STRATEGY_STATE_VERSION = 1


class StrategyTracker:
    """Tracks per-strategy runtime metrics for freeze decisions.

    Stored alongside the main risk state and persisted across restarts.
    Each strategy entry contains:
      - consecutive_losses: int
      - daily_pnl: float (account currency)
      - daily_dd_pct: float (percentage of daily starting balance)
      - open_positions: int
      - last_slippage_pips: float
    """

    def __init__(self) -> None:
        self._strategies: dict[str, dict] = {}

    def register(self, strategy_id: str) -> None:
        """Register a strategy. Idempotent."""
        if strategy_id not in self._strategies:
            self._strategies[strategy_id] = {
                "consecutive_losses": 0,
                "daily_pnl": 0.0,
                "daily_dd_pct": 0.0,
                "open_positions": 0,
                "last_slippage_pips": 0.0,
            }

    def update(
        self,
        strategy_id: str,
        *,
        consecutive_losses: int | None = None,
        daily_pnl: float | None = None,
        daily_dd_pct: float | None = None,
        open_positions: int | None = None,
        last_slippage_pips: float | None = None,
    ) -> None:
        """Update fields for a strategy. Auto-registers if unknown."""
        self.register(strategy_id)
        s = self._strategies[strategy_id]
        if consecutive_losses is not None:
            s["consecutive_losses"] = consecutive_losses
        if daily_pnl is not None:
            s["daily_pnl"] = daily_pnl
        if daily_dd_pct is not None:
            s["daily_dd_pct"] = daily_dd_pct
        if open_positions is not None:
            s["open_positions"] = open_positions
        if last_slippage_pips is not None:
            s["last_slippage_pips"] = last_slippage_pips

    def get(self, strategy_id: str) -> dict | None:
        """Return strategy metrics dict or None if not registered."""
        return self._strategies.get(strategy_id)

    def get_all(self) -> dict[str, dict]:
        """Return all strategy metrics."""
        return dict(self._strategies)

    def reset_daily(self) -> None:
        """Reset daily counters (call at session/day boundary)."""
        for s in self._strategies.values():
            s["consecutive_losses"] = 0
            s["daily_pnl"] = 0.0
            s["daily_dd_pct"] = 0.0

    def to_dict(self) -> dict:
        return {
            "version": STRATEGY_STATE_VERSION,
            "strategies": dict(self._strategies),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StrategyTracker":
        tracker = cls()
        strategies = data.get("strategies", {})
        for sid, sdata in strategies.items():
            tracker._strategies[sid] = {
                "consecutive_losses": sdata.get("consecutive_losses", 0),
                "daily_pnl": sdata.get("daily_pnl", 0.0),
                "daily_dd_pct": sdata.get("daily_dd_pct", 0.0),
                "open_positions": sdata.get("open_positions", 0),
                "last_slippage_pips": sdata.get("last_slippage_pips", 0.0),
            }
        return tracker


class StatePersistence:
    """Saves and restores risk system state across restarts."""

    def __init__(self, state_path: str = "data/risk_state.json"):
        self._path = Path(state_path)

    def get_state(self, sizer: SLPositionSizer) -> dict:
        """Extract sizer state as dict (for inspection or serialization)."""
        breaker = sizer.breaker
        return {
            "account_balance": sizer.account_balance,
            "peak_balance": sizer._peak_balance,
            "daily_risk_used": sizer._daily_risk_used,
            "open_risk": sizer._open_risk,
            "circuit_breaker": {
                "halted": breaker.halted,
                "halted_until": breaker.halted_until.isoformat() if breaker.halted_until else None,
                "halt_reason": breaker.halt_reason,
                "recent_trades": breaker.recent_trades,
                "daily_dd_pct": breaker.daily_dd_pct,
                "account_dd_pct": breaker.account_dd_pct,
            },
        }

    def save(self, sizer: SLPositionSizer, strategy_tracker: StrategyTracker | None = None) -> None:
        """Persist current sizer state atomically.

        If ``strategy_tracker`` is provided, per-strategy metrics are
        included in the persisted state file.
        """
        state = self.get_state(sizer)
        if strategy_tracker is not None:
            state["strategy_tracker"] = strategy_tracker.to_dict()
        self._atomic_write(state)
        logger.info("Saved risk state to %s", self._path)

    def restore(self, sizer: SLPositionSizer) -> bool:
        """Restore sizer state from disk. Returns True if successful."""
        if not self._path.exists():
            logger.warning("No state file at %s — starting fresh", self._path)
            return False

        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Corrupt state file %s: %s", self._path, e)
            return False

        try:
            sizer.account_balance = raw.get("account_balance", sizer.account_balance)
            sizer._peak_balance = raw.get("peak_balance", sizer.account_balance)
            sizer._daily_risk_used = raw.get("daily_risk_used", 0.0)
            sizer._open_risk = raw.get("open_risk", 0.0)

            cb = raw.get("circuit_breaker", {})
            breaker = sizer.breaker
            breaker.halted = cb.get("halted", False)
            breaker.halt_reason = cb.get("halt_reason", "")
            breaker.recent_trades = cb.get("recent_trades", [])
            breaker.daily_dd_pct = cb.get("daily_dd_pct", 0.0)
            breaker.account_dd_pct = cb.get("account_dd_pct", 0.0)

            halted_until = cb.get("halted_until")
            if halted_until:
                from datetime import datetime

                breaker.halted_until = datetime.fromisoformat(halted_until)
            else:
                breaker.halted_until = None

            logger.info("Restored risk state from %s", self._path)
            return True
        except Exception as e:
            logger.error("Failed to restore state: %s", e)
            return False

    def restore_strategy_tracker(self) -> StrategyTracker | None:
        """Load and return the per-strategy tracker from the state file.

        Returns None if the file is missing, corrupt, or contains no
        strategy data.
        """
        if not self._path.exists():
            return None

        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        st_data = raw.get("strategy_tracker")
        if st_data is None:
            return None

        try:
            return StrategyTracker.from_dict(st_data)
        except Exception as e:
            logger.error("Failed to restore strategy tracker: %s", e)
            return None

    def reconcile_positions(
        self,
        strategy_tracker: StrategyTracker,
        strategy_id: str,
        broker_positions: list[dict],
    ) -> dict:
        """Reconcile locally tracked positions against broker state.

        Compares the ``open_positions`` count tracked by the
        :class:`StrategyTracker` against the actual broker-reported
        positions for a given strategy.

        Args:
            strategy_tracker: The tracker holding local state.
            strategy_id: Strategy to reconcile.
            broker_positions: List of broker position dicts, each
                containing ``symbol``, ``volume``, and ``side``.

        Returns:
            Dict with ``matched`` (bool), ``local_count`` (int),
            ``broker_count`` (int), and ``mismatches`` (list of dicts).
        """
        local = strategy_tracker.get(strategy_id)
        local_count = local["open_positions"] if local else 0
        broker_count = len(broker_positions)

        mismatches: list[dict] = []

        if local_count != broker_count:
            mismatches.append(
                {
                    "type": "count_mismatch",
                    "strategy_id": strategy_id,
                    "local_count": local_count,
                    "broker_count": broker_count,
                }
            )

        # Flag high slippage as a potential reconciliation concern
        if local and local.get("last_slippage_pips", 0) > 5.0:
            mismatches.append(
                {
                    "type": "high_slippage_flag",
                    "strategy_id": strategy_id,
                    "last_slippage_pips": local["last_slippage_pips"],
                }
            )

        return {
            "matched": len(mismatches) == 0,
            "local_count": local_count,
            "broker_count": broker_count,
            "mismatches": mismatches,
        }

    def _atomic_write(self, state: dict) -> None:
        """Write state atomically via temp file + rename."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent,
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, str(self._path))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
