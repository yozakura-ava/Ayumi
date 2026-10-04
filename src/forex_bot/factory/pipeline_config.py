"""Validation pipeline configuration (§4 of the Strategy-Factory spec).

This module codifies the *constants* the rest of the pipeline reads from —
walk-forward windows, DSR ``n_trials`` scaling, regime gating thresholds,
minimum trade counts, OOS holdout, and the PBO acceptance bar.

Rules
-----
1. Every number here MUST be a dataclass field (no inline magic numbers).
2. The defaults match spec §4 verbatim.  Overriding them is a council-level
   decision and MUST be done by constructing a new ``PipelineConfig`` — not
   by patching this module.
3. The OOS holdout covers Jan–Jul 2026 — the spec §4.5 "last 6 months" rule.
4. Construction is purely declarative (no I/O, no Optuna, no market data) so
   the config can be unit-tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping


# ---------------------------------------------------------------------------
# §4.1 — Walk-forward windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkForwardWindowConfig:
    """One time-frame's walk-forward parameters.

    Mirrors the table in spec §4.1.  ``train_ratio + val_ratio + test_ratio``
    must sum to 1.0 (±0.001 for float rounding); embargo is in **bars** of
    the timeframe (e.g. M15 → 96 bars = 24h).
    """

    timeframe: str
    windows: int
    train_ratio: float
    val_ratio: float
    test_ratio: float
    overlap_ratio: float
    embargo_bars: int

    def __post_init__(self) -> None:
        if not self.timeframe:
            raise ValueError("WalkForwardWindowConfig.timeframe must be non-empty")
        if self.windows <= 0:
            raise ValueError(f"windows must be positive (got {self.windows})")
        total = self.train_ratio + self.val_ratio + self.test_ratio
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"train+val+test ratios must sum to 1.0 (got {total:.4f}) for "
                f"tf={self.timeframe!r}"
            )
        if self.overlap_ratio < 0 or self.overlap_ratio >= 1:
            raise ValueError(
                f"overlap_ratio must be in [0,1) (got {self.overlap_ratio})"
            )
        if self.embargo_bars < 0:
            raise ValueError(f"embargo_bars must be >= 0 (got {self.embargo_bars})")


def _canonical_wf_windows() -> tuple[WalkForwardWindowConfig, ...]:
    """Spec §4.1 verbatim."""
    return (
        WalkForwardWindowConfig(
            timeframe="M5",
            windows=8,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            overlap_ratio=0.20,
            embargo_bars=288,
        ),
        WalkForwardWindowConfig(
            timeframe="M15",
            windows=5,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            overlap_ratio=0.20,
            embargo_bars=96,
        ),
        WalkForwardWindowConfig(
            timeframe="H1",
            windows=5,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            overlap_ratio=0.20,
            embargo_bars=24,
        ),
        WalkForwardWindowConfig(
            timeframe="H4",
            windows=5,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            overlap_ratio=0.20,
            embargo_bars=6,
        ),
        WalkForwardWindowConfig(
            timeframe="D1",
            windows=5,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            overlap_ratio=0.00,
            embargo_bars=1,
        ),
    )


# ---------------------------------------------------------------------------
# §4.2 — DSR n_trials scaling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DSRConfig:
    """DSR configuration per spec §4.2.

    ``base_n_trials`` is the proven-conservative floor (160 = 10 strat × 4
    pairs × 4 TF); ``n_trials_multiple`` is the multiplier on the actual
    cell count.  Both are exposed so future sweeps can tighten or loosen
    the multiple-testing penalty without touching this module.
    """

    base_n_trials: int = 160
    n_trials_multiple: int = 3


def compute_dsr_n_trials(cell_count: int, cfg: DSRConfig | None = None) -> int:
    """Spec §4.2 ``max(base, 3 * cell_count)``.

    ``cell_count`` is the total number of (strategy × pair × TF × Optuna
    trial) cells in the sweep.  ``None`` cfg uses the defaults
    (:class:`DSRConfig`).
    """
    if cell_count < 0:
        raise ValueError(f"cell_count must be >= 0 (got {cell_count})")
    cfg = cfg or DSRConfig()
    return max(cfg.base_n_trials, cfg.n_trials_multiple * cell_count)


# ---------------------------------------------------------------------------
# §4.3 — Regime gating
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeGatingConfig:
    """Spec §4.3 regime-gating thresholds.

    ``min_trades_per_regime`` is the Liora "insufficient data" floor (10
    trades).  ``regime_detector_window_bars`` MUST be 100 (Bug #4 fix from
    the Jul 22 revalidation).
    """

    regime_detector_window_bars: int = 100
    min_trades_per_regime: int = 10
    freeze_detector_for_pipeline: bool = True  # Kaito ground rule


# ---------------------------------------------------------------------------
# §4.4 — Minimum trade counts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeCountConfig:
    """Spec §4.4 trade-count thresholds.

    * ``per_window_warning`` — kept (warning), not blocking (§4.4 "kept but flagged").
    * ``dsr_eligibility_floor`` — blocking; cells below this are
      DSR-ineligible and auto-rejected (§4.4 "DSR-ineligible → REJECT").
    """

    per_window_warning: int = 15
    dsr_eligibility_floor: int = 30


# ---------------------------------------------------------------------------
# §4.5 — OOS isolation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OOSConfig:
    """Out-of-sample holdout per spec §4.5 (Kaito ground rule).

    Default window covers Jan 2026 → Jul 2026 ("last 6 months held out").
    :attr:`require_explicit_unlock` enforces that *any* code path that wants
    to use OOS bars must set the flag explicitly — otherwise the factory
    raises :class:`PermissionError` before touching the bars.
    """

    holdout_start: date = field(default_factory=lambda: date(2026, 1, 1))
    holdout_end: date = field(default_factory=lambda: date(2026, 7, 31))
    require_explicit_unlock: bool = True

    def __post_init__(self) -> None:
        if self.holdout_start >= self.holdout_end:
            raise ValueError(
                f"OOS holdout start ({self.holdout_start}) must be before "
                f"end ({self.holdout_end})"
            )

    def contains(self, day: date) -> bool:
        """True iff ``day`` falls within the OOS window (inclusive)."""
        return self.holdout_start <= day <= self.holdout_end

    def assert_unlocked(self, unlocked: bool, *, caller: str = "pipeline") -> None:
        """Raise :class:`PermissionError` if the OOS unlock flag is missing.

        Use as a guard around any code path that wants to read OOS bars::

            oos_cfg.assert_unlocked(unlocked=my_unlock_flag)
        """
        if self.require_explicit_unlock and not unlocked:
            raise PermissionError(
                f"OOS window {self.holdout_start}..{self.holdout_end} is "
                f"locked for {caller!r}; pass explicit unlock flag to access "
                "(spec §4.5 / Kaito ground rule)."
            )


# ---------------------------------------------------------------------------
# §4.6 — PBO threshold
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PBOConfig:
    """Spec §4.6 PBO acceptance bar.

    Cells with PBO >= :attr:`reject_threshold` are quarantined (no promotion).
    Cells with PBO between :attr:`marginal_threshold` and
    :attr:`reject_threshold` can only be promoted to Tier C.
    """

    accept_threshold: float = 0.30
    marginal_threshold: float = 0.50
    reject_threshold: float = 0.50

    def __post_init__(self) -> None:
        if not 0.0 <= self.accept_threshold < self.marginal_threshold <= self.reject_threshold:
            raise ValueError(
                "PBO thresholds must satisfy 0 <= accept < marginal <= reject"
            )

    def tier_for(self, pbo: float | None) -> str:
        """Map a PBO score to a tier ceiling.

        Returns one of ``"A"`` / ``"B"`` / ``"C"`` / ``"REJECT"`` /
        ``"INSUFFICIENT"`` (when ``pbo`` is ``None``).
        """
        if pbo is None:
            return "INSUFFICIENT"
        if pbo < self.accept_threshold:
            return "A"
        if pbo < self.marginal_threshold:
            return "B"
        if pbo < self.reject_threshold:
            return "C"
        return "REJECT"


# ---------------------------------------------------------------------------
# Composite config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineConfig:
    """Aggregate validation-pipeline configuration (§4 in one struct)."""

    wf_windows: tuple[WalkForwardWindowConfig, ...]
    dsr: DSRConfig
    regime: RegimeGatingConfig
    trade_count: TradeCountConfig
    oos: OOSConfig
    pbo: PBOConfig

    def wf_for(self, timeframe: str) -> WalkForwardWindowConfig:
        """Look up WF config by timeframe; raise :class:`KeyError` if missing."""
        for entry in self.wf_windows:
            if entry.timeframe == timeframe:
                return entry
        raise KeyError(
            f"No walk-forward config for timeframe {timeframe!r} "
            f"(configured: {[e.timeframe for e in self.wf_windows]!r})"
        )


def default_pipeline_config() -> PipelineConfig:
    """Return the spec-§4-canonical pipeline configuration."""
    return PipelineConfig(
        wf_windows=_canonical_wf_windows(),
        dsr=DSRConfig(),
        regime=RegimeGatingConfig(),
        trade_count=TradeCountConfig(),
        oos=OOSConfig(),
        pbo=PBOConfig(),
    )


__all__ = [
    "DSRConfig",
    "OOSConfig",
    "PBOConfig",
    "PipelineConfig",
    "RegimeGatingConfig",
    "TradeCountConfig",
    "WalkForwardWindowConfig",
    "compute_dsr_n_trials",
    "default_pipeline_config",
]