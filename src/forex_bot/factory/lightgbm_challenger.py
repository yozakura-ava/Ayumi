"""LightGBM challenger benchmarked head-to-head vs the meta-labeler (Sprint D 3).

Sprint D 3 — card ``75c2346b-5c2a-46e9-8207-b784766ca882`` — layers a
``lightgbm.LGBMClassifier`` challenger on top of the
:class:`~forex_bot.factory.meta_labeling.CalibratedMetaClassifier`
baseline from Sprint D 1 (card ``64c6598f``).  The challenger is
**judged through the same Benjamini-Hochberg path** — no special-cased
ranking, no bypass.  Its calibrated P(win) simply substitutes for the
logistic meta-P(win) in :func:`rank_candidates_with_meta_gate`.

Contract
--------
1. **Single feature path.**  The challenger consumes the existing
   :data:`~forex_bot.factory.meta_labeling.META_FEATURE_NAMES`
   contract via :func:`~forex_bot.factory.meta_labeling.build_meta_features`
   — never a second feature builder, never a parallel schema.  Adding a
   feature therefore happens once in ``meta_labeling.py`` and the
   challenger inherits it automatically.
2. **Lazy ``lightgbm`` import.**  ``lightgbm`` is **not** pinned in
   ``requirements.txt`` (per the spec: pinning requires Craig's
   approval).  The module is therefore import-safe on a host that
   has not installed LightGBM, and every code path that *needs* it
   raises :class:`LightGBMChallengerUnavailable` with a clear message
   pointing operators to the install command.  Tests that actually
   fit / predict are gated with ``pytest.mark.skipif(no_lightgbm)``.
3. **Device selection** is delegated to the Sprint D 2 (card
   ``886fe5ef``) helper :func:`scripts.offload.executor.lgbm_gpu_device_flag`
   driven by :func:`scripts.offload.executor.probe_gpu`.  ``cuda``
   when the node GPU is present, ``cpu`` otherwise, and **never
   crash on either**: missing ``lightgbm`` is reported separately
   from a missing GPU, and a ``cpu`` fallback is always valid.
4. **Ranking integration.**  The challenger's P(win) is fed into
   :func:`~forex_bot.factory.risk_adjusted_ranking.rank_candidates_with_meta_gate`
   as ``meta_confidence_per_candidate`` — identical call signature
   to the logistic meta-labeler path.  No new ranking function.
5. **Benchmark artifact.**  :func:`benchmark_lightgbm_vs_meta_labeler`
   emits a :class:`BenchmarkArtifact` dataclass with side-by-side
   Brier, ROC-AUC, log-loss on **identical stratified folds** with a
   deterministic seed.  Optional ``emit_artifact_path=`` writes the
   JSON next to the orchestrator's other data artifacts.

Anti-lookahead
--------------
:func:`fit_lightgbm_challenger` and
:func:`consume_lightgbm_confidence_per_trial` consume the same
:class:`~forex_bot.factory.meta_labeling.MetaTradeContext` /
:class:`~forex_bot.factory.meta_labeling.MetaLabeledTrade` types as
the meta-labeler.  The feature builder is the only signal-time
reader; the outcome is read once, only at fit time.  The same
anti-lookahead regression suite (``tests/factory/test_meta_labeling.py::TestAntiLookahead``)
covers both paths because the contract is shared.

Public surface
--------------
* :class:`LightGBMChallengerError` — base error.
* :class:`LightGBMChallengerUnavailable` — ``lightgbm`` not installed.
* :class:`LightGBMChallengerShapeError` — shape mismatch.
* :class:`LightGBMChallenger` — classifier wrapper.
* :class:`BenchmarkArtifact` — side-by-side metric record.
* :func:`build_lightgbm_device_flag` — ``probe_gpu`` + ``lgbm_gpu_device_flag``.
* :func:`resolve_lightgbm_device_flag` — same but accepts a
  pre-probed :class:`GpuProbeResult` for testability.
* :func:`fit_lightgbm_challenger` — convenience fitter.
* :func:`predict_lightgbm_probability` — convenience predictor.
* :func:`consume_lightgbm_confidence_per_trial` — bridge to
  :class:`~forex_bot.factory.validation_runner.TrialReturnStore`.
* :func:`benchmark_lightgbm_vs_meta_labeler` — head-to-head benchmark.
* :data:`DEFAULT_BENCHMARK_SEED` — deterministic seed for folds.
* :data:`DEFAULT_BENCHMARK_N_FOLDS` — default ``5`` for stratified K-fold.
* :data:`DEFAULT_LIGHTGBM_DEVICE_LIST` — ``("cuda", "cpu")``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence, cast

import numpy as np

from forex_bot.factory.meta_labeling import (
    META_FEATURE_NAMES,
    MIN_META_TRADES,
    CalibratedMetaClassifier,
    MetaLabeledTrade,
    MetaLabelingError,
    MetaLabelingShapeError,
    MetaTradeContext,
    build_meta_features,
)
from forex_bot.factory.risk_adjusted_ranking import (
    DEFAULT_FDR_ALPHA,
    DEFAULT_META_GATE_THRESHOLD,
    rank_candidates_with_meta_gate,
)

logger = logging.getLogger(__name__)


# ── Errors ───────────────────────────────────────────────────────────────


class LightGBMChallengerError(MetaLabelingError):
    """Base error for the LightGBM challenger module."""


class LightGBMChallengerUnavailable(LightGBMChallengerError):
    """Raised when ``lightgbm`` is required but not installed.

    The module ships lazy so the factory's other paths keep working on
    hosts that have not installed LightGBM.  This error surfaces the
    install command and the regulatory note that pinning
    ``lightgbm`` in ``requirements.txt`` requires Craig's explicit
    approval (per the card's spec).
    """


class LightGBMChallengerShapeError(
    LightGBMChallengerError, MetaLabelingShapeError
):
    """Shape-mismatch error specific to the LightGBM challenger."""


# ── Constants ────────────────────────────────────────────────────────────


#: Deterministic seed for the benchmark fold splits + LightGBM internal
#: RNG.  The same seed drives *both* the stratified K-fold splits and
#: the LightGBM ``random_state``, so the benchmark is byte-stable
#: across runs and across the two classifiers.
DEFAULT_BENCHMARK_SEED: int = 17

#: Default stratified K-fold count for the head-to-head benchmark.
#: ``5`` mirrors the calibration CV default in
#: :class:`~forex_bot.factory.meta_labeling.CalibratedMetaClassifier`
#: so the comparison is fair on small samples.
DEFAULT_BENCHMARK_N_FOLDS: int = 5

#: LightGBM device flag whitelist.  ``lightgbm`` 4.x accepts both
#: ``"cpu"`` and ``"cuda"``; older 3.x builds used ``"gpu"``.  We
#: normalise via :func:`scripts.offload.executor.lgbm_gpu_device_flag`
#: so the rest of the factory only sees the canonical pair.
VALID_DEVICE_FLAGS: tuple[str, ...] = ("cpu", "cuda")

#: Re-export for callers that need the canonical pair.
DEFAULT_LIGHTGBM_DEVICE_LIST: tuple[str, ...] = VALID_DEVICE_FLAGS


# ── Lazy import ──────────────────────────────────────────────────────────


def _ensure_lightgbm():
    """Lazy-import ``lightgbm`` and surface a clear error if missing.

    Pinning ``lightgbm`` in ``requirements.txt`` requires Craig's
    explicit approval (per the card's spec).  Until then, every
    runtime path that needs LightGBM routes through here so the
    factory never silently produces a degenerate classifier.
    """
    try:
        import lightgbm
    except ImportError as exc:
        raise LightGBMChallengerUnavailable(
            "lightgbm is not installed in this environment; install it "
            "via `pip install lightgbm` (or `pip install lightgbm --extra-index-url "
            "https://pypi.org/simple/`) to enable the LightGBM challenger "
            "benchmark.  Pinning `lightgbm` in requirements.txt requires "
            "Craig's explicit approval.  Underlying import error: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return lightgbm


# ── Device flag wiring ──────────────────────────────────────────────────


def resolve_lightgbm_device_flag(
    gpu: Any,
    *,
    default: str = "cpu",
) -> str:
    """Resolve the LightGBM device flag from a pre-probed :class:`GpuProbeResult`.

    Thin wrapper over
    :func:`scripts.offload.executor.lgbm_gpu_device_flag` for
    testability: callers can pass a synthetic ``GpuProbeResult`` to
    exercise both branches without invoking ``nvidia-smi``.

    Parameters
    ----------
    gpu
        A :class:`scripts.offload.executor.GpuProbeResult` (duck-typed:
        must expose ``.available`` as a bool).
    default
        Fallback device when ``gpu`` is ``None``.  Defaults to
        ``"cpu"`` so the challenger is safe on a no-GPU host.
    """
    if gpu is None:
        if default not in VALID_DEVICE_FLAGS:
            raise LightGBMChallengerError(
                f"default device {default!r} is not in "
                f"{VALID_DEVICE_FLAGS!r}"
            )
        return default
    # Lazy import: scripts.offload.executor lives outside the
    # ``forex_bot`` package and is only needed at device-flag
    # resolution time.
    from scripts.offload.executor import lgbm_gpu_device_flag

    flag = lgbm_gpu_device_flag(gpu)
    if flag not in VALID_DEVICE_FLAGS:
        raise LightGBMChallengerError(
            f"lgbm_gpu_device_flag returned unexpected value {flag!r}; "
            f"expected one of {VALID_DEVICE_FLAGS!r}"
        )
    return flag


def build_lightgbm_device_flag(
    *,
    binary: str | None = None,
    timeout_s: float | None = None,
    default: str = "cpu",
) -> str:
    """Probe the local GPU and return the canonical LightGBM device flag.

    Thin wrapper over
    :func:`scripts.offload.executor.probe_gpu` +
    :func:`resolve_lightgbm_device_flag`.  On hosts without
    ``nvidia-smi`` (or with one that returns no devices) this returns
    ``"cpu"``; the probe never raises, and the LightGBM challenger
    itself never crashes on either device.

    ``binary`` / ``timeout_s`` are forwarded to ``probe_gpu`` for
    operator overrides).  When ``None``, the executor's defaults are
    used (the canonical ``nvidia-smi`` lookup at 5 s timeout).
    """
    from scripts.offload.executor import probe_gpu

    kwargs: dict[str, Any] = {}
    if binary is not None:
        kwargs["binary"] = binary
    if timeout_s is not None:
        kwargs["timeout_s"] = float(timeout_s)
    gpu = probe_gpu(**kwargs) if kwargs else probe_gpu()
    return resolve_lightgbm_device_flag(gpu, default=default)


def _coerce_device_flag(device_flag: str) -> Literal["cpu", "cuda"]:
    """Validate a runtime-resolved device flag and cast to the Literal type.

    ``LightGBMChallenger.__init__`` re-validates the flag in
    ``__post_init__`` so a misuse raises :class:`LightGBMChallengerError`
    with a clear message.  This helper exists so mypy strict mode is
    satisfied at the construction site of the convenience functions
    (``fit_lightgbm_challenger``, ``benchmark_lightgbm_vs_meta_labeler``)
    where the flag arrives as a plain ``str`` from an external caller.
    """
    if device_flag not in VALID_DEVICE_FLAGS:
        raise LightGBMChallengerError(
            f"device_flag must be one of {VALID_DEVICE_FLAGS!r}; "
            f"got {device_flag!r}"
        )
    return cast(Literal["cpu", "cuda"], device_flag)


# ── Classifier wrapper ───────────────────────────────────────────────────


@dataclass
class LightGBMChallenger:
    """LightGBM challenger with the same ``fit`` / ``predict_proba`` contract as
    :class:`~forex_bot.factory.meta_labeling.CalibratedMetaClassifier`.

    The class deliberately mirrors the meta-labeler's surface so the
    factory can swap one for the other with a single import change —
    callers do **not** see a different ``predict_proba`` shape, error
    type, or feature-contract behaviour.

    Attributes
    ----------
    n_estimators, num_leaves, max_depth, min_child_samples,
    learning_rate, feature_fraction, bagging_fraction, bagging_freq
        LightGBM hyper-parameters.  Defaults are the small-sample
        factory-friendly choices recommended in the LightGBM 4.x
        guide (``n_estimators=100``, ``num_leaves=31``, deterministic
        single-thread fit) so the benchmark completes inside the
        factory's typical 30 s budget per candidate.
    random_seed
        Forwarded as LightGBM's ``random_state`` so the benchmark is
        reproducible.  Mirrors :data:`DEFAULT_BENCHMARK_SEED`.
    deterministic
        LightGBM's deterministic flag.  ``True`` by default so
        repeated ``.fit`` calls on the same data produce the same
        model — required for the head-to-head benchmark to be
        byte-stable across runs.
    n_jobs
        ``1`` (single-thread) by default; the factory runs a single
        challenger fit at a time and the BQES lane caps concurrency
        to 1 CPU per agent.
    device
        LightGBM device flag — ``"cuda"`` when a GPU is available,
        ``"cpu"`` otherwise.  Resolved by
        :func:`build_lightgbm_device_flag` / :func:`resolve_lightgbm_device_flag`.
    feature_names
        Feature-name tuple locked at fit time.  Defaults to
        :data:`~forex_bot.factory.meta_labeling.META_FEATURE_NAMES`
        — the challenger consumes the SAME contract as the logistic
        meta-labeler.
    model_, is_fitted
        Fitted-state sentinels.  ``model_`` is ``None`` until
        :meth:`fit` succeeds; ``is_fitted`` flips to ``True`` on
        success.  Mirrors the meta-labeler's ``base_estimator`` /
        ``is_fitted`` pair.
    """

    n_estimators: int = 100
    learning_rate: float = 0.05
    num_leaves: int = 31
    max_depth: int = -1
    min_child_samples: int = 10
    feature_fraction: float = 1.0
    bagging_fraction: float = 1.0
    bagging_freq: int = 0
    random_seed: int = DEFAULT_BENCHMARK_SEED
    deterministic: bool = True
    n_jobs: int = 1
    device: Literal["cpu", "cuda"] = "cpu"
    feature_names: tuple[str, ...] = META_FEATURE_NAMES
    # Fitted artifacts (set by ``.fit()``).
    model_: Any = field(default=None, init=False)
    is_fitted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if int(self.n_estimators) < 1:
            raise LightGBMChallengerError(
                f"n_estimators must be >= 1; got {self.n_estimators!r}"
            )
        if not (0.0 < float(self.learning_rate) <= 1.0):
            raise LightGBMChallengerError(
                f"learning_rate must be in (0, 1]; got {self.learning_rate!r}"
            )
        if int(self.num_leaves) < 2:
            raise LightGBMChallengerError(
                f"num_leaves must be >= 2; got {self.num_leaves!r}"
            )
        if int(self.min_child_samples) < 1:
            raise LightGBMChallengerError(
                f"min_child_samples must be >= 1; got {self.min_child_samples!r}"
            )
        if not (0.0 < float(self.feature_fraction) <= 1.0):
            raise LightGBMChallengerError(
                f"feature_fraction must be in (0, 1]; got {self.feature_fraction!r}"
            )
        if not (0.0 < float(self.bagging_fraction) <= 1.0):
            raise LightGBMChallengerError(
                f"bagging_fraction must be in (0, 1]; got {self.bagging_fraction!r}"
            )
        if int(self.bagging_freq) < 0:
            raise LightGBMChallengerError(
                f"bagging_freq must be >= 0; got {self.bagging_freq!r}"
            )
        if self.device not in VALID_DEVICE_FLAGS:
            raise LightGBMChallengerError(
                f"device must be one of {VALID_DEVICE_FLAGS!r}; "
                f"got {self.device!r}"
            )

    # ── fit / predict ──────────────────────────────────────────────────

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        *,
        sample_weight: np.ndarray | None = None,
    ) -> "LightGBMChallenger":
        """Fit the LightGBM challenger.  Returns ``self`` for chaining.

        Parameters
        ----------
        X
            Feature matrix ``[N, F]`` as produced by
            :func:`~forex_bot.factory.meta_labeling.build_meta_features`.
        y
            Outcome array ``[N]`` of ``{0, 1}`` values.
        sample_weight
            Optional per-row weights.  Forwarded to LightGBM's
            ``fit``.  ``None`` (default) gives every trade equal
            weight, matching the Lopez de Prado convention.
        """
        lightgbm = _ensure_lightgbm()
        X_arr = np.asarray(X, dtype=float)
        y_arr = np.asarray(y, dtype=float)

        if X_arr.ndim != 2:
            raise LightGBMChallengerShapeError(
                f"X must be 2-D [N, F]; got ndim={X_arr.ndim}"
            )
        if y_arr.ndim != 1:
            raise LightGBMChallengerShapeError(
                f"y must be 1-D [N]; got ndim={y_arr.ndim}"
            )
        if X_arr.shape[0] != y_arr.shape[0]:
            raise LightGBMChallengerShapeError(
                f"X and y must have the same first dimension; "
                f"got X.shape={X_arr.shape} y.shape={y_arr.shape}"
            )
        if X_arr.shape[0] < MIN_META_TRADES:
            raise LightGBMChallengerShapeError(
                f"need at least {MIN_META_TRADES} labelled trades to "
                f"fit a LightGBM challenger; got {X_arr.shape[0]}"
            )
        if X_arr.shape[1] != len(self.feature_names):
            raise LightGBMChallengerShapeError(
                f"X has {X_arr.shape[1]} columns; expected "
                f"{len(self.feature_names)} ({list(self.feature_names)!r})"
            )
        if not np.all(np.isfinite(X_arr)):
            raise LightGBMChallengerShapeError(
                "X contains non-finite values (NaN/inf); "
                "build_meta_features should never produce these"
            )
        unique_classes = np.unique(y_arr.astype(int))
        if unique_classes.size < 2:
            raise LightGBMChallengerShapeError(
                "need both win (1) and loss (0) outcomes to fit a "
                f"LightGBM challenger; got only class {unique_classes.tolist()}"
            )

        params: dict[str, Any] = {
            "n_estimators": int(self.n_estimators),
            "learning_rate": float(self.learning_rate),
            "num_leaves": int(self.num_leaves),
            "max_depth": int(self.max_depth),
            "min_child_samples": int(self.min_child_samples),
            "feature_fraction": float(self.feature_fraction),
            "bagging_fraction": float(self.bagging_fraction),
            "bagging_freq": int(self.bagging_freq),
            "random_state": int(self.random_seed),
            "deterministic": bool(self.deterministic),
            "n_jobs": int(self.n_jobs),
            "device": str(self.device),
            "verbose": -1,
        }
        model = lightgbm.LGBMClassifier(**params)
        fit_kwargs: dict[str, Any] = {}
        if sample_weight is not None:
            fit_kwargs["sample_weight"] = np.asarray(
                sample_weight, dtype=float
            )
        model.fit(X_arr, y_arr.astype(int), **fit_kwargs)
        self.model_ = model
        self.is_fitted = True
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Return P(loss), P(win) for every row — same shape as the meta-labeler.

        Column ``0`` is P(loss), column ``1`` is P(win).  Callers
        slice ``[:, 1]`` for the meta-label probability, matching the
        meta-labeler's surface.
        """
        if not self.is_fitted or self.model_ is None:
            raise LightGBMChallengerError(
                "LightGBMChallenger must be fit before predict_proba; "
                "call .fit(X, y) first"
            )

        X_arr = np.asarray(X, dtype=float)
        if X_arr.ndim != 2:
            raise LightGBMChallengerShapeError(
                f"X must be 2-D [N, F]; got ndim={X_arr.ndim}"
            )
        if X_arr.shape[1] != len(self.feature_names):
            raise LightGBMChallengerShapeError(
                f"X has {X_arr.shape[1]} columns; expected "
                f"{len(self.feature_names)}"
            )

        proba = np.asarray(self.model_.predict_proba(X_arr), dtype=float)
        if proba.ndim != 2 or proba.shape[1] != 2:
            raise LightGBMChallengerError(
                f"predict_proba returned unexpected array {proba.shape}"
            )
        # Same clamp + renormalise as the meta-labeler so the two
        # paths are interchangeable at the ranker call site.
        proba = np.clip(proba, 0.0, 1.0)
        proba = proba / proba.sum(axis=1, keepdims=True)
        return proba

    def predict(self, X: np.ndarray, *, threshold: float = 0.5) -> np.ndarray:
        """Return argmax-decision (0/1) per row.  Threshold unused for argmax."""
        proba = self.predict_proba(X)
        return (proba[:, 1] >= threshold).astype(int)


# ── Convenience fitters / predictors ─────────────────────────────────────


def fit_lightgbm_challenger(
    trades: Sequence[MetaLabeledTrade],
    *,
    device_flag: str = "cpu",
    random_seed: int = DEFAULT_BENCHMARK_SEED,
    **kwargs: Any,
) -> LightGBMChallenger:
    """Fit a :class:`LightGBMChallenger` from labelled trades.

    Convenience over ``build_meta_features`` → ``LightGBMChallenger.fit``.
    Mirrors :func:`~forex_bot.factory.meta_labeling.fit_meta_classifier`
    so callers can swap implementations with one import-line change.

    Parameters
    ----------
    trades
        Sequence of :class:`~forex_bot.factory.meta_labeling.MetaLabeledTrade`.
    device_flag
        LightGBM device flag.  Defaults to ``"cpu"``; use
        :func:`build_lightgbm_device_flag` to resolve from a live GPU
        probe.  Forwarded verbatim to
        :class:`LightGBMChallenger.__init__`.
    random_seed
        Deterministic LightGBM seed.  Defaults to
        :data:`DEFAULT_BENCHMARK_SEED`.
    kwargs
        Forwarded to :class:`LightGBMChallenger.__init__` for
        hyper-parameter overrides (e.g. ``n_estimators``,
        ``num_leaves``).  Useful for the head-to-head benchmark.
    """
    if not trades:
        raise LightGBMChallengerShapeError(
            "fit_lightgbm_challenger requires at least one labelled trade"
        )
    X = build_meta_features([t.context for t in trades])
    y = np.asarray([int(t.outcome) for t in trades], dtype=float)
    # Drop feature_names from kwargs if supplied — the class locks it
    # to the meta-labeling contract so the two paths stay in lockstep.
    kwargs.pop("feature_names", None)
    kwargs.pop("device", None)
    clf = LightGBMChallenger(
        device=_coerce_device_flag(device_flag),
        random_seed=random_seed,
        **kwargs,
    )
    clf.fit(X, y)
    return clf


def predict_lightgbm_probability(
    clf: LightGBMChallenger,
    contexts: Sequence[MetaTradeContext],
) -> np.ndarray:
    """Return P(win) (1-D ``[N]``) for each context.

    Mirrors :func:`~forex_bot.factory.meta_labeling.predict_meta_probability`
    so the ranker call site is identical for the two paths.
    """
    if not contexts:
        return np.zeros(0, dtype=float)
    X = build_meta_features(contexts, feature_names=clf.feature_names)
    return clf.predict_proba(X)[:, 1]


# ── TrialReturnStore bridge ──────────────────────────────────────────────


def consume_lightgbm_confidence_per_trial(
    store: object,
    *,
    cell_key: tuple[str, str, str],
    clf: LightGBMChallenger,
    contexts_per_trial: Sequence[Sequence[MetaTradeContext]],
) -> np.ndarray | None:
    """Build the ``[T, N]`` P(win) matrix for one cell — LightGBM challenger.

    Mirrors
    :func:`~forex_bot.factory.meta_labeling.consume_meta_confidence_per_trial`
    so the two bridges are interchangeable at the
    :func:`~forex_bot.factory.risk_adjusted_ranking.rank_from_trial_return_store_with_meta_gate`
    call site.
    """
    matrix = store.matrix(cell_key)  # type: ignore[attr-defined]
    if matrix is None or matrix.ndim != 2 or matrix.shape[1] == 0:
        return None

    if len(contexts_per_trial) != matrix.shape[1]:
        raise LightGBMChallengerShapeError(
            f"contexts_per_trial length {len(contexts_per_trial)} "
            f"does not match trial-return matrix columns "
            f"{matrix.shape[1]} for cell {cell_key}"
        )

    out = np.full(matrix.shape, np.nan, dtype=float)
    for j in range(matrix.shape[1]):
        contexts = contexts_per_trial[j]
        if not contexts:
            continue
        if len(contexts) > matrix.shape[0]:
            raise LightGBMChallengerShapeError(
                f"cell {cell_key}, trial {j}: contexts length "
                f"{len(contexts)} > trial-return length "
                f"{matrix.shape[0]}"
            )
        probs = predict_lightgbm_probability(clf, contexts)
        out[: len(probs), j] = probs
    return out


# ── Head-to-head benchmark ───────────────────────────────────────────────


@dataclass
class BenchmarkFold:
    """Per-fold metric snapshot.

    Captures the metric pair for one stratified fold so an operator
    can audit fold-level variability (Brier swings across folds are a
    known LightGBM property; the artifact records them rather than
    hiding them).
    """

    fold_index: int
    n_train: int
    n_test: int
    challenger_brier: float
    meta_brier: float
    challenger_roc_auc: float
    meta_roc_auc: float
    challenger_log_loss: float
    meta_log_loss: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class BenchmarkArtifact:
    """Side-by-side head-to-head benchmark of the LightGBM challenger vs
    the logistic meta-labeler on identical stratified folds.

    The artifact is the workboard contract for Sprint D 3 — it is
    emitted as a JSON data artifact so downstream consumers (Rin
    review, the spec implementation table, the eventual R5 sweep
    runner) can verify the challenger was benchmarked under the same
    conditions as the meta-labeler.

    Attributes
    ----------
    challenger_id
        Identifier string for the challenger (``"lightgbm"``).
    meta_id
        Identifier string for the baseline (``"logistic_meta"``).
    n_signals
        Total labelled trades.
    n_wins
        Number of winning trades (outcome == 1).
    base_win_rate
        Mean of the outcome array.
    challenger_brier / meta_brier
        Aggregated Brier score across folds (mean of per-fold Brier).
    challenger_roc_auc / meta_roc_auc
        Aggregated ROC-AUC across folds.
    challenger_log_loss / meta_log_loss
        Aggregated log-loss across folds.
    delta_brier / delta_roc_auc
        Challenger minus meta (negative delta == challenger better).
    n_folds
        Stratified K-fold count.
    device_flag
        LightGBM device flag at fit time (``"cuda"`` or ``"cpu"``).
    random_seed
        The seed driving both the fold splits and the LightGBM RNG.
    fold_metrics
        Per-fold breakdown (:class:`BenchmarkFold` list).
    timestamp_utc
        ISO-8601 UTC timestamp when the benchmark ran.
    feature_names
        Tuple of feature names — locked to
        :data:`~forex_bot.factory.meta_labeling.META_FEATURE_NAMES`.
    """

    challenger_id: str = "lightgbm"
    meta_id: str = "logistic_meta"
    n_signals: int = 0
    n_wins: int = 0
    base_win_rate: float = 0.0
    challenger_brier: float = 0.0
    meta_brier: float = 0.0
    challenger_roc_auc: float = 0.0
    meta_roc_auc: float = 0.0
    challenger_log_loss: float = 0.0
    meta_log_loss: float = 0.0
    delta_brier: float = 0.0
    delta_roc_auc: float = 0.0
    n_folds: int = DEFAULT_BENCHMARK_N_FOLDS
    device_flag: str = "cpu"
    random_seed: int = DEFAULT_BENCHMARK_SEED
    fold_metrics: tuple[BenchmarkFold, ...] = field(default_factory=tuple)
    timestamp_utc: str = ""
    feature_names: tuple[str, ...] = META_FEATURE_NAMES

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        # Tuples → lists for JSON.
        d["feature_names"] = list(self.feature_names)
        d["fold_metrics"] = [f.to_dict() for f in self.fold_metrics]
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def write(self, path: str | Path) -> None:
        """Write the JSON artifact to ``path``.  Atomic via tmp + rename."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(path)


# ── Metric helpers ───────────────────────────────────────────────────────


def _brier_score(confidences: np.ndarray, outcomes: np.ndarray) -> float:
    """Direct Brier score — matches the meta-labeler fallback path."""
    p = np.asarray(confidences, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    return float(np.mean((p - y) ** 2))


def _roc_auc_score(confidences: np.ndarray, outcomes: np.ndarray) -> float:
    """ROC-AUC via sklearn.  Returns ``0.5`` for single-class folds.

    The ``0.5`` floor is the random-chance baseline so the benchmark
    artifact never records ``NaN`` for degenerate folds; callers can
    inspect ``fold_metrics`` for the fold-level variability.
    """
    from sklearn.metrics import roc_auc_score

    p = np.asarray(confidences, dtype=float)
    y = np.asarray(outcomes, dtype=int)
    if np.unique(y).size < 2:
        return 0.5
    return float(roc_auc_score(y, p))


def _log_loss(confidences: np.ndarray, outcomes: np.ndarray) -> float:
    """Log-loss via sklearn.  Clips confidences into ``(eps, 1 - eps)``
    so degenerate folds do not produce ``inf`` / ``-inf``.
    """
    from sklearn.metrics import log_loss

    p = np.asarray(confidences, dtype=float)
    y = np.asarray(outcomes, dtype=int)
    eps = 1e-15
    p_clipped = np.clip(p, eps, 1.0 - eps)
    return float(log_loss(y, p_clipped))


# ── Benchmark entry point ────────────────────────────────────────────────


def benchmark_lightgbm_vs_meta_labeler(
    trades: Sequence[MetaLabeledTrade],
    *,
    n_folds: int = DEFAULT_BENCHMARK_N_FOLDS,
    device_flag: str = "cpu",
    random_seed: int = DEFAULT_BENCHMARK_SEED,
    meta_classifier: CalibratedMetaClassifier | None = None,
    emit_artifact_path: str | Path | None = None,
    timestamp_utc: str | None = None,
) -> BenchmarkArtifact:
    """Head-to-head benchmark on identical stratified folds.

    The same :class:`sklearn.model_selection.StratifiedKFold` splits
    drive *both* classifiers, so the comparison is fold-by-fold
    identical (same train rows, same test rows).  A fixed
    :data:`DEFAULT_BENCHMARK_SEED` makes the splits and the LightGBM
    RNG reproducible across runs.

    Parameters
    ----------
    trades
        Sequence of labelled trades.  Must have at least
        :data:`~forex_bot.factory.meta_labeling.MIN_META_TRADES` rows
        AND enough rows for ``n_folds`` splits (both classes must be
        present in every fold).
    n_folds
        Stratified K-fold count.  Default :data:`DEFAULT_BENCHMARK_N_FOLDS`.
    device_flag
        LightGBM device flag for the challenger.  Resolved externally
        via :func:`build_lightgbm_device_flag`; the benchmark itself
        does not re-probe the GPU.
    random_seed
        Drives the fold splits + LightGBM RNG.  The meta-labeler
        shares the same fold splits but its internal RNG is left at
        sklearn's defaults (the calibration CV already has its own
        deterministic path).
    meta_classifier
        Optional pre-built :class:`CalibratedMetaClassifier` template.
        Defaults to ``CalibratedMetaClassifier(calibration="sigmoid",
        feature_names=META_FEATURE_NAMES, cv=5)`` so the calibration
        CV is itself deterministic.  Tests inject a custom
        configuration to exercise the calibration-method branch.
    emit_artifact_path
        Optional path for the JSON data artifact.  When ``None`` the
        artifact is returned but not persisted.
    timestamp_utc
        ISO-8601 UTC string recorded in the artifact.  When ``None``
        the function uses :func:`datetime.now(timezone.utc).isoformat`.

    Returns
    -------
    BenchmarkArtifact
        Side-by-side metric record.  Emitted to JSON when
        ``emit_artifact_path`` is supplied.
    """
    if int(n_folds) < 2:
        raise LightGBMChallengerError(
            f"n_folds must be >= 2; got {n_folds!r}"
        )
    if not trades:
        raise LightGBMChallengerShapeError(
            "benchmark_lightgbm_vs_meta_labeler requires at least one labelled trade"
        )

    contexts = [t.context for t in trades]
    X = build_meta_features(contexts)
    y = np.asarray([int(t.outcome) for t in trades], dtype=int)

    if X.shape[0] < MIN_META_TRADES:
        raise LightGBMChallengerShapeError(
            f"need at least {MIN_META_TRADES} labelled trades; "
            f"got {X.shape[0]}"
        )
    if np.unique(y).size < 2:
        raise LightGBMChallengerShapeError(
            "benchmark requires both win and loss outcomes"
        )

    # Lazy imports: only required at benchmark time, not at module
    # import.  The fold splitter and metrics live in sklearn which is
    # already pinned in requirements.txt.
    from sklearn.model_selection import StratifiedKFold

    skf = StratifiedKFold(
        n_splits=int(n_folds),
        shuffle=True,
        random_state=int(random_seed),
    )

    # The meta-classifier template is held constant across folds so the
    # benchmark is a fair comparison; calibration is a property of the
    # method, not the data.
    meta_template = meta_classifier or CalibratedMetaClassifier(
        calibration="sigmoid",
        feature_names=META_FEATURE_NAMES,
        cv=5,
    )

    fold_metrics: list[BenchmarkFold] = []
    for fold_index, (train_idx, test_idx) in enumerate(skf.split(X, y)):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        # Both classes must be present in the train fold for
        # LightGBM / logistic to fit; degenerate folds are skipped
        # rather than aborting the whole benchmark.
        if np.unique(y_train).size < 2 or np.unique(y_test).size < 2:
            continue

        # Challenger: fit a fresh LightGBM with the canonical seed.
        challenger = LightGBMChallenger(
            device=_coerce_device_flag(device_flag),
            random_seed=int(random_seed),
            feature_names=META_FEATURE_NAMES,
        )
        challenger.fit(X_train, y_train)
        challenger_p = challenger.predict_proba(X_test)[:, 1]

        # Meta-labeler: fit a fresh CalibratedMetaClassifier per fold.
        # ``meta_template`` is the *config* template; we re-instantiate
        # so each fold gets its own fitted classifier.
        meta_fold = CalibratedMetaClassifier(
            calibration=meta_template.calibration,
            feature_names=tuple(meta_template.feature_names),
            cv=int(meta_template.cv),
        )
        meta_fold.fit(X_train, y_train)
        meta_p = meta_fold.predict_proba(X_test)[:, 1]

        fold_metrics.append(
            BenchmarkFold(
                fold_index=int(fold_index),
                n_train=int(X_train.shape[0]),
                n_test=int(X_test.shape[0]),
                challenger_brier=_brier_score(challenger_p, y_test),
                meta_brier=_brier_score(meta_p, y_test),
                challenger_roc_auc=_roc_auc_score(challenger_p, y_test),
                meta_roc_auc=_roc_auc_score(meta_p, y_test),
                challenger_log_loss=_log_loss(challenger_p, y_test),
                meta_log_loss=_log_loss(meta_p, y_test),
            )
        )

    if not fold_metrics:
        raise LightGBMChallengerShapeError(
            "benchmark produced no usable folds — every fold was "
            "single-class; refine the synthetic set or lower n_folds"
        )

    def _mean(field_name: str) -> float:
        return float(np.mean([getattr(f, field_name) for f in fold_metrics]))

    challenger_brier = _mean("challenger_brier")
    meta_brier = _mean("meta_brier")
    challenger_roc_auc = _mean("challenger_roc_auc")
    meta_roc_auc = _mean("meta_roc_auc")
    challenger_log_loss = _mean("challenger_log_loss")
    meta_log_loss = _mean("meta_log_loss")

    artifact = BenchmarkArtifact(
        challenger_id="lightgbm",
        meta_id="logistic_meta",
        n_signals=int(X.shape[0]),
        n_wins=int(np.sum(y == 1)),
        base_win_rate=float(np.mean(y)),
        challenger_brier=challenger_brier,
        meta_brier=meta_brier,
        challenger_roc_auc=challenger_roc_auc,
        meta_roc_auc=meta_roc_auc,
        challenger_log_loss=challenger_log_loss,
        meta_log_loss=meta_log_loss,
        # Negative delta == challenger better than meta (Brier: lower is
        # better; ROC-AUC: higher is better — so the sign convention is
        # consistent: negative == challenger preferred).
        delta_brier=challenger_brier - meta_brier,
        delta_roc_auc=challenger_roc_auc - meta_roc_auc,
        n_folds=int(n_folds),
        device_flag=str(device_flag),
        random_seed=int(random_seed),
        fold_metrics=tuple(fold_metrics),
        timestamp_utc=timestamp_utc
        or datetime.now(timezone.utc).isoformat(),
        feature_names=META_FEATURE_NAMES,
    )

    if emit_artifact_path is not None:
        artifact.write(emit_artifact_path)
        logger.info(
            "benchmark_lightgbm_vs_meta_labeler wrote artifact to %s "
            "(n_signals=%d, n_folds=%d, device=%s)",
            emit_artifact_path,
            artifact.n_signals,
            artifact.n_folds,
            artifact.device_flag,
        )

    return artifact


# ── Ranking integration helper ──────────────────────────────────────────


def rank_with_lightgbm_challenger(
    trial_returns_per_candidate: Sequence[tuple[str, np.ndarray]],
    clf: LightGBMChallenger,
    contexts_per_candidate: Sequence[Sequence[MetaTradeContext]],
    *,
    meta_threshold: float = DEFAULT_META_GATE_THRESHOLD,
    cell_key: tuple[str, str, str] | None = None,
    alpha: float = DEFAULT_FDR_ALPHA,
) -> list:
    """Rank candidates after gating trials on the challenger's P(win).

    Pure pass-through over
    :func:`~forex_bot.factory.risk_adjusted_ranking.rank_candidates_with_meta_gate`:
    the challenger's calibrated P(win) substitutes for the logistic
    meta-P(win) via ``predict_lightgbm_probability``.  The signature
    is otherwise byte-identical so callers can swap the meta-labeler
    path for the challenger with a single import-line change.

    No new ranking function, no special-casing, no bypass — the
    BH-FDR step-up runs over the challenger's gate exactly the same
    way it runs over the logistic meta-labeler's gate.
    """
    if len(contexts_per_candidate) != len(trial_returns_per_candidate):
        raise LightGBMChallengerShapeError(
            "contexts_per_candidate must align 1-to-1 with "
            "trial_returns_per_candidate; got "
            f"{len(contexts_per_candidate)} contexts for "
            f"{len(trial_returns_per_candidate)} candidates"
        )

    meta_confidences: list[np.ndarray | None] = []
    for cid, _ in trial_returns_per_candidate:
        # zip() above guarantees alignment; index-find for symmetry.
        idx = next(
            i
            for i, (c, _) in enumerate(trial_returns_per_candidate)
            if c == cid
        )
        contexts = contexts_per_candidate[idx]
        if not contexts:
            meta_confidences.append(None)
        else:
            meta_confidences.append(
                predict_lightgbm_probability(clf, contexts)
            )

    return rank_candidates_with_meta_gate(
        trial_returns_per_candidate,
        meta_confidences,
        meta_threshold=meta_threshold,
        cell_key=cell_key,
        alpha=alpha,
    )


__all__ = [
    # Errors
    "LightGBMChallengerError",
    "LightGBMChallengerUnavailable",
    "LightGBMChallengerShapeError",
    # Constants
    "DEFAULT_BENCHMARK_SEED",
    "DEFAULT_BENCHMARK_N_FOLDS",
    "VALID_DEVICE_FLAGS",
    "DEFAULT_LIGHTGBM_DEVICE_LIST",
    # Device helpers
    "resolve_lightgbm_device_flag",
    "build_lightgbm_device_flag",
    # Classifier wrapper
    "LightGBMChallenger",
    # Convenience
    "fit_lightgbm_challenger",
    "predict_lightgbm_probability",
    # Bridge
    "consume_lightgbm_confidence_per_trial",
    # Benchmark
    "BenchmarkFold",
    "BenchmarkArtifact",
    "benchmark_lightgbm_vs_meta_labeler",
    # Ranking integration
    "rank_with_lightgbm_challenger",
]
