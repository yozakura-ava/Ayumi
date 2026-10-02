"""Alpha-decay pipeline — chains walk-forward output with PSI drift detection.

This module connects three existing Ayumi components:

1. **walk_forward_runner** — produces per-window performance metrics
   (win_rate, profit_factor, sharpe_ratio, etc.)
2. **btc_regime_overlay** — classifies the BTC macro regime for a
   given time window (trending_up, trending_down, high_vol, neutral)
3. **PSI drift detector** (this package) — detects distribution shift
   in feature values between baseline and current periods

The pipeline runs as a **scheduled check** (not real-time).  It produces
a :class:`DriftReport` that summarises whether the strategy is healthy,
should be monitored, or needs retraining.

Usage
-----
::

    from monitoring.psi_drift_detector import PSIDriftDetector
    from monitoring.alpha_decay_pipeline import AlphaDecayPipeline
    from quant.btc_regime_overlay import BtcRegimeOverlay

    # Build baseline from historical walk-forward results
    baseline = extract_features_from_windows(historical_wf.per_window)

    detector = PSIDriftDetector(threshold=0.2)
    for feat, values in baseline.items():
        detector.set_baseline(feat, values)

    pipeline = AlphaDecayPipeline(detector, BtcRegimeOverlay())

    # Run with current walk-forward results
    current = extract_features_from_windows(recent_wf.per_window)
    report = pipeline.run(baseline, current)

    if report.recommendation == "retrain":
        ...

Reference: SRB-AYU-001 (BQ-1154, BQ-1139)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence

from .psi_drift_detector import PSIAlert, PSIDriftDetector

logger = logging.getLogger(__name__)

# ── Feature extraction ─────────────────────────────────────────────────────


def extract_features_from_windows(
    windows: Sequence[Any],
) -> dict[str, list[float]]:
    """Extract feature distributions from walk-forward window metrics.

    Each window is expected to be a dataclass / namedtuple with the
    following numeric attributes (matching ``WindowMetrics`` from
    ``quant.walk_forward``):

    - ``win_rate``
    - ``profit_factor``
    - ``sharpe_ratio``
    - ``max_drawdown``
    - ``trade_count``
    - ``total_pnl``

    Returns
    -------
    dict mapping feature name → list of per-window values.
    """
    feature_keys = [
        "win_rate",
        "profit_factor",
        "sharpe_ratio",
        "max_drawdown",
        "trade_count",
        "total_pnl",
    ]

    result: dict[str, list[float]] = {k: [] for k in feature_keys}

    for w in windows:
        for key in feature_keys:
            val = getattr(w, key, None)
            if val is not None:
                try:
                    result[key].append(float(val))
                except (TypeError, ValueError):
                    pass  # skip non-numeric

    # Drop features that have no data
    return {k: v for k, v in result.items() if v}


# ── Protocol for BTC regime overlay (duck-typed) ────────────────────────────


class RegimeOverlay(Protocol):
    """Minimal interface for a BTC regime overlay."""

    def regime_for_window(self, start_ms: int, end_ms: int) -> str: ...


# ── Data structures ─────────────────────────────────────────────────────────


@dataclass
class DriftReport:
    """Output of a single alpha-decay pipeline run."""

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    psi_results: dict[str, PSIAlert] = field(default_factory=dict)
    btc_regime: str = "neutral"
    recommendation: str = "ok"  # "ok", "monitor", "retrain"
    window_count: int = 0
    baseline_window_count: int = 0
    alerts: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable one-line summary."""
        parts = [
            f"[{self.timestamp}]",
            f"recommendation={self.recommendation}",
            f"btc_regime={self.btc_regime}",
            f"windows={self.window_count}",
        ]
        if self.psi_results:
            psi_strs = [f"{f}={a.psi:.3f}" for f, a in self.psi_results.items()]
            parts.append("psi={" + ", ".join(psi_strs) + "}")
        if self.alerts:
            parts.append(f"alerts={len(self.alerts)}")
        return " ".join(parts)


# ── Pipeline ────────────────────────────────────────────────────────────────


class AlphaDecayPipeline:
    """Scheduled pipeline that checks for alpha decay via PSI drift.

    Parameters
    ----------
    psi_detector : a configured :class:`PSIDriftDetector` with baselines
        already set for the features to monitor.
    btc_overlay   : a BtcRegimeOverlay (or compatible) instance for
        macro-regime context.
    """

    def __init__(
        self,
        psi_detector: PSIDriftDetector,
        btc_overlay: RegimeOverlay,
    ) -> None:
        self.psi_detector = psi_detector
        self.btc_overlay = btc_overlay

    def run(
        self,
        baseline_features: dict[str, list[float]],
        current_features: dict[str, list[float]],
        window_start_ms: int | None = None,
        window_end_ms: int | None = None,
    ) -> DriftReport:
        """Execute the alpha-decay check.

        Parameters
        ----------
        baseline_features : feature dict from the training/reference period
            (used to set PSI baselines if not already set).
        current_features : feature dict from the current evaluation period.
        window_start_ms, window_end_ms : optional epoch-ms timestamps
            defining the current evaluation window, used to query the BTC
            regime overlay.  If omitted, regime defaults to ``"neutral"``.

        Returns
        -------
        DriftReport with PSI results, regime context, and recommendation.
        """
        report = DriftReport(
            baseline_window_count=len(next(iter(baseline_features.values()), [0])),
            window_count=len(next(iter(current_features.values()), [0])),
        )

        # ── Ensure baselines are set ──────────────────────────────────────
        for feature, values in baseline_features.items():
            if not self.psi_detector.has_baseline(feature):
                self.psi_detector.set_baseline(feature, values)

        # ── PSI drift check ──────────────────────────────────────────────
        psi_alerts: list[PSIAlert] = []
        for feature, values in current_features.items():
            if self.psi_detector.has_baseline(feature):
                try:
                    alert = self.psi_detector.check_drift(feature, values)
                    report.psi_results[feature] = alert
                    psi_alerts.append(alert)
                    if alert.is_alert:
                        report.alerts.append(f"PSI drift on '{feature}': {alert.psi:.4f} ({alert.severity})")
                except (ValueError, KeyError) as exc:
                    logger.warning(
                        "PSI check failed for '%s': %s",
                        feature,
                        exc,
                    )

        # ── BTC regime context ───────────────────────────────────────────
        if window_start_ms is not None and window_end_ms is not None:
            try:
                report.btc_regime = self.btc_overlay.regime_for_window(
                    window_start_ms,
                    window_end_ms,
                )
            except Exception as exc:
                logger.warning("BTC regime lookup failed: %s", exc)
                report.btc_regime = "neutral"

        # ── Recommendation logic ─────────────────────────────────────────
        major_count = sum(1 for a in psi_alerts if a.severity == "major")
        minor_count = sum(1 for a in psi_alerts if a.severity == "minor")

        if major_count > 0:
            report.recommendation = "retrain"
        elif minor_count >= 2:
            report.recommendation = "monitor"
        elif minor_count == 1:
            # Single minor drift + high-vol regime → monitor
            if report.btc_regime == "high_vol":
                report.recommendation = "monitor"
            else:
                report.recommendation = "ok"
        else:
            report.recommendation = "ok"

        logger.info(
            "Alpha-decay pipeline complete: %s",
            report.summary(),
        )

        return report
