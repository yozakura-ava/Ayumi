"""CUSUM decay monitor — live forward-test drift detection vs. backtest baseline.

Implements the cumulative-sum (CUSUM) statistical-process-control chart
described in the spec for card 8f41b881 (Sprint D 5 — Strategy Decay
Monitor). It compares the live forward-test return stream against the
backtest baseline mean/shape and emits a LOUD, structured alert when
either side of the two-sided CUSUM crosses the control limit ``h``.

Math
----
For each observation ``x_t`` (per-trade or per-day return, in the
units the operator chose for the baseline), compute the *standardized
residual*:

    z_t = (x_t - baseline_mean) / baseline_std

Two one-sided CUSUM statistics (standard Page 1954 form):

    S_plus_0  = 0
    S_plus_t  = max(0, S_plus_{t-1} + z_t - k)        # detects μ > μ₀

    S_minus_0 = 0
    S_minus_t = max(0, S_minus_{t-1} - z_t - k)       # detects μ < μ₀

where ``k`` (drift threshold / reference value) and ``h`` (control
limit / decision interval) are tunable in standard-deviation units.
``k`` is typically 0.5σ (half the smallest shift worth detecting) and
``h`` is typically 4.0–5.0σ (tunes the false-alarm rate). The
trade-off is the textbook CUSUM trade-off: smaller ``k`` raises
sensitivity (faster detection) at the cost of more false alarms;
larger ``h`` raises specificity at the cost of detection delay.

Alerting contract
-----------------
The monitor emits :class:`CusumAlert` records when **either** statistic
crosses ``h``:

    alert iff S_plus_t ≥ h    → direction = "up"
    alert iff S_minus_t ≥ h   → direction = "down"

Each alert carries the strategy id, the direction of drift, the CUSUM
value at the crossing, the run length, the baseline summary, the
threshold evidence (``k``, ``h`` in σ units), a unique alert id, and
the ISO timestamp. Alerts are also routed through ``logger.error``
under the ``ayumi.monitoring.cusum`` logger so they appear in the
existing alert/heartbeat pipeline (see
``common/logging_config.py`` for routing rules).

The monitor **never** silently retrains, adjusts, or modifies the
strategy, its baseline, or any config. It is observational only.
Decisions to kill a strategy belong to the kill-criteria owner
(``policy/kill_criteria.py``) which consumes the monitor's structured
output through :meth:`CusumDecayMonitor.as_kill_criteria`.

Reset policy
------------
Two reset modes are supported, both explicit (no silent resets ever):

* ``ResetPolicy.POST_ALERT_RESET`` — after an alert fires, both CUSUM
  statistics are reset to 0 so the next run starts clean. The reset
  itself is appended to ``self._reset_log`` with the alert id and
  timestamp so the audit trail shows what happened.
* ``ResetPolicy.NO_RESET`` — statistics are never reset automatically.
  The operator must call :meth:`reset` explicitly with a documented
  reason. Useful for forensic post-mortem analysis where you want
  the full CUSUM trajectory preserved.

Forward-test feed wiring
------------------------
The monitor consumes an :class:`ForwardTestFeed` (Protocol) — a thin
duck-typed interface with one method,
``get_strategy_returns(strategy_id) -> Iterable[float]``. The monitor
fail-loud raises :class:`CusumFeedMissingError` when:

* the feed object is ``None``,
* the strategy id is unknown to the feed (raises or returns empty),
* the feed yields no observations for the strategy.

This guarantees the monitor never silently degrades to "no signal"
when the live feed is missing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Iterator, Protocol, runtime_checkable

from core.conviction import KillCriterion

logger = logging.getLogger("ayumi.monitoring.cusum")


# ── Reset policy ────────────────────────────────────────────────────────────


class ResetPolicy(str, Enum):
    """Post-alert CUSUM reset policy.

    Both modes are explicit — the monitor never resets without a
    recorded reason. ``POST_ALERT_RESET`` resets automatically after
    an alert (useful when you want the next run to start clean);
    ``NO_RESET`` keeps the statistics running so you can inspect the
    post-alert trajectory during post-mortem.
    """

    POST_ALERT_RESET = "post_alert_reset"
    NO_RESET = "no_reset"


# ── Configuration ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CusumConfig:
    """Configuration for a single-strategy CUSUM decay monitor.

    Parameters
    ----------
    strategy_id:
        Stable identifier for the strategy being monitored (e.g.
        ``"srmr_plus"``). Embedded in every alert so the kill-criteria
        owner can route the alert to the right place.
    baseline_mean:
        Expected return per observation in the same units as the
        observation (e.g. per-trade R or per-day log-return).
        Sourced from the backtest baseline.
    baseline_std:
        Expected standard deviation of observations, in the same units.
        Used to standardize observations into σ-units so ``k`` and
        ``h`` are interpretable. Must be > 0 — passing 0 raises
        :class:`CusumConfigError`.
    drift_threshold_k:
        Reference value in σ-units. Typical value 0.5 (half the
        smallest shift worth detecting). Larger values reduce
        sensitivity. Must be > 0.
    control_limit_h:
        Decision interval in σ-units. Typical value 4.0–5.0. Larger
        values reduce false alarms at the cost of detection delay.
        Must be > 0.
    reset_policy:
        See :class:`ResetPolicy`. Defaults to POST_ALERT_RESET.
    """

    strategy_id: str
    baseline_mean: float
    baseline_std: float
    drift_threshold_k: float
    control_limit_h: float
    reset_policy: ResetPolicy = ResetPolicy.POST_ALERT_RESET

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise CusumConfigError("strategy_id must be non-empty")
        if self.baseline_std <= 0:
            raise CusumConfigError(
                f"baseline_std must be > 0 (got {self.baseline_std}); "
                "CUSUM standardisation needs a positive scale."
            )
        if self.drift_threshold_k < 0:
            raise CusumConfigError(
                f"drift_threshold_k must be >= 0 (got {self.drift_threshold_k})"
            )
        if self.control_limit_h <= 0:
            raise CusumConfigError(
                f"control_limit_h must be > 0 (got {self.control_limit_h})"
            )


# ── Exceptions ──────────────────────────────────────────────────────────────


class CusumError(Exception):
    """Base class for CUSUM monitor errors."""


class CusumConfigError(CusumError):
    """Raised when the CUSUM configuration is invalid."""


class CusumFeedMissingError(CusumError):
    """Raised when the forward-test feed is missing, empty, or wrong shape.

    The monitor never silently degrades to "no signal" — when the
    feed goes away, the monitor fail-louds so the underlying wiring
    problem surfaces rather than masquerading as a healthy strategy.
    """


# ── Result dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class CusumAlert:
    """One CUSUM crossing event.

    Attributes
    ----------
    alert_id:
        UUID4, unique per alert (the monitor never reuses ids even
        after a reset).
    strategy_id:
        Strategy the alert applies to (copied from config).
    direction:
        ``"up"`` if S_plus crossed h (returns trending above baseline);
        ``"down"`` if S_minus crossed h (returns trending below
        baseline).
    cusum_value:
        The CUSUM statistic value at the moment of crossing
        (S_plus_t or S_minus_t). Always ≥ h by construction.
    run_length:
        Number of consecutive samples that have contributed to the
        current CUSUM run (since the last reset of that side, or
        since the last time the cumulative sum was clamped back to
        zero). Larger run lengths correspond to slower shifts;
        shorter run lengths correspond to abrupt step changes.
    sample_count:
        Total samples processed by the monitor since the last reset
        (either explicit or post-alert). Reset per ``reset_policy``.
    h_threshold:
        The control limit at the time of the alert (in σ-units).
    k_threshold:
        The drift threshold at the time of the alert (in σ-units).
    baseline_mean, baseline_std:
        Baseline summary at the time of the alert (in observation
        units).
    timestamp:
        ISO 8601 timestamp of the crossing.
    evidence:
        Human-readable evidence string for logs and dashboards.
    reset_applied:
        Whether the post-alert reset (if configured) was applied
        before this alert record was constructed. Always False for
        the first alert in a run; True when reset_policy is
        POST_ALERT_RESET and the monitor has cleared state.
    """

    alert_id: str
    strategy_id: str
    direction: str  # "up" | "down"
    cusum_value: float
    run_length: int
    sample_count: int
    h_threshold: float
    k_threshold: float
    baseline_mean: float
    baseline_std: float
    timestamp: str
    evidence: str
    reset_applied: bool = False

    def __str__(self) -> str:
        return (
            f"CusumAlert[{self.alert_id[:8]} strategy={self.strategy_id} "
            f"dir={self.direction} S={self.cusum_value:.3f} run_length={self.run_length} "
            f"h={self.h_threshold:.3f}]"
        )


@dataclass(frozen=True)
class CusumResetRecord:
    """Audit record for an explicit CUSUM reset.

    The monitor never resets without producing one of these. Both
    automatic post-alert resets (per ResetPolicy.POST_ALERT_RESET)
    and operator-initiated resets (per ``reset()``) are recorded.
    """

    reset_id: str
    strategy_id: str
    reason: str  # "post_alert" | "operator"
    alert_id: str | None  # set when reason == "post_alert"
    timestamp: str


@dataclass(frozen=True)
class CusumState:
    """Snapshot of the monitor's current CUSUM state.

    Returned by :meth:`CusumDecayMonitor.current_state` so the
    kill-criteria owner and dashboards can read the live values
    without mutating anything.
    """

    strategy_id: str
    s_plus: float
    s_minus: float
    run_length_plus: int
    run_length_minus: int
    sample_count: int
    h_threshold: float
    k_threshold: float
    baseline_mean: float
    baseline_std: float
    reset_policy: ResetPolicy


# ── Forward-test feed protocol ──────────────────────────────────────────────


@runtime_checkable
class ForwardTestFeed(Protocol):
    """Minimal interface for the live forward-test return feed.

    A real adapter (cTrader, paper-trader, etc.) implements this by
    exposing one method that yields per-observation returns for a
    given strategy. The monitor never depends on a specific
    concrete class — it consumes the protocol, so tests can pass a
    list-backed stub without instantiating any live infrastructure.

    Contract
    --------
    * ``get_strategy_returns(strategy_id)`` returns an iterable of
      floats (list, tuple, generator — anything that yields floats).
    * Implementations MUST raise a ``KeyError`` or return an empty
      iterable when the strategy is unknown — the monitor treats
      both as "missing feed" and fail-louds.
    * Implementations MUST yield floats (not strings or None).
      Non-float observations are skipped with a logged warning so
      a single malformed bar doesn't break the monitor.
    """

    def get_strategy_returns(self, strategy_id: str) -> Iterable[float]: ...


# ── The monitor ─────────────────────────────────────────────────────────────


class CusumDecayMonitor:
    """Two-sided CUSUM decay monitor for one strategy.

    Parameters
    ----------
    config:
        A :class:`CusumConfig` carrying the baseline, thresholds,
        reset policy, and strategy id.

    Notes
    -----
    * **No silent resets.** Every state change is recorded in
      ``self._reset_log`` with reason + timestamp.
    * **No silent retrains.** The monitor never mutates the config,
      the baseline, or any external state. All alerting is observational;
      kill decisions belong to the kill-criteria owner.
    * **No silent degradation.** :meth:`evaluate_feed` raises
      :class:`CusumFeedMissingError` when the live feed is missing,
      unknown, or empty. Operators see the failure immediately
      rather than discovering the gap days later in a post-mortem.
    * **Thread-safety:** Not thread-safe by design. Construct one
      monitor per strategy per consumer; mutate state in a single
      thread. The forward-test loop is single-threaded per strategy
      so this is the natural shape.
    """

    def __init__(self, config: CusumConfig) -> None:
        self.config = config
        self._s_plus: float = 0.0
        self._s_minus: float = 0.0
        self._run_length_plus: int = 0
        self._run_length_minus: int = 0
        self._sample_count: int = 0
        self._alerts: list[CusumAlert] = []
        self._reset_log: list[CusumResetRecord] = []
        self._last_alert: CusumAlert | None = None

    # ── Read-only accessors ──────────────────────────────────────────────

    @property
    def alerts(self) -> list[CusumAlert]:
        """All alerts emitted since the last full reset, oldest first."""
        return list(self._alerts)

    @property
    def reset_log(self) -> list[CusumResetRecord]:
        """All reset records since process start, oldest first."""
        return list(self._reset_log)

    def current_state(self) -> CusumState:
        """Return a frozen snapshot of the current CUSUM state."""
        return CusumState(
            strategy_id=self.config.strategy_id,
            s_plus=self._s_plus,
            s_minus=self._s_minus,
            run_length_plus=self._run_length_plus,
            run_length_minus=self._run_length_minus,
            sample_count=self._sample_count,
            h_threshold=self.config.control_limit_h,
            k_threshold=self.config.drift_threshold_k,
            baseline_mean=self.config.baseline_mean,
            baseline_std=self.config.baseline_std,
            reset_policy=self.config.reset_policy,
        )

    def as_kill_criteria(self) -> list[KillCriterion]:
        """Return the current CUSUM state as two :class:`KillCriterion` rows.

        The kill-criteria owner (``policy/kill_criteria.py``) can
        append these rows to whatever evaluation pipeline it owns;
        they obey the same dataclass contract as every other kill
        criterion in the system (name, triggered, value, threshold,
        evidence).

        * ``cusum_up`` — ``S_plus >= h`` → triggered.
        * ``cusum_down`` — ``S_minus >= h`` → triggered.

        Both rows are always present (never None) so downstream
        consumers see a stable shape even before any samples have
        been processed.
        """
        h = self.config.control_limit_h
        s_plus_triggered = self._s_plus >= h
        s_minus_triggered = self._s_minus >= h
        s_plus_evidence = (
            f"CUSUM upward drift S_plus={self._s_plus:.4f} >= h={h:.4f} "
            f"after {self._sample_count} samples for strategy {self.config.strategy_id}"
            if s_plus_triggered
            else (
                f"CUSUM upward S_plus={self._s_plus:.4f} < h={h:.4f} "
                f"after {self._sample_count} samples for strategy {self.config.strategy_id}"
            )
        )
        s_minus_evidence = (
            f"CUSUM downward drift S_minus={self._s_minus:.4f} >= h={h:.4f} "
            f"after {self._sample_count} samples for strategy {self.config.strategy_id}"
            if s_minus_triggered
            else (
                f"CUSUM downward S_minus={self._s_minus:.4f} < h={h:.4f} "
                f"after {self._sample_count} samples for strategy {self.config.strategy_id}"
            )
        )
        return [
            KillCriterion(
                name="cusum_up",
                triggered=s_plus_triggered,
                value=self._s_plus,
                threshold=h,
                evidence=s_plus_evidence,
            ),
            KillCriterion(
                name="cusum_down",
                triggered=s_minus_triggered,
                value=self._s_minus,
                threshold=h,
                evidence=s_minus_evidence,
            ),
        ]

    # ── Update path ──────────────────────────────────────────────────────

    def update(self, observation: float) -> list[CusumAlert]:
        """Ingest one observation; return any alerts that crossed h.

        Standardises the observation against the baseline, updates
        S_plus and S_minus using the Page CUSUM recursion, and emits
        a :class:`CusumAlert` for any side whose value has reached
        or exceeded ``h``. Applies the post-alert reset policy when
        configured to do so, and records every reset in
        ``self._reset_log`` (no silent resets ever).
        """
        cfg = self.config
        z = (float(observation) - cfg.baseline_mean) / cfg.baseline_std
        self._sample_count += 1

        # Upper side — detects μ > μ₀ (positive drift)
        prev_s_plus = self._s_plus
        self._s_plus = max(0.0, prev_s_plus + z - cfg.drift_threshold_k)
        if self._s_plus > 0:
            if prev_s_plus > 0:
                self._run_length_plus += 1
            else:
                # Fresh run — S_plus just cleared zero on this sample.
                self._run_length_plus = 1
        else:
            self._run_length_plus = 0

        # Lower side — detects μ < μ₀ (negative drift)
        prev_s_minus = self._s_minus
        self._s_minus = max(0.0, prev_s_minus - z - cfg.drift_threshold_k)
        if self._s_minus > 0:
            if prev_s_minus > 0:
                self._run_length_minus += 1
            else:
                self._run_length_minus = 1
        else:
            self._run_length_minus = 0

        alerts: list[CusumAlert] = []
        if self._s_plus >= cfg.control_limit_h:
            alert = self._build_alert(
                direction="up",
                cusum_value=self._s_plus,
                run_length=self._run_length_plus,
                reset_applied=False,
            )
            alerts.append(alert)
            logger.error("CUSUM UP alert: %s", alert)
            if cfg.reset_policy == ResetPolicy.POST_ALERT_RESET:
                self._post_alert_reset(alert.alert_id)

        if self._s_minus >= cfg.control_limit_h:
            alert = self._build_alert(
                direction="down",
                cusum_value=self._s_minus,
                run_length=self._run_length_minus,
                reset_applied=False,
            )
            alerts.append(alert)
            logger.error("CUSUM DOWN alert: %s", alert)
            if cfg.reset_policy == ResetPolicy.POST_ALERT_RESET:
                self._post_alert_reset(alert.alert_id)

        if alerts:
            self._last_alert = alerts[-1]
            self._alerts.extend(alerts)
        return alerts

    def update_batch(self, observations: Iterable[float]) -> list[CusumAlert]:
        """Ingest an iterable of observations; return all alerts emitted."""
        out: list[CusumAlert] = []
        for obs in observations:
            out.extend(self.update(obs))
        return out

    def evaluate_feed(
        self,
        feed: ForwardTestFeed | None,
        strategy_id: str | None = None,
    ) -> list[CusumAlert]:
        """Pull observations from a forward-test feed and ingest them.

        Fail-loud contract:

        * ``feed is None`` → :class:`CusumFeedMissingError`.
        * strategy id mismatch → :class:`CusumFeedMissingError`.
        * Empty iterable returned → :class:`CusumFeedMissingError`.
        * Non-float observations → logged warning, skipped (never
          silently treated as zero).
        """
        sid = strategy_id or self.config.strategy_id
        if feed is None:
            raise CusumFeedMissingError(
                f"forward-test feed is None for strategy {sid!r}; "
                "monitor cannot evaluate without a live feed"
            )
        if not isinstance(feed, ForwardTestFeed):
            # Protocol check — covers explicit isinstance() callers.
            # runtime_checkable means hasattr is enough, but be defensive
            # for classes that look protocol-shaped without duck typing.
            if not hasattr(feed, "get_strategy_returns"):
                raise CusumFeedMissingError(
                    f"feed object of type {type(feed).__name__} does not implement "
                    "ForwardTestFeed (missing get_strategy_returns)"
                )
        try:
            raw = feed.get_strategy_returns(sid)
        except KeyError as exc:
            raise CusumFeedMissingError(
                f"forward-test feed has no entry for strategy {sid!r}: {exc}"
            ) from exc
        if raw is None:
            raise CusumFeedMissingError(
                f"forward-test feed returned None for strategy {sid!r}"
            )

        # Materialize so we can detect empty + filter non-floats in one pass.
        observations: list[float] = []
        try:
            iterator: Iterator[Any] = iter(raw)
        except TypeError as exc:
            raise CusumFeedMissingError(
                f"forward-test feed returned non-iterable for strategy {sid!r}: {exc}"
            ) from exc
        for item in iterator:
            if isinstance(item, bool):
                # bool is a subclass of int, not float — reject explicitly.
                logger.warning(
                    "CUSUM feed yielded bool (%r) for strategy %s — skipping",
                    item,
                    sid,
                )
                continue
            if isinstance(item, (int, float)):
                observations.append(float(item))
                continue
            try:
                observations.append(float(item))
            except (TypeError, ValueError):
                logger.warning(
                    "CUSUM feed yielded non-numeric %r for strategy %s — skipping",
                    item,
                    sid,
                )

        if not observations:
            raise CusumFeedMissingError(
                f"forward-test feed returned no observations for strategy {sid!r}"
            )

        return self.update_batch(observations)

    # ── Reset path ──────────────────────────────────────────────────────

    def reset(self, reason: str = "operator") -> None:
        """Explicit operator reset.

        Always recorded in ``self._reset_log``. Use this when you
        have decided to clear the CUSUM state for any reason
        outside the post-alert policy — for example after fixing
        a data-quality issue or after the operator manually
        re-fits the model.

        Parameters
        ----------
        reason:
            Free-form human-readable reason that will appear in the
            audit log. Defaults to ``"operator"`` but you should
            pass something more specific.
        """
        record = CusumResetRecord(
            reset_id=str(uuid.uuid4()),
            strategy_id=self.config.strategy_id,
            reason=reason,
            alert_id=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._do_reset()
        self._reset_log.append(record)
        logger.info(
            "CUSUM explicit reset for strategy %s: reason=%s reset_id=%s",
            self.config.strategy_id,
            reason,
            record.reset_id,
        )

    # ── Internals ───────────────────────────────────────────────────────

    def _do_reset(self) -> None:
        """Clear state without logging. Used by both reset paths."""
        self._s_plus = 0.0
        self._s_minus = 0.0
        self._run_length_plus = 0
        self._run_length_minus = 0
        self._sample_count = 0

    def _post_alert_reset(self, alert_id: str) -> None:
        """Apply the post-alert reset and record the audit entry."""
        record = CusumResetRecord(
            reset_id=str(uuid.uuid4()),
            strategy_id=self.config.strategy_id,
            reason="post_alert",
            alert_id=alert_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._do_reset()
        self._reset_log.append(record)
        logger.info(
            "CUSUM post-alert reset for strategy %s (alert_id=%s, reset_id=%s)",
            self.config.strategy_id,
            alert_id,
            record.reset_id,
        )

    def _build_alert(
        self,
        direction: str,
        cusum_value: float,
        run_length: int,
        reset_applied: bool,
    ) -> CusumAlert:
        cfg = self.config
        ts = datetime.now(timezone.utc).isoformat()
        evidence = (
            f"CUSUM {direction} drift detected for strategy {cfg.strategy_id}: "
            f"S_{direction}={cusum_value:.4f} crossed h={cfg.control_limit_h:.4f} "
            f"(k={cfg.drift_threshold_k:.4f}) after {run_length}-sample run "
            f"(total samples processed this epoch: {self._sample_count}); "
            f"baseline μ={cfg.baseline_mean:.6f}, σ={cfg.baseline_std:.6f}"
        )
        return CusumAlert(
            alert_id=str(uuid.uuid4()),
            strategy_id=cfg.strategy_id,
            direction=direction,
            cusum_value=cusum_value,
            run_length=run_length,
            sample_count=self._sample_count,
            h_threshold=cfg.control_limit_h,
            k_threshold=cfg.drift_threshold_k,
            baseline_mean=cfg.baseline_mean,
            baseline_std=cfg.baseline_std,
            timestamp=ts,
            evidence=evidence,
            reset_applied=reset_applied,
        )


__all__ = [
    "CusumConfig",
    "CusumAlert",
    "CusumResetRecord",
    "CusumState",
    "CusumDecayMonitor",
    "CusumConfigError",
    "CusumFeedMissingError",
    "CusumError",
    "ForwardTestFeed",
    "ResetPolicy",
]