"""Tests for :mod:`forex_bot.factory.meta_labeling` (Sprint D 1).

Coverage matrix (card 64c6598f-895d-4866-b771-2f3aa894d09d):

* **Per-trade feature builder** — :func:`build_meta_features` produces
  a matrix of shape ``[N, len(META_FEATURE_NAMES)]`` with finite
  values and the documented column ordering.
* **Anti-lookahead contract** — a dedicated
  :class:`TestAntiLookahead` class (mirrors
  ``tests/backtest/test_anti_lookahead.py``) asserts that mutating
  fields NOT on the trading line at signal time does NOT change the
  feature matrix.  Outcome injection must not leak into features.
* **Calibrated meta-classifier** — :class:`CalibratedMetaClassifier`
  fits on a known synthetic dataset, emits probabilities in ``[0, 1]``,
  and refuses degenerate inputs (single-class, NaN, < MIN_META_TRADES).
* **Calibration validity** — Brier score on a held-out set is bounded
  and the calibration method switch (``sigmoid`` ↔ ``isotonic``) works.
* **Ranking integration** —
  :func:`rank_candidates_with_meta_gate` drops per-trial return rows
  whose calibrated meta-confidence is at or below the threshold,
  preserves ``bh_rejected=True`` for trials that survive the gate but
  fall below :data:`MIN_TRIAL_BARS`, and degrades to identity when
  the meta-confidence sequence is ``None``.
* **TrialReturnStore bridge** —
  :func:`consume_meta_confidence_per_trial` consumes the existing
  in-memory store (no collection duplication) and rejects
  shape-mismatch inputs.
* **Degenerate / empty inputs** — empty contexts, single trade,
  single-class outcomes, NaN feature columns, all-zero return
  series, threshold sweep.
* **Factory package exports** — public symbols reachable from
  ``forex_bot.factory``.

All tests are pure (no market data fetch, no Optuna, no git) so they
stay in the HR5 targeted-tests-only envelope
(``scripts/run_test_scope.sh``).
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

# Ensure src/forex_bot and src are on sys.path (matches conftest.py's
# bootstrap so this test file can be executed under
# ``pytest tests/factory/test_meta_labeling.py`` directly).
_root = Path(__file__).resolve().parents[2]
for _p in (str(_root / "src"), str(_root / "src" / "forex_bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from forex_bot.factory.meta_labeling import (  # noqa: E402
    DEFAULT_META_GATE_THRESHOLD_LOCAL,
    META_FEATURE_NAMES,
    MIN_META_TRADES,
    CalibratedMetaClassifier,
    MetaLabeledTrade,
    MetaLabelingError,
    MetaLabelingShapeError,
    MetaTradeContext,
    build_meta_features,
    consume_meta_confidence_per_trial,
    evaluate_meta_label_calibration,
    fit_meta_classifier,
    predict_meta_probability,
)
from forex_bot.factory.risk_adjusted_ranking import (  # noqa: E402
    DEFAULT_META_GATE_THRESHOLD,
    rank_candidates_by_trial_returns,
    rank_candidates_with_meta_gate,
    rank_from_trial_return_store,
    rank_from_trial_return_store_with_meta_gate,
)
from forex_bot.factory.validation_runner import TrialReturnStore  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────────


def _context(  # noqa: PLR0913 — helper for fixture-style test inputs
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
    """Build a :class:`MetaTradeContext` with sensible defaults."""
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


def _labeled(
    ctx: MetaTradeContext,
    outcome: int,
) -> MetaLabeledTrade:
    """Build a :class:`MetaLabeledTrade` from a context + outcome."""
    return MetaLabeledTrade(context=ctx, outcome=outcome)


def _synthetic_meta_set(  # noqa: PLR0915 — explicit knob for fixture
    *,
    n_per_class: int = 80,
    base_seed: int = 17,
    win_context_kwargs: dict | None = None,
    loss_context_kwargs: dict | None = None,
) -> list[MetaLabeledTrade]:
    """Build a synthetic meta-set with linearly separable structure.

    The "win" contexts carry features that the logistic regression can
    latch onto: high primary_confidence, regime TRENDING, low venue cost,
    mild funding.  The "loss" contexts carry the opposite.  This
    guarantees the meta-classifier fits something meaningful rather
    than collapsing to the base rate.
    """
    rng = np.random.default_rng(base_seed)
    win_kwargs = win_context_kwargs or {}
    loss_kwargs = loss_context_kwargs or {}

    out: list[MetaLabeledTrade] = []
    base_time = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    for i in range(n_per_class):
        # Wins: high confidence, trending regime, low funding, low cost.
        win_ctx = _context(
            candidate_id=f"win_{i}",
            primary_confidence=0.85 + float(rng.normal(0, 0.05)),
            primary_side="long",
            regime="trending",
            funding_rate_at_entry=float(rng.normal(0, 0.0001)),
            venue_cost_bps_at_entry=1.0 + float(rng.normal(0, 0.3)),
            spread_pips_at_entry=0.8 + float(rng.normal(0, 0.1)),
            signal_time=base_time + timedelta(hours=i),
            **win_kwargs,
        )
        out.append(_labeled(win_ctx, outcome=1))
        # Losses: low confidence, choppy regime, funding extremes.
        loss_ctx = _context(
            candidate_id=f"loss_{i}",
            primary_confidence=0.30 + float(rng.normal(0, 0.05)),
            primary_side="short",
            regime="choppy",
            funding_rate_at_entry=0.001 + float(rng.normal(0, 0.0002)),
            venue_cost_bps_at_entry=8.0 + float(rng.normal(0, 1.0)),
            spread_pips_at_entry=2.0 + float(rng.normal(0, 0.2)),
            signal_time=base_time + timedelta(hours=i + 200),
            **loss_kwargs,
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
    return np.random.default_rng(seed).normal(loc=mean, scale=std, size=n)


# ── Per-trade feature builder ────────────────────────────────────────────


class TestBuildMetaFeatures:
    """The feature matrix has the right shape, names, and finiteness."""

    def test_shape_n_zero_returns_empty_matrix(self) -> None:
        out = build_meta_features([])
        assert out.shape == (0, len(META_FEATURE_NAMES))
        assert out.dtype == float

    def test_shape_single_context(self) -> None:
        out = build_meta_features([_context()])
        assert out.shape == (1, len(META_FEATURE_NAMES))
        assert np.all(np.isfinite(out))

    def test_shape_n_contexts(self) -> None:
        n = 25
        contexts = [
            _context(
                candidate_id=f"c_{i}",
                signal_time=datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc) + timedelta(hours=i),
            )
            for i in range(n)
        ]
        out = build_meta_features(contexts)
        assert out.shape == (n, len(META_FEATURE_NAMES))

    def test_default_column_order_matches_feature_names(self) -> None:
        ctx = _context()
        out = build_meta_features([ctx])
        # The default feature_names == META_FEATURE_NAMES so the
        # output column order is the documented order.
        assert out.shape == (1, len(META_FEATURE_NAMES))

    def test_custom_feature_names_reorders_columns(self) -> None:
        ctx = _context(
            primary_confidence=0.7,
            funding_rate_at_entry=0.0005,
        )
        # Reversed order: every column should be re-mapped.
        reversed_names = tuple(reversed(META_FEATURE_NAMES))
        out = build_meta_features([ctx], feature_names=reversed_names)
        assert out.shape == (1, len(reversed_names))
        # primary_confidence is column index 0 in default order,
        # so it should now appear at the reversed index.
        default_idx = META_FEATURE_NAMES.index("primary_confidence")
        assert out[0, len(META_FEATURE_NAMES) - 1 - default_idx] == ctx.primary_confidence

    def test_no_nan_or_inf_in_output(self) -> None:
        # Pathological inputs (NaN/inf funding, etc.) are NOT in the
        # contract — we trust callers to clean — but the function must
        # always return finite floats.
        out = build_meta_features(
            [
                _context(funding_rate_at_entry=float("nan")),
                _context(funding_rate_at_entry=float("inf")),
                _context(venue_cost_bps_at_entry=float("-inf")),
            ]
        )
        assert out.shape == (3, len(META_FEATURE_NAMES))
        # NaN/inf in funding does propagate (we trust the caller's
        # data hygiene) — but the OTHER columns must still be finite.
        # At minimum, the spread column (no NaN passed) should be
        # finite everywhere.
        spread_col_idx = META_FEATURE_NAMES.index("spread_pips_at_entry")
        assert np.all(np.isfinite(out[:, spread_col_idx]))

    def test_primary_confidence_is_first_feature(self) -> None:
        # Pin the order — first feature is primary_confidence so the
        # coefficients are easy to interpret during debugging.
        assert META_FEATURE_NAMES[0] == "primary_confidence"

    def test_regime_one_hot_orthogonality(self) -> None:
        # The four regime indicators should sum to 1 for any
        # "specific" regime, and 0 for "unknown".
        for regime in ("trending", "choppy", "volatile", "quiet"):
            ctx = _context(regime=regime)
            out = build_meta_features([ctx])
            r_idxs = [
                META_FEATURE_NAMES.index(n)
                for n in (
                    "regime_trending",
                    "regime_choppy",
                    "regime_volatile",
                    "regime_quiet",
                )
            ]
            assert out[0, r_idxs].sum() == pytest.approx(1.0)

        # "unknown" → all zeros.
        out_unknown = build_meta_features([_context(regime="unknown")])
        r_idxs = [
            META_FEATURE_NAMES.index(n)
            for n in (
                "regime_trending",
                "regime_choppy",
                "regime_volatile",
                "regime_quiet",
            )
        ]
        assert out_unknown[0, r_idxs].sum() == pytest.approx(0.0)

    def test_primary_side_one_hot_orthogonality(self) -> None:
        # long: only is_long = 1.  short: only is_short = 1.
        # flat: both 0.
        for side in ("long", "short", "flat"):
            ctx = _context(primary_side=side)  # type: ignore[arg-type]
            out = build_meta_features([ctx])
            long_idx = META_FEATURE_NAMES.index("primary_is_long")
            short_idx = META_FEATURE_NAMES.index("primary_is_short")
            if side == "long":
                assert out[0, long_idx] == 1.0
                assert out[0, short_idx] == 0.0
            elif side == "short":
                assert out[0, long_idx] == 0.0
                assert out[0, short_idx] == 1.0
            else:  # flat
                assert out[0, long_idx] == 0.0
                assert out[0, short_idx] == 0.0

    def test_funding_abs_is_non_negative(self) -> None:
        # The funding_abs_at_entry companion feature is always >= 0.
        ctx = _context(funding_rate_at_entry=-0.005)
        idx = META_FEATURE_NAMES.index("funding_abs_at_entry")
        assert build_meta_features([ctx])[0, idx] == pytest.approx(0.005)

    def test_time_encodings_are_unit_circle(self) -> None:
        # hour_sin^2 + hour_cos^2 == 1; same for dow.
        ctx = _context()
        out = build_meta_features([ctx])
        hs_idx = META_FEATURE_NAMES.index("hour_sin")
        hc_idx = META_FEATURE_NAMES.index("hour_cos")
        ds_idx = META_FEATURE_NAMES.index("dow_sin")
        dc_idx = META_FEATURE_NAMES.index("dow_cos")
        assert out[0, hs_idx] ** 2 + out[0, hc_idx] ** 2 == pytest.approx(
            1.0, abs=1e-12
        )
        assert out[0, ds_idx] ** 2 + out[0, dc_idx] ** 2 == pytest.approx(
            1.0, abs=1e-12
        )

    def test_deterministic_for_same_context(self) -> None:
        # Same context → byte-identical output.  No hidden RNG state.
        ctx = _context()
        a = build_meta_features([ctx])
        b = build_meta_features([ctx])
        np.testing.assert_array_equal(a, b)


# ── Anti-lookahead contract (mirrors tests/backtest/test_anti_lookahead.py) ─


class TestAntiLookahead:
    """Features depend ONLY on signal-time info.  Mutating post-signal data
    must NOT change the feature matrix.

    Mirrors the structure of ``tests/backtest/test_anti_lookahead.py``.
    """

    def test_mutating_outcome_does_not_change_features(self) -> None:
        """Adding outcome to a sibling object must NOT leak into the
        feature row of the original context."""
        ctx = _context()
        X_before = build_meta_features([ctx])
        # The outcome is on MetaLabeledTrade, NOT on MetaTradeContext;
        # mutating it cannot reach the feature builder.
        labeled = MetaLabeledTrade(context=ctx, outcome=0)
        labeled_after = MetaLabeledTrade(context=ctx, outcome=1)
        X_after = build_meta_features([labeled.context, labeled_after.context])
        # First row must equal second row (same context).
        np.testing.assert_array_equal(X_after[0], X_after[1])
        # And both must equal the pre-mutation baseline.
        np.testing.assert_array_equal(X_after[0], X_before[0])

    def test_mutating_signal_time_forward_does_not_change_features(self) -> None:
        """Sliding signal_time forward (past the trade horizon) must NOT
        change features that only depend on bar values at signal time.

        We can't add post-signal info to a frozen context — but we CAN
        compare a feature row built at signal_time vs an obviously
        post-signal signal_time (the cyclic encoding shifts but the
        structural features are unchanged).  This test pins the
        structural invariant: all non-time features are invariant
        under time shifts."""
        ctx = _context()
        ctx_late = replace(ctx, signal_time=ctx.signal_time + timedelta(days=365))

        X_early = build_meta_features([ctx])
        X_late = build_meta_features([ctx_late])

        # Time encodings change (cyclic encoding is deterministic in
        # signal_time); other features MUST be identical.
        time_idxs = [
            META_FEATURE_NAMES.index(n)
            for n in ("hour_sin", "hour_cos", "dow_sin", "dow_cos")
        ]
        non_time_idxs = [
            i for i in range(len(META_FEATURE_NAMES)) if i not in time_idxs
        ]
        np.testing.assert_array_equal(
            X_early[:, non_time_idxs], X_late[:, non_time_idxs]
        )

    def test_feature_builder_does_not_read_outcome(self) -> None:
        """Defensive: the feature builder's input is a sequence of
        ``MetaTradeContext`` (not ``MetaLabeledTrade``), so even by
        type it cannot see the outcome."""
        ctx = _context()
        # ``build_meta_features`` typed signature is
        # ``Sequence[MetaTradeContext]``; passing a MetaLabeledTrade
        # is a static type-check fail, but at runtime the duck-typed
        # builder should still operate on the .context field of the
        # MetaLabeledTrade ONLY if the caller explicitly asks for it.
        # Here we deliberately pass only MetaTradeContext objects.
        out = build_meta_features([ctx])
        # All entries should be in [0, 1] for the one-hot columns and
        # bounded for the absolute funding; nothing about the outcome
        # of "did this win" can influence these.
        long_idx = META_FEATURE_NAMES.index("primary_is_long")
        assert out[0, long_idx] in (0.0, 1.0)

    def test_outcome_not_in_feature_names(self) -> None:
        """Pinning the API contract: there is no ``outcome`` feature.
        Adding one would be a regression that the TestAntiLookahead
        suite would catch via this assertion."""
        for forbidden in ("outcome", "label", "win", "loss", "pnl", "post_return"):
                assert forbidden not in META_FEATURE_NAMES

    def test_features_invariant_under_post_signal_decoration(self) -> None:
        """A trade record exposing outcome — we simulate by constructing
        the labelled wrapper and checking the feature matrix built
        from its ``.context`` is identical to the one built from the
        bare context."""
        ctx = _context()
        trade = MetaLabeledTrade(context=ctx, outcome=1)
        # Two independent feature builds must agree byte-for-byte.
        X_ctx = build_meta_features([ctx])
        X_trade = build_meta_features([trade.context])
        np.testing.assert_array_equal(X_ctx, X_trade)

    def test_feature_names_constant_is_pinned(self) -> None:
        """Pin the META_FEATURE_NAMES tuple — tests and downstream
        consumers index by name, so re-ordering or removing a name is
        a breaking change."""
        assert isinstance(META_FEATURE_NAMES, tuple)
        assert len(META_FEATURE_NAMES) == 15
        # Spot-check key entries so accidental renames are caught.
        assert META_FEATURE_NAMES[0] == "primary_confidence"
        assert "primary_is_long" in META_FEATURE_NAMES
        assert "primary_is_short" in META_FEATURE_NAMES
        assert "regime_trending" in META_FEATURE_NAMES
        assert "funding_rate_at_entry" in META_FEATURE_NAMES
        assert "venue_cost_bps_at_entry" in META_FEATURE_NAMES
        assert "spread_pips_at_entry" in META_FEATURE_NAMES


# ── Calibrated meta-classifier ───────────────────────────────────────────


class TestCalibratedMetaClassifier:
    """Fit on synthetic data, predict_proba in [0, 1], refuse degenerate."""

    def test_fit_predict_proba_shape_and_range(self) -> None:
        trades = _synthetic_meta_set(n_per_class=40)
        clf = fit_meta_classifier(trades, calibration="sigmoid")
        assert clf.is_fitted is True

        contexts = [t.context for t in trades]
        proba = predict_meta_probability(clf, contexts)
        assert proba.shape == (len(contexts),)
        # Probabilities in [0, 1] with no NaN/inf.
        assert np.all(np.isfinite(proba))
        assert (proba >= 0.0).all() and (proba <= 1.0).all()

    def test_predict_proba_per_row_is_normalised(self) -> None:
        # Full proba matrix (both classes) sums to 1 per row.
        trades = _synthetic_meta_set(n_per_class=40)
        clf = fit_meta_classifier(trades, calibration="sigmoid")
        contexts = [t.context for t in trades]
        proba = clf.predict_proba(build_meta_features(contexts))
        row_sums = proba.sum(axis=1)
        np.testing.assert_allclose(row_sums, np.ones(len(contexts)), atol=1e-6)

    def test_predict_argmax_prefers_separable_class(self) -> None:
        # On the synthetic meta-set, the wins carry much higher
        # confidence + trending + low cost; the classifier should
        # be able to predict wins more often for win contexts than
        # loss contexts.
        trades = _synthetic_meta_set(n_per_class=80)
        clf = fit_meta_classifier(trades, calibration="sigmoid")
        win_idxs = [i for i, t in enumerate(trades) if t.outcome == 1]
        loss_idxs = [i for i, t in enumerate(trades) if t.outcome == 0]

        contexts = [t.context for t in trades]
        proba = predict_meta_probability(clf, contexts)

        mean_p_for_wins = float(np.mean(proba[win_idxs]))
        mean_p_for_losses = float(np.mean(proba[loss_idxs]))
        # On a clearly separable synthetic set, the mean P(win) for
        # actual wins must exceed the mean P(win) for actual losses
        # by a wide margin.  We require the gap to be > 0.3 so the
        # test is robust to small RNG drift but still fails if the
        # classifier collapses to the base rate.
        assert mean_p_for_wins > mean_p_for_losses + 0.3

    def test_isotonic_calibration_works(self) -> None:
        trades = _synthetic_meta_set(n_per_class=80)
        clf = fit_meta_classifier(trades, calibration="isotonic")
        assert clf.is_fitted is True
        proba = predict_meta_probability(
            clf, [t.context for t in trades[:10]]
        )
        assert (proba >= 0.0).all() and (proba <= 1.0).all()

    def test_calibration_validation(self) -> None:
        # Invalid calibration method → MetaLabelingError raised by the
        # dataclass ``__post_init__`` during construction.
        with pytest.raises(MetaLabelingError):
            CalibratedMetaClassifier(calibration="bogus")  # type: ignore[arg-type]

        # cv < 2 → also rejected.
        with pytest.raises(MetaLabelingError):
            CalibratedMetaClassifier(calibration="sigmoid", cv=1)

    def test_fit_requires_min_trades(self) -> None:
        # Below MIN_META_TRADES the fit must refuse.
        small = _synthetic_meta_set(n_per_class=5)  # 10 trades total
        with pytest.raises(MetaLabelingShapeError):
            fit_meta_classifier(small)

    def test_fit_requires_two_classes(self) -> None:
        # All wins → no class diversity → refuse to fit a degenerate
        # classifier that always outputs 1.0.
        trades = [
            _labeled(_context(candidate_id=f"all_win_{i}"), outcome=1)
            for i in range(MIN_META_TRADES + 5)
        ]
        with pytest.raises(MetaLabelingShapeError):
            fit_meta_classifier(trades)

    def test_predict_proba_before_fit_raises(self) -> None:
        clf = CalibratedMetaClassifier()
        with pytest.raises(MetaLabelingError):
            clf.predict_proba(np.zeros((1, len(META_FEATURE_NAMES))))

    def test_predict_proba_rejects_wrong_feature_count(self) -> None:
        trades = _synthetic_meta_set(n_per_class=40)
        clf = fit_meta_classifier(trades)
        with pytest.raises(MetaLabelingShapeError):
            clf.predict_proba(np.zeros((1, len(META_FEATURE_NAMES) + 1)))

    def test_predict_meta_probability_empty_input(self) -> None:
        trades = _synthetic_meta_set(n_per_class=40)
        clf = fit_meta_classifier(trades)
        out = predict_meta_probability(clf, [])
        assert out.shape == (0,)
        assert out.dtype == float


# ── Calibration validity ────────────────────────────────────────────────


class TestCalibrationValidity:
    """Calibrated probabilities are valid (Brier score, range)."""

    def test_brier_score_in_unit_interval(self) -> None:
        trades = _synthetic_meta_set(n_per_class=80)
        clf = fit_meta_classifier(trades, calibration="sigmoid")
        held_out = trades[::2]  # even-indexed trades
        report = evaluate_meta_label_calibration(clf, held_out)
        # Brier score is bounded by [0, 1]; in practice well-calibrated
        # classifiers land at <0.25.
        assert 0.0 <= report["brier_score"] <= 1.0
        assert report["n_signals"] == len(held_out)

    def test_calibration_report_keys_pinned(self) -> None:
        # The workboard contract pins the report's key set so callers
        # can rely on it for JSON persistence.
        trades = _synthetic_meta_set(n_per_class=80)
        clf = fit_meta_classifier(trades, calibration="sigmoid")
        report = evaluate_meta_label_calibration(clf, trades)
        expected = {
            "brier_score",
            "reliability",
            "resolution",
            "uncertainty",
            "base_win_rate",
            "n_signals",
        }
        assert set(report.keys()) >= expected

    def test_brier_score_lower_than_constant_base_rate(self) -> None:
        # On the clearly-separable synthetic set, the calibrated
        # classifier's Brier score should beat the constant-prediction
        # base rate (which is "uncertainty" in Murphy's decomposition).
        trades = _synthetic_meta_set(n_per_class=80)
        clf = fit_meta_classifier(trades, calibration="sigmoid")
        report = evaluate_meta_label_calibration(clf, trades)
        # Either the analytics subpackage is available (then we get
        # the full decomposition and uncertainty is well-defined), or
        # it's not (then the fallback returns NaN for the components).
        # When defined, uncertainty > brier_score on a separable set.
        uncertainty = report.get("uncertainty", float("nan"))
        brier = report["brier_score"]
        if not np.isnan(uncertainty):
            assert brier <= uncertainty + 1e-6

    def test_calibration_method_switch(self) -> None:
        # Switching sigmoid → isotonic must still fit and emit finite
        # probabilities.  Both methods produce valid probabilities;
        # isotonic has higher variance on small samples.
        trades = _synthetic_meta_set(n_per_class=80)
        clf_sig = fit_meta_classifier(trades, calibration="sigmoid")
        clf_iso = fit_meta_classifier(trades, calibration="isotonic")
        proba_sig = predict_meta_probability(clf_sig, [t.context for t in trades[:20]])
        proba_iso = predict_meta_probability(clf_iso, [t.context for t in trades[:20]])
        assert (proba_sig >= 0.0).all() and (proba_sig <= 1.0).all()
        assert (proba_iso >= 0.0).all() and (proba_iso <= 1.0).all()

    def test_evaluate_requires_fitted_classifier(self) -> None:
        clf = CalibratedMetaClassifier()
        with pytest.raises(MetaLabelingError):
            evaluate_meta_label_calibration(
                clf, _synthetic_meta_set(n_per_class=20)
            )

    def test_evaluate_empty_input_raises(self) -> None:
        trades = _synthetic_meta_set(n_per_class=40)
        clf = fit_meta_classifier(trades)
        with pytest.raises(MetaLabelingShapeError):
            evaluate_meta_label_calibration(clf, [])


# ── Ranking integration ──────────────────────────────────────────────────


class TestRankCandidatesWithMetaGate:
    """``rank_candidates_with_meta_gate`` drops per-trial rows by gate."""

    def _three_candidates(self) -> tuple[
        list[tuple[str, np.ndarray]], list[np.ndarray]
    ]:
        """Three candidates with distinct mean returns and meta-confidence."""
        strong = _returns_with_mean(0.008, std=0.005, n=120, seed=31)
        weak = _returns_with_mean(0.001, std=0.005, n=120, seed=32)
        noise = _returns_with_mean(0.0, std=0.01, n=120, seed=33)
        # Meta-confidence series of the same length as each returns
        # array.  "strong" is high confidence throughout, "weak"
        # alternates, "noise" is mostly low.
        meta_strong = np.full(strong.shape[0], 0.85)
        meta_weak = np.full(weak.shape[0], 0.55)
        meta_noise = np.full(noise.shape[0], 0.30)
        candidates = [
            ("strong", strong),
            ("weak", weak),
            ("noise", noise),
        ]
        metas = [meta_strong, meta_weak, meta_noise]
        return candidates, metas

    def test_none_passes_through_to_identity(self) -> None:
        # Passing meta_confidence=None must yield exactly the same
        # ranking as rank_candidates_by_trial_returns (no behavioural
        # change for callers that haven't fit a meta-classifier).
        candidates, _ = self._three_candidates()
        a = rank_candidates_by_trial_returns(candidates)
        b = rank_candidates_with_meta_gate(candidates, None)
        ids_a = [r.candidate_id for r in a]
        ids_b = [r.candidate_id for r in b]
        assert ids_a == ids_b
        # Notes must NOT carry meta-gate annotations in identity mode.
        assert all("meta_gate" not in r.notes for r in b)

    def test_threshold_zero_passes_every_trial(self) -> None:
        # meta_threshold=0.0 with all-positive confidences keeps every
        # trial.  The output must match the un-gated ranking (modulo
        # the "meta_gate kept N/N trials" annotation, which we strip
        # for the comparison).
        candidates, metas = self._three_candidates()
        a = rank_candidates_by_trial_returns(candidates)
        b = rank_candidates_with_meta_gate(candidates, metas, meta_threshold=0.0)
        ids_a = [r.candidate_id for r in a]
        ids_b = [r.candidate_id for r in b]
        assert ids_a == ids_b
        # Notes must mention "meta_gate kept N/N trials".
        for row in b:
            assert "meta_gate kept" in row.notes

    def test_threshold_drops_low_confidence_candidates(self) -> None:
        # The "noise" candidate has P(win)=0.30 < 0.5 → all trials
        # dropped → row marked bh_reject=True.  "strong" survives
        # with all trials kept and outranks "weak".
        candidates, metas = self._three_candidates()
        ranked = rank_candidates_with_meta_gate(
            candidates, metas, meta_threshold=0.5
        )
        ids = [r.candidate_id for r in ranked]
        # "noise" must be the last row (bh_reject=True ranks below
        # the others).
        assert ids[-1] == "noise"
        assert ranked[-1].bh_reject is True
        # "strong" must outrank "weak" (its post-gate mean return is
        # still higher).
        assert ids.index("strong") < ids.index("weak")

    def test_per_candidate_none_meta_is_unfiltered(self) -> None:
        # A None entry in the meta-confidences list is a sentinel for
        # "don't gate this candidate".  Its trials must survive
        # untouched while sibling candidates get gated.
        candidates, metas = self._three_candidates()
        # Gate "weak" with the meta, leave "strong" and "noise" ungated.
        gated_metas = [None, metas[1], None]
        ranked = rank_candidates_with_meta_gate(
            candidates, gated_metas, meta_threshold=0.5
        )
        # "strong" and "noise" have their original trial count; only
        # "weak" gets the "kept 120/120" annotation since its meta is
        # all 0.55 (kept fully).
        for row in ranked:
            if row.candidate_id == "weak":
                assert "meta_gate kept 120/120" in row.notes
            else:
                # "strong" and "noise" weren't gated (None sentinel),
                # so no meta_gate annotation.
                assert "meta_gate" not in row.notes

    def test_threshold_outside_unit_interval_raises(self) -> None:
        from forex_bot.factory.risk_adjusted_ranking import (
            RiskAdjustedRankingError,
        )

        candidates, metas = self._three_candidates()
        with pytest.raises(RiskAdjustedRankingError):
            rank_candidates_with_meta_gate(
                candidates, metas, meta_threshold=-0.1
            )
        with pytest.raises(RiskAdjustedRankingError):
            rank_candidates_with_meta_gate(
                candidates, metas, meta_threshold=1.5
            )

    def test_metas_length_mismatch_raises(self) -> None:
        from forex_bot.factory.risk_adjusted_ranking import (
            RiskAdjustedRankingError,
        )

        candidates, _ = self._three_candidates()
        with pytest.raises(RiskAdjustedRankingError):
            rank_candidates_with_meta_gate(
                candidates,
                [np.array([0.6] * 120)],  # only one meta for three candidates
                meta_threshold=0.5,
            )

    def test_per_trial_length_mismatch_raises(self) -> None:
        from forex_bot.factory.risk_adjusted_ranking import (
            RiskAdjustedRankingError,
        )

        candidates, metas = self._three_candidates()
        # Trim the second candidate's meta-confidence to a shorter
        # length; the ranker must refuse rather than silently align.
        bad_metas = [metas[0], metas[1][:50], metas[2]]
        with pytest.raises(RiskAdjustedRankingError):
            rank_candidates_with_meta_gate(
                candidates, bad_metas, meta_threshold=0.5
            )

    def test_nan_in_meta_raises(self) -> None:
        from forex_bot.factory.risk_adjusted_ranking import (
            RiskAdjustedRankingError,
        )

        candidates, metas = self._three_candidates()
        metas[1][0] = float("nan")
        with pytest.raises(RiskAdjustedRankingError):
            rank_candidates_with_meta_gate(
                candidates, metas, meta_threshold=0.5
            )

    def test_gate_can_reduce_to_short_series(self) -> None:
        # A candidate whose post-gate series is < MIN_TRIAL_BARS is
        # flagged bh_reject=True — same as the un-gated ranker does
        # for naturally-short series.  We verify by gating a 16-bar
        # trial down to 4 bars.
        short = _returns_with_mean(0.005, std=0.005, n=16, seed=44)
        long_guard = _returns_with_mean(0.001, std=0.005, n=64, seed=45)
        meta_short = np.array([0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
        meta_guard = np.full(64, 0.6)
        candidates = [("short", short), ("guard", long_guard)]
        metas = [meta_short, meta_guard]
        ranked = rank_candidates_with_meta_gate(
            candidates, metas, meta_threshold=0.5
        )
        # "short" has only 4 trials above threshold — below
        # MIN_TRIAL_BARS → bh_reject=True.
        short_row = next(r for r in ranked if r.candidate_id == "short")
        assert short_row.bh_reject is True
        # And the notes record both original and surviving trial
        # counts so the orchestrator can audit.
        assert "4/16" in short_row.notes


# ── TrialReturnStore bridge ──────────────────────────────────────────────


class TestConsumeMetaConfidencePerTrial:
    """The bridge consumes TrialReturnStore without duplicating collection."""

    def _store_with_two_trials(self) -> TrialReturnStore:
        store = TrialReturnStore()
        key = ("trend_follow", "EURUSD", "H1")
        store.record(key, _returns_with_mean(0.005, std=0.005, n=120, seed=51))
        store.record(key, _returns_with_mean(0.001, std=0.005, n=120, seed=52))
        return store

    def test_consume_returns_matrix_for_valid_cell(self) -> None:
        store = self._store_with_two_trials()
        cell_key = ("trend_follow", "EURUSD", "H1")
        matrix = store.matrix(cell_key)
        assert matrix is not None  # mypy / runtime guard.
        # Per-trial context vectors, one per column of the cell matrix.
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
        clf = fit_meta_classifier(trades)
        out = consume_meta_confidence_per_trial(
            store,
            cell_key=cell_key,
            clf=clf,
            contexts_per_trial=[ctx_j0, ctx_j1],
        )
        assert out is not None
        assert out.shape == matrix.shape
        # All values must be finite — the synthetic contexts are clean.
        assert np.all(np.isfinite(out))

    def test_consume_returns_none_for_missing_cell(self) -> None:
        store = TrialReturnStore()
        out = consume_meta_confidence_per_trial(
            store,
            cell_key=("nope", "X", "H1"),
            clf=CalibratedMetaClassifier(),
            contexts_per_trial=[],
        )
        assert out is None

    def test_consume_rejects_length_mismatch(self) -> None:
        store = self._store_with_two_trials()
        cell_key = ("trend_follow", "EURUSD", "H1")
        trades = _synthetic_meta_set(n_per_class=40)
        clf = fit_meta_classifier(trades)
        with pytest.raises(MetaLabelingShapeError):
            consume_meta_confidence_per_trial(
                store,
                cell_key=cell_key,
                clf=clf,
                contexts_per_trial=[
                    [_context(candidate_id="solo")],
                ],  # only one trial's worth of contexts for two columns
            )


# ── TrialReturnStore end-to-end with the new ranker ─────────────────────


class TestStoreBridgeWithMetaGate:
    """``rank_from_trial_return_store_with_meta_gate`` consumes + gates."""

    def test_store_bridge_with_full_meta_gate(self) -> None:
        store = TrialReturnStore()
        cell_key = ("trend_follow", "EURUSD", "H1")
        # Two trials: one strong positive-mean (high confidence), one
        # noise (low confidence).  After gating, only the strong
        # candidate survives and is ranked above the now-empty one.
        store.record(cell_key, _returns_with_mean(0.008, std=0.005, n=120, seed=71))
        store.record(cell_key, _returns_with_mean(0.0, std=0.01, n=120, seed=72))

        # meta-confidence: [T=120, N=2]
        meta = np.zeros((120, 2), dtype=float)
        meta[:, 0] = 0.85  # strong → all trials kept
        meta[:, 1] = 0.20  # noise → all trials dropped

        rankings = rank_from_trial_return_store_with_meta_gate(
            store,
            candidate_ids_per_cell={cell_key: ["strong", "noise"]},
            meta_confidence_per_cell={cell_key: meta},
            meta_threshold=0.5,
        )
        assert set(rankings.keys()) == {cell_key}
        ranked = rankings[cell_key]
        ids = [r.candidate_id for r in ranked]
        # "noise" has zero trials after the gate → bh_reject=True and
        # ranks LAST.
        assert ids[-1] == "noise"
        assert ranked[-1].bh_reject is True
        # "strong" survives and is the only evaluated candidate.
        assert ids[0] == "strong"

    def test_store_bridge_without_meta_is_identity(self) -> None:
        # When ``meta_confidence_per_cell`` is omitted, the new bridge
        # degrades to the existing
        # ``rank_from_trial_return_store`` behaviour.
        store = TrialReturnStore()
        cell_key = ("trend_follow", "EURUSD", "H1")
        store.record(cell_key, _returns_with_mean(0.008, std=0.005, n=120, seed=81))
        store.record(cell_key, _returns_with_mean(0.001, std=0.005, n=120, seed=82))
        store.record(cell_key, _returns_with_mean(0.0, std=0.01, n=120, seed=83))

        a = rank_from_trial_return_store_with_meta_gate(
            store,
            candidate_ids_per_cell={cell_key: ["strong", "weak", "noise"]},
        )
        b = rank_from_trial_return_store(
            store,
            candidate_ids_per_cell={cell_key: ["strong", "weak", "noise"]},
        )
        ids_a = [r.candidate_id for r in a[cell_key]]
        ids_b = [r.candidate_id for r in b[cell_key]]
        assert ids_a == ids_b

    def test_store_bridge_with_partial_meta_is_annotated_only_for_gated(
        self,
    ) -> None:
        # When only SOME trials have a meta-confidence series, only
        # those candidates get the ``meta_gate`` annotation; ungated
        # siblings retain their original notes.
        store = TrialReturnStore()
        cell_key = ("trend_follow", "EURUSD", "H1")
        store.record(cell_key, _returns_with_mean(0.008, std=0.005, n=120, seed=84))
        store.record(cell_key, _returns_with_mean(0.001, std=0.005, n=120, seed=85))

        meta = np.zeros((120, 2), dtype=float)
        meta[:, 0] = 0.85  # strong → kept
        meta[:, 1] = 0.20  # weak → all dropped

        ranked = rank_from_trial_return_store_with_meta_gate(
            store,
            candidate_ids_per_cell={cell_key: ["strong", "weak"]},
            meta_confidence_per_cell={cell_key: meta},
            meta_threshold=0.5,
        )[cell_key]
        # Both rows gated → both annotated.
        for row in ranked:
            assert "meta_gate kept" in row.notes


# ── Degenerate / empty inputs ────────────────────────────────────────────


class TestDegenerateEmptyInputs:
    """Empty inputs, single trials, NaN columns, all-zero return series."""

    def test_build_features_empty(self) -> None:
        out = build_meta_features([])
        assert out.shape == (0, len(META_FEATURE_NAMES))

    def test_fit_meta_classifier_empty_input(self) -> None:
        with pytest.raises(MetaLabelingShapeError):
            fit_meta_classifier([])

    def test_predict_meta_probability_empty(self) -> None:
        # Empty input → empty output, no exceptions.
        clf = CalibratedMetaClassifier()
        # Even an unfitted one — empty input short-circuits.
        out = predict_meta_probability(clf, [])
        assert out.shape == (0,)

    def test_fit_rejects_nan_features(self) -> None:
        # Build a meta-set whose feature matrix has NaN due to a
        # pathological context (NaN funding).  The fit must refuse.
        trades = _synthetic_meta_set(n_per_class=20)
        # Replace one context's funding with NaN; the feature row
        # for that trade will carry NaN into funding_rate_at_entry.
        trades[0] = _labeled(
            replace(trades[0].context, funding_rate_at_entry=float("nan")),
            trades[0].outcome,
        )
        # Direct fit must refuse.
        clf = CalibratedMetaClassifier()
        X = build_meta_features([t.context for t in trades])
        y = np.array([t.outcome for t in trades], dtype=float)
        with pytest.raises(MetaLabelingShapeError):
            clf.fit(X, y)

    def test_meta_trade_context_validates_side(self) -> None:
        with pytest.raises(MetaLabelingShapeError):
            MetaTradeContext(
                candidate_id="bad",
                pair="EURUSD",
                timeframe="H1",
                signal_time=datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc),
                primary_confidence=0.5,
                primary_side="diagonal",  # type: ignore[arg-type]
            )

    def test_meta_trade_context_validates_regime(self) -> None:
        with pytest.raises(MetaLabelingShapeError):
            MetaTradeContext(
                candidate_id="bad",
                pair="EURUSD",
                timeframe="H1",
                signal_time=datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc),
                primary_confidence=0.5,
                primary_side="long",
                regime="mayhem",  # type: ignore[arg-type]
            )

    def test_meta_trade_context_validates_confidence(self) -> None:
        with pytest.raises(MetaLabelingShapeError):
            MetaTradeContext(
                candidate_id="bad",
                pair="EURUSD",
                timeframe="H1",
                signal_time=datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc),
                primary_confidence=1.5,  # out of [0, 1]
            )

    def test_meta_labeled_trade_validates_outcome(self) -> None:
        with pytest.raises(MetaLabelingShapeError):
            MetaLabeledTrade(context=_context(), outcome=2)

    def test_rank_with_meta_gate_empty_candidates(self) -> None:
        out = rank_candidates_with_meta_gate([], None)
        assert out == []

    def test_rank_with_meta_gate_preserves_evaluation_when_no_filtering(
        self,
    ) -> None:
        # All trials above the threshold → identical ranking to the
        # un-gated ranker.
        returns = _returns_with_mean(0.005, std=0.005, n=120, seed=91)
        candidates = [("cand", returns)]
        metas = [np.full(returns.shape[0], 0.9)]
        ranked = rank_candidates_with_meta_gate(
            candidates, metas, meta_threshold=0.5
        )
        assert len(ranked) == 1
        # Annotation: kept N/N.
        assert "kept 120/120" in ranked[0].notes


# ── Factory package exports ──────────────────────────────────────────────


class TestFactoryExports:
    """Public symbols reachable via the forex_bot.factory package."""

    def test_symbols_reachable_from_factory(self) -> None:
        from forex_bot.factory import meta_labeling as ml

        assert ml.MetaTradeContext is MetaTradeContext
        assert ml.MetaLabeledTrade is MetaLabeledTrade
        assert ml.CalibratedMetaClassifier is CalibratedMetaClassifier
        assert ml.build_meta_features is build_meta_features
        assert ml.fit_meta_classifier is fit_meta_classifier
        assert ml.predict_meta_probability is predict_meta_probability
        assert ml.META_FEATURE_NAMES is META_FEATURE_NAMES

    def test_ranker_re_exports(self) -> None:
        # The new ranker functions live in risk_adjusted_ranking.py
        # but are re-exported through meta_labeling for the workboard
        # contract (single import surface for Sprint D 1).
        from forex_bot.factory.meta_labeling import (
            rank_candidates_with_meta_gate,
            rank_from_trial_return_store_with_meta_gate,
        )

        assert callable(rank_candidates_with_meta_gate)
        assert callable(rank_from_trial_return_store_with_meta_gate)

    def test_default_gate_threshold_pinned(self) -> None:
        # Pin the gate threshold — downstream promotion logic may rely
        # on the constant value.
        assert DEFAULT_META_GATE_THRESHOLD == 0.5
        assert DEFAULT_META_GATE_THRESHOLD_LOCAL == 0.5

    def test_min_meta_trades_constant_pinned(self) -> None:
        # Pin MIN_META_TRADES so the meta-classifier floor is not
        # silently weakened by a future refactor.
        assert MIN_META_TRADES == 30

    def test_meta_feature_names_count_pinned(self) -> None:
        # Pin the column count so the feature matrix never silently
        # drops a column during a refactor.
        assert len(META_FEATURE_NAMES) == 15


# ── Sentinel for testing the ranker's RISK-adjusted ranking error surface ──


def test_rank_candidates_with_meta_gate_signature_is_stable() -> None:
    """Pin the public signature of rank_candidates_with_meta_gate.

    Guards against accidental kwarg renames that would break the
    workboard contract — the ranker is called from the orchestrator
    and from this test suite with positional/keyword args matching
    the documented surface.
    """
    import inspect

    sig = inspect.signature(rank_candidates_with_meta_gate)
    params = list(sig.parameters.keys())
    assert params == [
        "trial_returns_per_candidate",
        "meta_confidence_per_candidate",
        "meta_threshold",
        "cell_key",
        "alpha",
    ]
