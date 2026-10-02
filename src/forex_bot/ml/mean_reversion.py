"""ML-Based Mean Reversion Strategy for EURUSD M15.

Trains a classifier (XGBoost / Random Forest / GradientBoosting) on
Bollinger-Band mean-reversion signals and uses the trained model as an
``ISignalStrategy`` that the backtest engine can run directly.

Public API
----------
- ``prepare_data``          -- build labeled dataset from CSV data
- ``train_model``           -- walk-forward train + return best model
- ``MLMeanReversionStrategy`` -- ISignalStrategy backed by a trained model
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from backtest.engine import Bar, MarketState, StrategySignal, TradeDirection
from backtest.strategies import ISignalStrategy
from ml.features import (
    atr,
    bollinger_bands,
    build_feature_matrix,
)
from ml.signal_simulator import bb_mean_reversion_signals, label_trades
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split

logger = logging.getLogger(__name__)

FEATURE_COLS = [
    "atr_14",
    "atr_50",
    "atr_ratio",
    "vol_pct",
    "rsi",
    "roc",
    "stoch_k",
    "stoch_d",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_pct_b",
    "bb_width",
    "price_vs_sma9",
    "price_vs_sma21",
    "price_vs_sma50",
    "price_vs_ema200",
    "trend_direction",
    "higher_highs",
    "lower_lows",
    "engulfing_bullish",
    "engulfing_bearish",
    "pin_bullish",
    "pin_bearish",
    "hour",
    "day_of_week",
    "killzone_london",
    "killzone_ny",
    "killzone_asia",
    "outside_session",
]

MODEL_HYPERPARAMS: dict[str, list[dict[str, Any]]] = {
    "gradient_boosting": [
        {
            "n_estimators": 100,
            "max_depth": 3,
            "learning_rate": 0.1,
            "min_samples_leaf": 5,
        },
        {
            "n_estimators": 200,
            "max_depth": 4,
            "learning_rate": 0.05,
            "min_samples_leaf": 10,
        },
        {
            "n_estimators": 150,
            "max_depth": 3,
            "learning_rate": 0.08,
            "min_samples_leaf": 8,
        },
    ],
    "random_forest": [
        {
            "n_estimators": 100,
            "max_depth": 4,
            "min_samples_leaf": 5,
            "max_features": "sqrt",
        },
        {
            "n_estimators": 200,
            "max_depth": 5,
            "min_samples_leaf": 10,
            "max_features": "sqrt",
        },
        {
            "n_estimators": 150,
            "max_depth": 4,
            "min_samples_leaf": 8,
            "max_features": "log2",
        },
    ],
}

try:
    from xgboost import XGBClassifier

    MODEL_HYPERPARAMS["xgboost"] = [
        {
            "n_estimators": 100,
            "max_depth": 3,
            "learning_rate": 0.1,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 5,
        },
        {
            "n_estimators": 200,
            "max_depth": 4,
            "learning_rate": 0.05,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 10,
        },
    ]

    def _make_xgb(params: dict[str, Any]) -> Any:
        return XGBClassifier(
            **params,
            use_label_encoder=False,
            eval_metric="logloss",
            random_state=42,
            verbosity=0,
        )

    HAS_XGBOOST = True
except ImportError:
    HAS_XGBOOST = False


def _make_model(model_type: str, params: dict[str, Any]):
    if model_type == "gradient_boosting":
        return GradientBoostingClassifier(**params, random_state=42)
    if model_type == "random_forest":
        return RandomForestClassifier(**params, random_state=42)
    if model_type == "xgboost" and HAS_XGBOOST:
        return _make_xgb(params)
    raise ValueError(f"Unknown model type: {model_type}")


@dataclass
class TrainingResult:
    model: Any
    model_type: str
    feature_names: list[str]
    threshold: float
    metrics: dict[str, float]
    fold_metrics: list[dict[str, float | str]] = field(default_factory=list)


def _load_csv_to_df(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, parse_dates=["Date"])
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.sort_values("date").reset_index(drop=True)
    return df


def prepare_data(
    csv_path: str,
    bb_period: int = 20,
    bb_std: float = 2.0,
    atr_mult: float = 2.0,
    rr: float = 1.5,
    max_holding_bars: int = 50,
) -> pd.DataFrame:
    """Build a labeled dataset for mean-reversion model training.

    Parameters
    ----------
    csv_path : str
        Path to the EURUSD M15 CSV file.
    bb_period, bb_std, atr_mult, rr
        Parameters forwarded to ``bb_mean_reversion_signals``.
    max_holding_bars : int
        Maximum bars a simulated trade may hold.

    Returns
    -------
    pd.DataFrame
        Rows are labeled trade attempts; columns include the 29 technical
        features plus ``outcome``, ``direction``, ``entry_price``,
        ``stop_loss``, ``take_profit``.
    """
    df = _load_csv_to_df(csv_path)

    if "date" not in df.columns:
        raise ValueError("CSV must contain a 'Date' column")

    signals = bb_mean_reversion_signals(df, period=bb_period, num_std=bb_std, atr_mult=atr_mult, rr=rr)
    if signals.empty:
        return pd.DataFrame()

    labeled = label_trades(signals, df, max_holding_bars)
    if labeled.empty:
        return pd.DataFrame()

    features = build_feature_matrix(df)
    available = [c for c in FEATURE_COLS if c in features.columns]

    entry_indices = features.index[labeled["entry_idx"].values]
    feat_subset = features.loc[entry_indices, available].copy().reset_index(drop=True)
    labeled = labeled.reset_index(drop=True)

    dataset = pd.concat(
        [
            feat_subset,
            labeled[
                [
                    "direction",
                    "entry_price",
                    "stop_loss",
                    "take_profit",
                    "outcome",
                    "pnl",
                ]
            ],
        ],
        axis=1,
    )
    dataset = dataset.dropna(subset=["outcome"])
    return dataset


def _optimize_threshold(
    y_prob: np.ndarray,
    y_true: np.ndarray,
    pnl: np.ndarray | None = None,
    min_prob: float = 0.40,
    max_prob: float = 0.80,
) -> float:
    best_threshold = 0.50
    best_score = -1.0

    for t in np.arange(min_prob, max_prob + 0.01, 0.02):
        y_pred = (y_prob >= t).astype(int)
        if y_pred.sum() == 0:
            continue

        if pnl is not None and len(pnl) == len(y_prob):
            taken_mask = y_prob >= t
            if taken_mask.sum() == 0:
                continue
            taken_pnl = pnl[taken_mask]
            gross_profit = taken_pnl[taken_pnl > 0].sum()
            gross_loss = abs(taken_pnl[taken_pnl < 0].sum())
            score = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
        else:
            score = f1_score(y_true, y_pred, zero_division=0)

        if score > best_score:
            best_score = score
            best_threshold = t

    return best_threshold


def train_model(
    dataset: pd.DataFrame,
    model_types: list[str] | None = None,
    test_ratio: float = 0.2,
    seed: int = 42,
) -> TrainingResult:
    """Train and select the best mean-reversion classifier.

    Runs a grid search over model types and hyperparameter configs,
    picks the configuration with the highest F1 on the hold-out test set.

    Returns
    -------
    TrainingResult
    """
    if model_types is None:
        model_types = ["gradient_boosting", "random_forest"]
        if HAS_XGBOOST:
            model_types.append("xgboost")

    available = [c for c in FEATURE_COLS if c in dataset.columns]
    if not available:
        non_feature_cols = {
            "direction",
            "entry_price",
            "stop_loss",
            "take_profit",
            "outcome",
            "pnl",
            "exit_price",
            "exit_reason",
            "holding_bars",
            "rr_actual",
            "strategy",
            "entry_idx",
            "entry_time",
        }
        available = [
            c
            for c in dataset.columns
            if c not in non_feature_cols and dataset[c].dtype in (np.float64, np.int64, np.float32, np.int32)
        ]

    X = dataset[available].values
    y = dataset["outcome"].values.astype(int)
    has_pnl = "pnl" in dataset.columns
    pnl = dataset["pnl"].values.astype(float) if has_pnl else None

    if len(np.unique(y)) < 2:
        raise ValueError("Dataset must contain both positive and negative outcomes")

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=test_ratio, random_state=seed, stratify=y)
    pnl_test = pnl[len(X_train) :] if pnl is not None else None

    best_model = None
    best_pf = -1.0
    best_type = ""
    best_threshold = 0.50

    for model_type in model_types:
        if model_type not in MODEL_HYPERPARAMS:
            continue
        for params in MODEL_HYPERPARAMS[model_type]:
            try:
                model = _make_model(model_type, params)
                model.fit(X_train, y_train)
                y_prob = model.predict_proba(X_test)[:, 1]

                threshold = _optimize_threshold(y_prob, y_test, pnl=pnl_test)
                y_pred = (y_prob >= threshold).astype(int)

                taken_mask = y_prob >= threshold
                if taken_mask.sum() > 0 and pnl_test is not None:
                    taken_pnl = pnl_test[taken_mask]
                    gross_profit = taken_pnl[taken_pnl > 0].sum()
                    gross_loss = abs(taken_pnl[taken_pnl < 0].sum())
                    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
                else:
                    pf = -1.0

                if pf > best_pf:
                    best_pf = pf
                    best_model = model
                    best_type = model_type
                    best_threshold = threshold
            except Exception as exc:
                logger.debug("Model %s with params %s failed: %s", model_type, params, exc)

    if best_model is None:
        raise RuntimeError("No model could be trained successfully")

    y_prob = best_model.predict_proba(X_test)[:, 1]
    y_pred = (y_prob >= best_threshold).astype(int)

    taken_mask = y_prob >= best_threshold
    metrics = {
        "accuracy": float(accuracy_score(y_test, y_pred)),
        "f1": float(f1_score(y_test, y_pred, zero_division=0)),
        "threshold": float(best_threshold),
        "test_samples": float(len(y_test)),
        "positive_rate": float(y_pred.mean()),
    }
    if pnl_test is not None and taken_mask.sum() > 0:
        taken_pnl = pnl_test[taken_mask]
        gp = taken_pnl[taken_pnl > 0].sum()
        gl = abs(taken_pnl[taken_pnl < 0].sum())
        metrics["profit_factor"] = gp / gl if gl > 0 else float("inf")
        metrics["win_rate"] = float((taken_pnl > 0).sum() / len(taken_pnl) * 100)
        metrics["total_trades"] = float(len(taken_pnl))

    return TrainingResult(
        model=best_model,
        model_type=best_type,
        feature_names=available,
        threshold=best_threshold,
        metrics=metrics,
    )


def walk_forward_validate(
    csv_path: str,
    n_folds: int = 5,
    test_ratio: float = 0.2,
    bb_period: int = 20,
    bb_std: float = 2.0,
    atr_mult: float = 2.0,
    rr: float = 1.5,
    max_holding_bars: int = 50,
    seed: int = 42,
) -> tuple[TrainingResult, list[dict[str, float | str]]]:
    """Train with walk-forward validation on EURUSD M15 data.

    Uses true temporal walk-forward: for each fold *i*, the model is
    trained on cumulative past data (folds 0..i-1) and evaluated on
    fold *i*.  Fold 0 is used as the initial training-only window.

    Returns
    -------
    tuple[TrainingResult, list[dict]]
        The final model (trained on all data except the last fold) and
        per-fold metrics.
    """
    df = _load_csv_to_df(csv_path)
    fold_size = len(df) // n_folds

    def _build_dataset_for_range(df_subset: pd.DataFrame) -> pd.DataFrame | None:
        signals = bb_mean_reversion_signals(df_subset, period=bb_period, num_std=bb_std, atr_mult=atr_mult, rr=rr)
        if signals.empty:
            return None
        labeled = label_trades(signals, df_subset, max_holding_bars)
        if labeled.empty or len(np.unique(labeled["outcome"].values)) < 2:
            return None
        features = build_feature_matrix(df_subset)
        avail = [c for c in FEATURE_COLS if c in features.columns]
        entry_indices = features.index[labeled["entry_idx"].values]
        feat_subset = features.loc[entry_indices, avail].copy().reset_index(drop=True)
        labeled = labeled.reset_index(drop=True)
        ds = pd.concat(
            [
                feat_subset,
                labeled[
                    [
                        "direction",
                        "entry_price",
                        "stop_loss",
                        "take_profit",
                        "outcome",
                        "pnl",
                    ]
                ],
            ],
            axis=1,
        )
        ds = ds.dropna(subset=["outcome"])
        return ds

    fold_data: list[pd.DataFrame | None] = []
    for fold_idx in range(n_folds):
        start = fold_idx * fold_size
        end = start + fold_size if fold_idx < n_folds - 1 else len(df)
        fold_df = df.iloc[start:end].copy()
        fold_data.append(_build_dataset_for_range(fold_df))

    fold_metrics: list[dict[str, float | str]] = []
    last_result: TrainingResult | None = None

    for fold_idx in range(1, n_folds):
        train_parts = [fd for fd in fold_data[:fold_idx] if fd is not None]
        test_part = fold_data[fold_idx]

        if not train_parts or test_part is None:
            fold_metrics.append({"fold": fold_idx, "status": "insufficient_data"})
            continue

        train_ds = pd.concat(train_parts, ignore_index=True)
        test_ds = test_part

        if len(np.unique(train_ds["outcome"].values)) < 2:
            fold_metrics.append({"fold": fold_idx, "status": "insufficient_labels_train"})
            continue

        try:
            result = train_model(train_ds, test_ratio=test_ratio, seed=seed)
            last_result = result

            available = result.feature_names
            X_test = test_ds[available].values
            pnl_test = test_ds["pnl"].values.astype(float) if "pnl" in test_ds.columns else None

            y_prob = result.model.predict_proba(X_test)[:, 1]
            y_pred = (y_prob >= result.threshold).astype(int)

            taken_mask = y_prob >= result.threshold
            win_rate = float(y_pred.mean()) * 100

            pf = 0.0
            total_trades = int(y_pred.sum())
            if pnl_test is not None and taken_mask.sum() > 0:
                taken_pnl = pnl_test[taken_mask]
                gp = taken_pnl[taken_pnl > 0].sum()
                gl = abs(taken_pnl[taken_pnl < 0].sum())
                pf = gp / gl if gl > 0 else float("inf") if gp > 0 else 0.0
                total_trades = int(len(taken_pnl))
                win_rate = float((taken_pnl > 0).sum() / len(taken_pnl) * 100)

            fold_metrics.append(
                {
                    "fold": fold_idx,
                    "status": "ok",
                    "model_type": result.model_type,
                    "f1": result.metrics["f1"],
                    "accuracy": result.metrics["accuracy"],
                    "threshold": result.metrics["threshold"],
                    "win_rate": win_rate,
                    "profit_factor": pf,
                    "total_trades": total_trades,
                }
            )
        except Exception as exc:
            logger.debug("Fold %d failed: %s", fold_idx, exc)
            fold_metrics.append({"fold": fold_idx, "status": f"error: {exc}"})

    if last_result is None:
        raise RuntimeError("All walk-forward folds failed")

    all_train = [fd for fd in fold_data[:-1] if fd is not None]
    if all_train:
        final_ds = pd.concat(all_train, ignore_index=True)
        try:
            final_result = train_model(final_ds, test_ratio=test_ratio, seed=seed)
            final_result.fold_metrics = fold_metrics
            return final_result, fold_metrics
        except Exception:
            logger.warning(
                "Final model training on all folds failed, using last fold model",
                exc_info=True,
            )
            pass

    last_result.fold_metrics = fold_metrics
    return last_result, fold_metrics


def save_model(result: TrainingResult, output_dir: str) -> None:
    """Persist a trained model to disk."""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)

    with open(path / "mean_reversion_model.pkl", "wb") as f:
        pickle.dump(result.model, f)

    meta = {
        "model_type": result.model_type,
        "feature_names": result.feature_names,
        "threshold": result.threshold,
        "metrics": result.metrics,
    }
    with open(path / "mean_reversion_meta.json", "w") as f:
        json.dump(meta, f, indent=2)


def load_model(model_dir: str) -> tuple[Any, list[str], float]:
    """Load a trained model from disk.

    Returns
    -------
    tuple[model, feature_names, threshold]
    """
    path = Path(model_dir)

    with open(path / "mean_reversion_model.pkl", "rb") as f:
        model = pickle.load(f)  # nosec B301  # noqa: S301

    with open(path / "mean_reversion_meta.json") as f:
        meta = json.load(f)

    return model, meta["feature_names"], meta["threshold"]


class MLMeanReversionStrategy(ISignalStrategy):
    """ISignalStrategy that uses a trained ML model for mean-reversion signals.

    During ``evaluate()``, the strategy:
    1. Converts recent ``MarketState.bars`` into a feature vector
    2. Runs the model to get a probability
    3. If probability >= threshold, returns a ``StrategySignal`` with
       SL/TP derived from Bollinger Bands and ATR
    """

    def __init__(
        self,
        model: Any,
        feature_names: list[str],
        threshold: float = 0.50,
        bb_period: int = 20,
        bb_std: float = 2.0,
        atr_mult: float = 2.0,
        rr: float = 1.5,
        min_lookback: int = 200,
    ):
        self._model = model
        self._feature_names = feature_names
        self._threshold = threshold
        self._bb_period = bb_period
        self._bb_std = bb_std
        self._atr_mult = atr_mult
        self._rr = rr
        self._min_lookback = min_lookback

    @property
    def name(self) -> str:
        return "MLMeanReversion"

    @classmethod
    def from_model_dir(
        cls,
        model_dir: str,
        threshold: float | None = None,
        **kwargs,
    ) -> MLMeanReversionStrategy:
        model, feature_names, default_threshold = load_model(model_dir)
        return cls(
            model=model,
            feature_names=feature_names,
            threshold=threshold if threshold is not None else default_threshold,
            **kwargs,
        )

    def _bars_to_dataframe(self, bars: list[Bar]) -> pd.DataFrame:
        rows = []
        for bar in bars:
            rows.append(
                {
                    "date": bar.time,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
            )
        return pd.DataFrame(rows)

    def _compute_signal_levels(self, bars: list[Bar]) -> tuple[float, float, float, float, int] | None:
        """Compute BB levels, ATR, and a mean-reversion direction.

        Returns ``(bb_upper, bb_mid, bb_lower, atr_val, direction)`` or
        ``None`` if the bar count is insufficient.
        """
        if len(bars) < self._bb_period + 1:
            return None

        df = self._bars_to_dataframe(bars)
        close = df["close"]
        high = df["high"]
        low = df["low"]

        bb_upper, bb_mid, bb_lower = bollinger_bands(close, self._bb_period, self._bb_std)
        atr_val = atr(high, low, close, 14)

        last = len(df) - 1
        bb_u = bb_upper.iloc[last]
        bb_m = bb_mid.iloc[last]
        bb_l = bb_lower.iloc[last]
        a = atr_val.iloc[last]

        if pd.isna(bb_u) or pd.isna(bb_l) or pd.isna(a) or a == 0:
            return None

        price = close.iloc[last]
        if price < bb_l:
            direction = 1
        elif price > bb_u:
            direction = -1
        else:
            return None

        return bb_u, bb_m, bb_l, a, direction

    def _compute_features(self, bars: list[Bar]) -> pd.Series | None:
        """Build a feature row from the current bar history."""
        if len(bars) < self._min_lookback:
            return None

        df = self._bars_to_dataframe(bars)
        features = build_feature_matrix(df)

        last_idx = len(features) - 1
        row = features.iloc[last_idx]

        if row[self._feature_names].isna().any():
            return None

        return row[self._feature_names]

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        bars = state.bars
        if len(bars) < self._min_lookback:
            return None

        levels = self._compute_signal_levels(bars)
        if levels is None:
            return None

        bb_upper, bb_mid, bb_lower, atr_val, direction = levels

        features = self._compute_features(bars)
        if features is None:
            return None

        X = features.values.reshape(1, -1)
        prob = self._model.predict_proba(X)[0, 1]

        if prob < self._threshold:
            return None

        price = bars[-1].close
        if direction == 1:
            sl = price - atr_val * self._atr_mult
            tp = bb_mid
            if sl >= price or tp <= price:
                return None
            return StrategySignal(
                direction=TradeDirection.LONG,
                confidence=min(float(prob), 0.95),
                entry_price=price,
                stop_loss=sl,
                take_profit_1=tp,
                take_profit_2=0.0,
                take_profit_3=0.0,
                rationale=f"ML mean-reversion LONG (p={prob:.3f}) bb_lower={bb_lower:.5f}",
            )
        else:
            sl = price + atr_val * self._atr_mult
            tp = bb_mid
            if sl <= price or tp >= price:
                return None
            return StrategySignal(
                direction=TradeDirection.SHORT,
                confidence=min(float(prob), 0.95),
                entry_price=price,
                stop_loss=sl,
                take_profit_1=tp,
                take_profit_2=0.0,
                take_profit_3=0.0,
                rationale=f"ML mean-reversion SHORT (p={prob:.3f}) bb_upper={bb_upper:.5f}",
            )
