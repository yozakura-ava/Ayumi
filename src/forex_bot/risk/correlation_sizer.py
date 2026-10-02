"""Correlation-aware position sizer for multi-strategy portfolios.

When several strategies take positions in the same direction on
correlated instruments, the *effective* portfolio risk is higher than
the sum of individual risks suggests because the positions will tend to
win or lose together.

:class:`CorrelationAwareSizer` tracks open positions across strategies,
queries a :class:`~risk.correlation_matrix.CorrelationMatrix`, and
adjusts the size of a *new* trade so that aggregate correlated exposure
stays within a configured limit.

Key limits (defaults reflect BQ-1237 acceptance criteria):
    - Per-trade risk cap:      0.5 %  (``per_trade_risk_pct``)
    - Aggregate correlated cap: 1.0 %  (``aggregate_risk_pct``)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Union

from risk.correlation_matrix import CorrelationMatrix

logger = logging.getLogger(__name__)


# Type alias for regime — accepts Regime enum or string label
RegimeLike = Union[str, Enum]


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class OpenPosition:
    """A currently-open position registered with the sizer."""

    strategy_id: str
    pair: str
    direction: Direction
    size_lots: float
    risk_pct: float  # fractional risk of this single position (e.g. 0.005 = 0.5 %)


@dataclass
class SizingResult:
    """Returned by :meth:`CorrelationAwareSizer.compute_adjusted_size`."""

    adjusted_size_lots: float
    adjusted_risk_pct: float
    original_size_lots: float
    original_risk_pct: float
    scale_factor: float
    correlated_exposure_pct: float  # sum of effective risk from correlated open positions
    blocked: bool = False
    block_reason: str = ""
    warnings: list[str] = field(default_factory=list)


class CorrelationAwareSizer:
    """Position sizer that understands cross-strategy correlation.

    Parameters
    ----------
    correlation_matrix : CorrelationMatrix
        Pre-populated matrix used for correlation lookups.  Can be updated
        externally; the sizer re-reads on every ``compute_adjusted_size``
        call.
    per_trade_risk_pct : float
        Maximum risk allowed on a single trade (default ``0.005`` = 0.5 %).
    aggregate_risk_pct : float
        Maximum *correlated* aggregate risk allowed (default ``0.01`` = 1.0 %).
    """

    def __init__(
        self,
        correlation_matrix: CorrelationMatrix,
        per_trade_risk_pct: float = 0.005,
        aggregate_risk_pct: float = 0.01,
    ) -> None:
        self.cm = correlation_matrix
        self.per_trade_risk_pct = per_trade_risk_pct
        self.aggregate_risk_pct = aggregate_risk_pct
        # strategy_id+pair -> OpenPosition  (unique per strategy-pair)
        self._positions: dict[tuple[str, str], OpenPosition] = {}

    # ------------------------------------------------------------------ #
    # Position registry
    # ------------------------------------------------------------------ #
    def register_position(
        self,
        strategy_id: str,
        pair: str,
        direction: Direction | str,
        size_lots: float,
        risk_pct: float = 0.005,
    ) -> None:
        """Register (or overwrite) an open position."""
        if isinstance(direction, str):
            direction = Direction(direction.lower())
        key = (strategy_id, pair)
        self._positions[key] = OpenPosition(
            strategy_id=strategy_id,
            pair=pair,
            direction=direction,
            size_lots=size_lots,
            risk_pct=risk_pct,
        )
        logger.debug(
            "Registered position: %s %s %s %.2f lots risk=%.3f%%",
            strategy_id,
            pair,
            direction.value,
            size_lots,
            risk_pct * 100,
        )

    def remove_position(self, strategy_id: str, pair: str) -> bool:
        """Remove an open position.  Returns ``True`` if a position was removed."""
        key = (strategy_id, pair)
        removed = self._positions.pop(key, None)
        if removed:
            logger.debug("Removed position: %s %s", strategy_id, pair)
        return removed is not None

    @property
    def open_positions(self) -> list[OpenPosition]:
        """Snapshot of all open positions."""
        return list(self._positions.values())

    # ------------------------------------------------------------------ #
    # Sizing logic
    # ------------------------------------------------------------------ #
    def _correlated_exposure(
        self,
        new_pair: str,
        new_direction: Direction,
    ) -> float:
        """Sum the *effective* risk from open positions correlated with *new_pair*.

        A position in the **same** direction contributes
        ``risk * corr``  (corr in [0, 1]).

        A position in the **opposite** direction *reduces* exposure
        (hedge benefit): contributes ``-risk * corr``.
        """
        total = 0.0
        for pos in self._positions.values():
            corr = self.cm.get_correlation(pos.pair, new_pair)
            # Only consider non-trivial correlations
            if abs(corr) < 0.01:
                continue
            if pos.direction == new_direction:
                total += pos.risk_pct * abs(corr)
            else:
                # Opposite direction → hedge
                total -= pos.risk_pct * abs(corr)
        return max(total, 0.0)  # exposure can't be negative

    def compute_adjusted_size(
        self,
        pair: str,
        direction: Direction | str,
        base_size_lots: float,
        base_risk_pct: float = 0.005,
        regime: RegimeLike | None = None,
    ) -> SizingResult:
        """Compute the size for a *new* trade given current portfolio state.

        Parameters
        ----------
        pair : str
            Instrument symbol (must match correlation matrix symbols).
        direction : Direction | str
            ``"long"`` or ``"short"``.
        base_size_lots : float
            The size the strategy *wants* to trade before correlation
            adjustment.
        base_risk_pct : float
            The risk of the base trade (default 0.5 %).
        regime : RegimeLike | None
            Current market regime (e.g. ``"STABLE"``, ``"BREAKDOWN"``,
            ``"TRANSITION"``).  When ``"BREAKDOWN"``, the aggregate risk
            cap is reduced by 50 % per BQ-1240b.  ``None`` preserves
            pre-existing behaviour (no regime adjustment).
        """
        if isinstance(direction, str):
            direction = Direction(direction.lower())

        warnings: list[str] = []

        # --- Regime-aware aggregate cap adjustment (BQ-1240b) ---------- #
        effective_aggregate_cap = self.aggregate_risk_pct
        if regime is not None:
            regime_label = regime.value if isinstance(regime, Enum) else str(regime).upper()
            if regime_label == "BREAKDOWN":
                effective_aggregate_cap = self.aggregate_risk_pct * 0.5
                warnings.append(
                    f"BREAKDOWN regime: aggregate risk cap reduced to "
                    f"{effective_aggregate_cap:.3%} (50 % exposure cut)."
                )

        # --- Enforce per-trade risk cap -------------------------------- #
        if base_risk_pct > self.per_trade_risk_pct:
            warnings.append(
                f"Base risk {base_risk_pct:.3%} exceeds per-trade cap {self.per_trade_risk_pct:.3%}; capping."
            )
            cap_scale = self.per_trade_risk_pct / base_risk_pct if base_risk_pct > 0 else 0.0
            base_risk_pct = self.per_trade_risk_pct
            base_size_lots *= cap_scale

        # --- Compute correlated exposure ------------------------------- #
        correlated_exposure = self._correlated_exposure(pair, direction)

        # Remaining risk budget (uses regime-adjusted cap)
        remaining_budget = effective_aggregate_cap - correlated_exposure

        if remaining_budget <= 0:
            # Fully correlated budget exhausted
            return SizingResult(
                adjusted_size_lots=0.0,
                adjusted_risk_pct=0.0,
                original_size_lots=base_size_lots,
                original_risk_pct=base_risk_pct,
                scale_factor=0.0,
                correlated_exposure_pct=correlated_exposure,
                blocked=True,
                block_reason=(
                    f"Correlated exposure {correlated_exposure:.3%} has reached "
                    f"effective aggregate cap {effective_aggregate_cap:.3%}"
                    + (" (regime-adjusted)" if effective_aggregate_cap != self.aggregate_risk_pct else "")
                ),
                warnings=warnings,
            )

        # Scale the new trade so its risk does not exceed remaining budget
        max_risk_for_new = min(base_risk_pct, remaining_budget)
        scale = max_risk_for_new / base_risk_pct if base_risk_pct > 0 else 0.0
        adjusted_size = base_size_lots * scale
        adjusted_risk = base_risk_pct * scale

        if scale < 1.0:
            warnings.append(
                f"Size reduced to {scale:.1%} of requested due to correlated exposure {correlated_exposure:.3%}."
            )

        return SizingResult(
            adjusted_size_lots=round(adjusted_size, 4),
            adjusted_risk_pct=adjusted_risk,
            original_size_lots=base_size_lots,
            original_risk_pct=base_risk_pct,
            scale_factor=round(scale, 4),
            correlated_exposure_pct=correlated_exposure,
            warnings=warnings,
        )
