"""
icir_monitor.py — Live rolling 30/60/90-day ICIR monitor for forward tests.

This module complements :mod:`quant.icir` with a stateful monitor class
intended to be called from the forward-test health output on cTrader (or
crypto) live trading. It maintains a rolling log of trade observations
and produces a status snapshot covering the last 30/60/90 calendar days.

Design
------
* Each ``update(confidence, r_multiple, timestamp)`` adds a single trade
  observation.
* On ``get_status()`` we:
    1. Bucket the buffered observations by ``bucket_granularity``
       (default: calendar day).
    2. Compute a Spearman IC per bucket that has at least
       :data:`quant.icir.MIN_OBS_FOR_IC` observations; otherwise the
       bucket contributes nothing.
    3. Filter buckets to a rolling window of the last ``N`` days
       (``N ∈ {30, 60, 90}``).
    4. Compute ICIR over the bucket-IC vector with
       :func:`quant.icir.icir`.
* ``decay_alert`` flips to ``True`` when the 30-day ICIR is below
  :data:`quant.icir.DECAY_ALERT_THRESHOLD` *and* there is enough evidence
  (the 30-day window has ≥ :data:`quant.icir.MIN_PERIODS_FOR_ICIR`
  non-NaN IC observations).

Why bucket by day
-----------------
The IC literature buckets by evaluation period (e.g. daily). With
multiple trades per day we get a non-trivial cross-section per bucket;
single-trade days drop out cleanly (NaN). The 30-day horizon therefore
yields up to 30 IC observations — enough for ``MIN_PERIODS_FOR_ICIR=3``
even with sparse trading days.

Per the research doc (§7.6) this is the *single highest-value* live ICIR
use — independently of PnL, it is the earliest signal of skill decay.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .icir import (
    DECAY_ALERT_THRESHOLD,
    MIN_PERIODS_FOR_ICIR,
    information_coefficient,
)
from .icir import (
    icir as compute_icir,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


#: Default evaluation windows (in days) for ``get_status()``.
DEFAULT_WINDOWS_DAYS: tuple[int, ...] = (30, 60, 90)

#: Default bucket granularity for computing per-period IC.
DEFAULT_BUCKET_GRANULARITY = "day"  # "day" or "week"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IcirMonitorStatus:
    """Status snapshot returned by :meth:`IcirMonitor.get_status`."""

    icir_30d: float | None
    icir_60d: float | None
    icir_90d: float | None
    n_obs_total: int
    n_obs_30d: int
    n_obs_60d: int
    n_obs_90d: int
    decay_alert: bool
    last_updated: str  # ISO 8601 timestamp of latest observation, or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "icir_30d": _nan_to_none(self.icir_30d),
            "icir_60d": _nan_to_none(self.icir_60d),
            "icir_90d": _nan_to_none(self.icir_90d),
            "n_obs_total": self.n_obs_total,
            "n_obs_30d": self.n_obs_30d,
            "n_obs_60d": self.n_obs_60d,
            "n_obs_90d": self.n_obs_90d,
            "decay_alert": self.decay_alert,
            "last_updated": self.last_updated,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nan_to_none(x: float | None) -> Any:
    if x is None:
        return None
    if isinstance(x, (int, float)) and math.isnan(x):
        return None
    return x


def _bucket_key(ts: datetime, granularity: str) -> datetime:
    """Reduce a timestamp to its bucket key (truncate to day or week)."""
    if granularity == "day":
        return datetime(ts.year, ts.month, ts.day)
    if granularity == "week":
        # ISO-week start (Monday); we use date() for the week start.
        d = ts.date()
        monday = d - timedelta(days=d.weekday())
        return datetime(monday.year, monday.month, monday.day)
    raise ValueError(f"unknown granularity: {granularity!r}")


# ---------------------------------------------------------------------------
# Monitor class
# ---------------------------------------------------------------------------


@dataclass
class IcirMonitor:
    """Stateful rolling-window ICIR monitor.

    Parameters
    ----------
    windows_days:
        Tuple of window sizes (in days) reported by :meth:`get_status`.
        Defaults to ``(30, 60, 90)``.
    bucket_granularity:
        ``"day"`` or ``"week"``. Controls how observations are grouped
        into evaluation periods for IC computation.
    decay_threshold:
        ICIR below this value triggers ``decay_alert`` (when the 30-day
        window has enough evidence). Defaults to
        :data:`quant.icir.DECAY_ALERT_THRESHOLD`.
    now_provider:
        Callable returning the current ``datetime``. Defaults to
        ``datetime.utcnow``; injected for tests.

    The monitor never throws on bad input — bad observations are skipped
    and ``None`` is reported for uncomputable metrics. Health checks on
    Ayumi live data frequently see partially-populated feeds.
    """

    windows_days: tuple[int, ...] = DEFAULT_WINDOWS_DAYS
    bucket_granularity: str = DEFAULT_BUCKET_GRANULARITY
    decay_threshold: float = DECAY_ALERT_THRESHOLD
    now_provider: Any = field(default=lambda: datetime.utcnow())

    # Internal storage. Public for inspection but not part of the API
    # contract; the order of ``_observations`` is append-only.
    _observations: list[tuple[datetime, float, float]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def update(
        self,
        confidence: float,
        r_multiple: float,
        timestamp: datetime,
    ) -> None:
        """Record one trade observation.

        Parameters
        ----------
        confidence:
            Signal confidence at time of decision, in ``[0, 1]``.
        r_multiple:
            Realized R-multiple of the trade (e.g. ``+1.5`` for a win at
            1.5×risk, ``-1.0`` for a full loss).
        timestamp:
            Observation timestamp. ``datetime`` required (not ``date``).
            Must be timezone-naive UTC or ``tzinfo``-aware — mixing the
            two within one monitor instance is undefined.

        Notes
        -----
        Observations with NaN / infinite confidence or R-multiple are
        silently skipped; a ``None`` timestamp or wrong type is also
        skipped. Duplicate timestamps are allowed (multiple trades per
        bar / per timestamp is the norm).
        """
        if not isinstance(timestamp, datetime):
            return
        try:
            conf_f = float(confidence)
            r_f = float(r_multiple)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(conf_f) and math.isfinite(r_f)):
            return
        self._observations.append((timestamp, conf_f, r_f))

    def clear(self) -> None:
        """Drop all buffered observations. Useful between sessions/tests."""
        self._observations.clear()

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def n_observations(self) -> int:
        """Total number of buffered observations."""
        return len(self._observations)

    def _bucket_observations(
        self,
    ) -> dict[datetime, tuple[list[float], list[float]]]:
        """Group observations into buckets keyed by their bucket timestamp.

        Returns
        -------
        dict[datetime, tuple[list[float], list[float]]]
            Mapping from bucket key → (confidences, r_multiples).
        """
        buckets: dict[datetime, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
        for ts, conf, r in self._observations:
            key = _bucket_key(ts, self.bucket_granularity)
            confs, rs = buckets[key]
            confs.append(conf)
            rs.append(r)
        return buckets

    def _icir_for_window(
        self,
        buckets: dict[datetime, tuple[list[float], list[float]]],
        window_days: int,
        reference: datetime,
    ) -> tuple[float | None, int, int]:
        """Compute ICIR + counts for one rolling window.

        Returns ``(icir, n_obs_in_window, n_buckets_with_ic)`` with NaN
        converted to ``None`` at the boundary.
        """
        cutoff = reference - timedelta(days=window_days)
        n_obs_window = 0
        bucket_ics: list[float] = []
        for key, (confs, rs) in buckets.items():
            if key < cutoff or key > reference:
                continue
            n_obs_window += len(confs)
            ic = information_coefficient(confs, rs)
            if not math.isnan(ic):
                bucket_ics.append(ic)

        if len(bucket_ics) < MIN_PERIODS_FOR_ICIR:
            # Not enough data; ICIR not computable yet.
            return (None, n_obs_window, len(bucket_ics))

        summary = compute_icir(bucket_ics)
        icir_v = summary.get("icir")
        if icir_v is None:
            return (None, n_obs_window, len(bucket_ics))
        return (float(icir_v), n_obs_window, len(bucket_ics))

    def get_status(
        self,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Compute a snapshot of rolling ICIR metrics.

        Parameters
        ----------
        now:
            Reference timestamp defining the rolling windows. Defaults to
            ``self.now_provider()``. Useful to test fixed-time behavior.

        Returns
        -------
        dict
            :meth:`IcirMonitorStatus.to_dict` shape. All ``icir_*`` fields
            are ``None`` when there are fewer than
            :data:`quant.icir.MIN_PERIODS_FOR_ICIR` valid IC observations
            in that window. ``decay_alert`` is ``True`` only when the
            30-day ICIR is computed *and* below
            ``self.decay_threshold``.
        """
        if now is None:
            now = self.now_provider()

        # Ensure 'now' is comparable to the bucket keys (which are naive
        # datetimes at midnight). If observations are tz-aware, drop the
        # tz for windowing — compare on the calendar wall clock. This is
        # a deliberate simplification; forward tests run on UTC.
        now_cmp = now.replace(tzinfo=None) if isinstance(now, datetime) else datetime.utcnow()

        buckets = self._bucket_observations()
        icir_by_window: dict[int, float | None] = {}
        n_obs_by_window: dict[int, int] = {}

        for window_days in self.windows_days:
            icir_v, n_obs, _n_buckets = self._icir_for_window(buckets, window_days, now_cmp)
            icir_by_window[window_days] = icir_v
            n_obs_by_window[window_days] = n_obs

        icir_30d = icir_by_window.get(30)
        icir_60d = icir_by_window.get(60)
        icir_90d = icir_by_window.get(90)

        decay_alert = bool(icir_30d is not None and icir_30d < self.decay_threshold)

        last_updated_iso = ""
        if self._observations:
            last_ts = max(ts for ts, _c, _r in self._observations)
            last_updated_iso = last_ts.isoformat()

        status = IcirMonitorStatus(
            icir_30d=icir_30d,
            icir_60d=icir_60d,
            icir_90d=icir_90d,
            n_obs_total=len(self._observations),
            n_obs_30d=n_obs_by_window.get(30, 0),
            n_obs_60d=n_obs_by_window.get(60, 0),
            n_obs_90d=n_obs_by_window.get(90, 0),
            decay_alert=decay_alert,
            last_updated=last_updated_iso,
        )
        return status.to_dict()


__all__ = [
    "DEFAULT_BUCKET_GRANULARITY",
    "DEFAULT_WINDOWS_DAYS",
    "IcirMonitor",
    "IcirMonitorStatus",
]
