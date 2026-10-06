"""Per-trade meta-labeling for the strategy factory (Sprint D 1).

Sprint D 1 — card 64c6598f-895d-4866-b771-2f3aa894d09d — layers Lopez de
Prado's meta-labeling (2018, *Advances in Financial ML*, Ch. 3) on top
of the Benjamini-Hochberg ranker that landed in Sprint C 1b.3 (card
cc90a6b6).  The meta-label answers the second-order question: *given
the primary model fired, is THIS trade likely to win?*.  The
calibrated probability flows into the ranker as a per-trial gate, so
strategies whose primary signals are untrustworthy on the meta-classifier's
test surface drop out of BH-FDR rather than winning on noise.

Design
------
1. **Per-trade features** are computed strictly from information that
   was on the trading line at signal time (``MetaTradeContext``).  The
   feature builder has no access to outcomes or to any post-signal
   bar — see :class:`MetaTradeContext` for the contract and the
   anti-lookahead test class in
   ``tests/factory/test_meta_labeling.py`` for the regression suite
   (mirrors ``tests/backtest/test_anti_lookahead.py``).
2. **Calibrated meta-classifier** is a logistic regression baseline
   wrapped in :class:`sklearn.calibration.CalibratedClassifierCV`.  Both
   Platt (sigmoid) and isotonic calibration are supported; the default
   is sigmoid because isotonic over-fits on the small per-strategy
   trade sets the factory feeds in (typically <500 trades).
3. **Ranking integration** is via :func:`rank_candidates_with_meta_gate`
   and :func:`rank_from_trial_return_store_with_meta_gate` — both
   re-exported from :mod:`forex_bot.factory.risk_adjusted_ranking`.
   They drop per-trial return rows whose calibrated meta-P(win) is at
   or below a threshold BEFORE the BH-FDR step-up.
4. **No duplicate collection**.  Per-trial return series come from
   :class:`forex_bot.factory.validation_runner.TrialReturnStore`
   (the existing Sprint C accumulator).  Per-trade features come
   from the orchestrator's trade-record stream — the module
   consumes the existing dataclasses and never re-implements the
   bookkeeping that lives in
   :mod:`forex_bot.factory.validation_runner`.

Dependencies
------------
Numpy, pandas, scikit-learn, scipy.  All are already pinned in
``requirements.txt`` — no new pip dependencies.

Public surface
--------------
* :class:`MetaTradeContext` — per-signal context (signal-time only).
* :class:`MetaLabeledTrade` — context + binary outcome (training example).
* :class:`CalibratedMetaClassifier` — calibrated meta-classifier wrapper.
* :func:`build_meta_features` — feature matrix builder.
* :func:`fit_meta_classifier` — fit a calibrated classifier.
* :func:`predict_meta_probability` — calibrated P(win) per row.
* :func:`consume_meta_confidence_per_trial` — bridge from
  ``TrialReturnStore`` per-cell records to per-trial meta-confidences.
* :func:`rank_candidates_with_meta_gate` — re-export from
  :mod:`forex_bot.factory.risk_adjusted_ranking`.
* :func:`rank_from_trial_return_store_with_meta_gate` — re-export.
* :exc:`MetaLabelingError` — base error.
* :exc:`MetaLabelingShapeError` — shape-mismatch error.
* :data:`META_FEATURE_NAMES` — fixed feature-name tuple.
* :data:`DEFAULT_META_GATE_THRESHOLD` — default calibrated-P(win) cut-off.
* :data:`MIN_META_TRADES` — minimum number of labelled trades to fit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping, Sequence

import numpy as np

from forex_bot.factory.risk_adjusted_ranking import (
    DEFAULT_FDR_ALPHA,
    DEFAULT_META_GATE_THRESHOLD,
    RankedCandidate,
    rank_candidates_with_meta_gate,
    rank_from_trial_return_store_with_meta_gate,
)

logger = logging.getLogger(__name__)


# ── Errors ────────────────────────────────────────────────────────────────


class MetaLabelingError(ValueError):
    """Base error for the meta-labeling module."""


class MetaLabelingShapeError(MetaLabelingError):
    """Raised when feature / context / outcome shapes do not align."""


# ── Constants ────────────────────────────────────────────────────────────


#: Fixed feature names produced by :func:`build_meta_features`, in column
#: order.  Tests pin this tuple — extending it requires updating
#: ``build_meta_features`` AND the anti-lookahead regression in
#: ``tests/factory/test_meta_labeling.py::TestAntiLookahead``.
#:
#: Feature rationale (Lopez de Prado 2018 §3.1):
#:
#: * ``primary_confidence`` — the primary model's raw confidence at
#:   signal time; the meta-classifier learns when over/under-confident
#:   primary signals should be ignored.
#: * ``primary_is_long`` / ``primary_is_short`` — one-hot on side, with
#:   "flat" being the all-zero row.
#: * ``regime_trending`` / ``regime_choppy`` / ``regime_volatile`` /
#:   ``regime_quiet`` — one-hot on the regime label at signal time
#:   (Sprint B regime detector).  "unknown" maps to all zeros.
#: * ``funding_rate_at_entry`` / ``funding_abs_at_entry`` — Sprint C
#:   1a.1 funding context.  The absolute-value companion lets the
#:   meta-classifier distinguish "carry favourable" (low |rate|) from
#:   "carry punishing" (high |rate|, either direction).
#: * ``venue_cost_bps_at_entry`` — Sprint C 1a.3 venue cost in basis
#:   points at signal time.
#: * ``spread_pips_at_entry`` — Sprint C 1b.2 spread-cost overlay at
#:   signal time.
#: * ``hour_sin`` / ``hour_cos`` / ``dow_sin`` / ``dow_cos`` — cyclic
#:   time-of-day / day-of-week encodings (one-hot time-of-day would
#:   blow up the feature dimension for a typical 200-trade meta-set).
META_FEATURE_NAMES: tuple[str, ...] = (
    "primary_confidence",
    "primary_is_long",
    "primary_is_short",
    "regime_trending",
    "regime_choppy",
    "regime_volatile",
    "regime_quiet",
    "funding_rate_at_entry",
    "funding_abs_at_entry",
    "venue_cost_bps_at_entry",
    "spread_pips_at_entry",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
)

#: Default per-trial meta-confidence threshold.  Trials whose calibrated
#: P(win) is at or below this are dropped by
#: :func:`rank_candidates_with_meta_gate` BEFORE the BH-FDR step-up.
#: ``0.5`` is the "primary signal is at least as likely correct as not"
#: gate from Lopez de Prado (2018, §3.5); tighten for higher precision,
#: loosen to ``0.0`` for an identity pass-through.
DEFAULT_META_GATE_THRESHOLD_LOCAL: float = DEFAULT_META_GATE_THRESHOLD

#: Minimum number of labelled trades to fit a meta-classifier.  Logistic
#: regression needs at least one sample per class plus enough for the
#: calibration CV folds (default 5 folds → at least 5 wins + 5 losses
#: for stratified calibration to engage); we round up to 30 to leave
#: room for the per-feature coefficient to stabilise.  Below this, fit
#: raises :class:`MetaLabelingShapeError`.
MIN_META_TRADES: int = 30

#: Numeric reg-time columns from ``(backtest/regime detector)``.
VALID_REGIMES: tuple[str, ...] = (
    "trending",
    "choppy",
    "volatile",
    "quiet",
    "unknown",
)

#: Numeric primary-model sides.
VALID_PRIMARY_SIDES: tuple[str, ...] = ("long", "short", "flat")


# ── Per-trade context (signal-time only) ─────────────────────────────────


@dataclass(frozen=True)
class MetaTradeContext:
    """Information available at signal time.  **NO outcome, NO future info.**

    This dataclass is the **anti-lookahead contract** for the
    meta-labeling module.  Adding a new field is fine provided the
    field is on the trading line at ``signal_time``; adding an
    outcome-derived field is a regression and the
    ``TestAntiLookahead`` regression suite will catch it.

    Attributes
    ----------
    candidate_id
        Stable candidate identifier (``strategy_id`` from the registry).
    pair
        Trading symbol (e.g. ``"EURUSD"``, ``"BTCUSDT"``).
    timeframe
        Bar timeframe (e.g. ``"H1"``, ``"D1"``).
    signal_time
        UTC datetime of the signal bar.  Naive datetimes are accepted
        but the feature builder does not localize — callers should
        pass tz-aware UTC values to match the rest of the factory.
    primary_confidence
        Primary model's confidence in ``[0, 1]`` at signal time.
    primary_side
        ``"long"``, ``"short"``, or ``"flat"``.
    regime
        Regime label at signal time (``"trending"``, ``"choppy"``,
        ``"volatile"``, ``"quiet"``, or ``"unknown"``).
    funding_rate_at_entry
        Signed funding rate at signal time (Sprint C 1a.1 overlay);
        positive means longs pay shorts.  Default ``0.0`` for FX
        (no funding) or for strategies without a funding overlay.
    venue_cost_bps_at_entry
        Round-trip venue cost in basis points at signal time (Sprint C
        1a.3 overlay).  Default ``0.0`` when no venue-cost overlay
        applies.
    spread_pips_at_entry
        Spread in pips at signal time (Sprint C 1b.2 spread-cost
        overlay).  Default ``0.0`` when no overlay applies.
    """

    candidate_id: str
    pair: str
    timeframe: str
    signal_time: datetime
    primary_confidence: float
    primary_side: Literal["long", "short", "flat"] = "long"
    regime: Literal["trending", "choppy", "volatile", "quiet", "unknown"] = (
        "unknown"
    )
    funding_rate_at_entry: float = 0.0
    venue_cost_bps_at_entry: float = 0.0
    spread_pips_at_entry: float = 0.0

    def __post_init__(self) -> None:
        if self.primary_side not in VALID_PRIMARY_SIDES:
            raise MetaLabelingShapeError(
                f"primary_side must be one of {VALID_PRIMARY_SIDES!r}; "
                f"got {self.primary_side!r}"
            )
        if self.regime not in VALID_REGIMES:
            raise MetaLabelingShapeError(
                f"regime must be one of {VALID_REGIMES!r}; "
                f"got {self.regime!r}"
            )
        if not (0.0 <= self.primary_confidence <= 1.0):
            raise MetaLabelingShapeError(
                f"primary_confidence must be in [0, 1]; "
                f"got {self.primary_confidence!r}"
            )


@dataclass(frozen=True)
class MetaLabeledTrade:
    """One meta-label training example: signal-time context + binary outcome.

    ``outcome`` is ``1`` for a winning trade (PnL > 0) and ``0`` for
    a losing trade (PnL ≤ 0).  Meta-labeling (Lopez de Prado 2018)
    does not weight by PnL — every trade contributes equally,
    regardless of magnitude, so the meta-classifier learns "did the
    primary signal predict the SIDE correctly?" rather than "did it
    predict the magnitude?".

    Tie-breaking rule (PnL == 0.0): classified as loss.  This matches
    the factory's ``cost_sensitivity`` convention where break-even
    trades do not earn edge credit.
    """

    context: MetaTradeContext
    outcome: int  # 1 = win, 0 = loss

    def __post_init__(self) -> None:
        if self.outcome not in (0, 1):
            raise MetaLabelingShapeError(
                f"outcome must be 0 (loss) or 1 (win); got {self.outcome!r}"
            )


# ── Feature builder ──────────────────────────────────────────────────────


def _hour_of_day(signal_time: datetime) -> float:
    """UTC hour-of-day, ``[0, 24)``.  Naive datetimes treated as UTC."""
    return float(signal_time.hour)


def _day_of_week(signal_time: datetime) -> float:
    """UTC day-of-week, ``[0, 7)`` with Monday=0."""
    return float(signal_time.weekday())


def build_meta_features(
    contexts: Sequence[MetaTradeContext],
    *,
    feature_names: Sequence[str] = META_FEATURE_NAMES,
) -> np.ndarray:
    """Build the per-trade feature matrix ``[N, F]`` from contexts.

    The output column order matches :data:`META_FEATURE_NAMES` by
    default (and is locked to whatever ``feature_names`` is supplied).
    All entries are finite (``0.0`` is used in place of NaN so sklearn's
    logistic regression does not have to drop rows).

    Anti-lookahead contract
    ------------------------
    This function reads **only** fields on
    :class:`MetaTradeContext` and writes a numeric matrix.  It does
    not read outcomes, post-signal bars, or any future data.  The
    ``TestAntiLookahead`` regression suite enforces this by mutating
    fields that should not affect features (e.g. ``outcome``,
    ``post_signal_pnl``) and asserting the output is byte-identical.
    """
    if not contexts:
        return np.zeros((0, len(feature_names)), dtype=float)

    n = len(contexts)
    out = np.zeros((n, len(feature_names)), dtype=float)

    name_to_col: dict[str, int] = {name: i for i, name in enumerate(feature_names)}

    for i, ctx in enumerate(contexts):
        for col_name, value in _context_to_features(ctx).items():
            if col_name in name_to_col:
                out[i, name_to_col[col_name]] = value

    return out


def _context_to_features(ctx: MetaTradeContext) -> dict[str, float]:
    """Map a single context to a dict of {feature_name: value}.

    Extracted from :func:`build_meta_features` so the column-name →
    value mapping is auditable in isolation.  Order in the returned
    dict does not matter; the caller re-projects onto the
    requested ``feature_names`` ordering.
    """
    if not (0.0 <= ctx.primary_confidence <= 1.0):
        # Trust ``__post_init__`` — defensive for direct callers that
        # bypass validation.
        primary_conf = float(np.clip(ctx.primary_confidence, 0.0, 1.0))
    else:
        primary_conf = float(ctx.primary_confidence)

    primary_is_long = 1.0 if ctx.primary_side == "long" else 0.0
    primary_is_short = 1.0 if ctx.primary_side == "short" else 0.0

    regime_trending = 1.0 if ctx.regime == "trending" else 0.0
    regime_choppy = 1.0 if ctx.regime == "choppy" else 0.0
    regime_volatile = 1.0 if ctx.regime == "volatile" else 0.0
    regime_quiet = 1.0 if ctx.regime == "quiet" else 0.0

    funding_rate = float(ctx.funding_rate_at_entry)
    funding_abs = float(abs(funding_rate))

    venue_cost_bps = float(ctx.venue_cost_bps_at_entry)
    spread_pips = float(ctx.spread_pips_at_entry)

    hour = _hour_of_day(ctx.signal_time)
    dow = _day_of_week(ctx.signal_time)

    # Cyclic time encodings (sin/cos).
    two_pi = 2.0 * np.pi
    hour_sin = float(np.sin(two_pi * hour / 24.0))
    hour_cos = float(np.cos(two_pi * hour / 24.0))
    dow_sin = float(np.sin(two_pi * dow / 7.0))
    dow_cos = float(np.cos(two_pi * dow / 7.0))

    return {
        "primary_confidence": primary_conf,
        "primary_is_long": primary_is_long,
        "primary_is_short": primary_is_short,
        "regime_trending": regime_trending,
        "regime_choppy": regime_choppy,
        "regime_volatile": regime_volatile,
        "regime_quiet": regime_quiet,
        "funding_rate_at_entry": funding_rate,
        "funding_abs_at_entry": funding_abs,
        "venue_cost_bps_at_entry": venue_cost_bps,
        "spread_pips_at_entry": spread_pips,
        "hour_sin": hour_sin,
        "hour_cos": hour_cos,
        "dow_sin": dow_sin,
        "dow_cos": dow_cos,
    }


def _outcomes_array(trades: Sequence[MetaLabeledTrade]) -> np.ndarray:
    """Stack outcomes into a 1-D array of {0, 1}."""
    return np.asarray([int(t.outcome) for t in trades], dtype=float)


# ── Calibrated meta-classifier ───────────────────────────────────────────


@dataclass
class CalibratedMetaClassifier:
    """Logistic-regression meta-classifier with calibration.

    Wraps :class:`sklearn.linear_model.LogisticRegression` in
    :class:`sklearn.calibration.CalibratedClassifierCV` so the
    :meth:`predict_proba` output is a valid probability in ``[0, 1]``
    that downstream consumers can interpret as P(win | features).

    Calibration method
    ------------------
    * ``"sigmoid"`` (Platt scaling) — default.  Parametric, stable on
      small samples, fits a logistic on top of the base estimator's
      decision function.  Recommended for ``n < 1000`` (the typical
      factory meta-set).
    * ``"isotonic"`` — non-parametric.  Higher variance on small
      samples; can over-fit when the win-rate history has fewer than ~500
      trades.  Use when ``n >= 500`` AND you can afford to lose the
      parametric guarantee.

    Attributes
    ----------
    calibration
        ``"sigmoid"`` or ``"isotonic"``.
    feature_names
        Feature-name tuple locked at fit time.  The ``fit`` method
        cross-checks this against the columns of the input matrix.
    cv
        Number of CV folds for calibration.  Default ``5`` mirrors
        sklearn's ``CalibratedClassifierCV(cv=5)`` default.
    base_estimator
        The fitted :class:`sklearn.linear_model.LogisticRegression`
        instance, or ``None`` before fit.  Exposed for tests that want
        to inspect coefficients directly.
    calibrated_classifier
        The fitted :class:`sklearn.calibration.CalibratedClassifierCV`
        instance, or ``None`` before fit.
    is_fitted
        ``True`` after :meth:`fit` succeeds.
    """

    calibration: Literal["sigmoid", "isotonic"] = "sigmoid"
    feature_names: tuple[str, ...] = META_FEATURE_NAMES
    cv: int = 5
    # The fitted estimator / calibrated wrapper; ``None`` until fit.
    base_estimator: Any = field(default=None, init=False)
    calibrated_classifier: Any = field(default=None, init=False)
    is_fitted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.calibration not in ("sigmoid", "isotonic"):
            raise MetaLabelingError(
                f"calibration must be 'sigmoid' or 'isotonic'; "
                f"got {self.calibration!r}"
            )
        if int(self.cv) < 2:
            raise MetaLabelingError(
                f"cv must be >= 2; got {self.cv!r}"
            )
        # Defer sklearn imports to fit-time so import-order issues
        # with sklearn's lazy-loaded estimator register do not surface
        # at module-load time.
        self._sklearn_modules: dict[str, Any] = {}

    def _ensure_sklearn(self) -> dict[str, Any]:
        """Lazy-load sklearn modules used by fit/predict."""
        if not self._sklearn_modules:
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.linear_model import LogisticRegression

            self._sklearn_modules = {
                "LogisticRegression": LogisticRegression,
                "CalibratedClassifierCV": CalibratedClassifierCV,
            }
        return self._sklearn_modules

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        *,
        class_weight: str | Mapping[int, float] | None = "balanced",
    ) -> "CalibratedMetaClassifier":
        """Fit the meta-classifier.  Returns ``self`` for chaining.

        Parameters
        ----------
        X
            Feature matrix ``[N, F]`` as produced by
            :func:`build_meta_features`.
        y
            Outcome array ``[N]`` of ``{0, 1}`` values.
        class_weight
            Forwarded to ``LogisticRegression`` — ``"balanced"``
            re-weights classes inversely to frequency so a 30%-win-rate
            meta-set still trains sensibly.  Pass ``None`` for
            unweighted (matches the literal win/loss ratio).
        """
        modules = self._ensure_sklearn()
        LogisticRegression = modules["LogisticRegression"]
        CalibratedClassifierCV = modules["CalibratedClassifierCV"]

        X_arr = np.asarray(X, dtype=float)
        y_arr = np.asarray(y, dtype=float)

        if X_arr.ndim != 2:
            raise MetaLabelingShapeError(
                f"X must be 2-D [N, F]; got ndim={X_arr.ndim}"
            )
        if y_arr.ndim != 1:
            raise MetaLabelingShapeError(
                f"y must be 1-D [N]; got ndim={y_arr.ndim}"
            )
        if X_arr.shape[0] != y_arr.shape[0]:
            raise MetaLabelingShapeError(
                f"X and y must have the same first dimension; "
                f"got X.shape={X_arr.shape} y.shape={y_arr.shape}"
            )
        if X_arr.shape[0] < MIN_META_TRADES:
            raise MetaLabelingShapeError(
                f"need at least {MIN_META_TRADES} labelled trades to "
                f"fit a meta-classifier; got {X_arr.shape[0]}"
            )
        if X_arr.shape[1] != len(self.feature_names):
            raise MetaLabelingShapeError(
                f"X has {X_arr.shape[1]} columns; expected "
                f"{len(self.feature_names)} ({list(self.feature_names)!r})"
            )
        if not np.all(np.isfinite(X_arr)):
            raise MetaLabelingShapeError(
                "X contains non-finite values (NaN/inf); "
                "build_meta_features should never produce these"
            )

        # Both classes required for stratified calibration.  Refuse to
        # fit on a single-class set rather than silently producing a
        # degenerate 0.5 / 0.5 predictor.
        unique_classes = np.unique(y_arr.astype(int))
        if unique_classes.size < 2:
            raise MetaLabelingShapeError(
                "need both win (1) and loss (0) outcomes to fit a "
                f"meta-classifier; got only class {unique_classes.tolist()}"
            )

        base = LogisticRegression(
            max_iter=1000,
            class_weight=class_weight,
            solver="lbfgs",
        )
        self.base_estimator = base
        self.calibrated_classifier = CalibratedClassifierCV(
            base,
            method=self.calibration,
            cv=int(self.cv),
        )
        # CalibratedClassifierCV.fit clones the base estimator ``cv``
        # times, so the ``self.base_estimator`` reference is a *copy*
        # used only for sanity-printing coefficients at test time.
        # The calibrated wrapper's fitted base estimators live inside
        # ``self.calibrated_classifier.calibrated_classifiers_``.
        self.calibrated_classifier.fit(X_arr, y_arr.astype(int))
        self.is_fitted = True
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Return calibrated P(win) and P(loss) for every row.

        Shape is ``[N, 2]``; column ``0`` is P(loss), column ``1`` is
        P(win).  Callers wanting the meta-label probability should
        slice ``[:, 1]``.
        """
        if not self.is_fitted or self.calibrated_classifier is None:
            raise MetaLabelingError(
                "CalibratedMetaClassifier must be fit before "
                "predict_proba; call .fit(X, y) first"
            )

        X_arr = np.asarray(X, dtype=float)
        if X_arr.ndim != 2:
            raise MetaLabelingShapeError(
                f"X must be 2-D [N, F]; got ndim={X_arr.ndim}"
            )
        if X_arr.shape[1] != len(self.feature_names):
            raise MetaLabelingShapeError(
                f"X has {X_arr.shape[1]} columns; expected "
                f"{len(self.feature_names)}"
            )

        proba = np.asarray(
            self.calibrated_classifier.predict_proba(X_arr), dtype=float
        )
        if proba.ndim != 2 or proba.shape[1] != 2:
            # Should never happen — sklearn guarantees 2 columns for
            # binary classification — but defend against future
            # sklearn versions that change the contract.
            raise MetaLabelingError(
                f"predict_proba returned unexpected array {proba.shape}"
            )
        # Force [0, 1] clamp: floating-point round-off can push values
        # a few ULPs outside the unit interval; downstream consumers
        # assume strict [0, 1].
        proba = np.clip(proba, 0.0, 1.0)
        proba = proba / proba.sum(axis=1, keepdims=True)
        return proba

    def predict(self, X: np.ndarray, *, threshold: float = 0.5) -> np.ndarray:
        """Return argmax-decision (0/1) per row.  Threshold unused for argmax."""
        proba = self.predict_proba(X)
        return (proba[:, 1] >= threshold).astype(int)


# ── Convenience fitters ──────────────────────────────────────────────────


def fit_meta_classifier(
    trades: Sequence[MetaLabeledTrade],
    *,
    calibration: Literal["sigmoid", "isotonic"] = "sigmoid",
    feature_names: Sequence[str] = META_FEATURE_NAMES,
    cv: int = 5,
    class_weight: str | Mapping[int, float] | None = "balanced",
) -> CalibratedMetaClassifier:
    """Fit a :class:`CalibratedMetaClassifier` from labelled trades.

    One-shot convenience over the
    ``build_meta_features`` → ``CalibratedMetaClassifier.fit`` flow.
    The feature matrix is built with :func:`build_meta_features` and
    the outcome array is the ``outcome`` field of each
    :class:`MetaLabeledTrade`.  Both must have the same length; the
    classifier refuses to fit below :data:`MIN_META_TRADES` or with
    fewer than 2 unique outcome classes.
    """
    if not trades:
        raise MetaLabelingShapeError(
            "fit_meta_classifier requires at least one labelled trade"
        )
    X = build_meta_features([t.context for t in trades], feature_names=feature_names)
    y = _outcomes_array(trades)
    clf = CalibratedMetaClassifier(
        calibration=calibration,
        feature_names=tuple(feature_names),
        cv=cv,
    )
    clf.fit(X, y, class_weight=class_weight)
    return clf


def predict_meta_probability(
    clf: CalibratedMetaClassifier,
    contexts: Sequence[MetaTradeContext],
) -> np.ndarray:
    """Return calibrated P(win) (1-D ``[N]``) for each context.

    Convenience over ``clf.predict_proba(X)[:, 1]``.  Empty input
    returns an empty 1-D array so callers don't have to special-case.
    """
    if not contexts:
        return np.zeros(0, dtype=float)
    X = build_meta_features(
        contexts, feature_names=clf.feature_names
    )
    return clf.predict_proba(X)[:, 1]


# ── Calibration validity ────────────────────────────────────────────────


def evaluate_meta_label_calibration(
    clf: CalibratedMetaClassifier,
    trades: Sequence[MetaLabeledTrade],
    *,
    n_bins: int = 10,
) -> dict[str, float]:
    """Compute Brier score + reliability/resolution on a held-out set.

    Returns a flat dict suitable for JSON persistence:

    * ``brier_score`` — overall Brier (lower is better).
    * ``reliability`` — within-bin mismatch between predicted and
      actual win rates.
    * ``resolution`` — sharp separation of wins from losses across bins.
    * ``uncertainty`` — base-rate floor (BS of a constant predictor).
    * ``base_win_rate`` — mean of the outcome array.
    * ``n_signals`` — number of trades in the input.

    The implementation delegates to ``forex_bot.quant.calibration``
    when available; otherwise computes the Brier score directly so the
    factory does not have a hard dep on the analytics subpackage.

    Tests pin the key set so the workboard contract is stable even if
    the implementation migrates between backends.
    """
    if not trades:
        raise MetaLabelingShapeError(
            "evaluate_meta_label_calibration requires at least one trade"
        )
    if not clf.is_fitted:
        raise MetaLabelingError(
            "evaluate_meta_label_calibration requires a fitted classifier"
        )

    contexts = [t.context for t in trades]
    y = _outcomes_array(trades)
    p_win = predict_meta_probability(clf, contexts)

    try:
        # Preferred path — uses the canonical Brier decomposition.
        # ``evaluate_calibration`` expects a ``WalkForwardResults``
        # first arg, so we route through the lower-level helpers
        # ``brier_score`` / ``brier_decomposition`` which have the
        # clean (confidences, outcomes) signature that matches our
        # input shape.
        from forex_bot.quant.calibration import (
            brier_decomposition,
            brier_score,
        )

        reliability, resolution, uncertainty = brier_decomposition(
            [float(p) for p in p_win], [int(v) for v in y], n_bins=n_bins
        )
        bs = brier_score([float(p) for p in p_win], [int(v) for v in y])
        return {
            "brier_score": float(bs),
            "reliability": float(reliability),
            "resolution": float(resolution),
            "uncertainty": float(uncertainty),
            "base_win_rate": float(np.mean(y)),
            "n_signals": int(y.size),
        }
    except ImportError:
        # Fallback: compute Brier directly + initialise (the three
        # decompositions require binning which lives in the analytics
        # subpackage).  Fallback is here so a missing analytics import
        # does not break the factory pipeline; the workboard contract
        # still pins the key set.
        brier = float(np.mean((p_win - y) ** 2))
        return {
            "brier_score": brier,
            "reliability": float("nan"),
            "resolution": float("nan"),
            "uncertainty": float("nan"),
            "base_win_rate": float(np.mean(y)),
            "n_signals": int(y.size),
        }


# ── TrialReturnStore bridge ──────────────────────────────────────────────


def consume_meta_confidence_per_trial(
    store: object,
    *,
    cell_key: tuple[str, str, str],
    clf: CalibratedMetaClassifier,
    contexts_per_trial: Sequence[Sequence[MetaTradeContext]],
) -> np.ndarray | None:
    """Build the ``[T, N]`` meta-confidence matrix for one cell.

    For each trial in the cell, parameter ``contexts_per_trial[j]``
    is the sequence of signal-time contexts that contributed to the
    ``j``-th column of ``store.matrix(cell_key)``.  The function
    returns ``np.ndarray`` of shape ``[T, N]`` where row ``i`` is the
    calibrated P(win) for the ``i``-th signal that contributed to
    each trial's return series.  This is what
    :func:`rank_from_trial_return_store_with_meta_gate` expects.

    No collection duplication
    ------------------------
    The trial-level return matrix comes from ``store.matrix(cell_key)``
    — the existing Sprint C accumulator.  The per-trial contexts come
    from the orchestrator's existing trade-record stream.  This
    function only computes the calibrated probability; it never
    re-derives per-trial returns or per-signal contexts.
    """
    matrix = store.matrix(cell_key)  # type: ignore[attr-defined]
    if matrix is None or matrix.ndim != 2 or matrix.shape[1] == 0:
        return None

    if len(contexts_per_trial) != matrix.shape[1]:
        raise MetaLabelingShapeError(
            f"contexts_per_trial length {len(contexts_per_trial)} "
            f"does not match trial-return matrix columns "
            f"{matrix.shape[1]} for cell {cell_key}"
        )

    n_trials = matrix.shape[1]
    # First, compute the calibrated probabilities for every (trial,
    # signal) pair.  Empty trial rows / missing signals are surfaced as
    # rows of NaN, which the ranker will treat as unpaired and refuse.
    out = np.full(matrix.shape, np.nan, dtype=float)
    for j in range(n_trials):
        contexts = contexts_per_trial[j]
        if not contexts:
            continue
        if len(contexts) > matrix.shape[0]:
            raise MetaLabelingShapeError(
                f"cell {cell_key}, trial {j}: contexts length "
                f"{len(contexts)} > trial-return length "
                f"{matrix.shape[0]}"
            )
        probs = predict_meta_probability(clf, contexts)
        out[: len(probs), j] = probs
    return out


__all__ = [
    # Errors
    "MetaLabelingError",
    "MetaLabelingShapeError",
    # Constants
    "META_FEATURE_NAMES",
    "DEFAULT_META_GATE_THRESHOLD_LOCAL",
    "MIN_META_TRADES",
    "VALID_PRIMARIES" if False else "VALID_PRIMARY_SIDES",
    "VALID_REGIMES",
    # Dataclasses
    "MetaTradeContext",
    "MetaLabeledTrade",
    "CalibratedMetaClassifier",
    # Functions
    "build_meta_features",
    "fit_meta_classifier",
    "predict_meta_probability",
    "evaluate_meta_label_calibration",
    "consume_meta_confidence_per_trial",
    # Re-exports from risk_adjusted_ranking (Sprint D 1 ranking integration)
    "DEFAULT_FDR_ALPHA",
    "DEFAULT_META_GATE_THRESHOLD",
    "RankedCandidate",
    "rank_candidates_with_meta_gate",
    "rank_from_trial_return_store_with_meta_gate",
]
