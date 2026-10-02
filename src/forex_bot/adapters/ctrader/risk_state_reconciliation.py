"""Cross-check the two risk-state persistence files used by Ayumi.

Sprint 024 / card 591cbfe6 (2026-08-21): the forward test pipeline has
two parallel risk-state files:

  * ``data/risk_state_blend.json`` — written by
    :class:`risk.state_persistence.StatePersistence`, the source of truth
    for the BLEND forward-test pipeline (sizer balance, circuit breaker,
    per-strategy tracking).

  * ``data/state/risk_guard_state.json`` — written by
    :class:`adapters.ctrader.risk_guard.RiskGuard`, the source of truth
    for the cTrader adapter risk guard (peak/current/daily-start balance,
    trade counters, circuit breaker flag, daily-loss block).

Historically these have drifted (different schemas, different write
cadences, different load paths). This module provides a reconciliation
helper that compares the overlapping fields and reports divergence so
operators can decide whether to trust the BLEND file or the
RISK_GUARD file for a given run.

The reconciliation is intentionally READ-ONLY — it never overwrites
either file. Resolving divergence is an operator decision (see
``docs/forex/ayumi-token-rotation-runbook.md`` for the playbook).

Public API:
    :func:`reconcile_risk_states` — full reconciliation from disk paths
    :func:`reconcile_risk_state_dicts` — pure dict-in / dict-out form
        used by tests with divergent fixtures
    :func:`RiskStateReconciliation` — dataclass returned by both
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ayumi.risk_state_reconciliation")

# Default file locations — overridable in tests via the public API.
DEFAULT_BLEND_PATH = "data/risk_state_blend.json"
DEFAULT_RISK_GUARD_PATH = "data/state/risk_guard_state.json"

# Tolerance for floating-point balance comparisons (USD). One cent is
# the smallest unit that matters for any reconciliation outcome.
_BALANCE_TOLERANCE_USD = 0.01


@dataclass
class RiskStateReconciliation:
    """Result of comparing the two risk-state files.

    Fields:
        matched: True if every overlapping field agreed within tolerance.
        divergences: List of human-readable strings describing each
            divergence (empty when ``matched``).
        blend_balance: ``account_balance`` read from the BLEND file.
        risk_guard_balance: ``current_balance`` read from the RISK_GUARD file.
        blend_peak_balance: ``peak_balance`` from the BLEND file.
        risk_guard_peak_balance: ``peak_balance`` from the RISK_GUARD file.
        blend_circuit_halted: ``circuit_breaker.halted`` from the BLEND file.
        risk_guard_circuit_triggered: ``circuit_breaker_triggered`` from the
            RISK_GUARD file.
        trust_blend: Suggested trust signal — True when the BLEND file is
            the newer or more specific source for a forward-test run that
            uses the BLEND pipeline. Always computed; never used to
            overwrite either file.
        generated_at: ISO-8601 UTC timestamp of when this reconciliation
            was produced (for audit log).
    """

    matched: bool
    divergences: list[str] = field(default_factory=list)
    blend_balance: Optional[float] = None
    risk_guard_balance: Optional[float] = None
    blend_peak_balance: Optional[float] = None
    risk_guard_peak_balance: Optional[float] = None
    blend_circuit_halted: Optional[bool] = None
    risk_guard_circuit_triggered: Optional[bool] = None
    trust_blend: bool = True
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def divergent(self) -> bool:
        """Convenience: inverse of :attr:`matched`."""
        return not self.matched


def _read_json(path: Path) -> dict:
    """Load JSON from ``path``. Returns empty dict on missing/corrupt file.

    Both risk-state files are operator-managed; missing files are NOT
    treated as errors (a fresh deployment may legitimately have one
    file but not the other). Corrupt files are logged as warnings.
    """
    if not path.exists():
        logger.debug("Risk-state file missing: %s", path)
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Risk-state file unreadable: %s (%s)", path, exc)
        return {}


def reconcile_risk_state_dicts(blend: dict, risk_guard: dict) -> RiskStateReconciliation:
    """Compare two risk-state dicts and report divergences.

    Pure function — no I/O. Tests use this with divergent fixtures to
    prove the solver identifies each class of drift.

    Args:
        blend: Dict parsed from ``risk_state_blend.json``. Recognised keys:
            ``account_balance``, ``peak_balance``, ``circuit_breaker.halted``.
        risk_guard: Dict parsed from ``risk_guard_state.json``. Recognised
            keys: ``current_balance``, ``peak_balance``,
            ``circuit_breaker_triggered``.

    Returns:
        :class:`RiskStateReconciliation` summarising the comparison.
    """
    divergences: list[str] = []

    blend_balance = blend.get("account_balance")
    risk_guard_balance = risk_guard.get("current_balance")
    blend_peak = blend.get("peak_balance")
    risk_guard_peak = risk_guard.get("peak_balance")
    blend_cb_halted = _nested_get(blend, "circuit_breaker", "halted")
    risk_guard_cb_triggered = risk_guard.get("circuit_breaker_triggered")

    # 1. Current/account balance — the most safety-critical comparison.
    if blend_balance is not None and risk_guard_balance is not None:
        if abs(blend_balance - risk_guard_balance) > _BALANCE_TOLERANCE_USD:
            divergences.append(
                f"balance_drift: blend={blend_balance:.2f} "
                f"risk_guard={risk_guard_balance:.2f} "
                f"delta={blend_balance - risk_guard_balance:+.2f}"
            )

    # 2. Peak balance — secondary signal. Different operations update
    # each (the sizer updates BLEND on every trade; RiskGuard updates
    # RISK_GUARD on live-balance sync), so a small lag is normal. We
    # only flag when one is meaningfully larger than the other — the
    # smaller one is then stale and the operator should re-sync.
    if blend_peak is not None and risk_guard_peak is not None:
        if abs(blend_peak - risk_guard_peak) > _BALANCE_TOLERANCE_USD:
            divergences.append(
                f"peak_balance_drift: blend={blend_peak:.2f} "
                f"risk_guard={risk_guard_peak:.2f} "
                f"delta={blend_peak - risk_guard_peak:+.2f}"
            )

    # 3. Circuit-breaker state — divergence here is a SAFETY issue.
    # If one says halted and the other says not, an operator decision
    # is required (do not auto-resolve; either could be wrong).
    if (
        blend_cb_halted is not None
        and risk_guard_cb_triggered is not None
        and blend_cb_halted != risk_guard_cb_triggered
    ):
        divergences.append(
            f"circuit_breaker_mismatch: blend_halted={blend_cb_halted} "
            f"risk_guard_triggered={risk_guard_cb_triggered}"
        )

    matched = len(divergences) == 0

    # Trust heuristic: prefer the BLEND file for forward-test runs
    # because the BLEND pipeline is the active path for the forward
    # test launcher (scripts/launch_blend_forward_test.py). If the
    # BLEND file is missing entirely and only RISK_GUARD has data,
    # suggest trusting RISK_GUARD instead.
    trust_blend = bool(blend)

    return RiskStateReconciliation(
        matched=matched,
        divergences=divergences,
        blend_balance=float(blend_balance) if blend_balance is not None else None,
        risk_guard_balance=float(risk_guard_balance) if risk_guard_balance is not None else None,
        blend_peak_balance=float(blend_peak) if blend_peak is not None else None,
        risk_guard_peak_balance=float(risk_guard_peak) if risk_guard_peak is not None else None,
        blend_circuit_halted=_as_bool_or_none(blend_cb_halted),
        risk_guard_circuit_triggered=_as_bool_or_none(risk_guard_cb_triggered),
        trust_blend=trust_blend,
    )


def reconcile_risk_states(
    blend_path: str | Path = DEFAULT_BLEND_PATH,
    risk_guard_path: str | Path = DEFAULT_RISK_GUARD_PATH,
) -> RiskStateReconciliation:
    """Load both risk-state files from disk and reconcile them.

    Convenience wrapper around :func:`reconcile_risk_state_dicts` for
    the production code path (operator-driven / one-shot CLI use).

    Args:
        blend_path: Path to ``risk_state_blend.json``. Defaults to the
            forward-test deployment location.
        risk_guard_path: Path to ``risk_guard_state.json``. Defaults to
            the cTrader-adapter deployment location.

    Returns:
        :class:`RiskStateReconciliation` populated from disk. Either
        source dict may be empty if its file is missing/corrupt.
    """
    blend = _read_json(Path(blend_path))
    risk_guard = _read_json(Path(risk_guard_path))
    return reconcile_risk_state_dicts(blend, risk_guard)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _nested_get(d: dict, *keys: str):
    """Walk a dotted path through nested dicts. Returns None if absent."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


def _as_bool_or_none(value) -> Optional[bool]:
    """Return value if it's bool/None, else coerce. Defensive against
    JSON shapes that occasionally hand us 0/1 instead of true/false."""
    if value is None or isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    return None


__all__ = [
    "DEFAULT_BLEND_PATH",
    "DEFAULT_RISK_GUARD_PATH",
    "RiskStateReconciliation",
    "reconcile_risk_state_dicts",
    "reconcile_risk_states",
]
