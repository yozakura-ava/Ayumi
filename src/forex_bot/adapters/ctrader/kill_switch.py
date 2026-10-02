"""Kill Switch Manager — centralized safety system for trading operations.

Provides global kill/freeze activation, file-based state persistence, and
audit logging. Designed as the single source of truth for kill switch state
across all trading components.

Design principles:
  - Fail-safe defaults: corrupt/missing state → KILL
  - Atomic file writes: temp + rename, never partial writes
  - Append-only audit log
  - In-memory fast path with file durability
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Optional

logger = logging.getLogger("ayumi.ctrader.kill_switch")


# ── Constants ─────────────────────────────────────────────────────────────────

STATE_VERSION = 1
DEFAULT_STATE_DIR = "data/kill_switches"
GLOBAL_STATE_FILE = "global.state"
STRATEGY_STATE_FILE = "strategies.json"
HISTORY_FILE = "history.jsonl"


# ── Harness / Production Boundary Guards (card d85c8d89) ────────────────────
#
# Root-cause finding (card d85c8d89 / diagnosis card 37227dea): on 2026-09-08
# 16:30:25 UTC a backtest-harness KillSwitch instance ran WITHOUT the
# ``_state_dir`` isolation monkey-patch and wrote ``ftmo_daily_loss_limit``
# to the production ``data/kill_switches/history.jsonl``. The matching
# ``global.state`` is still missing on disk. Harness events reached the
# production write path because the WRITE-SIDE had no ownership-binding
# guard at the framework boundary.
#
# Fix: detect harness-mode processes via env vars and treat any state_dir
# that resolves outside the OS tmp prefix as production. A harness-mode
# process MUST redirect ``state_dir`` to a temp dir; if it doesn't, the
# ``__init__`` constructor and every persistence call refuse with a
# ``RuntimeError`` so the contamination is fail-loud at the boundary
# instead of silently leaking into production audit logs.
#
# The guard is opt-in via ``AYUMI_HARNESS=1`` so production launcher paths
# (which never set the env var) are unaffected by this check.

# Environment variables that mark a process as a harness / backtest / test
# runner. A process is in harness mode when ANY of these are set to "1",
# "true", or "yes". See scripts/backtest_blend_harness.py for the canonical
# harness env vars; pytest also sets ``PYTEST_CURRENT_TEST`` per-test, but
# we intentionally do NOT treat that as harness mode here — pytest tests
# already use ``tmp_path`` fixtures and the conftest's
# ``_guard_repo_data_writes`` autouse fixture (card 84df1bcc) catches any
# test that writes under ``<repo>/data/``. Treating pytest as harness
# mode would break legitimate tests that exercise this module.
_HARNESS_ENV_VARS: tuple[str, ...] = (
    "AYUMI_HARNESS",
    "AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT",
)


def _is_harness_mode() -> bool:
    """Return True if the current process is a harness / backtest run.

    The check is explicit and opt-in via env vars so production launcher
    paths (which never set the env var) are unaffected. Card d85c8d89.
    """
    for var in _HARNESS_ENV_VARS:
        val = os.environ.get(var, "").strip().lower()
        if val in ("1", "true", "yes"):
            return True
    return False


def _is_production_state_dir(state_dir: Path) -> bool:
    """Return True if ``state_dir`` resolves outside the OS tmp prefix.

    A state_dir is treated as "production" (i.e., not isolated) when its
    resolved path is NOT under any of:
      - ``tempfile.gettempdir()`` (canonical OS tmp)
      - the conventional ``/tmp`` and macOS ``/private/tmp`` prefixes
      - a pytest ``tmp_path`` fixture (``/tmp/pytest-*`` / ``.../pytest-*``)

    Any harness-mode process that lands here would write audit entries to
    production ``data/kill_switches/history.jsonl`` — exactly the regression
    that surfaced on 2026-09-08. Card d85c8d89.

    Note: the check is conservative. If a relative path like
    ``"data/kill_switches"`` is passed and the CWD is the main repo, the
    resolved path is ``<main>/data/kill_switches`` which is NOT under tmp
    and therefore flagged as production. If CWD is a worktree, the resolved
    path is the worktree's data dir, which is also not under tmp — this is
    intentional: harness runs MUST redirect state_dir to a temp dir; using
    the worktree's data dir would still pollute the worktree and risk
    leaking via ``git add`` to the main tree.
    """
    try:
        resolved = Path(state_dir).resolve()
    except (OSError, RuntimeError):
        # If resolution fails (e.g., path doesn't exist on Windows UNC),
        # be conservative: treat as production so the guard fires.
        return True
    resolved_str = str(resolved)
    # Collect tmp prefixes from the platform plus the conventional names.
    tmp_prefixes: list[str] = []
    try:
        tmp_prefixes.append(tempfile.gettempdir())
    except (OSError, FileNotFoundError):
        pass
    tmp_prefixes.extend(
        [
            "/tmp",
            "/private/tmp",  # macOS canonical /tmp alias
            "/var/folders",  # macOS per-user tmp
        ]
    )
    for prefix in tmp_prefixes:
        prefix = prefix.rstrip("/")
        if resolved_str == prefix or resolved_str.startswith(prefix + "/"):
            return False
    return True

# ── Auto-Freeze Thresholds ───────────────────────────────────────────────────

AUTO_FREEZE_CONSECUTIVE_LOSSES = 3
AUTO_FREEZE_DAILY_DD_PCT = 1.5
AUTO_FREEZE_SLIPPAGE_PIPS = 5.0

# Trigger source identifiers (used by activated_by field)
SOURCE_FTMO = "ftmo_guard"


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class StrategyFreezeState:
    """Serializable per-strategy freeze state."""

    strategy_id: str = ""
    frozen: bool = False
    reason: str = ""
    triggered_by: str = ""
    triggered_at: Optional[str] = None
    consecutive_losses: int = 0
    daily_dd_pct: float = 0.0
    last_slippage_pips: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StrategyFreezeState":
        return cls(
            strategy_id=data.get("strategy_id", ""),
            frozen=data.get("frozen", False),
            reason=data.get("reason", ""),
            triggered_by=data.get("triggered_by", ""),
            triggered_at=data.get("triggered_at"),
            consecutive_losses=data.get("consecutive_losses", 0),
            daily_dd_pct=data.get("daily_dd_pct", 0.0),
            last_slippage_pips=data.get("last_slippage_pips", 0.0),
            metadata=data.get("metadata", {}),
        )


@dataclass
class GlobalKillState:
    """Serializable global kill switch state."""

    version: int = STATE_VERSION
    active: bool = False
    level: str = "global"
    mode: str = "kill"  # "kill" or "freeze"
    reason: str = ""
    triggered_by: str = ""
    triggered_at: Optional[str] = None
    positions_closed: bool = False
    close_count: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GlobalKillState":
        return cls(
            version=data.get("version", STATE_VERSION),
            active=data.get("active", False),
            level=data.get("level", "global"),
            mode=data.get("mode", "kill"),
            reason=data.get("reason", ""),
            triggered_by=data.get("triggered_by", ""),
            triggered_at=data.get("triggered_at"),
            positions_closed=data.get("positions_closed", False),
            close_count=data.get("close_count", 0),
            metadata=data.get("metadata", {}),
        )


# ── KillSwitchManager ────────────────────────────────────────────────────────


class KillSwitchManager:
    """Centralized kill switch state manager.

    Single source of truth for all kill switch state.
    Used by ForwardTestEngine, PaperTrader, and external interfaces.

    Thread-safe. File-persisted. Fail-safe.

    When ``_disabled`` is True, all activation is suppressed and
    ``is_active()`` always returns False. This is a safety hatch for
    when the kill switch triggers on false positives and blocks the
    system from operating. Set to False to re-enable.
    """

    _disabled: bool = True  # Craig directive Jun 27: disabled until properly investigated

    # Kill levels
    LEVEL_GLOBAL = "global"
    LEVEL_ACCOUNT = "account"
    LEVEL_SESSION = "session"
    LEVEL_STRATEGY = "strategy"

    # Kill modes
    MODE_KILL = "kill"
    MODE_FREEZE = "freeze"

    def __init__(
        self,
        state_dir: str = DEFAULT_STATE_DIR,
        disabled: bool | None = None,
    ):
        """Initialize the kill switch manager.

        Args:
            state_dir: Directory for state persistence.
            disabled: Explicit override for the administratively-disabled flag.
                - ``True``  — suppress all activations (production launcher default).
                - ``False`` — enable activations (harness/backtest default).
                - ``None``  — inherit the class-level default (Craig directive Jun 27,
                  ``_disabled = True``). The class-level default is preserved so
                  production paths that do not pass an explicit value continue to
                  behave the same way; the chosen value is logged loudly at
                  startup so operators can never silently lose enforcement.
        """
        # Honor explicit override; otherwise fall through to class default.
        # Class-level ``_disabled`` is a flag owned by Craig's directive; do not
        # flip it silently. The harness path passes ``disabled=False``; the
        # production launcher leaves it None (→ class default True).
        if disabled is not None:
            self._disabled: bool = disabled

        # ── Harness isolation guard (card d85c8d89) ─────────────────────────
        # Fail-loud at construction time when a harness-mode process lands on
        # a non-tmp state_dir. The 2026-09-08 16:30:25 UTC regression wrote
        # harness events to the production ``data/kill_switches/`` directory
        # because the harness entry path skipped the per-run isolation patch
        # (see card 758273a7 / 37227dea). Raising here means any future
        # entry path that forgets the redirect fails loudly at startup
        # instead of silently corrupting the production audit log.
        if _is_harness_mode() and _is_production_state_dir(state_dir):
            logger.error(
                "REFUSED: harness-mode process attempted to construct "
                "KillSwitchManager with a production state_dir=%s. "
                "Card d85c8d89: harness runs MUST redirect state_dir to a "
                "temp dir (see scripts/backtest_blend_harness.py "
                "_isolation_patch / _resolve_state_dir).",
                state_dir,
            )
            raise RuntimeError(
                "Harness-mode process attempted to construct "
                f"KillSwitchManager with production state_dir={state_dir!r}. "
                "Harness runs MUST redirect state_dir to a temp dir "
                "(unset AYUMI_HARNESS for production paths, or pass an "
                "explicit state_dir under tempfile.gettempdir()). "
                "Card d85c8d89."
            )

        self._state_dir = Path(state_dir)
        self._state_file = self._state_dir / GLOBAL_STATE_FILE
        self._history_file = self._state_dir / HISTORY_FILE
        self._lock = RLock()

        # Ensure state directory exists
        self._state_dir.mkdir(parents=True, exist_ok=True)

        # In-memory state (fast path)
        self._state: GlobalKillState = GlobalKillState()

        # Load persisted state
        self._load_state()

        # Cached flags for ultra-fast path (< 0.01ms)
        self._killed_cache: bool = self._state.active and self._state.mode == self.MODE_KILL
        self._frozen_cache: bool = self._state.active and self._state.mode == self.MODE_FREEZE

        # Per-strategy freeze states
        self._strategy_state_file = self._state_dir / STRATEGY_STATE_FILE
        self._strategy_states: dict[str, StrategyFreezeState] = {}
        self._load_strategy_states()

        # Loud startup log — operators must never be uncertain about whether
        # the kill switch is enforcing. Card 09e99147 (2026-09-08): the prior
        # default was set silently; this line makes the choice unmissable.
        effective_disabled = getattr(self, "_disabled", KillSwitchManager._disabled)
        if effective_disabled:
            logger.warning(
                "KillSwitch STARTUP: ADMINISTRATIVELY DISABLED — FTMOGuard/RiskGuard "
                "activations will be SUPPRESSED (no-op). Pass disabled=False to "
                "the KillSwitchManager constructor to enable enforcement. "
                "(class default: Craig directive Jun 27 2026-09-08)"
            )
        else:
            logger.warning(
                "KillSwitch STARTUP: ENABLED — FTMOGuard/RiskGuard activations "
                "will HALT the eval loop on breach. (explicit disabled=False "
                "override applied)"
            )

    # ── Public API: Query ──────────────────────────────────────────────────

    def is_globally_killed(self) -> bool:
        """Fast-path check: is the system globally killed?

        Target latency: < 0.01ms (single bool read, no I/O).
        """
        if self._disabled:
            return False
        return self._killed_cache

    def is_globally_frozen(self) -> bool:
        """Fast-path check: is the system globally frozen?

        Target latency: < 0.01ms (single bool read, no I/O).
        """
        if self._disabled:
            return False
        return self._frozen_cache

    def is_active(self) -> bool:
        """Is any kill switch active (kill or freeze)?"""
        if self._disabled:
            return False
        return self._state.active

    @property
    def is_disabled(self) -> bool:
        """Check if the kill switch is administratively disabled.

        When True, all activation calls are suppressed (no-op).
        External guards (e.g. FTMOGuard) can check this to log
        that their breach would be suppressed.
        """
        return self._disabled

    def get_status(self) -> dict:
        """Full status dict for external consumption."""
        with self._lock:
            return self._state.to_dict()

    # ── Public API: Activation ─────────────────────────────────────────────

    def activate_global_kill(
        self,
        reason: str,
        triggered_by: str,
        close_positions: bool = True,
    ) -> None:
        """Activate global KILL — stops all new trades AND closes positions.

        Args:
            reason: Human-readable reason for the kill.
            triggered_by: Who/what triggered the kill (e.g. "manual", "watchdog").
            close_positions: If True, signal that positions should be closed.
        """
        if self._disabled:
            logger.info(
                "activate_global_kill suppressed (kill switch disabled): reason=%s by=%s",
                reason,
                triggered_by,
            )
            return

        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            was_active = self._state.active

            self._state = GlobalKillState(
                version=STATE_VERSION,
                active=True,
                level=self.LEVEL_GLOBAL,
                mode=self.MODE_KILL,
                reason=reason,
                triggered_by=triggered_by,
                triggered_at=now,
                positions_closed=not close_positions,  # False = still need to close
                close_count=0,
                metadata={},
            )

            self._killed_cache = True
            self._frozen_cache = False

            self._save_state()
            self._append_history(
                {
                    "ts": now,
                    "event": "activated",
                    "level": self.LEVEL_GLOBAL,
                    "mode": self.MODE_KILL,
                    "reason": reason,
                    "triggered_by": triggered_by,
                    "close_positions": close_positions,
                    "was_reactivation": was_active,
                }
            )

            if was_active:
                logger.warning(
                    "Global kill RE-ACTIVATED (was already active): reason=%s, by=%s",
                    reason,
                    triggered_by,
                )
            else:
                logger.critical(
                    "GLOBAL KILL ACTIVATED: reason=%s, by=%s, close_positions=%s",
                    reason,
                    triggered_by,
                    close_positions,
                )

    def activate_profit_target_freeze(
        self,
        triggered_by: str = "phase6_profit_target_detector",
        current_balance: float | None = None,
        target_pct: float | None = None,
    ) -> bool:
        """Activate the global FREEZE for FTMO challenge-completion.

        Phase-6 convenience wrapper. Calls :meth:`activate_global_freeze` with
        a canonical reason so that the audit log shows the freeze was caused
        by reaching the profit target, not a manual operator freeze.

        Returns True if the freeze activation was attempted (i.e., the
        hook is reachable). When the kill switch is administratively
        disabled (``self._disabled``), the underlying activation is
        suppressed — we still return True so callers can confirm the
        decision was recorded in the audit trail via ``log_path``.
        """
        reason = (
            f"ftmo_target_reached (balance={current_balance:.2f}, target_pct={target_pct:.4f})"
            if current_balance is not None and target_pct is not None
            else "ftmo_target_reached"
        )
        self.activate_global_freeze(reason=reason, triggered_by=triggered_by)
        return True

    def activate_global_freeze(self, reason: str, triggered_by: str) -> None:
        """Activate global FREEZE — stops new trades, holds existing positions."""
        if self._disabled:
            logger.info(
                "activate_global_freeze suppressed (kill switch disabled): reason=%s by=%s",
                reason,
                triggered_by,
            )
            return

        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            was_active = self._state.active

            self._state = GlobalKillState(
                version=STATE_VERSION,
                active=True,
                level=self.LEVEL_GLOBAL,
                mode=self.MODE_FREEZE,
                reason=reason,
                triggered_by=triggered_by,
                triggered_at=now,
                positions_closed=True,  # Freeze doesn't close positions
                close_count=0,
                metadata={},
            )

            self._killed_cache = False
            self._frozen_cache = True

            self._save_state()
            self._append_history(
                {
                    "ts": now,
                    "event": "activated",
                    "level": self.LEVEL_GLOBAL,
                    "mode": self.MODE_FREEZE,
                    "reason": reason,
                    "triggered_by": triggered_by,
                    "was_reactivation": was_active,
                }
            )

            logger.warning(
                "GLOBAL FREEZE ACTIVATED: reason=%s, by=%s",
                reason,
                triggered_by,
            )

    def deactivate(self, reason: str = "manual_recovery") -> None:
        """Deactivate any active kill/freeze.

        Args:
            reason: Reason for deactivation.
        """
        with self._lock:
            if not self._state.active:
                logger.info("Deactivate called but no kill switch active")
                return

            now = datetime.now(timezone.utc).isoformat()
            prev_mode = self._state.mode
            prev_reason = self._state.reason

            self._state = GlobalKillState(
                version=STATE_VERSION,
                active=False,
            )

            self._killed_cache = False
            self._frozen_cache = False

            self._save_state()
            self._append_history(
                {
                    "ts": now,
                    "event": "deactivated",
                    "level": self.LEVEL_GLOBAL,
                    "previous_mode": prev_mode,
                    "previous_reason": prev_reason,
                    "reason": reason,
                }
            )

            logger.info(
                "Kill switch DEACTIVATED: previous_mode=%s, previous_reason=%s, reason=%s",
                prev_mode,
                prev_reason,
                reason,
            )

    # ── Public API: Per-Strategy Freeze ───────────────────────────────────

    def register_strategy(self, strategy_id: str) -> None:
        """Register a strategy for per-strategy freeze tracking.

        Idempotent: re-registering an existing strategy is a no-op.
        """
        with self._lock:
            if strategy_id not in self._strategy_states:
                self._strategy_states[strategy_id] = StrategyFreezeState(
                    strategy_id=strategy_id,
                )
                self._save_strategy_states()
                logger.info("Strategy registered: %s", strategy_id)

    def freeze_strategy(
        self,
        strategy_id: str,
        reason: str,
        triggered_by: str = "manual",
    ) -> bool:
        """Freeze a specific strategy.

        Other strategies continue running.  Returns True if the strategy
        was newly frozen, False if it was already frozen or not registered.
        """
        with self._lock:
            st = self._strategy_states.get(strategy_id)
            if st is None:
                logger.warning(
                    "freeze_strategy: strategy '%s' not registered — auto-registering",
                    strategy_id,
                )
                st = StrategyFreezeState(strategy_id=strategy_id)
                self._strategy_states[strategy_id] = st

            if st.frozen:
                logger.info("Strategy '%s' already frozen", strategy_id)
                return False

            now = datetime.now(timezone.utc).isoformat()
            st.frozen = True
            st.reason = reason
            st.triggered_by = triggered_by
            st.triggered_at = now

            self._save_strategy_states()
            self._append_history(
                {
                    "ts": now,
                    "event": "strategy_frozen",
                    "level": self.LEVEL_STRATEGY,
                    "strategy_id": strategy_id,
                    "reason": reason,
                    "triggered_by": triggered_by,
                }
            )
            logger.warning(
                "STRATEGY FROZEN: %s — reason=%s, by=%s",
                strategy_id,
                reason,
                triggered_by,
            )
            return True

    def unfreeze_strategy(
        self,
        strategy_id: str,
        reason: str = "manual_recovery",
    ) -> bool:
        """Unfreeze a specific strategy.

        Returns True if the strategy was frozen and is now unfrozen.
        Resets consecutive_losses and daily_dd_pct counters.
        """
        with self._lock:
            st = self._strategy_states.get(strategy_id)
            if st is None or not st.frozen:
                logger.info("Strategy '%s' not frozen — nothing to unfreeze", strategy_id)
                return False

            now = datetime.now(timezone.utc).isoformat()
            prev_reason = st.reason
            st.frozen = False
            st.reason = ""
            st.triggered_by = ""
            st.triggered_at = None
            st.consecutive_losses = 0
            st.daily_dd_pct = 0.0

            self._save_strategy_states()
            self._append_history(
                {
                    "ts": now,
                    "event": "strategy_unfrozen",
                    "level": self.LEVEL_STRATEGY,
                    "strategy_id": strategy_id,
                    "previous_reason": prev_reason,
                    "reason": reason,
                }
            )
            logger.info(
                "STRATEGY UNFROZEN: %s — previous_reason=%s, reason=%s",
                strategy_id,
                prev_reason,
                reason,
            )
            return True

    def is_strategy_frozen(self, strategy_id: str) -> bool:
        """Check if a specific strategy is frozen.

        Target latency: < 0.01ms (dict lookup + bool read).
        """
        st = self._strategy_states.get(strategy_id)
        return st is not None and st.frozen

    def get_strategy_status(self, strategy_id: str) -> dict:
        """Get full status dict for a specific strategy."""
        with self._lock:
            st = self._strategy_states.get(strategy_id)
            if st is None:
                return {
                    "strategy_id": strategy_id,
                    "frozen": False,
                    "registered": False,
                }
            result = st.to_dict()
            result["registered"] = True
            return result

    def get_all_frozen_strategies(self) -> list[str]:
        """Return list of all currently frozen strategy IDs."""
        with self._lock:
            return [sid for sid, st in self._strategy_states.items() if st.frozen]

    def check_auto_freeze(
        self,
        strategy_id: str,
        consecutive_losses: int = 0,
        daily_dd_pct: float = 0.0,
        slippage_pips: float = 0.0,
    ) -> bool:
        """Check auto-freeze triggers and freeze if any threshold breached.

        Triggers:
          - consecutive_losses >= AUTO_FREEZE_CONSECUTIVE_LOSSES (3)
          - daily_dd_pct >= AUTO_FREEZE_DAILY_DD_PCT (1.5%)
          - slippage_pips >= AUTO_FREEZE_SLIPPAGE_PIPS (5.0)

        Returns True if the strategy was newly auto-frozen.
        """
        with self._lock:
            st = self._strategy_states.get(strategy_id)
            if st is None:
                st = StrategyFreezeState(strategy_id=strategy_id)
                self._strategy_states[strategy_id] = st

            # Update tracking counters
            st.consecutive_losses = consecutive_losses
            st.daily_dd_pct = daily_dd_pct
            st.last_slippage_pips = slippage_pips

            if st.frozen:
                return False  # Already frozen

            # Check triggers
            if consecutive_losses >= AUTO_FREEZE_CONSECUTIVE_LOSSES:
                self._auto_freeze(
                    strategy_id,
                    st,
                    reason=f"auto: {consecutive_losses} consecutive losses",
                    trigger="consecutive_losses",
                )
                return True

            if daily_dd_pct >= AUTO_FREEZE_DAILY_DD_PCT:
                self._auto_freeze(
                    strategy_id,
                    st,
                    reason=f"auto: daily DD {daily_dd_pct:.2f}% >= {AUTO_FREEZE_DAILY_DD_PCT}%",
                    trigger="daily_dd",
                )
                return True

            if slippage_pips >= AUTO_FREEZE_SLIPPAGE_PIPS:
                self._auto_freeze(
                    strategy_id,
                    st,
                    reason=f"auto: slippage {slippage_pips:.1f} pips >= {AUTO_FREEZE_SLIPPAGE_PIPS}",
                    trigger="slippage",
                )
                return True

            return False

    def _auto_freeze(
        self,
        strategy_id: str,
        st: StrategyFreezeState,
        reason: str,
        trigger: str,
    ) -> None:
        """Internal: execute auto-freeze for a strategy."""
        now = datetime.now(timezone.utc).isoformat()
        st.frozen = True
        st.reason = reason
        st.triggered_by = f"auto_freeze:{trigger}"
        st.triggered_at = now

        self._save_strategy_states()
        self._append_history(
            {
                "ts": now,
                "event": "strategy_auto_frozen",
                "level": self.LEVEL_STRATEGY,
                "strategy_id": strategy_id,
                "reason": reason,
                "trigger": trigger,
                "consecutive_losses": st.consecutive_losses,
                "daily_dd_pct": st.daily_dd_pct,
                "slippage_pips": st.last_slippage_pips,
            }
        )
        logger.warning(
            "STRATEGY AUTO-FROZEN: %s — trigger=%s, reason=%s",
            strategy_id,
            trigger,
            reason,
        )

    # ── Public API: Position Close Tracking ────────────────────────────────

    def record_positions_closed(self, count: int) -> None:
        """Record that positions were closed as part of a kill activation."""
        with self._lock:
            self._state.positions_closed = True
            self._state.close_count = count
            self._save_state()
            self._append_history(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event": "positions_closed",
                    "count": count,
                }
            )

    # ── Persistence ────────────────────────────────────────────────────────

    def _assert_no_harness_production_write(self, source: str) -> None:
        """Belt-and-suspenders guard against harness → production writes.

        Called at every persistence site (``_save_state``, ``_append_history``,
        ``_save_strategy_states``) to refuse writes from a harness-mode
        process whose ``self._state_dir`` resolves to a non-tmp production
        path.

        The ``__init__`` constructor fires the primary guard at construction
        time. This method re-checks at each write so subclasses that bypass
        ``__init__`` (e.g. ``scripts/backtest_blend_harness.py:_HarnessIsolatedKS``
        constructed via ``__new__`` in tests, or any code that reassigns
        ``self._state_dir`` post-init) cannot silently leak harness events
        into the production ``data/kill_switches/history.jsonl`` audit log.

        Card d85c8d89 (root-cause diagnosis card 37227dea): the 2026-09-08
        16:30:25 UTC regression wrote ``ftmo_daily_loss_limit`` to the
        production audit log because the harness entry path skipped the
        per-run isolation patch and the WRITE-SIDE had no ownership-binding
        check. Adding this guard at every persistence site closes the gap.

        Args:
            source: short label of the calling site (e.g. ``"_save_state"``)
                for the error message and log line.

        Raises:
            RuntimeError: when harness mode is active and ``self._state_dir``
                resolves outside the OS tmp prefix.
        """
        if not _is_harness_mode():
            return
        if not _is_production_state_dir(self._state_dir):
            return
        logger.error(
            "REFUSED: harness-mode process attempted %s with production "
            "state_dir=%s. Card d85c8d89: harness runs MUST redirect "
            "state_dir to a temp dir.",
            source,
            self._state_dir,
        )
        raise RuntimeError(
            f"Harness-mode process attempted {source} with production "
            f"state_dir={self._state_dir!r}. Card d85c8d89: harness runs "
            "MUST redirect state_dir to a temp dir."
        )

    def _save_state(self) -> None:
        """Atomic write state to file (temp + rename).

        Never raises on failure — logs CRITICAL but continues operating
        in-memory. The safety implication of a failed write is that the
        kill switch won't survive a restart, which is logged.

        Harness isolation (card d85c8d89): if this method is called from
        a harness-mode process whose ``self._state_dir`` resolves to a
        non-tmp production path, refuse the write loudly. The ``__init__``
        guard normally catches this at construction time, but subclasses
        like ``scripts/backtest_blend_harness.py:_HarnessIsolatedKS`` or
        any post-init ``self._state_dir = ...`` reassignment would slip
        past it; this is the belt-and-suspenders second line of defense.
        """
        # Belt-and-suspenders harness isolation guard (card d85c8d89).
        # ``__init__`` raises if harness + production state_dir, but a
        # subclass or reassignment can land here, so re-check before write.
        self._assert_no_harness_production_write("_save_state")
        try:
            data = self._state.to_dict()
            json_str = json.dumps(data, indent=2)

            # Write to temp file in the SAME directory (guaranteed same filesystem)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._state_dir),
                prefix=".global.state.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(json_str)
                    f.flush()
                    os.fsync(f.fileno())
                # Atomic rename
                os.replace(tmp_path, str(self._state_file))
            except Exception:
                # Clean up temp file on error
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.critical(
                "Failed to persist kill switch state: %s — KILL SWITCH WILL NOT SURVIVE RESTART",
                exc,
            )

    def _load_state(self) -> None:
        """Load state from file on startup.

        Fail-safe behavior:
          - File missing: start inactive (fresh start)
          - File corrupt: default to KILL (safest assumption)
          - File valid: use persisted state
        """
        if not self._state_file.exists():
            logger.info("No kill switch state file found — starting INACTIVE")
            return

        try:
            raw = self._state_file.read_text()
            data = json.loads(raw)
            self._state = GlobalKillState.from_dict(data)

            if self._state.active:
                if self._state.mode == self.MODE_KILL:
                    logger.critical(
                        "STARTUP: Kill switch is ACTIVE (KILL mode): reason=%s, triggered_by=%s, triggered_at=%s",
                        self._state.reason,
                        self._state.triggered_by,
                        self._state.triggered_at,
                    )
                elif self._state.mode == self.MODE_FREEZE:
                    logger.critical(
                        "STARTUP: Kill switch is ACTIVE (FREEZE mode): reason=%s, triggered_by=%s, triggered_at=%s",
                        self._state.reason,
                        self._state.triggered_by,
                        self._state.triggered_at,
                    )

            logger.info(
                "Kill switch state loaded: active=%s, mode=%s",
                self._state.active,
                self._state.mode,
            )

        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            # Corrupt file → fail-safe: default to KILL
            logger.critical(
                "Kill switch state file CORRUPT (%s) — defaulting to KILL for safety",
                exc,
            )
            self._state = GlobalKillState(
                version=STATE_VERSION,
                active=True,
                level=self.LEVEL_GLOBAL,
                mode=self.MODE_KILL,
                reason="corrupt_state_file",
                triggered_by="system_fail_safe",
                triggered_at=datetime.now(timezone.utc).isoformat(),
            )
            self._save_state()
            self._append_history(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event": "fail_safe_activated",
                    "level": self.LEVEL_GLOBAL,
                    "mode": self.MODE_KILL,
                    "reason": "corrupt_state_file",
                    "triggered_by": "system_fail_safe",
                    "error": str(exc),
                }
            )

    def _append_history(self, event: dict) -> None:
        """Append event to history.jsonl (append-only audit log).

        Uses >> append mode. Never raises on failure — logs warning.

        Harness isolation (card d85c8d89): the 2026-09-08 16:30:25 UTC
        regression wrote ``ftmo_daily_loss_limit`` to the production
        ``data/kill_switches/history.jsonl`` from this method, so it is
        the most critical guard site. Refuse the append if harness mode
        is active and ``self._state_dir`` is production-anchored.
        """
        # Belt-and-suspenders harness isolation guard (card d85c8d89).
        # This is the primary regression site (the harness run wrote
        # through this method on 2026-09-08), so it fires loudly here
        # even though __init__ would normally catch it at construction.
        self._assert_no_harness_production_write("_append_history")
        try:
            with open(self._history_file, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as exc:
            logger.warning("Failed to append kill switch history: %s", exc)

    # ── Per-Strategy Persistence ───────────────────────────────────────────

    def _save_strategy_states(self) -> None:
        """Atomically persist all strategy freeze states to JSON.

        Harness isolation (card d85c8d89): refuse the write if harness
        mode is active and ``self._state_dir`` resolves outside the OS
        tmp prefix. The guard is belt-and-suspenders; ``__init__``
        catches this at construction time in the standard case.
        """
        # Belt-and-suspenders harness isolation guard (card d85c8d89).
        self._assert_no_harness_production_write("_save_strategy_states")
        try:
            data = {
                "version": STATE_VERSION,
                "strategies": {sid: st.to_dict() for sid, st in self._strategy_states.items()},
            }
            json_str = json.dumps(data, indent=2)

            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._state_dir),
                prefix=".strategies.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(json_str)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(self._strategy_state_file))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.critical(
                "Failed to persist strategy states: %s — STRATEGY FREEZE STATES WILL NOT SURVIVE RESTART",
                exc,
            )

    def _load_strategy_states(self) -> None:
        """Load strategy freeze states from disk on startup.

        Missing file → empty registry (strategies registered as needed).
        Corrupt file → empty registry + warning (fail-open, not fail-safe,
        because per-strategy freeze is an enhancement layer on top of
        the global kill switch which has its own fail-safe behavior).
        """
        if not self._strategy_state_file.exists():
            logger.info("No strategy state file found — starting with empty registry")
            return

        try:
            raw = self._strategy_state_file.read_text()
            data = json.loads(raw)
            strategies = data.get("strategies", {})

            for sid, sdata in strategies.items():
                self._strategy_states[sid] = StrategyFreezeState.from_dict(sdata)

            frozen_count = sum(1 for s in self._strategy_states.values() if s.frozen)
            logger.info(
                "Strategy states loaded: %d registered, %d frozen",
                len(self._strategy_states),
                frozen_count,
            )

        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                "Strategy state file corrupt (%s) — starting with empty registry",
                exc,
            )
