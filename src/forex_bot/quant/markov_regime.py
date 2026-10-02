"""First-Order Markov Regime Filter (AYUAA-401, Phase 1).

Pure-numpy implementation of a first-order Markov chain over a discrete
state space. Used to model regime persistence / transition dynamics on
top of the existing volatility×trend regime classifier
(``quant.regime``). The Phase 0 validation established a 6-state space
with statistically significant transition structure (chi-square p ≈ 0)
across GBPUSD, EURUSD, and USDJPY (67,100 H1 observations).

Why a Markov chain and not an HMM?
    Phase 1 deliberately uses a *first-order* Markov chain with Laplace
    smoothing. The advantages over an HMM for this scope are:

    * Closed-form maximum-likelihood estimates from transition counts.
    * No latent state inference — we directly observe the regime, so an
      HMM would be over-parameterised for the question being asked.
    * Tiny memory footprint (6×6 matrix, no covariance, no hidden
      state sequence). Trivially online-updatable via ``observe``.
    * Deterministic for a given transition history, which keeps the
      filter reproducible across phases and easy to unit-test.

    HMM upgrades (state-dependent emission distributions, full Bayesian
    smoothing) are deferred to a later phase if the simple chain turns
    out to be insufficient.

Conventions
    * ``P[i, j] = Pr(X_{t+1} = j | X_t = i)``. Rows are stochastic.
    * The transition matrix is built from raw transition counts with
      add-1 (Laplace) smoothing so every state pair has a non-zero
      probability mass — required for cold-start robustness and for
      ``predict_next`` to never produce a zero row.
    * Multi-step prediction is ``e_i @ P**h`` via
      ``numpy.linalg.matrix_power``.
    * ``confidence`` reads the diagonal entry — i.e., the probability
      that the current regime persists one bar.
    * ``size_multiplier`` turns that persistence into a position-size
      factor. Cold-start returns ``1.0`` so the filter never *reduces*
      size before it has enough evidence.

Usage::

    from quant.markov_regime import MarkovRegimeFilter

    f = MarkovRegimeFilter(
        states=[
            "low_ranging", "low_trending",
            "normal_ranging", "normal_trending",
            "high_ranging", "high_trending",
        ],
        min_history=100,
    )
    f.train(observed_transitions)  # list[tuple[str, str]]
    f.observe("normal_ranging", "normal_ranging")
    dist = f.predict_next("normal_ranging", horizon=5)
    mult = f.size_multiplier("normal_ranging")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Sequence

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistence tiers (used by ``size_multiplier``).
# ---------------------------------------------------------------------------
# These thresholds are intentionally hard-coded: they are the documented
# contract between the regime filter and the position-sizing layer. Bumping
# any tier requires a separate evaluation phase; do not "tune" them in place.

_PERSISTENCE_TIER_FLOOR: Final[tuple[float, ...]] = (0.35, 0.50, 0.65, 0.80)
# Multiplier per tier, indexed by the *lower* bound of the tier.
#   <  0.35              -> 0.5
#   0.35 <= p < 0.50     -> 0.8
#   0.50 <= p < 0.65     -> 1.0
#   0.65 <= p < 0.80     -> 1.1
#   0.80 <= p            -> 1.2
_TIER_MULTIPLIERS: Final[tuple[float, ...]] = (0.5, 0.8, 1.0, 1.1, 1.2)

# Hard safety bounds on the multiplier. The tier mapping can never push
# size past these clamps regardless of the persistence reading.
_MULTIPLIER_FLOOR: Final[float] = 0.5
_MULTIPLIER_CEILING: Final[float] = 1.3

# Cold-start: until the filter has seen ``min_history`` observations we
# return neutral sizing so the filter never *reduces* exposure before
# it has enough evidence to act.
_COLD_START_MULTIPLIER: Final[float] = 1.0

# Phase 0 validation defaults. The Markov transition matrix was
# calibrated against regime labels produced with these exact
# parameters. Changing them without re-running Phase 0 means the
# transition probabilities no longer match the underlying state space.
# ``MarkovRegimeFilter.__init__`` can check these at construction time
# when the caller supplies the active regime config values.
_PHASE0_ATR_LOOKBACK: Final[int] = 50
_PHASE0_ADX_PERIOD: Final[int] = 14


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MarkovRegimeSummary:
    """Snapshot of the current filter state.

    Returned by :meth:`MarkovRegimeFilter.summary`. Useful for logging,
    debugging, and for downstream consumers that want a read-only view
    without taking a reference to the live numpy arrays.
    """

    states: tuple[str, ...]
    transition_matrix: tuple[tuple[float, ...], ...]
    total_observations: int
    is_ready: bool

    @property
    def n_states(self) -> int:
        return len(self.states)


# ---------------------------------------------------------------------------
# Filter implementation
# ---------------------------------------------------------------------------
class MarkovRegimeFilter:
    """Pure-numpy first-order Markov chain over a discrete state space.

    The filter is built from observed ``(from_state, to_state)`` pairs,
    with Laplace (add-1) smoothing applied uniformly to keep every cell
    strictly positive. This guarantees a valid probability matrix even
    when history is sparse — important for ``predict_next`` and
    ``confidence`` at cold start.

    Parameters
    ----------
    states:
        Ordered state labels. Order is preserved and used to index
        the underlying numpy arrays. Must be non-empty with no
        duplicates.
    min_history:
        Minimum number of observed transitions before the filter
        is considered "ready" (``is_ready()``). Until then
        :meth:`size_multiplier` returns the cold-start neutral value.
    """

    def __init__(
        self,
        states: Sequence[str],
        min_history: int = 100,
        *,
        regime_atr_lookback: int | None = None,
        regime_adx_period: int | None = None,
    ) -> None:
        """Initialise the filter.

        Parameters
        ----------
        states:
            Ordered state labels for the Markov chain.
        min_history:
            Minimum transitions before the filter is ready.
        regime_atr_lookback:
            If provided, checked against the Phase 0 default
            (``_PHASE0_ATR_LOOKBACK``). A mismatch logs a warning.
        regime_adx_period:
            If provided, checked against the Phase 0 default
            (``_PHASE0_ADX_PERIOD``). A mismatch logs a warning.
        """
        if not states:
            raise ValueError("states must be a non-empty list")
        if len(set(states)) != len(states):
            raise ValueError("states must be unique")
        if min_history < 0:
            raise ValueError("min_history must be non-negative")

        # Phase 0 config compatibility warning (init-time, non-fatal).
        if regime_atr_lookback is not None and regime_atr_lookback != _PHASE0_ATR_LOOKBACK:
            logger.warning(
                "MarkovRegimeFilter: regime.atr_lookback=%d differs from "
                "Phase 0 default (%d). Transition probabilities may be "
                "stale — re-run Phase 0 validation with the new parameters.",
                regime_atr_lookback,
                _PHASE0_ATR_LOOKBACK,
            )
        if regime_adx_period is not None and regime_adx_period != _PHASE0_ADX_PERIOD:
            logger.warning(
                "MarkovRegimeFilter: regime.adx_period=%d differs from "
                "Phase 0 default (%d). Transition probabilities may be "
                "stale — re-run Phase 0 validation with the new parameters.",
                regime_adx_period,
                _PHASE0_ADX_PERIOD,
            )

        self._states: tuple[str, ...] = tuple(states)
        self._state_to_idx: dict[str, int] = {s: i for i, s in enumerate(self._states)}
        self._n: int = len(self._states)
        self._min_history: int = min_history
        self._total_observations: int = 0
        # Laplace smoothing: every cell starts at 1.
        self._counts: np.ndarray = np.ones((self._n, self._n), dtype=np.float64)
        self._matrix: np.ndarray = self._counts / self._counts.sum(axis=1, keepdims=True)
        self._dirty: bool = False  # Lazy refresh flag

    # ------------------------------------------------------------------ #
    # Construction & incremental update                                  #
    # ------------------------------------------------------------------ #
    def train(self, transitions: list[tuple[str, str]]) -> None:
        """Build the transition matrix from a batch of observed transitions.

        Resets any prior counts (Laplace seed included), then applies
        the new transitions. Use :meth:`observe` for online updates
        that preserve prior history.
        """
        # Reset counts to the Laplace seed.
        self._counts = np.ones((self._n, self._n), dtype=np.float64)
        self._total_observations = 0
        for from_state, to_state in transitions:
            self._bump(from_state, to_state)
        self._refresh_matrix()
        self._dirty = False

    def reset(self) -> None:
        """Reset the filter to its initial Laplace-smoothed state.

        Call this when switching symbols or restarting a backtest
        to prevent cross-pair transition contamination.
        """
        self._counts = np.ones((self._n, self._n), dtype=np.float64)
        self._total_observations = 0
        self._matrix = self._counts / self._counts.sum(axis=1, keepdims=True)
        self._dirty = False
        logger.debug("MarkovRegimeFilter reset: all counts cleared")

    def observe(self, from_state: str, to_state: str) -> None:
        """Record a single new transition without resetting the matrix."""
        self._bump(from_state, to_state)
        # Lazy refresh: mark dirty, defer matrix recomputation to next query.
        self._dirty = True

    # ------------------------------------------------------------------ #
    # Inference                                                           #
    # ------------------------------------------------------------------ #
    def _ensure_fresh(self) -> None:
        """Recompute the probability matrix if pending observations exist."""
        if self._dirty:
            self._refresh_matrix()
            self._dirty = False

    def predict_next(
        self,
        current_state: str,
        horizon: int = 5,
    ) -> list[tuple[str, float]]:
        """Predict the state distribution ``horizon`` bars from now.

        Computes ``e_i @ P**horizon`` where ``e_i`` is the one-hot
        indicator for ``current_state``. Returns ``[(state, prob), ...]``
        sorted by probability in descending order. Probabilities sum
        to 1.0 within floating-point precision.

        ``horizon`` must be a non-negative integer. ``horizon == 0``
        returns the degenerate distribution that places all mass on
        the current state.
        """
        if horizon < 0:
            raise ValueError("horizon must be a non-negative integer")
        self._ensure_fresh()
        idx = self._require_state(current_state)

        if horizon == 0:
            dist = np.zeros(self._n, dtype=np.float64)
            dist[idx] = 1.0
        else:
            # matrix_power uses repeated squaring — numerically stable
            # for the small (≤10×10) matrices this filter handles.
            step = np.linalg.matrix_power(self._matrix, horizon)
            dist = step[idx]

        order = np.argsort(-dist, kind="stable")
        return [(self._states[i], float(dist[i])) for i in order]

    def confidence(self, current_state: str) -> float:
        """Probability that the current regime persists one more bar.

        This is the diagonal entry of the transition matrix for
        ``current_state`` — i.e., Pr(X_{t+1} = current_state | X_t =
        current_state). Higher means more persistence / mean-reversion
        in that regime.
        """
        self._ensure_fresh()
        idx = self._require_state(current_state)
        return float(self._matrix[idx, idx])

    # ------------------------------------------------------------------ #
    # Sizing                                                              #
    # ------------------------------------------------------------------ #
    def size_multiplier(self, current_state: str) -> float:
        """Position-size multiplier derived from regime persistence.

        Cold start (``is_ready() is False``) returns 1.0 so the filter
        does not reduce exposure before it has evidence. Once ready,
        the persistence reading is bucketed into five tiers and
        clamped to ``[_MULTIPLIER_FLOOR, _MULTIPLIER_CEILING]``.
        """
        if not self.is_ready():
            logger.debug("size_multiplier cold-start: returning %.1f", _COLD_START_MULTIPLIER)
            return _COLD_START_MULTIPLIER

        persistence = self.confidence(current_state)
        raw = self._tier_lookup(persistence)
        logger.debug(
            "size_multiplier: state=%s persistence=%.3f tier_multiplier=%.2f",
            current_state,
            persistence,
            raw,
        )
        # Final safety clamp. In practice the tier mapping stays well
        # inside [0.5, 1.3], but we don't want a future tier change
        # to be able to silently violate the documented bound.
        return float(max(_MULTIPLIER_FLOOR, min(_MULTIPLIER_CEILING, raw)))

    # ------------------------------------------------------------------ #
    # Readiness & introspection                                           #
    # ------------------------------------------------------------------ #
    def is_ready(self) -> bool:
        """``True`` once at least ``min_history`` transitions have been observed."""
        return self._total_observations >= self._min_history

    @property
    def total_observations(self) -> int:
        return self._total_observations

    @property
    def min_history(self) -> int:
        return self._min_history

    @property
    def n_states(self) -> int:
        return self._n

    @property
    def states(self) -> tuple[str, ...]:
        return self._states

    @property
    def transition_matrix(self) -> np.ndarray:
        """Copy of the current transition probability matrix.

        Returns a defensive copy so callers cannot mutate internal
        state. The copy is writable but modifications have no effect
        on the filter.
        """
        self._ensure_fresh()
        return self._matrix.copy()

    def summary(self) -> MarkovRegimeSummary:
        """Frozen snapshot of the filter state for logging / downstream use."""
        self._ensure_fresh()
        rows = tuple(tuple(float(self._matrix[i, j]) for j in range(self._n)) for i in range(self._n))
        return MarkovRegimeSummary(
            states=self._states,
            transition_matrix=rows,
            total_observations=self._total_observations,
            is_ready=self.is_ready(),
        )

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #
    def _require_state(self, state: str) -> int:
        try:
            return self._state_to_idx[state]
        except KeyError as exc:
            raise ValueError(f"unknown state {state!r}; expected one of {list(self._states)}") from exc

    def _bump(self, from_state: str, to_state: str) -> None:
        i = self._require_state(from_state)
        j = self._require_state(to_state)
        self._counts[i, j] += 1.0
        self._total_observations += 1
        self._dirty = True

    def _refresh_matrix(self) -> None:
        # Row-normalise. Each row is guaranteed non-zero because
        # Laplace smoothing initialised every cell to 1.
        row_sums = self._counts.sum(axis=1, keepdims=True)
        self._matrix = self._counts / row_sums

    def _tier_lookup(self, persistence: float) -> float:
        # Map persistence to the multiplier tier. The default is the
        # lowest multiplier; each threshold we cross upgrades it.
        #   p <  0.35             -> 0.5
        #   0.35 <= p < 0.50      -> 0.8
        #   0.50 <= p < 0.65      -> 1.0
        #   0.65 <= p < 0.80      -> 1.1
        #   0.80 <= p             -> 1.2
        mult: float = _TIER_MULTIPLIERS[0]
        for floor, next_mult in zip(_PERSISTENCE_TIER_FLOOR, _TIER_MULTIPLIERS[1:]):  # noqa: B905
            if persistence >= floor:
                mult = next_mult
        return mult


__all__ = [
    "MarkovRegimeFilter",
    "MarkovRegimeSummary",
]
