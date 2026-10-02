#!/usr/bin/env python3
"""Multi-strategy forward test launcher using BlendForwardTestRunner pipeline.

Replaces single-strategy launcher with a 3-strategy blend pipeline:
  FIX Tick Stream → Bar Building → SRMR+/Killzone/Momentum → Correlation Gate → Blend Runner → Paper

Uses existing BlendForwardTestRunner for confidence/risk/sizing and
existing cTraderLiveAdapter for strategy evaluation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal as sig_module
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml  # noqa: F401  — Kept for backward compat (legacy YAML loader removed)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))


def _refuse_root():
    """Refuse to run the trading service as root.

    Ayumi must run as $USER to avoid file-ownership conflicts on
    .env, PID files, lock files and runtime state.  This guard exits
    *before* any broker connection or credential read so that a
    mistaken root launch cannot create state that a subsequent
    $USER launch cannot clean up.
    """
    if os.geteuid() == 0:
        sys.exit(
            "FATAL: Refusing to run Ayumi forward test as root.\n"
            "Use 'systemctl start ayumi-forward-test.service' or run as $USER user.\n"
            "This guard prevents permission conflicts and credential ownership issues."
        )


if __name__ == "__main__":
    _refuse_root()

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import numpy as np
from adapters.ctrader.forward_test_engine import (  # noqa: I001
    ForwardTestConfig,
    ForwardTestEngine,
    _is_forex_market_closed,
)
from adapters.ctrader.models import CTraderTradeSignal, cTraderCredentials
from adapters.ctrader.risk_guard import FTMOConfig
from common.logging_config import setup_logging
from core.types import Bar, BarPeriod
from forward_test.blend_runner import BlendForwardTestRunner
from regime.detector import Regime, RegimeConfig, RegimeDetector
from reporting.equity_tracker import EquityTracker
from risk.ftmo_guard import FTMOGuard
from risk.ftmo_params import FTMO_REFERENCE_ACCOUNT_SIZE
from strategies.donchian_atr_trend_v2 import (
    DonchianATRConfig,
    DonchianATRTrendV2Strategy,
)
from strategies.dual_tf_squeeze_pro import (
    DualTFSqueezeProConfig,
    DualTFSqueezeProStrategy,
)
from strategies.killzone_momentum import (
    KillzoneMomentumConfig,
    KillzoneMomentumStrategy,
)
from strategies.london_breakout_retest import (
    LondonBreakoutConfig,
    LondonBreakoutRetestStrategy,
)
from strategies.srmr_plus import (
    SRMRPlusConfig,
    SRMRPlusStrategy,
    load_srmr_config_from_yaml,
)
from strategies.ttc_xauusd import TTCXAUUSDStrategy

logger = logging.getLogger("ayumi.blend_launcher")

import os as _os

_os.umask(0o022)  # Ensure files are created 644/755 regardless of process owner


# ── Correlation Gate ──────────────────────────────────────────────────────────


def _compute_pnl_from_start_pct(start: float, current: float) -> float:
    """Account P&L % from starting balance (positive = up, negative = down).

    Canonical display field for the heartbeat ``risk:`` segment.  Renamed
    from the buggy ``_dd_pct`` whose formula ``(start - current) / start``
    produced a drawdown with flipped sign — the account would report
    ``dd=-2.62%`` while it was UP +2.62% (see card 627b4f66, evidence
    DA-1).

    The canonical peak-based drawdown lives in
    ``FTMOGuard.get_status()['current_dd_pct']`` and is reported
    separately in the ``ftmo:`` segment of the heartbeat.

    Returns 0.0 for non-positive starting balance (sentinel — never
    divide by zero or report a misleading percentage for a zero/negative
    baseline).
    """
    if start <= 0:
        return 0.0
    return (current - start) / start * 100.0


def _reconcile_ftmo_peak_from_persisted_state(
    ftmo_guard: FTMOGuard,
    state_path: Path,
) -> float:
    """Reconcile ``FTMOGuard._state.peak_balance`` with the persisted RiskGuard
    state file on startup (card 627b4f66, evidence DA-8).

    Why: ``FTMOGuard.__init__`` (see ``risk/ftmo_guard.py``) seeds
    ``peak_balance = starting_balance``.  Without reconcile, every restart
    would silently reset the FTMO peak high-water-mark to the reference
    account size ($10K), even when the broker has previously reported a
    higher balance (e.g. $10,415.81).  This corrupts the trailing-DD
    calculation (drawdown reads larger than reality after each restart).

    Paper-vs-live semantics: ``data/state/risk_guard_state.json`` is
    written by ``RiskGuard`` in both paper and live modes.  The same
    reconcile applies to both — a high-water-mark observed in paper
    mode must not be silently lost on the next live restart, and vice
    versa.  Peak is peak regardless of trading mode.

    The reconcile is the MAX of persisted peak and the guard's current
    peak, never below.  If persisted peak <= current peak, no mutation
    is performed and the guard's existing value is preserved (the
    in-memory value is at least as fresh as the persisted one).

    Failure handling: any I/O or parse error returns the guard's existing
    peak unchanged and logs a warning.  The reconcile is best-effort
    startup hygiene — never fatal.

    Returns the resolved peak after reconcile (for tests + logging).
    """
    try:
        if not state_path.exists():
            return ftmo_guard._state.peak_balance
        raw = json.loads(state_path.read_text())
        persisted_peak = float(raw.get("peak_balance", 0.0))
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        logger.warning(
            "FTMO peak reconcile skipped: %s (%s)", state_path, exc,
        )
        return ftmo_guard._state.peak_balance

    if persisted_peak > ftmo_guard._state.peak_balance:
        logger.info(
            "FTMO peak reconciled UP: %.2f -> %.2f (persisted state)",
            ftmo_guard._state.peak_balance,
            persisted_peak,
        )
        ftmo_guard._state.peak_balance = persisted_peak
    return ftmo_guard._state.peak_balance


class CorrelationGate:
    """Per-symbol at-risk slot cap (Craig policy, card 4083ac2d-...).

    Replaces the legacy single-slot-per-(symbol, direction) cap with a cap
    of ``MAX_ATRISK_PER_SYMBOL`` concurrent AT-RISK positions per symbol.
    Risk-free positions (LONG with SL >= entry, SHORT with SL <= entry) do
    NOT count against the cap and may breathe without occupying slots.

    At-risk definition (Craig, 2026-09-08):
      LONG  at-risk iff current SL <  entry  (a stop-out loses money).
      SHORT at-risk iff current SL >  entry.
      Risk-free iff LONG: SL >= entry  OR  SHORT: SL <= entry.

    Lifecycle:
      check()             — atomically reserves a pending slot if the symbol
                            has < MAX_ATRISK_PER_SYMBOL at-risk positions.
                            Pending slots are counted as at-risk (conservative)
                            until the trade fills.
      attach_position()   — promotes a pending slot to a real slot using the
                            actual position_id, entry_price, and SL after fill.
      update_sl()         — updates a real slot's SL; emits slot_release when
                            the position transitions from at-risk to risk-free.
      release_position()  — removes a specific position slot (on close).
      release_pending()   — removes a pending reservation (on signal reject
                            downstream, before the trade fills).
      release()           — legacy back-compat: release by (symbol, direction).

    External API preserved where cheap (``check``, ``release``, ``active_count``)
    so the 12 internal call sites and the harness import path
    (``scripts/backtest_blend_harness.py:361``) keep working without churn.
    """

    MAX_ATRISK_PER_SYMBOL = 3  # Craig policy 2026-09-08: max 3 at-risk per symbol.

    def __init__(self) -> None:
        # Real slots: position_id -> _Slot (entry+sl known, is_at_risk computable).
        self._slots: dict[str, _Slot] = {}
        # Pending reservations (signal accepted, trade not yet filled).
        # Key: (symbol, strategy_id) so attach_position() can find and promote.
        # Value: position_id placeholder (string starting with "_pending:").
        self._pending: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        # Slot-release event log (in-memory diagnostics; not persisted).
        self._slot_release_log: list[dict[str, Any]] = []
        self._slot_release_callbacks: list[Callable[[dict[str, Any]], None]] = []

    # ── Public API ───────────────────────────────────────────────────────────

    def check(self, symbol: str, direction: str, strategy_id: str) -> tuple[bool, str]:
        """Reserve a pending slot iff symbol has < MAX_ATRISK_PER_SYMBOL at-risk.

        Returns (allowed, reason). Pending slots count as at-risk (conservative)
        until the trade fills and ``attach_position`` is called.
        """
        sym = symbol.upper()
        dir_ = direction.upper()
        with self._lock:
            at_risk = self._count_at_risk_locked(sym)
            if at_risk >= self.MAX_ATRISK_PER_SYMBOL:
                return (
                    False,
                    (
                        f"symbol_atrisk_cap: {sym} has {at_risk} at-risk positions "
                        f"(max {self.MAX_ATRISK_PER_SYMBOL})"
                    ),
                )
            pending_id = f"_pending:{sym}:{strategy_id}:{len(self._slots) + len(self._pending)}"
            self._pending[(sym, strategy_id)] = pending_id
            self._slots[pending_id] = _Slot(
                position_id=pending_id,
                symbol=sym,
                direction=dir_,
                strategy_id=strategy_id,
                entry_price=None,
                sl=None,
                is_pending=True,
            )
            return True, ""

    def attach_position(
        self,
        symbol: str,
        strategy_id: str,
        position_id: str,
        direction: str,
        entry_price: float,
        sl: float,
    ) -> None:
        """Promote the pending reservation to a real slot with entry + SL.

        Called after the trade fills (paper or live). Looks up the pending
        slot by (symbol, strategy_id) and replaces it with a slot keyed by
        the actual ``position_id``.
        """
        sym = symbol.upper()
        dir_ = direction.upper()
        with self._lock:
            self._pending.pop((sym, strategy_id), None)
            # Drop the pending placeholder slot if present.
            attach_pids = [
                p
                for p, s in self._slots.items()
                if s.is_pending and s.symbol == sym and s.strategy_id == strategy_id
            ]
            for pid in attach_pids:
                self._slots.pop(pid, None)
            self._slots[position_id] = _Slot(
                position_id=position_id,
                symbol=sym,
                direction=dir_,
                strategy_id=strategy_id,
                entry_price=entry_price,
                sl=sl,
                is_pending=False,
            )

    def update_sl(self, position_id: str, new_sl: float) -> bool:
        """Update a real slot's SL; emit slot_release when at-risk → risk-free.

        Returns True if a slot_release event fired. No-op for unknown or
        pending slots (no SL known yet).
        """
        with self._lock:
            slot = self._slots.get(position_id)
            if not slot or slot.is_pending or slot.entry_price is None:
                return False
            was_at_risk = slot.is_at_risk
            updated = _Slot(
                position_id=slot.position_id,
                symbol=slot.symbol,
                direction=slot.direction,
                strategy_id=slot.strategy_id,
                entry_price=slot.entry_price,
                sl=new_sl,
                is_pending=False,
            )
            self._slots[position_id] = updated
            now_at_risk = updated.is_at_risk
            transitioned = was_at_risk and not now_at_risk
            if transitioned:
                event = {
                    "event": "slot_release",
                    "position_id": position_id,
                    "symbol": updated.symbol,
                    "direction": updated.direction,
                    "strategy_id": updated.strategy_id,
                    "entry_price": updated.entry_price,
                    "new_sl": new_sl,
                    "reason": "sl_to_breakeven_or_profit",
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                self._slot_release_log.append(event)
                logger.info(
                    "slot_release: %s %s position_id=%s SL=%.5f entry=%.5f "
                    "(transitioned at-risk → risk-free, slot freed)",
                    updated.symbol,
                    updated.direction,
                    position_id,
                    new_sl,
                    updated.entry_price if updated.entry_price is not None else 0.0,
                )
                for cb in self._slot_release_callbacks:
                    try:
                        cb(event)
                    except Exception as cb_exc:  # noqa: BLE001
                        logger.warning("slot_release callback failed: %s", cb_exc)
            return transitioned

    def release_position(self, position_id: str) -> None:
        """Remove a specific position's slot (called on position close)."""
        with self._lock:
            self._slots.pop(position_id, None)

    def release_pending(self, symbol: str, strategy_id: str) -> None:
        """Remove a pending reservation by (symbol, strategy_id).

        Used when the blend runner rejects a signal after the gate already
        reserved a pending slot but before the trade fills.
        """
        sym = symbol.upper()
        with self._lock:
            self._pending.pop((sym, strategy_id), None)
            pending_pids = [
                p
                for p, s in self._slots.items()
                if s.is_pending and s.symbol == sym and s.strategy_id == strategy_id
            ]
            for pid in pending_pids:
                self._slots.pop(pid, None)

    def release(self, symbol: str, direction: str, position_id: Optional[str] = None) -> None:
        """Legacy back-compat release.

        With ``position_id``: removes only that slot (preferred post-fill path).
        Without ``position_id``: drops ONLY the pending reservation matching
        (symbol, direction) — matches the legacy single-slot semantics where
        each (symbol, direction) tuple had its own slot. Real (filled) slots
        are NEVER removed by this overload; they are managed via
        ``release_position(position_id)`` after the trade fills.
        """
        sym = symbol.upper()
        dir_ = direction.upper()
        with self._lock:
            if position_id is not None:
                self._slots.pop(position_id, None)
                return
            # Legacy back-compat path.  Drops ALL pending reservations
            # matching (symbol, direction) regardless of strategy_id — only
            # used as a fallback in on_position_closed_release when the
            # position has no position_id.  Pre-fill rejection paths use
            # release_pending(symbol, strategy_id) instead so they don't
            # remove other strategies' pending reservations.
            #
            # Walk pending slots, drop matching ones, then sweep orphan
            # _pending entries whose slot no longer exists so the dict
            # stays consistent.  (The previous version of this block had a
            # dead loop that compared k[1] (strategy_id) to direction —
            # never matched, so it never ran.  The _slots sweep below did
            # the actual work but left orphan _pending entries behind.)
            for pid in [
                p
                for p, s in self._slots.items()
                if s.is_pending and s.symbol == sym and s.direction == dir_
            ]:
                self._slots.pop(pid, None)
            orphan_keys = [
                k
                for k, pid in self._pending.items()
                if pid not in self._slots
            ]
            for k in orphan_keys:
                self._pending.pop(k, None)

    # ── Diagnostics / hooks ──────────────────────────────────────────────────

    def on_slot_release(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback fired on slot_release events (SL → breakeven/profit)."""
        with self._lock:
            self._slot_release_callbacks.append(callback)

    @property
    def active_count(self) -> int:
        """Total tracked slots (pending + real). Back-compat with the prior API."""
        with self._lock:
            return len(self._slots)

    def at_risk_count(self, symbol: str) -> int:
        """Number of currently at-risk positions for the symbol (incl. pending)."""
        sym = symbol.upper()
        with self._lock:
            return self._count_at_risk_locked(sym)

    def at_risk_counts(self) -> dict[str, int]:
        """Per-symbol at-risk counts snapshot for diagnostics."""
        with self._lock:
            counts: dict[str, int] = {}
            for slot in self._slots.values():
                if slot.is_at_risk:
                    counts[slot.symbol] = counts.get(slot.symbol, 0) + 1
            return counts

    def slot_release_log(self) -> list[dict[str, Any]]:
        """Snapshot of slot_release events fired (most recent last)."""
        with self._lock:
            return list(self._slot_release_log)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _count_at_risk_locked(self, symbol_upper: str) -> int:
        """Caller MUST hold ``self._lock``."""
        n = 0
        for slot in self._slots.values():
            if slot.symbol == symbol_upper and slot.is_at_risk:
                n += 1
        return n


@dataclass(frozen=True)
class _Slot:
    """Internal record for a single tracked slot (pending reservation or real position).

    ``is_at_risk`` follows Craig's verbatim definition:
      LONG  at-risk iff SL <  entry.
      SHORT at-risk iff SL >  entry.
      Pending slots (entry/SL unknown) count as at-risk (conservative default).
    """

    position_id: str
    symbol: str
    direction: str
    strategy_id: str
    entry_price: Optional[float]
    sl: Optional[float]
    is_pending: bool

    @property
    def is_at_risk(self) -> bool:
        if self.is_pending or self.entry_price is None or self.sl is None:
            return True  # conservative default until entry/SL known
        if self.direction == "LONG":
            return self.sl < self.entry_price
        if self.direction == "SHORT":
            return self.sl > self.entry_price
        # Unknown direction — conservative default.
        return True


# ── Regime Gate ───────────────────────────────────────────────────────────────


class RegimeGate:
    """Regime-based signal gate. Each strategy has a regime affinity.

    A signal passes only if the current market regime is in the strategy's
    allowed set, the ADX falls in the strategy's sweet spot (if defined),
    and the bar's UTC hour falls in the strategy's allowed session window
    (if defined).  The gate is intentionally permissive at the default
    config — if a strategy has no entry for a given key, the corresponding
    dimension is unconstrained.
    """

    def __init__(self):
        self._detector = RegimeDetector(RegimeConfig())
        # Strategy → set of allowed regimes. Signal only passes if current regime matches.
        self._strategy_regimes = {
            "killzone_momentum": {Regime.QUIET, Regime.CHOPPY},
            "dual_tf_squeeze_pro": {Regime.VOLATILE, Regime.CHOPPY},
            "donchian_atr_trend_v2": {Regime.QUIET, Regime.CHOPPY, Regime.TRENDING},
            "srmr_plus": {Regime.QUIET},
            "london_breakout_retest": {
                Regime.QUIET,
                Regime.CHOPPY,
                Regime.TRENDING,
                Regime.VOLATILE,
            },
        }
        # Strategy → ADX range [min, max]. (0,100) disables the ADX gate.
        self._strategy_adx = {
            "killzone_momentum": (18.0, 25.0),
            "dual_tf_squeeze_pro": (0.0, 100.0),  # no ADX gate
            "donchian_atr_trend_v2": (0.0, 30.0),
            "srmr_plus": (0.0, 100.0),  # no ADX gate
            "london_breakout_retest": (15.0, 30.0),
        }
        # Strategy → allowed sessions (hours UTC). None disables the session gate.
        #   asia   : 0-7 UTC
        #   london : 7-12 UTC
        #   ny_am  : 12-17 UTC
        self._strategy_sessions = {
            "killzone_momentum": {"london"},
            "dual_tf_squeeze_pro": {"asia", "ny_am"},
            "donchian_atr_trend_v2": None,
            "srmr_plus": {"london"},
            "london_breakout_retest": {"london"},
        }

    @staticmethod
    def _hour_to_session(hour_utc: int) -> str:
        if 0 <= hour_utc < 7:
            return "asia"
        elif 7 <= hour_utc < 12:
            return "london"
        elif 12 <= hour_utc < 17:
            return "ny_am"
        return "other"

    def check(self, strategy_id: str, bars: list, current_bar) -> tuple[bool, str]:
        """Returns (allowed, reason).

        ``allowed=True`` means the gate has nothing to say about this signal;
        downstream logic still applies (correlation gate, blend runner, etc.).
        ``allowed=False`` means the signal should be suppressed.
        """
        regime_allowed = self._strategy_regimes.get(strategy_id)
        if regime_allowed is None:
            return True, "no_gate"

        # Need enough bars for regime detection (ADX warm-up + ATR percentile window).
        if len(bars) < 100:
            return False, "insufficient_bars_for_regime"

        highs = np.array([b.high for b in bars[-100:]])
        lows = np.array([b.low for b in bars[-100:]])
        closes = np.array([b.close for b in bars[-100:]])

        try:
            current_regime = self._detector.detect_current(highs, lows, closes)
        except Exception:
            return False, "regime_detection_failed"

        if current_regime not in regime_allowed:
            return False, f"regime_{current_regime.value}_not_in_allowed"

        # ADX gate — only applies when a non-default range is configured.
        adx_range = self._strategy_adx.get(strategy_id)
        if adx_range and adx_range != (0.0, 100.0):
            try:
                from indicators import adx

                adx_vals = adx(highs, lows, closes, 14)
                if adx_vals is not None and len(adx_vals) > 0:
                    current_adx = float(adx_vals[-1])
                    if current_adx < adx_range[0] or current_adx > adx_range[1]:
                        return False, f"adx_{current_adx:.1f}_outside_{adx_range}"
            except Exception:  # noqa: S110
                # Don't block on ADX calculation failure — regime check already passed.
                pass

        # Session gate.
        sessions = self._strategy_sessions.get(strategy_id)
        if sessions is not None:
            hour_utc = getattr(getattr(current_bar, "time", None), "hour", 0)
            session = self._hour_to_session(int(hour_utc))
            if session not in sessions:
                return False, f"session_{session}_not_in_allowed"

        return True, "passed"


# ── Counterfactual Regime-Gate Observability (card d2be30f4) ─────────────────

# Expanded gate that Phase 1.3 will experiment with: same regimes the
# OPTUNA-validated 3-strategy blend runs under, plus the two killzones
# where London Breakout Retest and Donchian ATR Trailing Trend v2
# historically do their best work.  This is a dict literal — there is NO
# env override for the expanded gate (F-1, deliberately out of scope for
# this card; the override would land on a follow-up card before the
# gate-loosening experiment).
_EXPANDED_REGIMES: frozenset = frozenset({Regime.QUIET, Regime.CHOPPY, Regime.TRENDING})
_EXPANDED_SESSIONS: frozenset = frozenset({"london", "ny_am"})

# Path is project-root-relative so the live process writes to the same
# location the operator reads from in heartbeats.  Default off — the
# logger singleton returns None until AYUMI_COUNTERFACTUAL_LOG=1 is set.
_COUNTERFACTUAL_LOG_PATH: Path = PROJECT_ROOT / "data" / "counterfactual_gate_log.jsonl"


def _counterfactual_log_enabled() -> bool:
    """Card d2be30f4 AC1: only the literal env value '1' enables the log.

    Re-evaluated on every call so the operator can toggle the flag at
    runtime without restarting the live launcher (the next per-bar call
    will pick up the new value via ``os.getenv``).
    """
    return os.getenv("AYUMI_COUNTERFACTUAL_LOG", "") == "1"


def _expanded_gate_would_pass(
    regime: Regime | None,
    session: str,
) -> str:
    """Apply the expanded gate (regimes {QUIET, CHOPPY, TRENDING} AND sessions {london, ny_am}).

    Returns ``"pass"`` iff the bar's detected regime is in the expanded
    regime set AND the bar's session is in the expanded session set.
    Returns ``"reject"`` otherwise — including when the regime detector
    returned ``None`` (insufficient bars or detector failure) so the
    counterfactual log is honest about the unknown case.  This matches
    the spec's pass/reject-only schema.
    """
    if regime is None:
        return "reject"
    if regime not in _EXPANDED_REGIMES:
        return "reject"
    if session not in _EXPANDED_SESSIONS:
        return "reject"
    return "pass"


def _extract_regime_and_session(
    bars: list,
    current_bar,
) -> tuple[Regime | None, float | None, str]:
    """Strategy-agnostic regime / ADX / session extraction for the counterfactual log.

    Mirrors :meth:`RegimeGate.check`'s data extraction (last 100 bars,
    ``RegimeDetector.detect_current``, ADX(14), hour-to-session mapping)
    but does NOT apply the strategy-specific regime / ADX / session
    filters.  Returns ``(None, None, session)`` when there are fewer
    than 100 bars or the detector raises — the log line must still
    emit so operators can see the unknown-regime cases.

    The session is computed independently of the regime because the
    current gate may reject on session before the regime even matters;
    the expanded gate's two predicates (regime AND session) are
    evaluated separately by :func:`_expanded_gate_would_pass`.
    """
    session = "unknown"
    try:
        hour_utc = getattr(getattr(current_bar, "time", None), "hour", None)
        if hour_utc is not None:
            session = RegimeGate._hour_to_session(int(hour_utc))
    except Exception:  # noqa: S110 — defensive, never crash the launcher
        session = "unknown"

    if len(bars) < 100:
        return None, None, session

    try:
        highs = np.array([b.high for b in bars[-100:]])
        lows = np.array([b.low for b in bars[-100:]])
        closes = np.array([b.close for b in bars[-100:]])
        regime = RegimeDetector(RegimeConfig()).detect_current(highs, lows, closes)
    except Exception:
        return None, None, session

    adx_value: float | None = None
    try:
        from indicators import adx as _adx

        adx_series = _adx(highs, lows, closes, 14)
        if adx_series is not None and len(adx_series) > 0:
            adx_value = float(adx_series.iloc[-1]) if hasattr(adx_series, "iloc") else float(adx_series[-1])
    except Exception:  # noqa: S110 — ADX failure must not block logging
        adx_value = None

    return regime, adx_value, session


def _build_counterfactual_payload(
    symbol: str,
    strategy_id: str,
    regime: Regime | None,
    session: str,
    adx: float | None,
    current_gate_decision: str,
    expanded_gate_decision: str,
    strategy_emitted: bool,
) -> dict:
    """Build one JSONL line per card d2be30f4 spec.

    Field shape matches the spec exactly: ``{ts, symbol, strategy_id,
    regime, session, adx, current_gate, expanded_gate_would,
    strategy_emitted}``.  ``ts`` is set by the caller (UTC ISO-8601) so
    this helper stays pure and easy to test.
    """
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "strategy_id": strategy_id,
        "regime": regime.value if regime is not None else "unknown",
        "session": session,
        "adx": adx,
        "current_gate": current_gate_decision,
        "expanded_gate_would": expanded_gate_decision,
        "strategy_emitted": strategy_emitted,
    }


class _CounterfactualLogger:
    """Best-effort JSONL writer for card d2be30f4.

    The file handle is opened lazily on the first write so the off-by-
    default path pays zero I/O cost.  All OSError paths (open failure,
    write failure, full disk, permission denied) are caught and logged
    ONCE — subsequent failures stay silent so a flaky disk does not
    flood the logs.  This logger is observability infrastructure and
    must never crash the live launcher (AC: ``Log writes must never
    crash the live launcher``).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path if path is not None else _COUNTERFACTUAL_LOG_PATH
        self._fh = None
        self._warned_failure = False

    def _open_if_needed(self) -> None:
        if self._fh is not None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._path, "a", encoding="utf-8")
        except OSError as exc:
            if not self._warned_failure:
                logger.warning(
                    "Counterfactual log: failed to open %s: %s (further errors suppressed)",
                    self._path,
                    exc,
                )
                self._warned_failure = True
            self._fh = None

    def write(self, payload: dict) -> None:
        """Best-effort JSONL write.  Never raises."""
        try:
            self._open_if_needed()
            if self._fh is None:
                return
            self._fh.write(json.dumps(payload, default=str) + "\n")
            self._fh.flush()
        except OSError as exc:
            if not self._warned_failure:
                logger.warning(
                    "Counterfactual log: write failed: %s (further errors suppressed)",
                    exc,
                )
                self._warned_failure = True

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    @property
    def warned_failure(self) -> bool:
        return self._warned_failure

    @property
    def path(self) -> Path:
        return self._path


# Module-level singleton — lazily initialized by ``_get_counterfactual_logger``
# only when the env flag is on.  Kept at module scope so the file handle
# stays open for the lifetime of the launcher (closing/reopening on
# every bar would defeat the point of streaming observability).
_counterfactual_logger: _CounterfactualLogger | None = None


def _get_counterfactual_logger() -> _CounterfactualLogger | None:
    """Return the lazy singleton logger, or ``None`` if the flag is off.

    Off-by-default (returns ``None`` and pays zero I/O cost) — the
    calling code uses ``if _cf_logger is None: continue`` as the
    default-off branch.
    """
    global _counterfactual_logger
    if not _counterfactual_log_enabled():
        return None
    if _counterfactual_logger is None:
        _counterfactual_logger = _CounterfactualLogger()
    return _counterfactual_logger


# ── Heartbeat ─────────────────────────────────────────────────────────────────


class HeartbeatTracker:
    """Logs pipeline health every N bars."""

    def __init__(self, interval: int = 100):
        self._interval = interval
        self._bars_evaluated = 0
        self._signals_generated = 0
        self._signals_accepted = 0
        self._signals_rejected = 0
        self._lock = threading.RLock()

    def record_bar(self):
        with self._lock:
            self._bars_evaluated += 1
            if self._bars_evaluated % self._interval == 0:
                self._log()

    def record_signal(self, accepted: bool):
        with self._lock:
            self._signals_generated += 1
            if accepted:
                self._signals_accepted += 1
            else:
                self._signals_rejected += 1

    def _log(self):
        with self._lock:
            total_rejected = self._signals_rejected
            total_accepted = self._signals_accepted
            total_generated = self._signals_generated
            bars = self._bars_evaluated
        logger.info(
            "Heartbeat: Bars evaluated=%d, Signals generated=%d, Accepted=%d, Rejected=%d",
            bars,
            total_generated,
            total_accepted,
            total_rejected,
        )


# ── Bar Fetcher (now handled by single-connection spot feed) ───────────────

GBPUSD_SYMBOL_ID = 2
USDJPY_SYMBOL_ID = 4

# Symbol name → OpenAPI symbol_id mapping (static fallback)
SYMBOL_IDS = {
    "GBPUSD": 2,
    "USDJPY": 4,
    "EURUSD": 1,
}


def raw_bars_to_bar_objects(raw_bars: list[dict], period: BarPeriod | None = None) -> list[Bar]:
    """Convert OpenAPI raw bar dicts to Bar objects."""
    if period is None:
        period = BarPeriod.H1()
    result = []
    for rb in raw_bars:
        result.append(
            Bar(
                time=datetime.fromtimestamp(rb["timestamp"] / 1000, tz=timezone.utc),
                open=rb["open"],
                high=rb["high"],
                low=rb["low"],
                close=rb["close"],
                volume=rb["volume"],
                period=period,
            )
        )
    return result


# ── Signal Conversion ─────────────────────────────────────────────────────────


def trade_signal_to_blend_dict(signal: CTraderTradeSignal, strategy_name: str) -> dict:
    """Convert cTrader CTraderTradeSignal to the dict format BlendForwardTestRunner.on_signal() expects."""
    return {
        "symbol": signal.symbol,
        "direction": signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
        "entry_price": signal.entry_price,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit_1 or 0.0,
        "confidence": signal.confidence,
        "timestamp": datetime.now(timezone.utc),
    }


def _evaluate_b5_health_warning(health, live_fills: int, live_mode: bool) -> dict | None:
    """Card 0d7d7557: predicate for the B5 zero-fills-while-attempting warning.

    Returns a dict with the warning payload (branch + counts) when the
    warning should fire, or ``None`` when it should stay silent.

    The original predicate used ``signals_generated > 0`` which misfired
    on regime-gate / correlation / sizer rejections (signals blocked
    upstream of the broker). The predicate now uses attempted-submission
    count ``signals_sent + signals_failed_live + signals_unreachable``
    so only broker-attempted signals can trigger the warning. Pre-flight
    skips (outcome is None) do NOT increment any counter, so they
    correctly keep the warning silent.

    Branches (card 0d7d7557 iter2):
        rejected     — signals_failed_live > 0: orders reached broker and
                      were REJECTED / CANCELLED. Operators should
                      inspect the rejection log for the errorCode.
        unreachable — signals_unreachable > 0: broker contact failed
                      (NOT_CONNECTED, pre- or post-feed). Operators
                      should inspect the spot-feed connection state, NOT
                      the rejection log — the broker was never
                      consulted in this branch.
        no_fill     — signals_sent > 0, signals_failed_live == 0,
                      signals_unreachable == 0: orders SENT / TIMEOUT
                      awaiting ack, no fill yet.

    Branch priority: if both rejected and unreachable are non-zero, the
    rejected branch wins (rejections are the more diagnostic failure
    mode; unreachability is downstream of feed health, not order
    rejection). Callers can still inspect ``health.signals_unreachable``
    for the unreachability count independently.
    """
    if not live_mode:
        return None
    attempted = health.signals_sent + health.signals_failed_live + getattr(health, "signals_unreachable", 0)
    if attempted == 0 or live_fills > 0:
        return None
    if health.signals_failed_live > 0:
        return {
            "branch": "rejected",
            "attempted": attempted,
            "sent": health.signals_sent,
            "failed": health.signals_failed_live,
            "unreachable": getattr(health, "signals_unreachable", 0),
        }
    if getattr(health, "signals_unreachable", 0) > 0:
        return {
            "branch": "unreachable",
            "attempted": attempted,
            "sent": health.signals_sent,
            "unreachable": getattr(health, "signals_unreachable", 0),
        }
    return {
        "branch": "no_fill",
        "sent": health.signals_sent,
    }


# ── Blend-Aware Forward Test Engine ──────────────────────────────────────────


class BlendForwardTestEngine(ForwardTestEngine):
    """Extended ForwardTestEngine that routes signals through BlendForwardTestRunner
    instead of directly to PaperTrader."""

    def __init__(
        self,
        *args,
        blend_runner: Optional[BlendForwardTestRunner] = None,
        correlation_gate: Optional[CorrelationGate] = None,
        heartbeat: Optional[HeartbeatTracker] = None,
        strategy_id_map: Optional[dict[str, str]] = None,
        regime_gate: Optional[RegimeGate] = None,
        **kwargs,
    ):
        # Card 4083ac2d-...: when a correlation gate is provided, wire its
        # update_sl method as the engine's slot_tracker so live SL amends
        # keep the per-symbol at-risk cap accurate. Resolved eagerly so the
        # gate reference passed by the caller is the same instance used
        # elsewhere in the launcher (otherwise the engine would hold a
        # different CorrelationGate than _route_signal uses).
        gate = correlation_gate or CorrelationGate()
        if "slot_tracker" not in kwargs:
            kwargs["slot_tracker"] = gate.update_sl
        super().__init__(*args, **kwargs)
        self._blend_runner = blend_runner
        self._correlation_gate = gate
        self._heartbeat = heartbeat or HeartbeatTracker()
        self._strategy_id_map = strategy_id_map or {}  # strategy_name -> strategy_id
        self._regime_gate = regime_gate or RegimeGate()

    def _blend_signal_id(self, signal: CTraderTradeSignal) -> str:
        """Delegate to BlendForwardTestRunner.make_signal_id so we never
        drift out of sync with the canonical id construction.

        Must match the pattern in blend_runner.on_signal:
            signal.strategy_id + "_" + str(signal.timestamp.timestamp())
        """
        return self._blend_runner.make_signal_id(signal)

    def _evaluate_strategies(self, symbol: str):
        """Override: route signals through blend pipeline with multi-TF support."""
        if self._live_adapter is None:
            return

        if not self._eval_semaphore.acquire(blocking=False):
            return

        try:
            # Build per-timeframe bar snapshots
            tf_bars: dict[int, list[Bar]] = {}
            with self._lock:
                for tf in self._required_timeframes:
                    key = self._bar_key(symbol, tf)
                    bars = list(self._bars.get(key, []))
                    current = self._current_bar.get(key)
                    if current is not None:
                        bars.append(current)
                    tf_bars[tf] = bars

            if not any(tf_bars.values()):
                return

            generated = 0
            for strategy_name in self._live_adapter._strategies.keys():
                # Resolve this strategy's timeframe
                tf = self._strategy_timeframes.get(strategy_name, self._config.bar_period_minutes)
                bars = tf_bars.get(tf, [])

                # Per-timeframe evaluation threshold (Rei #7)
                if len(bars) < self._config.min_bars_for_evaluation:
                    continue

                from backtest.engine import MarketState

                state = MarketState(bars=bars)

                # Per-strategy exception isolation (Kaito #3)
                try:
                    adapter = self._live_adapter.get_adapter(strategy_name, symbol)
                    if not adapter:
                        continue
                    s = adapter.evaluate_and_trade(state, spread=self._current_spread)
                except Exception as exc:
                    logger.error(
                        "Strategy %s evaluation error: %s",
                        strategy_name,
                        exc,
                        exc_info=True,
                    )
                    with self._lock:
                        self._health.evaluation_errors += 1
                    continue

                # S1: Per-strategy diagnostic counters (mirrors base class pattern)
                # Bump counters for every strategy that passes the bar threshold,
                # regardless of whether a signal was generated. This is what makes
                # the [S1 Health] log show non-zero evals. (BQ-1037)
                with self._lock:
                    self._strategy_eval_counts[strategy_name] = self._strategy_eval_counts.get(strategy_name, 0) + 1
                    if s is None:
                        self._strategy_no_signal_counts[strategy_name] = (
                            self._strategy_no_signal_counts.get(strategy_name, 0) + 1
                        )
                    self._strategy_last_eval[strategy_name] = time.monotonic()

                # S1: INFO-level per-strategy eval log
                logger.info(
                    "[S1] Strategy %s: eval #%d, signals=%d, total_no_signal=%d",
                    strategy_name,
                    self._strategy_eval_counts[strategy_name],
                    1 if s is not None else 0,
                    self._strategy_no_signal_counts[strategy_name],
                )

                # Card d2be30f4: resolve the canonical strategy_id BEFORE
                # the no-signal short-circuit so it is available to both
                # branches of the counterfactual log.
                _strategy_id_for_regime = self._strategy_id_map.get(
                    strategy_name,
                    strategy_name.lower().replace(" ", "_"),
                )

                if s is None:
                    # Card d2be30f4: counterfactual observability —
                    # log the no-signal evaluation so operators can
                    # distinguish "strategy never fires" (this branch)
                    # from "gate blocks fires" (the next branch).
                    # Gate behavior is UNCHANGED — the live launcher
                    # still skips the broker path on no-signal.  The
                    # log is additive only, env-gated off by default.
                    _cf_logger = _get_counterfactual_logger()
                    if _cf_logger is not None:
                        _cf_regime, _cf_adx, _cf_session = _extract_regime_and_session(
                            bars,
                            bars[-1] if bars else None,
                        )
                        _cf_logger.write(
                            _build_counterfactual_payload(
                                symbol=symbol,
                                strategy_id=_strategy_id_for_regime,
                                regime=_cf_regime,
                                session=_cf_session,
                                adx=_cf_adx,
                                current_gate_decision="reject",
                                expanded_gate_decision=_expanded_gate_would_pass(
                                    _cf_regime, _cf_session,
                                ),
                                strategy_emitted=False,
                            )
                        )
                    continue

                generated += 1
                self._heartbeat.record_bar()

                # Regime gate check (must run BEFORE correlation gate,
                # which is invoked by _route_signal).  The gate is
                # internally defensive (try/except around detector +
                # ADX) so a single bad bar cannot crash the eval loop.
                try:
                    regime_allowed, regime_reason = self._regime_gate.check(
                        _strategy_id_for_regime,
                        bars,
                        bars[-1] if bars else None,
                    )
                except Exception as _rg_exc:
                    # Gate must never crash the eval loop — fail open.
                    logger.warning(
                        "[REGIME-GATE] check raised on %s: %s — allowing signal",
                        strategy_name,
                        _rg_exc,
                    )
                    regime_allowed, regime_reason = True, "gate_error_fail_open"
                # Card d2be30f4: counterfactual observability — log the
                # actual gate decision (pass/reject) and what the
                # EXPANDED gate would have done.  Reads the gate's
                # already-computed verdict; does NOT call the gate
                # twice.  Off by default; when on, the log line
                # distinguishes "gate rejects" from "strategy never
                # fired" in hb177-style heartbeat analyses.
                _cf_logger = _get_counterfactual_logger()
                if _cf_logger is not None:
                    _cf_regime, _cf_adx, _cf_session = _extract_regime_and_session(
                        bars,
                        bars[-1] if bars else None,
                    )
                    _cf_logger.write(
                        _build_counterfactual_payload(
                            symbol=symbol,
                            strategy_id=_strategy_id_for_regime,
                            regime=_cf_regime,
                            session=_cf_session,
                            adx=_cf_adx,
                            current_gate_decision="pass" if regime_allowed else "reject",
                            expanded_gate_decision=_expanded_gate_would_pass(
                                _cf_regime, _cf_session,
                            ),
                            strategy_emitted=True,
                        )
                    )
                if not regime_allowed:
                    logger.info(
                        "[REGIME-GATE] %s signal rejected: %s",
                        strategy_name,
                        regime_reason,
                    )
                    # Card 0d7d7557: bump the regime-gate rejection counter
                    # so operators can see gate churn in the B5 health line.
                    # This is a read-path observability increment and does
                    # NOT touch the live-submit path. The signal is still
                    # skipped (continue) and never reaches the broker.
                    with self._lock:
                        self._health.signals_filtered_by_regime_gate += 1
                    continue

                self._route_signal(s, strategy_name)

            with self._lock:
                self._health.signals_generated += generated

        except Exception as exc:
            with self._lock:
                self._health.evaluation_errors += 1
            logger.error("Strategy evaluation error: %s", exc, exc_info=True)
        finally:
            self._eval_semaphore.release()

    def _route_signal(self, signal: CTraderTradeSignal, strategy_name: str):
        """Route a single signal through correlation gate → blend runner."""
        strategy_id = self._strategy_id_map.get(strategy_name, strategy_name.lower().replace(" ", "_"))

        direction_str = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)

        if self._blend_runner:
            # Check correlation gate
            allowed, reason = self._correlation_gate.check(signal.symbol, direction_str, strategy_id)
            if not allowed:
                logger.info(
                    "Signal blocked: %s | %s %s conf=%.2f — %s",
                    strategy_id,
                    direction_str,
                    signal.symbol,
                    signal.confidence,
                    reason,
                )
                with self._lock:
                    self._health.signals_rejected += 1
                self._heartbeat.record_signal(accepted=False)
                return

            # Convert to blend format
            signal_dict = trade_signal_to_blend_dict(signal, strategy_id)
            signal_dict["strategy_id"] = strategy_id

            try:
                order = self._blend_runner.on_signal(strategy_id, signal_dict)
                if order.rejected:
                    logger.info(
                        "Signal rejected by blend: %s %s %s conf=%.2f — %s",
                        strategy_id,
                        direction_str,
                        signal.symbol,
                        signal.confidence,
                        order.rejection_reason,
                    )
                    with self._lock:
                        self._health.signals_rejected += 1
                    self._correlation_gate.release_pending(signal.symbol, strategy_id)
                    self._heartbeat.record_signal(accepted=False)
                else:
                    logger.info(
                        "Signal accepted: %s %s %s @ %.5f conf=%.2f lots=%.4f",
                        strategy_id,
                        direction_str,
                        signal.symbol,
                        signal.entry_price,
                        signal.confidence,
                        order.lots,
                    )
                    # T2: do NOT bump ``signals_traded`` here yet — that
                    # counter now means "execution confirmed successful,"
                    # not "blend runner accepted."  We bump it only after
                    # the order reaches the broker with a FILLED outcome
                    # (or after a paper-mode ``process_signal`` returns
                    # success).  ``signals_accepted`` captures the blend-side
                    # accept count for operators who want to see it.
                    with self._lock:
                        self._health.signals_accepted = getattr(self._health, "signals_accepted", 0) + 1
                    self._heartbeat.record_signal(accepted=True)

                    # Execute directly: live → cTrader, paper → PaperTrader
                    # Single-connection architecture: no dual execution path.
                    #
                    # Card 0d7d7557 (iter2): split the prior monolithic
                    # try/except into per-mode blocks so the broker-failure
                    # counter ``signals_failed_live`` is incremented ONLY on
                    # the live broker-attempted path. The original block
                    # wrapped both live and paper paths, so a paper-mode
                    # exception (e.g. paper-trader state bug) would
                    # incorrectly bump signals_failed_live and false-trigger
                    # the B5 broker-rejection warning. Each path now has
                    # its own except: live exceptions bump
                    # signals_failed_live + release risk + release corr gate;
                    # paper exceptions log + release risk + release corr
                    # gate but DO NOT touch the live counters.
                    if self._config.live_mode:
                        # Live mode — exceptions here are broker-attempt
                        # failures; bump signals_failed_live to keep the
                        # B5 attempted-submission basis honest.
                        try:
                            exec_signal = CTraderTradeSignal(
                                symbol=signal.symbol,
                                direction=signal.direction,
                                entry_price=signal.entry_price,
                                stop_loss=signal.stop_loss,
                                take_profit_1=signal.take_profit_1,
                                take_profit_2=signal.take_profit_2,
                                take_profit_3=signal.take_profit_3,
                                volume=order.lots,  # Sized by blend runner orchestrator
                                confidence=signal.confidence,
                                rationale=getattr(signal, "rationale", ""),
                            )
                            # Direct cTrader execution — skip paper trader entirely
                            from adapters.ctrader.forward_test_engine import (
                                LiveExecutionStatus,
                            )

                            outcome = self._execute_signal_live(exec_signal, strategy_id=strategy_id)
                            if outcome is None:
                                logger.warning(
                                    "Live execution skipped (pre-flight): %s %s %.4f lots",
                                    strategy_id,
                                    direction_str,
                                    order.lots,
                                )
                                self._blend_runner.cancel_risk(
                                    self._blend_signal_id(signal),
                                    order.risk_amount,
                                )
                                self._correlation_gate.release_pending(signal.symbol, strategy_id)
                            elif outcome.status == LiveExecutionStatus.FILLED:
                                self._live_fill_count = getattr(self, "_live_fill_count", 0) + 1
                                with self._lock:
                                    self._health.signals_traded += 1
                                # Card 4083ac2d-...: promote the pending
                                # reservation to a real slot keyed by the
                                # actual position_id so the per-symbol at-risk
                                # cap reflects entry+SL on subsequent checks.
                                _filled_position_id = (
                                    getattr(outcome.order, "order_id", None)
                                    or getattr(outcome, "position_id", None)
                                    or f"live:{signal.symbol}:{strategy_id}:{int(time.time() * 1000)}"
                                )
                                try:
                                    self._correlation_gate.attach_position(
                                        symbol=signal.symbol,
                                        strategy_id=strategy_id,
                                        position_id=str(_filled_position_id),
                                        direction=direction_str,
                                        entry_price=signal.entry_price,
                                        sl=signal.stop_loss,
                                    )
                                except Exception as _attach_exc:
                                    logger.warning(
                                        "correlation_gate.attach_position (live) failed: %s",
                                        _attach_exc,
                                    )
                                logger.info(
                                    "Live trade executed: %s %s %.4f lots order_id=%s",
                                    strategy_id,
                                    direction_str,
                                    order.lots,
                                    str(_filled_position_id),
                                )
                            elif outcome.status == LiveExecutionStatus.SENT:
                                # Order sent to cTrader but no execution event
                                # yet — bump signals_sent and signals_pending.
                                # Do NOT count as a fill.  Late-fill callbacks
                                # will upgrade it if the event arrives late.
                                with self._lock:
                                    self._health.signals_sent += 1
                                    self._health.signals_pending += 1
                                logger.info(
                                    "Live order SENT, awaiting ack: %s %s %.4f lots order_id=%s",
                                    strategy_id,
                                    direction_str,
                                    order.lots,
                                    getattr(outcome.order, "order_id", ""),
                                )
                            elif outcome.status == LiveExecutionStatus.TIMEOUT:
                                # Our local wait_for_event fired without seeing
                                # the broker's execution event. The order WAS
                                # transmitted; the verdict isn't terminal yet.
                                # Treat as a pending ack state (mirrors SENT) and
                                # hand the verdict to the late-fill callback.
                                # This split is required because every TIMEOUT
                                # that later confirms as FILLED was previously
                                # double-counted in signals_failed_live, which
                                # then mirrored live_fills 1:1 in the B5 health
                                # line. Holding the correlation gate + risk
                                # budget here matches SENT semantics: the broker
                                # may still deliver an execution event late.
                                with self._lock:
                                    self._health.signals_sent += 1
                                    self._health.signals_pending += 1
                                logger.warning(
                                    "Live order TIMEOUT awaiting ack: %s %s %.4f lots order_id=%s — deferring verdict to late-fill callback",  # noqa: E501
                                    strategy_id,
                                    direction_str,
                                    order.lots,
                                    getattr(outcome.order, "order_id", ""),
                                )
                            elif outcome.status == LiveExecutionStatus.NOT_CONNECTED:
                                # Card 0d7d7557 (iter2): NOT_CONNECTED is
                                # *unreachability*, not rejection. The order
                                # never reached the broker — either the spot
                                # feed wasn't operational pre-contact (order
                                # is None), or new_order returned with
                                # reason="not_connected" post-contact. Either
                                # way the broker-attempted count is honest
                                # only if we bump signals_unreachable, NOT
                                # signals_failed_live — the latter is the
                                # broker-rejected bucket. The B5 warning
                                # distinguishes these two failure modes so
                                # operators see "broker unreachable" vs
                                # "broker rejected" instead of the old text
                                # which conflated both into "orders
                                # reaching broker but being rejected".
                                with self._lock:
                                    self._health.signals_unreachable += 1
                                logger.warning(
                                    "Live execution unreachable: %s %s %.4f lots reason=%s",
                                    strategy_id,
                                    direction_str,
                                    order.lots,
                                    outcome.reason,
                                )
                                self._blend_runner.cancel_risk(
                                    self._blend_signal_id(signal),
                                    order.risk_amount,
                                )
                                self._correlation_gate.release_pending(signal.symbol, strategy_id)
                            else:
                                # REJECTED / CANCELLED — terminal broker
                                # rejection only. SENT, TIMEOUT, and
                                # NOT_CONNECTED are handled in their own
                                # branches above. signals_unreachable is
                                # bumped only for NOT_CONNECTED (handled
                                # above); signals_failed_live covers
                                # broker REJECTED / CANCELLED outcomes.
                                with self._lock:
                                    self._health.signals_failed_live += 1
                                logger.warning(
                                    "Live execution failed: %s %s %.4f lots status=%s reason=%s",
                                    strategy_id,
                                    direction_str,
                                    order.lots,
                                    outcome.status.value,
                                    outcome.reason,
                                )
                                self._blend_runner.cancel_risk(
                                    self._blend_signal_id(signal),
                                    order.risk_amount,
                                )
                                self._correlation_gate.release_pending(signal.symbol, strategy_id)
                        except Exception as exec_err:
                            logger.error("Live execution error: %s", exec_err, exc_info=True)
                            # Card 0d7d7557 (iter2): the exception path
                            # around _execute_signal_live bypasses both
                            # signals_sent and signals_failed_live
                            # increments, so a real broker outage that
                            # throws would undercount attempts and the B5
                            # warning would stay silent. Count the attempt
                            # as a failed live submission so the
                            # attempted-submission basis correctly
                            # reflects broker failures. This except is
                            # LIVE-mode only (paper-mode has its own
                            # block below), so the increment is in scope:
                            # we know the signal passed pre-flight,
                            # reached _execute_signal_live, and threw
                            # before any counter could be bumped.
                            with self._lock:
                                self._health.signals_failed_live += 1
                            # Free risk budget on execution error too
                            self._blend_runner.cancel_risk(
                                self._blend_signal_id(signal),
                                order.risk_amount,
                            )
                            self._correlation_gate.release_pending(signal.symbol, strategy_id)
                    else:
                        # Paper mode — exceptions here are paper-trader
                        # state bugs / network simulation issues, NOT
                        # broker failures. They must NOT bump
                        # signals_failed_live or any live counter; doing
                        # so would mislabel paper-mode failures as
                        # broker failures and false-trigger the B5
                        # warning. The original monolithic except block
                        # had this bug (Rin iter2 M1 finding).
                        try:
                            exec_signal = CTraderTradeSignal(
                                symbol=signal.symbol,
                                direction=signal.direction,
                                entry_price=signal.entry_price,
                                stop_loss=signal.stop_loss,
                                take_profit_1=signal.take_profit_1,
                                take_profit_2=signal.take_profit_2,
                                take_profit_3=signal.take_profit_3,
                                volume=order.lots,  # Sized by blend runner orchestrator
                                confidence=signal.confidence,
                                rationale=getattr(signal, "rationale", ""),
                            )
                            exec_result = self._paper_trader.process_signal(exec_signal, spread=self._current_spread)
                            if exec_result.success:
                                with self._lock:
                                    self._health.signals_traded += 1
                                # Card 4083ac2d-...: promote pending reservation
                                # to a real slot keyed by the actual position_id
                                # so the per-symbol at-risk cap reflects entry+SL
                                # on subsequent checks.
                                _paper_position_id = (
                                    getattr(getattr(exec_result, "position", None), "position_id", None)
                                    or getattr(getattr(exec_result, "order", None), "order_id", None)
                                    or f"paper:{signal.symbol}:{strategy_id}:{int(time.time() * 1000)}"
                                )
                                try:
                                    self._correlation_gate.attach_position(
                                        symbol=signal.symbol,
                                        strategy_id=strategy_id,
                                        position_id=str(_paper_position_id),
                                        direction=direction_str,
                                        entry_price=signal.entry_price,
                                        sl=signal.stop_loss,
                                    )
                                except Exception as _attach_exc:
                                    logger.warning(
                                        "correlation_gate.attach_position (paper) failed: %s",
                                        _attach_exc,
                                    )
                                logger.info(
                                    "Paper trade executed: %s %s %.4f lots position_id=%s",
                                    strategy_id,
                                    direction_str,
                                    order.lots,
                                    str(_paper_position_id),
                                )
                            else:
                                logger.warning(
                                    "Trade execution failed: %s",
                                    exec_result.rejection_reason,
                                )
                                self._blend_runner.cancel_risk(
                                    self._blend_signal_id(signal),
                                    order.risk_amount,
                                )
                                self._correlation_gate.release_pending(signal.symbol, strategy_id)
                        except Exception as exec_err:
                            logger.error("Paper execution error: %s", exec_err, exc_info=True)
                            # Paper-mode exceptions must NOT touch live
                            # counters. Only release risk + correlation
                            # gate so subsequent signals aren't blocked.
                            self._blend_runner.cancel_risk(
                                self._blend_signal_id(signal),
                                order.risk_amount,
                            )
                            self._correlation_gate.release_pending(signal.symbol, strategy_id)

                    # Write last_signal.txt for watchdog health check
                    try:
                        import json as _json

                        _sig_data = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "strategy": strategy_id,
                            "symbol": signal.symbol,
                            "direction": direction_str,
                            "entry_price": signal.entry_price,
                            "confidence": signal.confidence,
                            "lots": order.lots,
                            "stop_loss": signal.stop_loss,
                            "take_profit": signal.take_profit,
                        }
                        _sig_path = Path(PROJECT_ROOT) / "data" / "last_signal.txt"
                        _sig_path.parent.mkdir(parents=True, exist_ok=True)
                        _sig_path.write_text(_json.dumps(_sig_data, indent=2) + "\n")
                    except Exception:  # noqa: S110
                        pass  # Non-critical — don't break signal flow
            except Exception as exc:
                logger.error("Blend runner error: %s", exc, exc_info=True)
                self._correlation_gate.release_pending(signal.symbol, strategy_id)
                with self._lock:
                    self._health.signals_rejected += 1
                self._heartbeat.record_signal(accepted=False)
        else:
            # Fallback: direct to paper trader (original behavior)
            with self._lock:
                self._health.signals_traded += 1
            logger.info(
                "Signal traded (direct): %s %s %s @ %.5f conf=%.2f",
                strategy_id,
                direction_str,
                signal.symbol,
                signal.entry_price,
                signal.confidence,
            )
            self._heartbeat.record_signal(accepted=True)

    def on_position_closed_release(self, position):
        """Release correlation gate on position close to avoid stale blocks.

        Card 4083ac2d-...: per-symbol at-risk cap. On close, free the
        specific position's slot via ``release_position(position_id)`` so
        the gate's pending reservations are not affected. Falls back to the
        legacy (symbol, direction) release if the position has no
        ``position_id`` attribute (defensive).
        """
        try:
            direction = position.direction.value if hasattr(position.direction, "value") else str(position.direction)
            position_id = getattr(position, "position_id", None)
            if position_id is not None:
                self._correlation_gate.release_position(position_id)
            else:
                self._correlation_gate.release(position.symbol, direction)
            logger.info(
                "Correlation gate released: %s %s position_id=%s (active=%d)",
                direction,
                position.symbol,
                position_id,
                self._correlation_gate.active_count,
            )
        except Exception as exc:
            logger.warning("Failed to release correlation gate on close: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

STRATEGY_ID_MAP = {
    "Killzone Momentum": "killzone_momentum",
    "Dual-TF Squeeze Pro": "dual_tf_squeeze_pro",
    "Donchian ATR Trailing Trend v2": "donchian_atr_trend_v2",
    "SRMR+": "srmr_plus",
    "London Breakout Retest": "london_breakout_retest",
    "TTC XAUUSD M15": "ttc_xauusd",
}

# Strategy -> bar period minutes mapping
STRATEGY_TIMEFRAMES = {
    "Killzone Momentum": 15,  # M15
    "Dual-TF Squeeze Pro": 15,  # M15 (aggregates H1 internally)
    "Donchian ATR Trailing Trend v2": 60,  # H1
    "SRMR+": 15,  # M15
    "London Breakout Retest": 15,  # M15
    "TTC XAUUSD M15": 15,  # M15 per Optuna tuning study
}

# ── Strategy pool is hard-coded to the validated 4-strategy blend ────────
# YAML-based Optuna variants have been removed (Jul 22, 2026 — regime-gated
# blend update).  ``strategies.yaml`` is still consulted by other tools (the
# canary deck and stage-1 transition plan) but the forward test launcher
# now uses a single default-config instance per validated strategy.


def build_blend_runner() -> BlendForwardTestRunner:
    config = {
        "account_balance": 10_000.0,
        "risk_per_trade_pct": 0.0025,
        "daily_risk_cap_pct": 0.02,
        "max_sniper": 3,
        "max_swarm": 5,
        "spread_pips": {"GBPUSD": 2.0, "EURUSD": 0.8, "XAUUSD": 0.3, "USDJPY": 0.8},
        "atr_cache_path": "data/atr_cache.json",
        "state_path": "data/risk_state_blend.json",
        "log_level": "INFO",
    }
    runner = BlendForwardTestRunner(config)
    runner.start()
    return runner


def wire_connection_reliability(connection_manager):
    """Wire connection reliability modules (watchdog, OAuth refresh).

    TokenLifecycle handles refresh with 5-day buffer (day-25 proactive refresh
    on 30-day tokens). BQ-978 two-token-path conflict is safe: TokenLifecycle
    reads from .env via CredentialStore; OAuthRefreshManager reads from
    data/.credentials JSON — different stores, no race.
    """
    connection_manager.start_watchdog()
    try:
        connection_manager.refresh_oauth_if_needed()
    except Exception as exc:
        logger.warning("OAuth refresh on startup failed: %s", exc)


# ── Forward Test Health JSON Writer ──────────────────────────────────────────

_HEALTH_JSON_PATH = PROJECT_ROOT / "data" / "forward_test_health.json"
_OVERSEER_STATE_PATH = PROJECT_ROOT / "data" / "overseer_state.json"
_SYSTEMD_UNIT_NAME = "ayumi-forward-test.service"
# Card 080094ef r2: in-process exit record. The systemd unit's live
# ``Result=``/``ExecMainStatus`` describe the CURRENT activation (which is
# ``success``/0 after ``Restart=always`` reactivation), not the prior exit
# that triggered the restart. Writing our own exit record at known exit
# sites gives a source of truth that survives the reactivation. Without
# this, a crash followed by successful reactivation is mis-classified as
# ``clean_24h_rotation`` (Rin HIGH finding, comment b27b6a40).
_LAST_EXIT_RECORD_PATH = PROJECT_ROOT / "data" / "forward_test_last_exit.json"
_RESTART_REASON_LABELS = {
    "success": "clean_24h_rotation",
    "exit-code": "launch_failure",
    "signal": "external_signal_termination",
    "watchdog": "watchdog_timeout",
    "core-dump": "core_dump",
    "timeout": "startup_timeout",
}


def _read_systemd_restart_status(unit: str = _SYSTEMD_UNIT_NAME) -> dict:
    """Read the systemd unit's restart counter + last-exit metadata.

    Returns a dict with keys:

      * ``n_restarts`` (int) — ``systemctl show -p NRestarts``
      * ``last_restart_at`` (ISO-8601 UTC string or None)
      * ``last_result`` (raw systemd ``Result=`` value, or a synthesized
        value from the recorded exit — see ``exit_source``)
      * ``last_exit_code`` (parsed int or None — sourced from
        ``ExecMainStatus`` for the live path, or from the recorded exit)
      * ``exit_source`` — ``"recorded"`` (in-process exit record written
        by ``_record_last_exit`` at known exit sites; **source of truth**
        for what actually triggered the prior restart), ``"live"`` (the
        live systemd unit properties, which describe the CURRENT
        activation after ``Restart=always`` reactivation and can mask the
        prior crash as ``success``/0), or ``"none"`` (no record exists
        and ``systemctl`` was unavailable).
      * ``last_exit_triggered_by``, ``last_exit_signal_name``,
        ``last_exit_detail``, ``last_exit_at`` — populated when
        ``exit_source == "recorded"``.

    Never raises — ``systemctl`` is unavailable in some test/dev
    environments and the caller must always receive a fully-populated
    dict with safe defaults.

    Card 080094ef r2 (Rin HIGH, comment b27b6a40): the recorded exit is
    the authoritative source for "why did the previous activation end"
    because the live unit's ``Result=success, ExecMainStatus=0`` describe
    the CURRENT run (after Restart=always reactivation), not the prior
    crash. A crash followed by successful reactivation would otherwise
    be mis-classified as ``clean_24h_rotation``.

    For untrapped signals (``SIGKILL``, ``SIGABRT``, ``SIGSEGV``) the
    process dies before our in-process recorder can write — those exits
    only appear in the systemd journal. We chain ``recorded`` →
    ``journal`` (excluding current MainPID) → ``live`` so both caught
    and uncaught exits are classified correctly.

    Implementation: shells out to ``systemctl show <unit> -p NRestarts,
    -p MainPID, -p ActiveEnterTimestamp, -p Result, -p ExecMainStatus``
    with a 1s timeout. Output is key=value lines; values are split on
    the first ``=`` and stripped. ``ExecMainStatus`` is the
    systemd-reported status code for the main process (e.g.
    ``2/INVALIDARGUMENT``); the leading integer is captured as
    ``last_exit_code`` for classification.
    """
    import subprocess as _sp
    out: dict = {
        "n_restarts": 0,
        "last_restart_at": None,
        "last_result": "unknown",
        "last_exit_code": None,
        "exit_source": "none",
        "main_pid": None,
        "last_exit_triggered_by": None,
        "last_exit_signal_name": None,
        "last_exit_detail": None,
        "last_exit_at": None,
    }
    try:
        proc = _sp.run(  # noqa: S603 — fully-controlled argv, check=False, timeout=1s; unit name is module constant.
            ["/usr/bin/systemctl", "show", unit,
             "-p", "NRestarts",
             "-p", "MainPID",
             "-p", "ActiveEnterTimestamp",
             "-p", "Result",
             "-p", "ExecMainStatus"],
            capture_output=True, text=True, timeout=1.0, check=False,
        )
    except (FileNotFoundError, _sp.TimeoutExpired, OSError):
        pass
    else:
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key == "NRestarts":
                    try:
                        out["n_restarts"] = int(value)
                    except ValueError:
                        pass
                elif key == "MainPID":
                    try:
                        out["main_pid"] = int(value) if value and value != "0" else None
                    except ValueError:
                        pass
                elif key == "ActiveEnterTimestamp":
                    # systemctl timestamp format: "Tue 2026-09-15 21:27:17 UTC"
                    # or "[not set]". Parse the human form only if it matches.
                    if value and value != "[not set]":
                        try:
                            _ts = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                            out["last_restart_at"] = _ts.isoformat()
                        except ValueError:
                            out["last_restart_at"] = None
                elif key == "Result":
                    out["last_result"] = value or "unknown"
                elif key == "ExecMainStatus":
                    # Format: "2/INVALIDARGUMENT" or "0/SUCCESS" or "[not set]"
                    head = value.split("/", 1)[0].strip()
                    try:
                        out["last_exit_code"] = int(head)
                    except ValueError:
                        out["last_exit_code"] = None

    # Card 080094ef r2: prefer the recorded exit (in-process, source of
    # truth for the prior exit) over the live systemd unit state. The
    # live state describes the CURRENT activation; after Restart=always
    # reactivation it is "success"/0 regardless of what the prior
    # activation actually exited with.
    recorded = _read_last_exit_record()
    if recorded is not None:
        out["exit_source"] = "recorded"
        out["last_exit_triggered_by"] = recorded.get("triggered_by")
        out["last_exit_signal_name"] = recorded.get("signal_name")
        out["last_exit_detail"] = recorded.get("detail")
        out["last_exit_at"] = recorded.get("exit_at")
        out["last_exit_code"] = recorded.get("exit_code")
        # Synthesize ``last_result`` so the classifier can route based on
        # the recorded exit's nature (signal vs explicit code). The
        # classifier below (``_classify_restart_reason``) reads
        # ``exit_source`` directly and dispatches accordingly; we only set
        # ``last_result`` here for backwards-compatible callers / debug
        # introspection.
        triggered_by = out["last_exit_triggered_by"]
        if triggered_by == "signal":
            out["last_result"] = "recorded-signal"
        elif triggered_by in ("sys_exit", "atexit", "exception"):
            out["last_result"] = "recorded-exit-code"
    else:
        # No recorded file — fall through to the journal query. The
        # journal covers untrapped signals (SIGKILL, SIGABRT, SIGSEGV)
        # where the process dies before our recorder runs. We exclude
        # the current MainPID so the CURRENT activation's log entries
        # don't mask the prior activation's exit.
        journal = _read_journal_last_exit(unit, exclude_pid=out.get("main_pid"))
        if journal is not None:
            out["exit_source"] = "journal"
            out["last_exit_triggered_by"] = journal.get("triggered_by")
            out["last_exit_signal_name"] = journal.get("signal_name")
            out["last_exit_detail"] = journal.get("detail")
            out["last_exit_at"] = journal.get("exit_at")
            out["last_exit_code"] = journal.get("exit_code")
            triggered_by = out["last_exit_triggered_by"]
            if triggered_by == "signal":
                out["last_result"] = "journal-signal"
            else:
                out["last_result"] = "journal-exit-code"
        elif out["last_result"] != "unknown" or out["last_exit_code"] is not None:
            out["exit_source"] = "live"

    return out


def _classify_restart_reason(sd_status: dict) -> str:
    """Map the recorded/live systemd exit state to a stable reason label.

    Card 080094ef r2: takes the full ``sd_status`` dict from
    ``_read_systemd_restart_status`` so it can dispatch on the recorded
    exit source (in-process truth) versus the live systemd unit state
    (which describes the CURRENT activation, not the prior exit that
    triggered the restart). After a crash followed by successful
    ``Restart=always`` reactivation, the live state is
    ``Result=success, ExecMainStatus=0`` — that path masks the prior
    crash as ``clean_24h_rotation`` (Rin HIGH finding).

    Dispatch:

      * ``exit_source in {"recorded", "journal"}``: classify based on what
        the prior activation ACTUALLY exited with:

          - ``triggered_by="signal"`` + ``exit_code=0``: our SIGTERM/SIGINT
            handler ran and exited cleanly → ``clean_24h_rotation`` (the
            proactive-24h rotation path, see card 7d3b535d).
          - ``triggered_by="signal"`` + non-zero exit: abnormal signal-path
            shutdown → ``external_signal_termination``.
          - ``triggered_by="sys_exit"`` + ``exit_code=0``: explicit clean
            stop → ``clean_24h_rotation``.
          - ``triggered_by="sys_exit"`` + ``exit_code=1``: engine-start
            failure / paper-on-live rejection → ``pid_guard_or_launch_failure``.
          - ``triggered_by="sys_exit"`` + ``exit_code=2``: foreign-UID
            signal_stats guard → ``foreign_uid_signal_stats``.
          - ``triggered_by="sys_exit"`` + other code → ``unknown_exit_<n>``.
          - ``triggered_by="atexit"`` + ``exit_code=0``: implicit clean
            stop → ``clean_24h_rotation``.
          - ``triggered_by="exception"``: uncaught exception in ``main()``;
            label preserves the exit code so the operator can correlate
            against the traceback.
          - ``triggered_by="journal"`` (only when ``exit_source == "journal"``):
            durable record from ``journalctl``; shape matches recorded and
            uses the same routing above.

      * ``exit_source == "live"`` (fallback, no recorded file): the live
        systemd properties describe the most-recent activation's result.
        Reliable only when the unit is currently INACTIVE. For active
        units this is unreliable (see above) but is the best signal
        available, so we keep the r1 priority-ordered classifier.

          - ``watchdog`` → ``watchdog_timeout``.
          - ``signal`` → ``external_signal_termination``.
          - ``core-dump`` → ``core_dump``.
          - ``last_exit_code is None`` → ``pre_startup``.
          - ``last_exit_code == 0`` → ``clean_24h_rotation``.
          - ``last_exit_code == 2`` → ``foreign_uid_signal_stats``.
          - ``last_exit_code == 1`` → ``pid_guard_or_launch_failure``.
          - other → ``unknown_exit_<n>``.

    Returns a non-empty string label.
    """
    exit_source = sd_status.get("exit_source", "live")

    if exit_source in ("recorded", "journal"):
        triggered_by = sd_status.get("last_exit_triggered_by")
        exit_code = sd_status.get("last_exit_code")

        # Our SIGTERM/SIGINT handler ran and exited cleanly via
        # sys.exit(0) — the proactive-24h rotation path.
        if triggered_by == "signal" and exit_code == 0:
            return "clean_24h_rotation"
        # Signal from our handler but with non-zero exit — abnormal.
        if triggered_by == "signal":
            return "external_signal_termination"

        # Explicit sys.exit(N) calls in main():
        # - N=0 is the clean-rotation path
        # - N=1 is paper-on-live rejection OR engine-start failure
        # - N=2 is foreign-UID signal_stats rejection
        if triggered_by == "sys_exit":
            if exit_code == 0:
                return "clean_24h_rotation"
            if exit_code == 1:
                return "pid_guard_or_launch_failure"
            if exit_code == 2:
                return "foreign_uid_signal_stats"
            return f"unknown_exit_{exit_code}"

        # atexit hook (catches sys.exit paths not wrapped explicitly).
        if triggered_by == "atexit":
            if exit_code == 0:
                return "clean_24h_rotation"
            return f"unknown_exit_{exit_code}"

        # Uncaught exception path (finally block recorded this).
        if triggered_by == "exception":
            code_part = (
                f"_{exit_code}" if exit_code is not None else "_unknown"
            )
            return f"uncaught_exception_exit{code_part}"

        # Recorded but triggered_by is unrecognized — fall through to live.

    # Fallback: live systemd properties. Used only when no recorded
    # exit exists (e.g. first-ever start, or a crash that bypassed all
    # our recording sites). The r1 priority-ordered classifier lives
    # here.
    last_result = sd_status.get("last_result", "unknown")
    last_exit_code = sd_status.get("last_exit_code")

    if last_result == "watchdog":
        return "watchdog_timeout"
    if last_result == "signal":
        return "external_signal_termination"
    if last_result == "core-dump":
        return "core_dump"
    if last_exit_code is None:
        return "pre_startup"
    if last_exit_code == 0:
        return "clean_24h_rotation"
    if last_exit_code == 2:
        return "foreign_uid_signal_stats"
    if last_exit_code == 1:
        return "pid_guard_or_launch_failure"
    return f"unknown_exit_{last_exit_code}"


def _signal_name_for(signum: int) -> str:
    """Map a numeric signal to its canonical name (e.g. ``SIGTERM``).

    Falls back to ``"SIG_<n>"`` if the platform does not declare the signal
    (some rare signals are Linux-only). Never raises.
    """
    try:
        return sig_module.Signals(signum).name
    except (ValueError, AttributeError):
        return f"SIG_{signum}"


def _record_last_exit(
    exit_code: int,
    triggered_by: str,
    *,
    signal_name: str | None = None,
    detail: str | None = None,
) -> None:
    """Persist the most-recent process exit to ``data/forward_test_last_exit.json``.

    Called at every known exit site in ``main()`` (signal handlers,
    ``sys.exit(...)`` calls, and the catch-all ``except Exception`` block)
    so a subsequent restart cycle can classify the prior exit by what
    ACTUALLY happened — not by the live systemd unit's ``Result=success,
    ExecMainStatus=0`` (which describes the CURRENT activation, after
    ``Restart=always`` reactivation, not the prior crash that triggered
    the restart).

    Never raises — a failure to write the record must never break the
    shutdown sequence. Matches the defensive posture of the health/overseer
    writers in this module.

    Args:
        exit_code: numeric exit code passed to ``sys.exit`` or signal handler.
        triggered_by: one of ``"signal"`` (caught by our SIGTERM/SIGINT
            handler), ``"sys_exit"`` (explicit ``sys.exit(N)`` call), or
            ``"exception"`` (uncaught exception in ``main()``).
        signal_name: optional signal name when ``triggered_by == "signal"``.
        detail: optional human-readable detail (e.g. ``"engine_start_failure"``).
    """
    try:
        rec = {
            "exit_code": int(exit_code),
            "triggered_by": str(triggered_by),
            "signal_name": signal_name,
            "detail": detail,
            "exit_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        _LAST_EXIT_RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LAST_EXIT_RECORD_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, indent=2) + "\n")
        os.replace(str(tmp), _LAST_EXIT_RECORD_PATH)
    except Exception as _rec_exc:
        # WARNING level only — recording failure must never propagate.
        logger.warning(
            "Failed to record last exit (code=%s, by=%s): %s",
            exit_code, triggered_by, _rec_exc,
        )


def _read_last_exit_record() -> dict | None:
    """Read the most-recent recorded exit, or ``None`` if absent/corrupt.

    Never raises — returns ``None`` on any read/parse error so the caller
    always has a usable value. The dict shape matches what
    ``_record_last_exit`` writes.
    """
    try:
        if not _LAST_EXIT_RECORD_PATH.exists():
            return None
        raw = json.loads(_LAST_EXIT_RECORD_PATH.read_text())
        if not isinstance(raw, dict):
            return None
        return raw
    except (json.JSONDecodeError, OSError):
        return None


def _read_journal_last_exit(
    unit: str,
    *,
    exclude_pid: int | None = None,
    timeout_s: float = 2.0,
) -> dict | None:
    """Query journalctl for the last completed activation of the unit.

    Card 080094ef r2 (Rin HIGH): the recording approach covers caught
    signals (``SIGTERM``/``SIGINT``), ``sys.exit(...)`` paths, and uncaught
    exceptions, but NOT untrapped signals (``SIGKILL``, ``SIGABRT``,
    ``SIGSEGV``) — for those, the process dies before our handler can
    record anything. After ``Restart=always`` reactivation, the live unit
    state is ``Result=success, ExecMainStatus=0`` which masks the prior
    crash.

    The journal is the durable record. ``systemd`` writes lines like
    ``Main process exited, code=exited, status=2/INVALIDARGUMENT`` or
    ``Main process exited, code=killed, status=6/ABRT`` on every service
    exit. We scan recent entries for this unit (excluding the current
    ``MainPID``, which describes the CURRENT activation) and return the
    most recent exit. This complements the in-process record: together,
    recorded + journal cover both caught and uncaught exits.

    Returns a dict matching ``_read_last_exit_record()``'s shape, or
    ``None`` if journalctl is unavailable, returns no usable entries, or
    no prior activation can be distinguished from the current one.

    Never raises — all filesystem / subprocess errors are swallowed.
    """
    import re as _re_jrnl
    import subprocess as _sp_jrnl
    try:
        proc = _sp_jrnl.run(  # noqa: S603 — fully-controlled argv, check=False, timeout=timeout_s
            ["/usr/bin/journalctl",
             f"_SYSTEMD_UNIT={unit}",
             "--no-pager", "-n", "200", "-o", "json"],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except (FileNotFoundError, _sp_jrnl.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None

    last_exit: dict | None = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            entry_pid = int(entry.get("_PID", 0))
        except (TypeError, ValueError):
            continue
        if entry_pid == 0 or entry_pid == exclude_pid:
            continue
        message = str(entry.get("MESSAGE", ""))
        exit_code: int | None = None
        triggered_by = "journal"
        signal_name: str | None = None
        detail: str | None = None
        # systemd writes: "Main process exited, code=exited, status=2/INVALIDARGUMENT"
        # or: "Main process exited, code=killed, status=6/ABRT"
        m_exit = _re_jrnl.search(r"code=(\w+),\s*status=(\d+)/(\w+)", message)
        if m_exit:
            kind, code_str, label = m_exit.group(1), m_exit.group(2), m_exit.group(3)
            try:
                exit_code = int(code_str)
            except ValueError:
                continue
            detail = f"{kind}/{label}"
            if kind == "killed":
                # Killed by signal — synthesize the same shape the recorded
                # path produces so the classifier dispatches identically.
                triggered_by = "signal"
                signal_name = label  # e.g. "ABRT", "KILL", "TERM"
        elif "Main process received signal" in message:
            m_sig = _re_jrnl.search(r"received signal (\w+)", message)
            if m_sig:
                triggered_by = "signal"
                signal_name = m_sig.group(1)
                detail = message
        if exit_code is not None or triggered_by == "signal":
            last_exit = {
                "exit_code": exit_code,
                "triggered_by": triggered_by,
                "signal_name": signal_name,
                "detail": detail,
                "exit_at": entry.get("__REALTIME_TIMESTAMP"),
                "pid": entry_pid,
            }
    return last_exit


def write_overseer_state_restart_mirror(n_restarts: int, last_restart_at: str | None, reason: str) -> None:
    """Mirror the systemd restart counter into ``data/overseer_state.json``.

    Card 080094ef AC2: overseer heartbeats consume this file (not
    ``forward_test_health.json``) so a stripped-down field mirror prevents
    shape drift and keeps the existing field contract intact. We read the
    current overseer state (best-effort, may be missing on a fresh deploy),
    overwrite only the restart fields, and atomic-write the result.

    Never raises — any parse/permission error is logged at WARNING and
    swallowed so the health loop never breaks on a write failure. Matches
    the defensive posture of ``write_forward_test_health_json`` above.
    """
    try:
        if _OVERSEER_STATE_PATH.exists():
            try:
                raw = json.loads(_OVERSEER_STATE_PATH.read_text())
            except (json.JSONDecodeError, OSError) as _ov_exc:
                logger.warning("overseer_state.json unreadable: %s — resetting mirror", _ov_exc)
                raw = {}
        else:
            raw = {}
        raw["forward_test_restart_counter"] = n_restarts
        raw["forward_test_last_restart_at"] = last_restart_at
        raw["forward_test_last_restart_reason"] = reason
        raw["forward_test_restart_mirror_updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = _OVERSEER_STATE_PATH.with_suffix(".json.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(raw, indent=2) + "\n")
        os.replace(str(tmp), str(_OVERSEER_STATE_PATH))
    except Exception as _ov_exc:
        logger.warning("Failed to mirror restart counter to overseer_state.json: %s", _ov_exc)


def write_forward_test_health_json(engine: ForwardTestEngine) -> None:
    """Atomically write forward-test health status to ``data/forward_test_health.json``.

    Called on startup (immediately clears stale ``down`` state) and every 60s
    from the periodic health loop.
    """
    try:
        health = engine.health
        stats = engine.get_stats()
        trading = stats.get("trading", {})

        # Connection state from the spot feed's state manager
        feed = getattr(engine, "_market_feed", None)
        state_mgr = getattr(feed, "_state_mgr", None)
        connection_state = state_mgr.state.value if state_mgr else "unknown"

        # trades_executed observability fix (live mode):
        # In live mode, PaperTrader.trades_executed stays at 0 because the
        # paper trader's execute path is bypassed (live orders are sent
        # directly to cTrader and counted via engine._live_fill_count).
        # Reading from the paper trader in live mode produces a false zero
        # in this health file even when real fills have happened.
        is_live = bool(getattr(getattr(engine, "_config", None), "live_mode", False))
        if is_live:
            live_fills = getattr(engine, "_live_fill_count", 0)
            # closed_trades_live: fills - currently-open positions. The paper
            # trader's order manager does mirror live positions in live mode
            # (execute_live_order is called for each live signal), so its
            # get_open_positions() is a valid count of still-open trades.
            try:
                paper = getattr(engine, "_paper_trader", None)
                open_positions = (
                    len(paper.get_open_positions()) if paper is not None and hasattr(paper, "get_open_positions") else 0
                )
            except Exception:
                open_positions = 0
            closed_trades_live = max(0, int(live_fills) - int(open_positions))
            trades_executed = int(live_fills)
        else:
            trades_executed = trading.get("trades_executed", 0)
            closed_trades_live = 0

        # Card 080094ef: enrich with systemd restart observability. These
        # three fields (restart_reason, restart_counter, last_restart_at)
        # let operators distinguish proactive-24h rotation from a real
        # exit fault at a glance — without them the counter ticks up on
        # every clean exit and the alert signal is buried.
        #
        # Card 080094ef r2: pass the full sd_status dict so the classifier
        # can dispatch on the recorded-exit source (in-process truth) when
        # available. The live systemd properties alone would mask a prior
        # crash as ``clean_24h_rotation`` because they describe the
        # CURRENT activation after ``Restart=always`` reactivation.
        _sd_status = _read_systemd_restart_status()
        _restart_reason = _classify_restart_reason(_sd_status)

        health_data = {
            "service_status": "up" if engine.is_running else "down",
            "ticks_received": health.ticks_received,
            "bars_built": health.bars_built,
            "signals_generated": health.signals_generated,
            "trades_executed": trades_executed,
            "closed_trades_live": closed_trades_live,
            "last_tick_time": health.last_tick_at.isoformat() if health.last_tick_at else None,
            "connection_state": connection_state,
            "market_closed": _is_forex_market_closed(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            # Card 080094ef: restart observability surface (was missing
            # pre-card; only "previous_pid"+"rotation_kind" existed).
            "restart_reason": _restart_reason,
            "restart_counter": _sd_status["n_restarts"],
            "last_restart_at": _sd_status["last_restart_at"],
        }

        json_str = json.dumps(health_data, indent=2)
        _HEALTH_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = _HEALTH_JSON_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json_str)
        os.replace(str(tmp_path), str(_HEALTH_JSON_PATH))

        # Mirror the counter into the overseer's heartbeat intake so
        # restart-flap alerts can route through the existing overseer
        # channel instead of needing a new consumer. Mirrors the health
        # JSON semantics — same source of truth, different consumer.
        write_overseer_state_restart_mirror(
            _sd_status["n_restarts"],
            _sd_status["last_restart_at"],
            _restart_reason,
        )
    except Exception as exc:
        logger.warning("Failed to write forward_test_health.json: %s", exc)


# ── Stale Log Compression (MAINT) ────────────────────────────────────────────

# Threshold for compressing stale logs. Files older than this on disk get
# gzipped and originals removed (data preserved in .gz archives). The active
# forward_test.log is rotated daily by TimedRotatingFileHandler in
# common.logging_config; this routine handles older files left over from
# prior logging configs (e.g. the `ayumi_*.log` family).
_STALE_LOG_MAX_AGE_DAYS = 7
_STALE_LOG_MIN_SIZE_BYTES = 1024  # skip empty / sub-KB stubs


def compress_stale_logs(log_dir: Path, max_age_days: int = _STALE_LOG_MAX_AGE_DAYS) -> int:
    """Gzip-compress ``*.log`` files in ``log_dir`` older than ``max_age_days``.

    Skips files that are already compressed (``*.log.gz``), tiny stubs below
    :data:`_STALE_LOG_MIN_SIZE_BYTES`, or that fail to read. After successful
    compression the original uncompressed file is removed — data is preserved
    in the ``.gz`` archive, never truly deleted.

    Returns the number of files compressed.
    """
    import gzip
    import time as _time

    if not log_dir.exists():
        return 0

    cutoff_mtime = _time.time() - (max_age_days * 86400)
    compressed = 0
    try:
        for log_file in log_dir.glob("*.log"):
            # Skip files already compressed
            if log_file.with_suffix(log_file.suffix + ".gz").exists():
                continue
            try:
                stat = log_file.stat()
            except OSError:
                continue
            # Skip small stubs and anything not old enough
            if stat.st_size < _STALE_LOG_MIN_SIZE_BYTES:
                continue
            if stat.st_mtime >= cutoff_mtime:
                continue
            gz_path = log_file.with_suffix(log_file.suffix + ".gz")
            try:
                with (
                    open(log_file, "rb") as src,
                    gzip.open(gz_path, "wb", compresslevel=6) as dst,
                ):
                    # chunked copy so very large logs don't balloon RSS
                    while True:
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                # Verify the .gz is valid before removing original
                import gzip as _gzip_check

                with _gzip_check.open(gz_path, "rb") as _verify:
                    _verify.read(1024)  # read a bit to confirm integrity
                log_file.unlink()  # remove original — data preserved in .gz
                compressed += 1
            except OSError as exc:
                # Don't fail startup over a single bad log
                logger.warning("Failed to compress %s: %s", log_file, exc)
                # Remove partial .gz if it was started
                if gz_path.exists():
                    try:
                        gz_path.unlink()
                    except OSError:
                        pass
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("compress_stale_logs encountered unexpected error: %s", exc)

    if compressed:
        logger.info(
            "Compressed %d stale log(s) in %s (older than %d days)",
            compressed,
            log_dir,
            max_age_days,
        )
    return compressed


# ── Main ──────────────────────────────────────────────────────────────────────


def _check_signal_stats_uid(
    path: Path,
    current_uid: int,
    allow_foreign_uid: bool,
) -> tuple[bool, str]:
    """Pre-launch ownership guard for ``data/signal_stats.jsonl`` (card a38b853d).

    A root-run process can create the stats file as root:root mode 0600
    during a service-restart gap, which blocks subsequent forward-test
    writes (uid 1000) with EACCES. ``signal_stats._append_line`` now
    enforces 0o644 and self-heals on every write, but this guard surfaces
    the ownership mismatch loudly at startup so operators notice the
    underlying root-contamination issue instead of silently relying on
    self-heal.

    Returns ``(ok, reason)``:
      - missing file               -> ``(True, "missing_ok")``
      - same-owner file            -> ``(True, "owner_match")``
      - foreign-owned, allowed     -> ``(True, reason)``  (warns via logger)
      - foreign-owned, not allowed -> ``(False, reason)`` (caller should exit)

    ``allow_foreign_uid`` should be the OR of the ``--allow-foreign-uid``
    CLI flag and the ``AYUMI_ALLOW_FOREIGN_UID`` env override.
    """
    if not path.exists():
        return True, "missing_ok"
    try:
        st = path.stat()
    except OSError as exc:
        return False, f"stat_failed:{exc}"
    if st.st_uid == current_uid:
        return True, "owner_match"
    try:
        import pwd

        owner = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, OSError):
        owner = str(st.st_uid)
    reason = (
        f"data/signal_stats.jsonl owned by {owner} (uid={st.st_uid}) "
        f"but current process is uid={current_uid} "
        f"(mode={oct(st.st_mode & 0o777)}). "
        f"Likely root contamination from a prior service-restart gap."
    )
    if allow_foreign_uid:
        logging.getLogger("ayumi.blend_launcher").warning(
            "signal_stats foreign-uid BYPASSED via --allow-foreign-uid/env: %s",
            reason,
        )
        return True, reason
    return False, reason


def main():
    parser = argparse.ArgumentParser(description="Ayumi Multi-Strategy Forward Test")
    parser.add_argument(
        "--symbols",
        default="GBPUSD,USDJPY,EURUSD",
        help="Comma-separated symbols (default: GBPUSD,USDJPY,EURUSD)",
    )
    parser.add_argument(
        "--allow-foreign-uid",
        action="store_true",
        help=(
            "Proceed even when data/signal_stats.jsonl is owned by a "
            "different uid (typically root contamination from a prior "
            "service-restart gap). signal_stats._append_line self-heals "
            "to mode 0644 on every write, so the file will be repaired "
            "on the first append. Also settable via AYUMI_ALLOW_FOREIGN_UID=1."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="Execution mode (paper/live). If not specified, derives from --live flag.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Send real orders to cTrader via OpenAPI using account id from CTRADER_OPENAPI_ACCOUNT_ID (default: paper-only)",  # noqa: E501
    )
    parser.add_argument(
        "--paper-only",
        action="store_true",
        help="Run in paper-only mode (default, overridden by --live)",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated strategy names to keep (drops rest). e.g. --only 'Killzone Momentum'",
    )
    args = parser.parse_args()

    # Resolve execution mode: --mode takes priority, then fall back to --live
    if args.mode:
        execution_mode = args.mode
    else:
        execution_mode = "live" if args.live else "paper"

    # Pre-launch signal_stats ownership guard (card a38b853d). Refuses to
    # start when data/signal_stats.jsonl is foreign-owned unless the
    # operator explicitly opts in via --allow-foreign-uid or
    # AYUMI_ALLOW_FOREIGN_UID=1. Missing file = OK (forward test will
    # create it with the correct ownership on the first signal).
    _allow_foreign_uid = bool(
        args.allow_foreign_uid
        or os.getenv("AYUMI_ALLOW_FOREIGN_UID", "").lower() in ("1", "true", "yes"),
    )
    _stats_path = PROJECT_ROOT / "data" / "signal_stats.jsonl"
    _stats_ok, _stats_reason = _check_signal_stats_uid(
        _stats_path,
        os.getuid(),
        _allow_foreign_uid,
    )
    if not _stats_ok:
        logger.error(
            "REFUSING to start: %s. Re-run with --allow-foreign-uid (or set "
            "AYUMI_ALLOW_FOREIGN_UID=1) to proceed; the stats file will be "
            "self-healed to mode 0644 on the first write.",
            _stats_reason,
        )
        sys.exit(2)

    # Fail-closed: refuse paper mode on a live endpoint
    from adapters.ctrader.environment import (
        Environment,
        _infer_environment,
    )

    _startup_host = os.getenv("CTRADER_HOST", "") or os.getenv("CTRADER_OPENAPI_HOST", "")
    if _startup_host:
        _startup_env = _infer_environment(_startup_host)
        if execution_mode == "paper" and _startup_env == Environment.LIVE:
            logger.error(
                "Cannot start in paper mode on a live endpoint (%s). Use --mode live or --live.",
                _startup_host,
            )
            sys.exit(1)
    symbols = [s.strip().upper().replace("/", "") for s in args.symbols.split(",")]

    # Check for files in data/ not owned by current user (defense-in-depth)
    import pwd

    _current_uid = os.getuid()
    _data_dir = Path("data")
    if _data_dir.exists():
        _foreign_files = []
        for _f in _data_dir.rglob("*"):
            if _f.is_file():
                try:
                    _st = _f.stat()
                    if _st.st_uid != _current_uid:
                        _owner = pwd.getpwuid(_st.st_uid).pw_name
                        _foreign_files.append(f"{_f} (owned by {_owner})")
                except (KeyError, OSError):
                    pass
        if _foreign_files:
            logger.warning(
                "Found %d file(s) in data/ not owned by current user: %s",
                len(_foreign_files),
                ", ".join(_foreign_files[:5]),
            )

    # Check for root-owned log files (root contamination from prior runs).
    # Remove foreign-owned .log files so they get recreated with correct
    # ownership on the next setup_logging() call.  Unix allows deleting a
    # root-owned file if the containing directory is writable by current user.
    _logs_dir = PROJECT_ROOT / "logs"
    if _logs_dir.exists():
        for _log_f in _logs_dir.glob("*.log"):
            try:
                _lst = _log_f.stat()
                if _lst.st_uid != _current_uid:
                    _log_owner = pwd.getpwuid(_lst.st_uid).pw_name
                    logger.warning(
                        "Found foreign-owned log %s (owned by %s) — removing",
                        _log_f,
                        _log_owner,
                    )
                    _log_f.unlink()
                    logger.info(
                        "Removed foreign-owned %s (uid=%d) — will be recreated with correct ownership (uid=%d)",
                        _log_f.name,
                        _lst.st_uid,
                        _current_uid,
                    )
            except (KeyError, OSError) as _log_fix_err:
                logger.error(
                    "Could not remove foreign-owned %s: %s — run 'sudo chown %s:%s %s' to fix manually",
                    _log_f,
                    _log_fix_err,
                    os.getenv("USER", "$USER"),
                    os.getenv("USER", "$USER"),
                    _log_f,
                )

    # ── Single-instance guard (B1) ─────────────────────────────────────────
    from adapters.ctrader.pid_guard import acquire_pid_lock

    _pid_path = PROJECT_ROOT / "data" / "forward_test.pid"
    # Guard must be acquired BEFORE logging setup floods, but we need logging
    # for the guard's own messages, so set up basic logging first.
    setup_logging(level="DEBUG")
    # Specifically enable the spot feed and execution event loggers
    logging.getLogger("ayumi.openapi_spot_feed").setLevel(logging.DEBUG)
    # Compress stale logs (>7d) from prior logging configs. Replaces
    # originals with .gz archives — data preserved, space reclaimed.
    # Cheap to run at startup; avoids `logs/` growing without bound.
    compress_stale_logs(PROJECT_ROOT / "logs")
    _pid_ctx = acquire_pid_lock(_pid_path)
    _pid_guard = _pid_ctx.__enter__()  # acquire lock, exit(1) if duplicate
    _pid_guard.write_pid()

    logger.info("=== Ayumi Multi-Strategy Forward Test (Blend Pipeline) ===")
    logger.info("Symbols: %s", symbols)

    # ── Single-Connection Architecture ────────────────────────────────────
    # The old architecture created a separate CTraderOpenApiClient to fetch
    # historical bars, disconnected, slept 3s, then connected the spot feed
    # with the same credentials → cTrader's single-session rule caused a
    # death loop. Now the spot feed connects ONCE and historical bars are
    # fetched through the same authenticated connection by the engine's
    # _preload_historical_bars() after start().

    # 1. Build credentials
    credentials = cTraderCredentials(
        host=os.getenv("CTRADER_HOST", "demo-uk-eqx-01.p.c-trader.com"),
        port=int(os.getenv("CTRADER_SSL_PORT", "5212")),
        use_ssl=True,
        username=os.getenv("CTRADER_ACCOUNT", ""),
        password=os.getenv("CTRADER_PASSWORD", ""),
        sender_comp_id=os.getenv("CTRADER_SENDER_COMP_ID", ""),
        target_comp_id=os.getenv("CTRADER_TARGET_COMP_ID", ""),
        sender_sub_id=os.getenv("CTRADER_SENDER_SUB_ID", ""),
    )

    # 3. Instantiate strategies (validated 5-strategy regime-gated blend, +LBO Jul 22 2026)
    # SRMR+ uses its Optuna-validated params from src/forex_bot/config/strategies.yaml
    # when a config entry exists for the active XAUUSD symbol on M15 (card 25cbea7a).
    # Falls back to defaults if no validated entry is present.
    _srmr_yaml_config = load_srmr_config_from_yaml(
        symbol="XAUUSD",
        timeframe="M15",
        config_path=PROJECT_ROOT / "src" / "forex_bot" / "config" / "strategies.yaml",
    )
    if _srmr_yaml_config is not None:
        _srmr_config = _srmr_yaml_config
        logger.info("SRMR+ using validated strategies.yaml config 'srmr_xauusd_m15' (PF=7.16, WR=73.4%)")
    else:
        _srmr_config = SRMRPlusConfig(symbol="XAUUSD")
        logger.info("SRMR+ using default config (no validated strategies.yaml entry)")

    strategies = [
        KillzoneMomentumStrategy(config=KillzoneMomentumConfig()),
        TTCXAUUSDStrategy(),
        DualTFSqueezeProStrategy(config=DualTFSqueezeProConfig()),
        DonchianATRTrendV2Strategy(config=DonchianATRConfig()),
        SRMRPlusStrategy(config=_srmr_config),
        LondonBreakoutRetestStrategy(config=LondonBreakoutConfig()),
    ]

    logger.info(
        "Strategy pool: %d total — %s",
        len(strategies),
        [s.name for s in strategies],
    )

    # Apply --only filter if specified
    if args.only:
        keep = {s.strip() for s in args.only.split(",")}
        strategies = [s for s in strategies if s.name in keep]
        logger.info(
            "Strategy filter --only '%s': %d strategies kept",
            ", ".join(keep),
            len(strategies),
        )

    # Verify .name properties match STRATEGY_ID_MAP keys
    for s in strategies:
        assert s.name in STRATEGY_ID_MAP, f"Strategy .name '{s.name}' not in STRATEGY_ID_MAP"
        assert s.name in STRATEGY_TIMEFRAMES, f"Strategy .name '{s.name}' not in STRATEGY_TIMEFRAMES"
    logger.info("All strategy .name properties verified against maps")

    # When --only is used, filter the maps to match the active pool
    active_names = {s.name for s in strategies}
    active_strategy_timeframes = {k: v for k, v in STRATEGY_TIMEFRAMES.items() if k in active_names}
    active_strategy_id_map = {k: v for k, v in STRATEGY_ID_MAP.items() if k in active_names}

    # 4. Build blend runner
    blend_runner = build_blend_runner()
    correlation_gate = CorrelationGate()
    regime_gate = RegimeGate()
    heartbeat = HeartbeatTracker(interval=100)

    # 5. Build engine config with strategy_timeframes and multi-symbol
    config = ForwardTestConfig(
        symbol=symbols[0],
        symbols=symbols,
        starting_balance=10_000.0,
        min_confidence=0.30,  # Match SRF-validated threshold
        max_bars_per_symbol=500,
        min_bars_for_evaluation=55,
        live_mode=(execution_mode == "live"),
        execution_mode=execution_mode,
        strategy_timeframes=active_strategy_timeframes,
        bar_period_minutes=15,  # M15 — aligns primary TF with fastest strategy in the blend
        preload_bar_count=200,  # bars per symbol/timeframe fetched through spot feed
    )

    # 6. Create blend-aware engine
    engine = BlendForwardTestEngine(
        config=config,
        strategies=strategies,
        ftmo_config=FTMOConfig(min_risk_reward=0.0),
        credentials=credentials,
        blend_runner=blend_runner,
        correlation_gate=correlation_gate,
        heartbeat=heartbeat,
        strategy_id_map=active_strategy_id_map,
        regime_gate=regime_gate,
        blend_mode=True,
    )

    # Release correlation slots when paper positions close.
    engine.register_callback("on_position_closed", engine.on_position_closed_release)

    # Historical bars are fetched automatically by engine.start() through
    # the single spot feed connection (_preload_historical_bars). No separate
    # client connection needed.

    # 8. Shutdown handler
    def shutdown(signum, frame):
        logger.info("Shutdown signal — stopping engine...")
        engine.stop()
        blend_runner.stop()
        sys.exit(0)

    sig_module.signal(sig_module.SIGINT, shutdown)
    sig_module.signal(sig_module.SIGTERM, shutdown)

    # ── Connection reliability wiring (BQ-716) ──────────────────────────
    from adapters.ctrader.connection_manager import (
        ConnectionManager as _ConnectionManager,
    )

    _connection_mgr = _ConnectionManager()
    wire_connection_reliability(_connection_mgr)

    # ── Startup diagnostics (B5) ──────────────────────────────────────────
    logger.info("=== STARTING MULTI-STRATEGY FORWARD TEST (Single-Connection) ===")
    logger.info("Pipeline: KZ + DualTF + Donchian + SRMR+ (regime-gated) → Correlation Gate → Blend Runner → cTrader")
    logger.info("Startup diagnostic: strategies=%s", [s.name for s in strategies])
    logger.info("Startup diagnostic: symbols=%s", symbols)
    logger.info(
        "Startup diagnostic: bar_period=%dm, min_confidence=%.2f",
        config.bar_period_minutes,
        config.min_confidence,
    )
    logger.info("Startup diagnostic: strategy_timeframes=%s", STRATEGY_TIMEFRAMES)
    # ── Startup retry guard (card 237f5427) ───────────────────────────────
    # Bug: 2026-08-24 21:50–21:53 UTC — cTrader demo API outage caused TCP
    # connect timeouts on initial startup. The engine's 20-attempt reconnect
    # circuit breaker only covers mid-flight sessions; an initial-startup
    # timeout fails the launch immediately → systemd status=1/FAILURE. With
    # StartLimitBurst=10 in 600s, a sustained outage silently stops the
    # service. Wrap engine.start() with bounded exponential backoff so a
    # brief broker outage (~60s) is absorbed at startup. systemd
    # Restart=always + StartLimitBurst remain as last-resort backstop if
    # all retries fail.
    _STARTUP_RETRY_ATTEMPTS = 5  # total attempts (1 initial + 4 retries)
    _STARTUP_RETRY_BACKOFFS_S = (2.0, 4.0, 8.0, 16.0, 30.0)  # 4 backoffs between 5 attempts
    started = False
    for _startup_attempt in range(1, _STARTUP_RETRY_ATTEMPTS + 1):
        try:
            started = engine.start()
        except Exception as _startup_exc:
            # Treat unexpected exceptions as transient — log + retry.
            if _startup_attempt < _STARTUP_RETRY_ATTEMPTS:
                _backoff = _STARTUP_RETRY_BACKOFFS_S[_startup_attempt - 1]
                logger.warning(
                    "Engine start raised %s (attempt %d/%d) — retrying in %.1fs",
                    type(_startup_exc).__name__,
                    _startup_attempt,
                    _STARTUP_RETRY_ATTEMPTS,
                    _backoff,
                )
                time.sleep(_backoff)
                continue
            logger.error(
                "Engine start raised %s on final attempt (%d/%d): %s",
                type(_startup_exc).__name__,
                _startup_attempt,
                _STARTUP_RETRY_ATTEMPTS,
                _startup_exc,
            )
            started = False
            break
        if started:
            if _startup_attempt > 1:
                logger.info(
                    "Engine started on attempt %d/%d after transient failures",
                    _startup_attempt,
                    _STARTUP_RETRY_ATTEMPTS,
                )
            break
        if _startup_attempt < _STARTUP_RETRY_ATTEMPTS:
            _backoff = _STARTUP_RETRY_BACKOFFS_S[_startup_attempt - 1]
            logger.warning(
                "Engine failed to start (attempt %d/%d) — retrying in %.1fs",
                _startup_attempt,
                _STARTUP_RETRY_ATTEMPTS,
                _backoff,
            )
            time.sleep(_backoff)
    if not started:
        logger.error("Engine failed to start after %d attempts. See logs above for the specific failure reason.", _STARTUP_RETRY_ATTEMPTS)
        logger.error(
            "For live mode, verify: CTRADER_OPENAPI_CLIENT_ID, CTRADER_OPENAPI_CLIENT_SECRET, "
            "CTRADER_OPENAPI_ACCESS_TOKEN, CTRADER_OPENAPI_REFRESH_TOKEN, "
            "CTRADER_OPENAPI_ACCOUNT_ID, CTRADER_OPENAPI_TRADER_LOGIN in .env"
        )
        blend_runner.stop()
        # Card 080094ef r2: record exit so the next restart can classify
        # this as ``pid_guard_or_launch_failure`` instead of being masked
        # by the live unit's success state.
        _record_last_exit(1, "sys_exit", detail="engine_start_failure_after_retries")
        sys.exit(1)

    # ── FTMOGuard wiring (card dd32226b) ──────────────────────────────────
    # FTMOGuard enforces peak-based trailing DD at the runner level,
    # complementing RiskGuard's starting-balance DD at the signal level.
    # They share the same kill_switch instance so that a freeze on
    # either path halts trading via the existing global freeze path.
    # Sized off the FTMO 1-Step Standard reference account ($10K).
    _ftmo_guard = FTMOGuard(
        kill_switch=getattr(engine, "_kill_switch", None),
        starting_balance=FTMO_REFERENCE_ACCOUNT_SIZE,
    )
    logger.info(
        "FTMOGuard active: starting_balance=$%.2f dd_reduce=%.1f%% dd_freeze=%.1f%% daily_loss=%.1f%% kill_switch=%s",
        FTMO_REFERENCE_ACCOUNT_SIZE,
        _ftmo_guard._dd_reduce_pct,
        _ftmo_guard._dd_freeze_pct,
        _ftmo_guard._max_daily_loss_pct,
        "wired" if getattr(engine, "_kill_switch", None) is not None else "NONE",
    )

    # Card 627b4f66: Reconcile persisted peak from RiskGuard state file
    # BEFORE any FTMO update() call, so trailing-DD computation uses the
    # true high-water-mark from the previous session (see
    # ``_reconcile_ftmo_peak_from_persisted_state`` docstring for the
    # full rationale + paper-vs-live semantics).
    _reconciled_peak = _reconcile_ftmo_peak_from_persisted_state(
        _ftmo_guard,
        PROJECT_ROOT / "data" / "state" / "risk_guard_state.json",
    )
    logger.info(
        "FTMO peak reconcile complete: peak=$%.2f (post-reconcile)",
        _reconciled_peak,
    )

    # ── Startup reconciliation (card 0e0338d4) ─────────────────────────────
    # engine.start() already calls _seed_existing_positions() which ADDS
    # to the sizer's _open_positions dict, but does not clear phantom
    # entries left over from the _open_risk restore path or duplicate
    # registrations. Run a nuke-and-rebuild reconciliation now so the
    # sizer's positions_carried count matches the broker exactly from
    # tick #1 of the new session.
    _broker_positions: list = []
    _feed = getattr(engine, "_market_feed", None)
    if _feed is not None and hasattr(_feed, "reconcile"):
        try:
            _broker_positions = _feed.reconcile() or []
            logger.info(
                "Startup reconciliation: broker reports %d open position(s)",
                len(_broker_positions),
            )
        except Exception as _recon_exc:
            logger.warning(
                "Startup reconciliation: broker.reconcile() failed (non-fatal): %s",
                _recon_exc,
            )
            _broker_positions = []
    try:
        _recon_result = blend_runner.reconcile_with_broker(_broker_positions)
        logger.info(
            "Startup reconciliation result: positions %d→%d (seeded=%d, diverged=%s, open_risk $%.2f→$%.2f)",
            _recon_result["before_count"],
            _recon_result["after_count"],
            _recon_result["seeded_count"],
            _recon_result["diverged"],
            _recon_result["before_open_risk"],
            _recon_result["after_open_risk"],
        )
    except Exception as _recon_exc:
        logger.warning(
            "Startup reconciliation: blend_runner.reconcile_with_broker() "
            "failed (non-fatal, continuing with engine-seeded positions): %s",
            _recon_exc,
        )

    # Write health JSON immediately on startup to clear any stale "down" state
    write_forward_test_health_json(engine)
    logger.info("Forward test health JSON written on startup")

    # ── Equity tracker (A8) ─────────────────────────────────────────────
    _equity_tracker = EquityTracker(
        data_dir=PROJECT_ROOT / "data",
        starting_balance=10_000.0,
    )
    _equity_record_interval = 300.0  # 5 minutes
    _last_equity_record = 0.0  # record immediately on first loop
    _last_balance_sync = 0.0  # sync RiskGuard from cTrader every 5 min
    _reconcile_interval = 300.0  # 5 minutes — sizer/broker drift check
    _last_reconcile = 0.0  # reconcile immediately on first loop

    # ── Periodic health loop (B5) ─────────────────────────────────────────
    try:
        _health_interval = 60.0
        _last_health_log = time.monotonic()
        while True:
            time.sleep(1)
            if not engine.is_running:
                # Card 7d3b535d (rework of f37e7b74 INSUFFICIENT): surface the
                # exit reason so post-mortem analysis can distinguish proactive
                # 24h rotation from chaotic circuit-breaker flap. The
                # ``_running`` flag is the single observed signal here —
                # ``_start_monotonic`` being None indicates engine never
                # properly started; attempts counter surviving the run
                # indicates a circuit-breaker flap.
                _exit_uptime = None
                try:
                    if getattr(engine, "_start_monotonic", None) is not None:
                        _exit_uptime = time.monotonic() - engine._start_monotonic
                except Exception:  # noqa: BLE001
                    _exit_uptime = None
                _attempts = getattr(getattr(engine, "_health", None), "reconnection_attempts", None)
                if _exit_uptime is not None and _exit_uptime >= 23 * 3600 + 30 * 60:
                    logger.info(
                        "[Rotation] Engine exited cleanly at uptime=%.0fs "
                        "after proactive 24h rotation (reconnection_attempts=%s) — "
                        "systemd will auto-restart with fresh session",
                        _exit_uptime,
                        _attempts,
                    )
                else:
                    logger.warning(
                        "[Exit] Engine is no longer running — uptime=%s reconnection_attempts=%s "
                        "(card 7d3b535d: investigate flap if not proactive rotation)",
                        f"{_exit_uptime:.0f}s" if _exit_uptime is not None else "n/a",
                        _attempts,
                    )
                break
            now = time.monotonic()
            if now - _last_health_log >= _health_interval:
                _last_health_log = now
                try:
                    stats = engine.get_stats()
                    h = stats.get("health", {})
                    t = stats.get("trading", {})
                    # Determine live execution status (BQ-1042: prevent false trade claims)
                    _live_fills = getattr(engine, "_live_fill_count", 0)
                    _paper_trades = t.get("trades_executed", 0)
                    _paper_balance = t.get("current_balance", 0.0)
                    _live_mode = execution_mode == "live"

                    # Fetch real cTrader balance in live mode
                    _balance_str = f"balance=${_paper_balance:.2f}"
                    if _live_mode and hasattr(engine, "_market_feed") and engine._market_feed is not None:
                        try:
                            from adapters.ctrader.account_state import (
                                get_balance as _get_balance,
                            )

                            _feed = engine._market_feed
                            _real_balance = _get_balance(
                                _feed.connection,
                                _feed.ctid_account_id,
                                timeout=5.0,
                            )
                            if _real_balance is not None:
                                engine._live_balance = float(_real_balance)
                                _balance_str = f"ctrader=${float(_real_balance):.2f}"
                            else:
                                _balance_str = "ctrader=N/A (timeout)"
                        except Exception as _bal_err:
                            _balance_str = f"ctrader=ERR ({_bal_err})"
                    _stats_fails = getattr(engine, "_stats_fail_count", 0)
                    # Periodic balance sync from cTrader → RiskGuard (every 5 min)
                    if now - _last_balance_sync >= 300.0:
                        _last_balance_sync = now
                        if hasattr(engine, "_sync_live_balance"):
                            engine._sync_live_balance()
                        # Activate live-balance mode on RiskGuard so that
                        # PaperTrader.update_market_prices tick recalculation
                        # does not overwrite the synced cTrader balance.
                        # PaperTrader recalculates _current_balance from
                        # starting_balance + pnl on every tick, which reverts
                        # the balance to $10K between 5-min sync intervals.
                        # sync_live_balance() sets a flag that makes
                        # update_balance() a no-op until the next sync.
                        # Daily counter reset hook (card 18d69d04): when the
                        # 17:00 America/Toronto trading-day boundary has
                        # crossed since the last sync, reset the per-day
                        # health counters (signals_sent, signals_failed_live,
                        # signals_pending, signals_cancelled, signals_rejected,
                        # signals_traded, signals_accepted).
                        if hasattr(engine, "_paper_trader") and engine._paper_trader:
                            _rg_for_reset = getattr(engine._paper_trader, "_risk_guard", None)
                            if _rg_for_reset is not None:
                                _current_trading_day = _rg_for_reset._current_trading_day()
                                _last_health_day = getattr(
                                    engine._health,
                                    "_last_health_trading_day",
                                    None,
                                )
                                if _last_health_day is None or _current_trading_day != _last_health_day:
                                    engine._health.reset_daily_counters()
                                    engine._health._last_health_trading_day = _current_trading_day
                                    logger.info(
                                        "[B5 Daily Reset] counters reset at trading_day=%s "
                                        "(17:00 America/Toronto boundary crossed)",
                                        _current_trading_day.isoformat(),
                                    )
                        if hasattr(engine, "_live_balance") and engine._live_balance:
                            _rg_pre = getattr(engine._paper_trader, "_risk_guard", None)
                            if _rg_pre is not None:
                                _rg_pre.sync_live_balance(engine._live_balance)

                        # ── FTMOGuard peak-based trailing DD update (card dd32226b) ──
                        # Runs on the same 5-min cadence as RiskGuard sync.
                        # Calls kill_switch.activate_global_freeze() on breach
                        # via the shared kill_switch instance. Best-effort: any
                        # exception here must not break the health loop.
                        try:
                            _ftmo_balance = float(
                                getattr(engine, "_live_balance", 0.0)
                                or (engine._paper_trader._current_balance if engine._paper_trader else 0.0)
                            )
                            _ftmo_open = (
                                len(engine._paper_trader._open_positions)
                                if engine._paper_trader and hasattr(engine._paper_trader, "_open_positions")
                                else 0
                            )
                            _ftmo_prev_action = _ftmo_guard.action_level.value
                            _ftmo_action = _ftmo_guard.update(
                                current_balance=_ftmo_balance,
                                open_positions=_ftmo_open,
                            )
                            if _ftmo_action.value != _ftmo_prev_action:
                                _ftmo_status = _ftmo_guard.get_status()
                                logger.warning(
                                    "[FTMO Guard] action=%s balance=$%.2f "
                                    "peak=$%.2f dd=%.2f%% daily_loss=%.2f%% "
                                    "open_positions=%d",
                                    _ftmo_action.value,
                                    _ftmo_status["current_balance"],
                                    _ftmo_status["peak_balance"],
                                    _ftmo_status["current_dd_pct"],
                                    _ftmo_status["daily_loss_pct"],
                                    _ftmo_open,
                                )
                        except Exception as _ftmo_err:
                            logger.warning(
                                "[FTMO Guard] update failed (non-fatal): %s",
                                _ftmo_err,
                            )

                    # Risk guard status (B5 health extension — A6)
                    # Uses two-balance model: starting=$10K baseline, live=cTrader
                    # NOTE (card 627b4f66): ``_display_pnl_from_start_pct`` is
                    # the canonical P&L-from-start field.  The previous
                    # ``_dd_pct`` formula ``(start - current) / start`` had a
                    # flipped sign and was mislabeled as drawdown; the
                    # canonical peak-based drawdown is reported separately
                    # in the ``ftmo:`` segment from
                    # ``FTMOGuard.get_status()['current_dd_pct']``.
                    _rg = getattr(engine._paper_trader, "_risk_guard", None)
                    if _rg is not None:
                        _start_bal = _rg._starting_balance
                        _bal = _rg._current_balance
                        _daily_pnl = _bal - _rg._daily_start_balance
                        _display_pnl_from_start_pct = _compute_pnl_from_start_pct(_start_bal, _bal)
                        _breaker = "ON" if _rg._circuit_breaker_triggered else "OFF"
                        _halt = "NONE"
                        if _rg._blocked_until is not None:
                            _halt = f"until {_rg._blocked_until.isoformat()}"
                        _risk_str = (
                            f"risk: starting=${_start_bal:.2f} balance=${_bal:.2f} "
                            f"daily_pnl=${_daily_pnl:.2f} pnl_from_start={_display_pnl_from_start_pct:+.2f}% "
                            f"dd_breaker={_breaker} halt={_halt}"
                        )
                    else:
                        _risk_str = "risk: N/A"

                    # FTMOGuard status (B5 health extension — card dd32226b)
                    # Peak-based trailing DD at the runner level, complements
                    # RiskGuard's starting-balance DD at the signal level.
                    _ftmo_status = _ftmo_guard.get_status()
                    _ftmo_str = (
                        f"ftmo: action={_ftmo_status['action_level']} "
                        f"dd={_ftmo_status['current_dd_pct']:.2f}% "
                        f"daily_loss={_ftmo_status['daily_loss_pct']:.2f}% "
                        f"peak=${_ftmo_status['peak_balance']:.2f}"
                    )

                    logger.info(
                        "[B5 Health] ticks=%d tps=%.2f bars=%d signals=%d "
                        "trades=%d live_fills=%d signals_failed_live=%d "
                        "signals_unreachable=%d regime_filtered=%d "
                        "stats_fails=%d %s %s %s uptime=%.0fs",
                        h.get("ticks_received", 0),
                        h.get("ticks_per_second", 0.0),
                        engine.health.bars_built,
                        engine.health.signals_generated,
                        _paper_trades,
                        _live_fills,
                        engine.health.signals_failed_live,
                        getattr(engine.health, "signals_unreachable", 0),
                        engine.health.signals_filtered_by_regime_gate,
                        _stats_fails,
                        _balance_str,
                        _risk_str,
                        _ftmo_str,
                        h.get("uptime_sec", 0),
                    )
                    # Signal lifecycle (card 0d7d7557):
                    #   strategy emit  →  regime gate  →  correlation gate
                    #     →  sizer  →  broker live submit
                    #
                    # The original warning used ``signals_generated > 0``,
                    # which counts every strategy emit that survives the
                    # bar threshold. That conflates regime-gate and
                    # correlation/sizer rejections with broker-side
                    # failures: 4/4 regime_choppy SRMR+ rejects would fire
                    # a "orders may not be reaching cTrader" warning even
                    # though the broker was never contacted.
                    #
                    # The warning now uses attempted-submission count
                    # (signals_sent + signals_failed_live +
                    # signals_unreachable) as the predicate via
                    # ``_evaluate_b5_health_warning``: only broker-attempted
                    # signals can trigger the warning. signals_generated is
                    # still logged in the B5 health line for operator
                    # context, but is no longer the trigger.
                    # signals_filtered_by_regime_gate is logged separately
                    # so operators can see gate churn without false-positive
                    # broker warnings.
                    #
                    # Card 0d7d7557 iter2 (M2): the warning now distinguishes
                    # rejection vs unreachability. NOT_CONNECTED returns
                    # can occur pre-contact (spot feed not operational,
                    # outcome.order is None) — the broker was never
                    # consulted in that case. The unreachable branch text
                    # points operators at the spot-feed connection state
                    # instead of the rejection log, so they diagnose the
                    # actual failure mode.
                    _warn = _evaluate_b5_health_warning(engine.health, _live_fills, _live_mode)
                    if _warn is not None:
                        if _warn["branch"] == "rejected":
                            logger.warning(
                                "[B5 Health] ⚠️  live_fills=0 attempted=%d "
                                "(signals_sent=%d signals_failed_live=%d) "
                                "— broker attempt failed/rejected; inspect "
                                "rejection log for errorCode (signals_unreachable=%d)",
                                _warn["attempted"],
                                _warn["sent"],
                                _warn["failed"],
                                _warn.get("unreachable", 0),
                            )
                        elif _warn["branch"] == "unreachable":
                            logger.warning(
                                "[B5 Health] ⚠️  live_fills=0 attempted=%d "
                                "(signals_sent=%d signals_unreachable=%d) "
                                "— broker unreachable; inspect spot-feed "
                                "connection state (NOT a broker rejection — "
                                "no order reached cTrader)",
                                _warn["attempted"],
                                _warn["sent"],
                                _warn["unreachable"],
                            )
                        else:  # no_fill
                            logger.warning(
                                "[B5 Health] ⚠️  live_fills=0 but signals_submitted=%d "
                                "— orders reaching broker but no fills confirmed "
                                "(SENT/TIMEOUT awaiting ack — see late-fill callbacks)",
                                _warn["sent"],
                            )
                    # Tick-to-bar pipeline health (Amendment 4)
                    # During market-closed hours (Fri 21:00 UTC → Sun 21:00 UTC),
                    # cTrader delivers stale/dribble ticks but no new bars form —
                    # that's expected. Downgrade to INFO so we don't generate
                    # false-positive stall warnings every health cycle.
                    #
                    # Amendment 5 (tick-to-bar stall fix): The bars_built counter
                    # only counts bars finalized from LIVE ticks at period
                    # boundaries — it does NOT include preloaded historical bars.
                    # With 200 preloaded bars and an H1 timeframe, bars_built
                    # stays 0 until the first hour boundary (up to 60 min),
                    # causing false-positive stall warnings every 60s. Fix:
                    # count total bars (including preloaded) before warning.
                    if h.get("ticks_received", 0) > 0 and engine.health.bars_built == 0:
                        # Count total bars across all keys, including preloaded
                        # and currently-forming bars (mirrors engine's internal
                        # B5 check at line ~2756 in forward_test_engine.py).
                        _total_bars_all = sum(len(v) for v in engine._bars.values()) + sum(
                            1 for v in engine._current_bar.values() if v is not None
                        )
                        if _total_bars_all > 0:
                            # Preloaded/forming bars exist — pipeline is healthy,
                            # just waiting for first live bar boundary crossing.
                            logger.debug(
                                "[B5 Pipeline] ticks=%d bars_built=%d total_bars=%d "
                                "— awaiting first bar boundary (preloaded bars available)",
                                h.get("ticks_received", 0),
                                engine.health.bars_built,
                                _total_bars_all,
                            )
                        elif _is_forex_market_closed():
                            logger.info(
                                "[B5 Pipeline] Market closed — ticks=%d bars=%d (idle, expected)",
                                h.get("ticks_received", 0),
                                engine.health.bars_built,
                            )
                        else:
                            logger.warning(
                                "[B5 Pipeline] Ticks received (%d) but zero bars built — "
                                "tick-to-bar pipeline may be stalled",
                                h.get("ticks_received", 0),
                            )

                    # Write health JSON for external watchdogs / dashboards
                    write_forward_test_health_json(engine)

                    # ── Periodic sizer/broker reconciliation (card 0e0338d4)
                    # Every 5 min: query cTrader for open positions and
                    # nuke-and-rebuild the sizer's _open_positions dict.
                    # Catches any drift introduced by duplicate
                    # registration, late close callbacks, or the
                    # _legacy_open_risk restore path. The first call
                    # fires immediately on the first health tick
                    # (_last_reconcile starts at 0).
                    if now - _last_reconcile >= _reconcile_interval:
                        _last_reconcile = now
                        _feed_recon = getattr(engine, "_market_feed", None)
                        if _feed_recon is not None and hasattr(_feed_recon, "reconcile"):
                            try:
                                _positions_now = _feed_recon.reconcile() or []
                                _recon = blend_runner.reconcile_with_broker(_positions_now)
                                if _recon.get("diverged"):
                                    logger.warning(
                                        "[B5 Reconcile] sizer/broker drift: "
                                        "positions %d→%d (seeded=%d, "
                                        "open_risk $%.2f→$%.2f)",
                                        _recon["before_count"],
                                        _recon["after_count"],
                                        _recon["seeded_count"],
                                        _recon["before_open_risk"],
                                        _recon["after_open_risk"],
                                    )
                                else:
                                    logger.info(
                                        "[B5 Reconcile] sizer/broker in sync: positions=%d, open_risk=$%.2f",
                                        _recon["after_count"],
                                        _recon["after_open_risk"],
                                    )
                            except Exception as _recon_err:
                                logger.warning(
                                    "[B5 Reconcile] failed (non-fatal): %s",
                                    _recon_err,
                                )

                    # ── Equity snapshot (A8) — every 5 min ──────────────────
                    if now - _last_equity_record >= _equity_record_interval:
                        _last_equity_record = now
                        try:
                            # Determine current balance and trade count
                            _eq_balance = _paper_balance
                            if hasattr(engine, "_live_balance"):
                                _eq_balance = engine._live_balance
                            _eq_trades = t.get("trades_executed", 0)
                            if _live_mode:
                                _eq_trades = getattr(engine, "_live_fill_count", 0)
                            _equity_tracker.record(_eq_balance, _eq_trades)
                            logger.info(
                                "[A8 Equity] Recorded: balance=$%.2f trades=%d",
                                _eq_balance,
                                _eq_trades,
                            )
                        except Exception as _eq_err:
                            logger.warning("[A8 Equity] Record failed: %s", _eq_err)

                        # ── Edge telemetry canonical-state snapshot (card d8c2a10b)
                        # Re-writes data/edge_telemetry_state.json with the current
                        # strategy×symbol rolling stats on the same 5-min cadence as
                        # the operational equity record. Closes the 47h+ drift gap
                        # between the trade-by-trade JSONL (only writes on close) and
                        # the operating live feed (health/equity/signal_stats every
                        # 60s–5min). Best-effort: any error logs and continues.
                        try:
                            _edge_tracker = blend_runner.get_edge_tracker()
                            _snap = _edge_tracker.write_state_snapshot()
                            logger.info(
                                "[F4 EdgeTelemetry] Canonical state snapshot written: "
                                "pairs=%d trades=%d",
                                _snap.get("total_strategy_symbol_pairs", 0),
                                _snap.get("total_trades", 0),
                            )
                        except Exception as _snap_err:
                            logger.warning(
                                "[F4 EdgeTelemetry] State snapshot failed: %s", _snap_err
                            )
                except Exception as exc:
                    logger.warning("[B5 Health] Error logging health: %s", exc)
    except KeyboardInterrupt:
        shutdown(None, None)
    except Exception as _exc:
        # Card 080094ef r2: record uncaught exceptions so the next restart
        # cycle can label the prior exit as ``uncaught_exception_exit_<n>``
        # instead of being masked as ``clean_24h_rotation``. Re-raise so
        # the existing traceback behaviour is preserved for ops triage.
        try:
            _record_last_exit(
                1, "exception",
                detail=f"{type(_exc).__name__}: {_exc}",
            )
        finally:
            raise
        # Write daily equity report on exit (A8)
        try:
            _report_path = _equity_tracker.write_daily_report()
            if _report_path:
                logger.info("[A8 Equity] Daily report written: %s", _report_path)
        except Exception:  # noqa: S110
            pass
        # Release PID lock on exit
        try:
            _pid_ctx.__exit__(None, None, None)
        except Exception:  # noqa: S110
            pass


if __name__ == "__main__":
    main()
