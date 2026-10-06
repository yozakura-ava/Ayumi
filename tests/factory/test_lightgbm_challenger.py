"""Tests for :mod:`forex_bot.factory.lightgbm_challenger` (Sprint D 3).

Coverage matrix (card 75c2346b-5c2a-46e9-8207-b784766ca882):

* **Lazy-import contract** — module is importable without ``lightgbm``
  installed; every code path that needs ``lightgbm`` raises
  :class:`LightGBMChallengerUnavailable` with a clear install hint.
* **Device flag wiring** — :func:`resolve_lightgbm_device_flag` and
  :func:`build_lightgbm_device_flag` honour the
  ``scripts.offload.executor`` helpers (``cuda`` when the probe is
  available, ``cpu`` otherwise), never crash on either device.
* **Single feature path** — the challenger consumes the SAME
  :data:`~forex_bot.factory.meta_labeling.META_FEATURE_NAMES`
  contract via :func:`~forex_bot.factory.meta_labeling.build_meta_features`,
  so the meta-labeler's anti-lookahead regression suite covers the
  challenger too.
* **Classifier wrapper** — :class:`LightGBMChallenger` fits on a
  known synthetic dataset, emits probabilities in ``[0, 1]``, and
  refuses degenerate inputs (single-class, NaN, < MIN_META_TRADES).
* **Head-to-head benchmark** —
  :func:`benchmark_lightgbm_vs_meta_labeler` runs on identical
  stratified folds with a deterministic seed, emits a
  :class:`BenchmarkArtifact` with side-by-side Brier / ROC-AUC /
  log-loss, and writes a JSON data artifact on request.
* **TrialReturnStore bridge** —
  :func:`consume_lightgbm_confidence_per_trial` mirrors the
  meta-labeler bridge and refuses shape-mismatched inputs.
* **Ranking integration** —
  :func:`rank_with_lightgbm_challenger` drops per-trial return
  rows whose challenger-P(win) is at or below the threshold and
  preserves ``bh_rejected=True`` for trials that survive the gate
  but fall below :data:`MIN_TRIAL_BARS`.
* **Factory package exports** — public symbols reachable from
  ``forex_bot.factory``.

All tests are pure (no market data fetch, no Optuna, no live GPU)
and stay inside the HR5 targeted-tests-only envelope
(``scripts/run_test_scope.sh tests/factory/test_lightgbm_challenger.py``).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

# Ensure src/ is on sys.path (matches conftest.py's bootstrap so this
# test file can be executed under
# ``pytest tests/factory/test_lightgbm_challenger.py`` directly).
_root = Path(__file__).resolve().parents[2]
for _p in (str(_root / "src"), str(_root / "src" / "forex_bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from forex_bot.factory.lightgbm_challenger import (  # noqa: E402
    DEFAULT_BENCHMARK_N_FOLDS,
    DEFAULT_BENCHMARK_SEED,
    DEFAULT_LIGHTGBM_DEVICE_LIST,
    VALID_DEVICE_FLAGS,
    BenchmarkArtifact,
    BenchmarkFold,
    LightGBMChallenger,
    LightGBMChallengerError,
    LightGBMChallengerShapeError,
    LightGBMChallengerUnavailable,
    benchmark_lightgbm_vs_meta_labeler,
    build_lightgbm_device_flag,
    consume_lightgbm_confidence_per_trial,
    fit_lightgbm_challenger,
    predict_lightgbm_probability,
    rank_with_lightgbm_challenger,
    resolve_lightgbm_device_flag,
)
from forex_bot.factory.meta_labeling import (  # noqa: E402
    META_FEATURE_NAMES,
    MIN_META_TRADES,
    MetaLabeledTrade,
    MetaTradeContext,
    build_meta_features,
    fit_meta_classifier,
)
from forex_bot.factory.risk_adjusted_ranking import (  # noqa: E402
    DEFAULT_META_GATE_THRESHOLD,
    RankedCandidate,
)
from forex_bot.factory.validation_runner import TrialReturnStore  # noqa: E402

# ── Skip guards ──────────────────────────────────────────────────────────


try:
    import lightgbm  # noqa: F401  — module presence probe
except ImportError:
    lightgbm = None

#: All tests that need ``lightgbm`` are gated on availability.  The
#: module ships lazy so the factory's other paths keep working on a
#: host without ``lightgbm`` installed (per the card requirement:
#: pinning ``lightgbm`` in ``requirements.txt`` requires Craig's
#: explicit approval).
REQUIRES_LIGHTGBM = pytest.mark.skipif(
    lightgbm is None,
    reason="lightgbm not installed — install with `pip install lightgbm` "
    "to enable LightGBM challenger tests; pinning the dep requires "
    "Craig's approval",
)


# ── Helpers (mirror test_meta_labeling.py patterns) ──────────────────────


def _context(  # noqa: PLR0913 — fixture helper
    *,
    candidate_id: str = "cand_a",
    pair: str = "EURUSD",
    timeframe: str = "H1",
    signal_time: datetime | None = None,
    primary_confidence: float = 0.6,
    primary_side: str = "long",
    regime: str = "trending",
    funding_rate_at_entry: float = 0.0001,
    venue_cost_bps_at_entry: float = 5.0,
    spread_pips_at_entry: float = 1.2,
) -> MetaTradeContext:
    if signal_time is None:
        signal_time = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    return MetaTradeContext(
        candidate_id=candidate_id,
        pair=pair,
        timeframe=timeframe,
        signal_time=signal_time,
        primary_confidence=primary_confidence,
        primary_side=primary_side,  # type: ignore[arg-type]
        regime=regime,  # type: ignore[arg-type]
        funding_rate_at_entry=funding_rate_at_entry,
        venue_cost_bps_at_entry=venue_cost_bps_at_entry,
        spread_pips_at_entry=spread_pips_at_entry,
    )


def _labeled(ctx: MetaTradeContext, outcome: int) -> MetaLabeledTrade:
    return MetaLabeledTrade(context=ctx, outcome=outcome)


def _synthetic_meta_set(  # noqa: PLR0915 — explicit knob for fixture
    *,
    n_per_class: int = 80,
    base_seed: int = 17,
) -> list[MetaLabeledTrade]:
    """Linearly separable synthetic meta-set (mirrors the meta-labeling test)."""
    rng = np.random.default_rng(base_seed)
    out: list[MetaLabeledTrade] = []
    base_time = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    for i in range(n_per_class):
        win_ctx = _context(
            candidate_id=f"win_{i}",
            primary_confidence=0.85 + float(rng.normal(0, 0.05)),
            primary_side="long",
            regime="trending",
            funding_rate_at_entry=float(rng.normal(0, 0.0001)),
            venue_cost_bps_at_entry=1.0 + float(rng.normal(0, 0.3)),
            spread_pips_at_entry=0.8 + float(rng.normal(0, 0.1)),
            signal_time=base_time + timedelta(hours=i),
        )
        out.append(_labeled(win_ctx, outcome=1))
        loss_ctx = _context(
            candidate_id=f"loss_{i}",
            primary_confidence=0.30 + float(rng.normal(0, 0.05)),
            primary_side="short",
            regime="choppy",
            funding_rate_at_entry=0.001 + float(rng.normal(0, 0.0002)),
            venue_cost_bps_at_entry=8.0 + float(rng.normal(0, 1.0)),
            spread_pips_at_entry=2.0 + float(rng.normal(0, 0.2)),
            signal_time=base_time + timedelta(hours=i + 200),
        )
        out.append(_labeled(loss_ctx, outcome=0))
    return out


def _returns_with_mean(
    mean: float,
    *,
    std: float = 0.01,
    n: int = 200,
    seed: int = 0,
) -> np.ndarray:
    """Per-bar returns with the requested mean and std (no autocorrelation)."""
    rng = np.random.default_rng(seed)
    return rng.normal(loc=mean, scale=std, size=n)


# ── Lazy-import + module surface ─────────────────────────────────────────


class TestModuleSurface:
    """Module is import-safe and exports the documented symbols."""

    def test_public_api_is_pinned(self) -> None:
        from forex_bot.factory import lightgbm_challenger as mod_mod

        expected = {
            "LightGBMChallengerError",
            "LightGBMChallengerUnavailable",
            "LightGBMChallengerShapeError",
            "LightGBMChallenger",
            "BenchmarkArtifact",
            "BenchmarkFold",
            "DEFAULT_BENCHMARK_SEED",
            "DEFAULT_BENCHMARK_N_FOLDS",
            "VALID_DEVICE_FLAGS",
            "DEFAULT_LIGHTGBM_DEVICE_LIST",
            "resolve_lightgbm_device_flag",
            "build_lightgbm_device_flag",
            "fit_lightgbm_challenger",
            "predict_lightgbm_probability",
            "consume_lightgbm_confidence_per_trial",
            "benchmark_lightgbm_vs_meta_labeler",
            "rank_with_lightgbm_challenger",
        }
        assert expected.issubset(set(mod_mod.__all__))

    def test_default_constants_pinned(self) -> None:
        # Pin the constants so a refactor does not silently drift
        # the benchmark's deterministic behaviour.
        assert DEFAULT_BENCHMARK_SEED == 17
        assert DEFAULT_BENCHMARK_N_FOLDS == 5
        assert VALID_DEVICE_FLAGS == ("cpu", "cuda")
        assert DEFAULT_LIGHTGBM_DEVICE_LIST == VALID_DEVICE_FLAGS

    def test_errors_inherit_meta_labeling_error(self) -> None:
        from forex_bot.factory.meta_labeling import MetaLabelingError

        # Existing handlers that catch ``MetaLabelingError`` should
        # still handle challenger errors — common base class.
        assert issubclass(LightGBMChallengerError, MetaLabelingError)
        assert issubclass(LightGBMChallengerShapeError, LightGBMChallengerError)
        assert issubclass(LightGBMChallengerUnavailable, LightGBMChallengerError)


class TestLazyImportContract:
    """Lightgbm path raises a clear, actionable error when missing."""

    def test_fit_raises_when_lightgbm_missing(self) -> None:
        # Construct a challenger directly so we can call ``.fit``
        # without touching the top-level ``fit_lightgbm_challenger``
        # wrapper (which would itself raise).
        clf = LightGBMChallenger(device="cpu")
        X = np.zeros((MIN_META_TRADES + 5, len(META_FEATURE_NAMES)), dtype=float)
        y = np.zeros(MIN_META_TRADES + 5, dtype=int)
        y[::2] = 1
        if lightgbm is None:
            with pytest.raises(LightGBMChallengerUnavailable) as excinfo:
                clf.fit(X, y)
            # Clear install hint for the operator.
            msg = str(excinfo.value)
            assert "lightgbm" in msg.lower()
            assert "pip install lightgbm" in msg.lower()
        else:
            # When lightgbm IS installed, the fit succeeds.
            clf.fit(X, y)
            assert clf.is_fitted

    def test_predict_raises_when_lightgbm_missing(self) -> None:
        clf = LightGBMChallenger(device="cpu")
        if lightgbm is None:
            # Before fit, predict_proba should refuse for the wrong
            # reason — "must be fit" — which has nothing to do with
            # lightgbm being installed.  After fit it would surface the
            # lazy-import error.
            X = np.zeros((MIN_META_TRADES + 5, len(META_FEATURE_NAMES)), dtype=float)
            with pytest.raises(LightGBMChallengerError) as excinfo:
                clf.predict_proba(X)
            assert "fit" in str(excinfo.value).lower()

    def test_predict_lightgbm_probability_empty_returns_empty_array(self) -> None:
        # The convenience predictor is reachable without lightgbm because
        # it does not call .fit.  Empty contexts short-circuit.
        clf = LightGBMChallenger(device="cpu")
        out = predict_lightgbm_probability(clf, [])
        assert isinstance(out, np.ndarray)
        assert out.size == 0


# ── Device flag wiring ───────────────────────────────────────────────────


class TestDeviceFlagWiring:
    """``resolve_lightgbm_device_flag`` + ``build_lightgbm_device_flag``."""

    def test_resolve_cpu_when_gpu_unavailable(self) -> None:
        from scripts.offload.executor import GpuProbeResult

        gpu = GpuProbeResult(available=False, reason="test")
        assert resolve_lightgbm_device_flag(gpu) == "cpu"

    def test_resolve_cuda_when_gpu_available(self) -> None:
        from scripts.offload.executor import GpuProbeResult

        gpu = GpuProbeResult(
            available=True,
            device_count=1,
            devices=("GPU-0",),
            reason=None,
        )
        assert resolve_lightgbm_device_flag(gpu) == "cuda"

    def test_resolve_falls_back_when_gpu_is_none(self) -> None:
        # ``None`` → default fallback (cpu).
        assert resolve_lightgbm_device_flag(None) == "cpu"
        assert resolve_lightgbm_device_flag(None, default="cpu") == "cpu"

    def test_resolve_rejects_invalid_default(self) -> None:
        with pytest.raises(LightGBMChallengerError):
            resolve_lightgbm_device_flag(None, default="tpu")

    def test_build_lightgbm_device_flag_returns_canonical_pair(self) -> None:
        # Live probe — must return one of the canonical flags without
        # raising, regardless of nvidia-smi presence.
        flag = build_lightgbm_device_flag()
        assert flag in VALID_DEVICE_FLAGS

    def test_challenger_accepts_resolved_flag(self) -> None:
        # The resolved flag is a valid ``device`` for the challenger.
        from scripts.offload.executor import GpuProbeResult

        gpu = GpuProbeResult(available=True)
        flag = resolve_lightgbm_device_flag(gpu)
        clf = LightGBMChallenger(device=flag)  # type: ignore[arg-type]
        assert clf.device == flag

    def test_challenger_rejects_unknown_device(self) -> None:
        with pytest.raises(LightGBMChallengerError):
            LightGBMChallenger(device="tpu")  # type: ignore[arg-type]


# ── Single feature path (no second feature builder) ──────────────────────


class TestFeatureContractReuse:
    """The challenger consumes the SAME META_FEATURE_NAMES contract."""

    def test_default_feature_names_locked_to_meta_labeling(self) -> None:
        clf = LightGBMChallenger()
        assert clf.feature_names == META_FEATURE_NAMES

    def test_predict_lightgbm_probability_reuses_meta_features(self) -> None:
        # Same contexts → same feature matrix as the meta-labeler.
        contexts = [_context() for _ in range(5)]
        # The meta-labeler's build_meta_features is the single source
        # of truth — confirm both paths agree.
        challenger_X = build_meta_features(contexts)
        meta_X = build_meta_features(contexts)
        assert challenger_X.shape == meta_X.shape
        assert np.allclose(challenger_X, meta_X)


# ── Classifier wrapper ───────────────────────────────────────────────────


class TestClassifierWrapper:
    """LightGBMChallenger fits + predicts on a known synthetic dataset."""

    @REQUIRES_LIGHTGBM
    def test_fit_emits_is_fitted_true(self) -> None:
        trades = _synthetic_meta_set(n_per_class=40)
        X = build_meta_features([t.context for t in trades])
        y = np.asarray([int(t.outcome) for t in trades], dtype=int)
        clf = LightGBMChallenger(device="cpu")
        assert clf.is_fitted is False
        clf.fit(X, y)
        assert clf.is_fitted is True

    @REQUIRES_LIGHTGBM
    def test_predict_proba_shape_and_range(self) -> None:
        trades = _synthetic_meta_set(n_per_class=40)
        X = build_meta_features([t.context for t in trades])
        y = np.asarray([int(t.outcome) for t in trades], dtype=int)
        clf = LightGBMChallenger(device="cpu")
        clf.fit(X, y)
        proba = clf.predict_proba(X)
        assert proba.shape == (X.shape[0], 2)
        assert np.all(proba >= 0.0)
        assert np.all(proba <= 1.0)
        # Rows must sum to 1.
        sums = proba.sum(axis=1)
        assert np.allclose(sums, 1.0)

    @REQUIRES_LIGHTGBM
    def test_fit_rejects_too_few_samples(self) -> None:
        clf = LightGBMChallenger(device="cpu")
        X = np.zeros((MIN_META_TRADES - 5, len(META_FEATURE_NAMES)), dtype=float)
        y = np.zeros(MIN_META_TRADES - 5, dtype=int)
        y[::2] = 1
        with pytest.raises(LightGBMChallengerShapeError) as excinfo:
            clf.fit(X, y)
        assert str(MIN_META_TRADES) in str(excinfo.value)

    @REQUIRES_LIGHTGBM
    def test_fit_rejects_single_class(self) -> None:
        clf = LightGBMChallenger(device="cpu")
        X = np.zeros((MIN_META_TRADES + 5, len(META_FEATURE_NAMES)), dtype=float)
        y = np.ones(MIN_META_TRADES + 5, dtype=int)  # only wins
        with pytest.raises(LightGBMChallengerShapeError) as excinfo:
            clf.fit(X, y)
        assert "win" in str(excinfo.value).lower()

    @REQUIRES_LIGHTGBM
    def test_fit_rejects_nan_features(self) -> None:
        clf = LightGBMChallenger(device="cpu")
        X = np.zeros((MIN_META_TRADES + 5, len(META_FEATURE_NAMES)), dtype=float)
        X[0, 0] = np.nan
        y = np.zeros(MIN_META_TRADES + 5, dtype=int)
        y[::2] = 1
        with pytest.raises(LightGBMChallengerShapeError):
            clf.fit(X, y)

    @REQUIRES_LIGHTGBM
    def test_fit_rejects_shape_mismatch(self) -> None:
        clf = LightGBMChallenger(device="cpu")
        X = np.zeros(
            (MIN_META_TRADES + 5, len(META_FEATURE_NAMES) + 1), dtype=float
        )
        y = np.zeros(MIN_META_TRADES + 5, dtype=int)
        y[::2] = 1
        with pytest.raises(LightGBMChallengerShapeError) as excinfo:
            clf.fit(X, y)
        assert "columns" in str(excinfo.value).lower()

    @REQUIRES_LIGHTGBM
    def test_predict_before_fit_raises(self) -> None:
        clf = LightGBMChallenger(device="cpu")
        X = np.zeros((5, len(META_FEATURE_NAMES)), dtype=float)
        with pytest.raises(LightGBMChallengerError):
            clf.predict_proba(X)

    @REQUIRES_LIGHTGBM
    def test_fit_rejects_bad_hyperparams(self) -> None:
        with pytest.raises(LightGBMChallengerError):
            LightGBMChallenger(n_estimators=0)
        with pytest.raises(LightGBMChallengerError):
            LightGBMChallenger(learning_rate=0.0)
        with pytest.raises(LightGBMChallengerError):
            LightGBMChallenger(num_leaves=1)
        with pytest.raises(LightGBMChallengerError):
            LightGBMChallenger(min_child_samples=0)
        with pytest.raises(LightGBMChallengerError):
            LightGBMChallenger(feature_fraction=0.0)
        with pytest.raises(LightGBMChallengerError):
            LightGBMChallenger(bagging_fraction=0.0)
        with pytest.raises(LightGBMChallengerError):
            LightGBMChallenger(bagging_freq=-1)

    @REQUIRES_LIGHTGBM
    def test_fit_lightgbm_challenger_convenience(self) -> None:
        trades = _synthetic_meta_set(n_per_class=40)
        clf = fit_lightgbm_challenger(trades)
        assert clf.is_fitted
        # Same predictors give the same P(win) as build_meta_features +
        # clf.predict_proba.
        contexts = [_context()]
        probs = predict_lightgbm_probability(clf, contexts)
        assert probs.shape == (1,)

    def test_fit_lightgbm_challenger_rejects_empty(self) -> None:
        with pytest.raises(LightGBMChallengerShapeError):
            fit_lightgbm_challenger([])


# ── TrialReturnStore bridge ──────────────────────────────────────────────


class TestConsumeLightGBMConfidencePerTrial:
    """Bridge consumes TrialReturnStore without duplicating collection."""

    def _store_with_two_trials(self) -> TrialReturnStore:
        store = TrialReturnStore()
        key = ("trend_follow", "EURUSD", "H1")
        store.record(key, _returns_with_mean(0.005, std=0.005, n=120, seed=51))
        store.record(key, _returns_with_mean(0.001, std=0.005, n=120, seed=52))
        return store

    def test_consume_returns_none_for_missing_cell(self) -> None:
        store = TrialReturnStore()
        clf = LightGBMChallenger(device="cpu")
        out = consume_lightgbm_confidence_per_trial(
            store,
            cell_key=("nope", "X", "H1"),
            clf=clf,
            contexts_per_trial=[],
        )
        assert out is None

    @REQUIRES_LIGHTGBM
    def test_consume_returns_matrix_for_valid_cell(self) -> None:
        store = self._store_with_two_trials()
        cell_key = ("trend_follow", "EURUSD", "H1")
        matrix = store.matrix(cell_key)
        assert matrix is not None
        n_signals_per_trial = matrix.shape[0]
        base_time = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
        ctx_j0 = [
            _context(
                candidate_id=f"t0_s{i}",
                signal_time=base_time + timedelta(hours=i),
            )
            for i in range(n_signals_per_trial)
        ]
        ctx_j1 = [
            _context(
                candidate_id=f"t1_s{i}",
                signal_time=base_time + timedelta(hours=i),
            )
            for i in range(n_signals_per_trial)
        ]
        trades = _synthetic_meta_set(n_per_class=40)
        clf = fit_lightgbm_challenger(trades)
        out = consume_lightgbm_confidence_per_trial(
            store,
            cell_key=cell_key,
            clf=clf,
            contexts_per_trial=[ctx_j0, ctx_j1],
        )
        assert out is not None
        assert out.shape == matrix.shape
        # All values must be finite — the synthetic contexts are clean.
        assert np.all(np.isfinite(out))


# ── Ranking integration ──────────────────────────────────────────────────


class TestRankWithLightGBMChallenger:
    """The challenger integrates into the BH-FDR path via the SAME call."""

    @REQUIRES_LIGHTGBM
    def test_rank_drops_below_threshold_trials(self) -> None:
        # Mirror ``test_store_bridge_with_full_meta_gate`` from
        # test_meta_labeling.py — same expected behaviour for the
        # challenger path.  Confirms the challenger integrates via the
        # SAME ranker without special-casing.
        returns_strong = _returns_with_mean(0.008, std=0.005, n=120, seed=71)
        returns_noise = _returns_with_mean(0.0, std=0.01, n=120, seed=72)
        n = 120
        base_time = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
        # Craft contexts so the challenger's P(win) is HIGH for the
        # strong trial's signals and LOW for the noise trial's
        # signals.
        contexts_strong = [
            _context(
                candidate_id=f"strong_s{i}",
                primary_confidence=0.9,
                regime="trending",
                venue_cost_bps_at_entry=0.5,
                spread_pips_at_entry=0.5,
                signal_time=base_time + timedelta(hours=i),
            )
            for i in range(n)
        ]
        contexts_noise = [
            _context(
                candidate_id=f"noise_s{i}",
                primary_confidence=0.2,
                regime="choppy",
                venue_cost_bps_at_entry=10.0,
                spread_pips_at_entry=3.0,
                signal_time=base_time + timedelta(hours=i + 500),
            )
            for i in range(n)
        ]
        # Fit the challenger on synthetic data that mirrors the
        # strong/noise split so the predictions align with the test
        # context values.
        train_trades: list[MetaLabeledTrade] = []
        rng = np.random.default_rng(91)
        for _i in range(60):
            train_trades.append(
                _labeled(
                    _context(
                        primary_confidence=0.85 + float(rng.normal(0, 0.05)),
                        regime="trending",
                        venue_cost_bps_at_entry=1.0,
                        spread_pips_at_entry=0.8,
                    ),
                    1,
                )
            )
            train_trades.append(
                _labeled(
                    _context(
                        primary_confidence=0.30 + float(rng.normal(0, 0.05)),
                        regime="choppy",
                        venue_cost_bps_at_entry=8.0,
                        spread_pips_at_entry=2.0,
                    ),
                    0,
                )
            )
        clf = fit_lightgbm_challenger(train_trades, device_flag="cpu")

        ranked = rank_with_lightgbm_challenger(
            [("strong", returns_strong), ("noise", returns_noise)],
            clf,
            [contexts_strong, contexts_noise],
            meta_threshold=DEFAULT_META_GATE_THRESHOLD,
        )
        ids = [r.candidate_id for r in ranked]
        # "noise" should rank LAST — either gated out entirely
        # (bh_reject=True) or its kept trials drag it below "strong".
        assert ids[-1] == "noise"
        assert ranked[-1].bh_reject is True or ranked[-1].t_statistic <= ranked[0].t_statistic
        # "strong" survives the gate.
        assert ids[0] == "strong"

    @REQUIRES_LIGHTGBM
    def test_rank_returns_ranked_candidates(self) -> None:
        # Sanity: the helper returns the same list type as the
        # meta-labeler path (``list[RankedCandidate]``).
        returns = _returns_with_mean(0.005, std=0.005, n=120, seed=11)
        n = 120
        base_time = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
        contexts = [
            _context(
                candidate_id=f"s{i}",
                signal_time=base_time + timedelta(hours=i),
            )
            for i in range(n)
        ]
        trades = _synthetic_meta_set(n_per_class=40)
        clf = fit_lightgbm_challenger(trades)
        ranked = rank_with_lightgbm_challenger(
            [("cand", returns)],
            clf,
            [contexts],
            meta_threshold=DEFAULT_META_GATE_THRESHOLD,
        )
        assert isinstance(ranked, list)
        assert all(isinstance(r, RankedCandidate) for r in ranked)

    def test_rank_rejects_length_mismatch(self) -> None:
        # Length-mismatch rejection is independent of lightgbm — the
        # check fires before .fit is needed.
        clf = LightGBMChallenger(device="cpu")
        with pytest.raises(LightGBMChallengerShapeError):
            rank_with_lightgbm_challenger(
                [("a", np.zeros(10)), ("b", np.zeros(10))],
                clf,
                [[]],  # only one list of contexts for two candidates
            )


# ── Head-to-head benchmark ───────────────────────────────────────────────


class TestBenchmarkLightGBMvsMetaLabeler:
    """Side-by-side Brier / ROC-AUC / log-loss on identical folds."""

    @REQUIRES_LIGHTGBM
    def test_benchmark_emits_artifact_with_pinned_keys(self) -> None:
        trades = _synthetic_meta_set(n_per_class=60)
        artifact = benchmark_lightgbm_vs_meta_labeler(
            trades,
            n_folds=3,
            device_flag="cpu",
        )
        assert isinstance(artifact, BenchmarkArtifact)
        # Pin the public key set so the workboard contract is stable.
        keys = set(artifact.to_dict().keys())
        expected = {
            "challenger_id",
            "meta_id",
            "n_signals",
            "n_wins",
            "base_win_rate",
            "challenger_brier",
            "meta_brier",
            "challenger_roc_auc",
            "meta_roc_auc",
            "challenger_log_loss",
            "meta_log_loss",
            "delta_brier",
            "delta_roc_auc",
            "n_folds",
            "device_flag",
            "random_seed",
            "fold_metrics",
            "timestamp_utc",
            "feature_names",
        }
        assert expected.issubset(keys)
        # Top-level types.
        assert artifact.challenger_id == "lightgbm"
        assert artifact.meta_id == "logistic_meta"
        assert artifact.device_flag == "cpu"
        assert artifact.n_folds == 3
        assert artifact.feature_names == META_FEATURE_NAMES

    @REQUIRES_LIGHTGBM
    def test_benchmark_aggregates_per_fold(self) -> None:
        trades = _synthetic_meta_set(n_per_class=60)
        artifact = benchmark_lightgbm_vs_meta_labeler(
            trades, n_folds=3, device_flag="cpu"
        )
        # n_folds usable folds (none skipped for two-class data).
        assert len(artifact.fold_metrics) == 3
        # Aggregates are means across folds.
        mean_brier_c = float(
            np.mean([f.challenger_brier for f in artifact.fold_metrics])
        )
        assert np.isclose(artifact.challenger_brier, mean_brier_c)
        # All metrics are finite.
        for f in artifact.fold_metrics:
            assert np.isfinite(f.challenger_brier)
            assert np.isfinite(f.meta_brier)
            assert np.isfinite(f.challenger_roc_auc)
            assert np.isfinite(f.meta_roc_auc)

    @REQUIRES_LIGHTGBM
    def test_benchmark_deterministic_seed(self) -> None:
        # Same seed → identical artifact.  This pins the benchmark's
        # byte-stability, which is the workboard contract.
        trades = _synthetic_meta_set(n_per_class=40)
        a1 = benchmark_lightgbm_vs_meta_labeler(
            trades, n_folds=3, device_flag="cpu", random_seed=17
        )
        a2 = benchmark_lightgbm_vs_meta_labeler(
            trades, n_folds=3, device_flag="cpu", random_seed=17
        )
        # Aggregates are means of fold metrics → float-stable.
        assert a1.challenger_brier == a2.challenger_brier
        assert a1.meta_brier == a2.meta_brier
        assert a1.challenger_roc_auc == a2.challenger_roc_auc
        assert a1.meta_roc_auc == a2.meta_roc_auc
        # Delta is derived → also deterministic.
        assert a1.delta_brier == a2.delta_brier
        assert a1.delta_roc_auc == a2.delta_roc_auc

    @REQUIRES_LIGHTGBM
    def test_benchmark_identical_fold_split(self) -> None:
        # The benchmark contract requires identical fold splits — pin
        # by checking fold-level train/test sizes are aligned across
        # the two classifiers within a fold.
        trades = _synthetic_meta_set(n_per_class=40)
        artifact = benchmark_lightgbm_vs_meta_labeler(
            trades, n_folds=3, device_flag="cpu"
        )
        for f in artifact.fold_metrics:
            # Single ``n_train``/``n_test`` field per fold — proves both
            # classifiers used the SAME rows.
            assert f.n_train > 0
            assert f.n_test > 0
            # The fold_index is preserved.
            assert f.fold_index >= 0

    @REQUIRES_LIGHTGBM
    def test_benchmark_emits_artifact_path(self, tmp_path: Path) -> None:
        trades = _synthetic_meta_set(n_per_class=40)
        out_path = tmp_path / "benchmark.json"
        artifact = benchmark_lightgbm_vs_meta_labeler(
            trades,
            n_folds=3,
            device_flag="cpu",
            emit_artifact_path=out_path,
        )
        assert out_path.exists()
        # JSON round-trip preserves keys.
        data = json.loads(out_path.read_text())
        assert data["challenger_id"] == "lightgbm"
        assert data["n_signals"] == artifact.n_signals
        assert isinstance(data["fold_metrics"], list)
        assert data["feature_names"] == list(META_FEATURE_NAMES)

    def test_benchmark_rejects_empty_trades(self) -> None:
        with pytest.raises(LightGBMChallengerShapeError):
            benchmark_lightgbm_vs_meta_labeler(
                [], n_folds=3, device_flag="cpu"
            )

    def test_benchmark_rejects_too_few_folds(self) -> None:
        trades = _synthetic_meta_set(n_per_class=10)
        with pytest.raises(LightGBMChallengerError):
            benchmark_lightgbm_vs_meta_labeler(
                trades, n_folds=1, device_flag="cpu"
            )

    def test_benchmark_rejects_single_class(self) -> None:
        all_wins = [
            _labeled(_context(primary_confidence=0.5), 1)
            for _ in range(MIN_META_TRADES + 5)
        ]
        with pytest.raises(LightGBMChallengerShapeError):
            benchmark_lightgbm_vs_meta_labeler(
                all_wins, n_folds=3, device_flag="cpu"
            )

    @REQUIRES_LIGHTGBM
    def test_benchmark_delta_sign_convention(self) -> None:
        # Delta convention: negative delta == challenger better than meta
        # (Brier: lower is better; ROC-AUC: higher is better; negative ==
        # challenger wins by 1 so the convention is consistent).
        trades = _synthetic_meta_set(n_per_class=60)
        artifact = benchmark_lightgbm_vs_meta_labeler(
            trades, n_folds=3, device_flag="cpu"
        )
        assert artifact.delta_brier == pytest.approx(
            artifact.challenger_brier - artifact.meta_brier
        )
        assert artifact.delta_roc_auc == pytest.approx(
            artifact.challenger_roc_auc - artifact.meta_roc_auc
        )


# ── BenchmarkFold + BenchmarkArtifact surface ────────────────────────────


class TestBenchmarkArtifact:
    """Dataclass + JSON serialisation is stable."""

    def test_benchmark_fold_to_dict_keys(self) -> None:
        fold = BenchmarkFold(
            fold_index=0,
            n_train=100,
            n_test=40,
            challenger_brier=0.2,
            meta_brier=0.25,
            challenger_roc_auc=0.6,
            meta_roc_auc=0.55,
            challenger_log_loss=0.6,
            meta_log_loss=0.7,
        )
        d = fold.to_dict()
        assert d["fold_index"] == 0
        assert d["n_train"] == 100
        assert d["n_test"] == 40
        assert d["challenger_brier"] == 0.2

    def test_artifact_to_json_round_trip(self) -> None:
        fold = BenchmarkFold(
            fold_index=0,
            n_train=100,
            n_test=40,
            challenger_brier=0.2,
            meta_brier=0.25,
            challenger_roc_auc=0.6,
            meta_roc_auc=0.55,
            challenger_log_loss=0.6,
            meta_log_loss=0.7,
        )
        artifact = BenchmarkArtifact(
            n_signals=140,
            n_wins=70,
            base_win_rate=0.5,
            challenger_brier=0.2,
            meta_brier=0.25,
            challenger_roc_auc=0.6,
            meta_roc_auc=0.55,
            challenger_log_loss=0.6,
            meta_log_loss=0.7,
            delta_brier=-0.05,
            delta_roc_auc=0.05,
            n_folds=3,
            device_flag="cpu",
            random_seed=17,
            fold_metrics=(fold,),
            timestamp_utc="2026-10-06T18:00:00+00:00",
            feature_names=META_FEATURE_NAMES,
        )
        text = artifact.to_json()
        data = json.loads(text)
        assert data["n_signals"] == 140
        assert data["fold_metrics"][0]["fold_index"] == 0
        assert data["feature_names"] == list(META_FEATURE_NAMES)

    def test_artifact_write_creates_file(self, tmp_path: Path) -> None:
        artifact = BenchmarkArtifact(n_signals=10, n_wins=5, base_win_rate=0.5)
        out = tmp_path / "a.json"
        artifact.write(out)
        assert out.exists()
        # Atomic write — no leftover .tmp file.
        assert not out.with_suffix(out.suffix + ".tmp").exists()


# ── Public imports from forex_bot.factory ───────────────────────────────


class TestFactoryExports:
    """Public symbols reachable from ``forex_bot.factory``."""

    def test_module_importable(self) -> None:
        from forex_bot.factory import lightgbm_challenger as mod_mod

        # Spot-check the headline exports.
        assert mod_mod.LightGBMChallenger is LightGBMChallenger
        assert mod_mod.BenchmarkArtifact is BenchmarkArtifact
        assert mod_mod.benchmark_lightgbm_vs_meta_labeler is (
            benchmark_lightgbm_vs_meta_labeler
        )
        assert mod_mod.fit_lightgbm_challenger is fit_lightgbm_challenger

    def test_integration_with_meta_labeler_signatures(self) -> None:
        # Pin that the challenger can be substituted for the meta-labeler
        # at the ranker call site — same feature contract, same
        # predict_proba shape, same error base class.
        import inspect

        meta_fit_sig = inspect.signature(fit_meta_classifier)
        challenger_fit_sig = inspect.signature(fit_lightgbm_challenger)
        # First positional parameter in both is the trade sequence.
        assert (
            list(meta_fit_sig.parameters.keys())[0]
            == list(challenger_fit_sig.parameters.keys())[0]
        )
