from __future__ import annotations

import json
import os
import pickle  # nosec B403
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split

from .features import (
    ICT_FEATURE_NAMES,
    add_multi_timeframe_features,
    build_feature_matrix,
    load_csv,
)
from .signal_simulator import build_labeled_dataset

try:
    from xgboost import XGBClassifier

    _HAS_XGBOOST = True
except ImportError:
    _HAS_XGBOOST = False

FEATURE_COLUMNS = [
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
    "h4_trend",
    "h4_sma21_dist",
    "d1_trend",
    "d1_ema200_dist",
    "tf_alignment",
] + ICT_FEATURE_NAMES

MODEL_TYPE_DEFAULT = "gradient_boosting"

MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "gradient_boosting": {
        "display_name": "GradientBoostingClassifier",
        "factory": lambda params, rs: GradientBoostingClassifier(
            n_estimators=params["n_estimators"],
            max_depth=params["max_depth"],
            learning_rate=params["learning_rate"],
            min_samples_leaf=params["min_samples_leaf"],
            subsample=0.8,
            random_state=rs,
        ),
        "param_grid": [
            {
                "n_estimators": 100,
                "max_depth": 3,
                "learning_rate": 0.05,
                "min_samples_leaf": 20,
            },
            {
                "n_estimators": 200,
                "max_depth": 4,
                "learning_rate": 0.05,
                "min_samples_leaf": 15,
            },
            {
                "n_estimators": 150,
                "max_depth": 3,
                "learning_rate": 0.1,
                "min_samples_leaf": 20,
            },
            {
                "n_estimators": 200,
                "max_depth": 5,
                "learning_rate": 0.05,
                "min_samples_leaf": 10,
            },
            {
                "n_estimators": 100,
                "max_depth": 4,
                "learning_rate": 0.1,
                "min_samples_leaf": 15,
            },
        ],
    },
    "random_forest": {
        "display_name": "RandomForestClassifier",
        "factory": lambda params, rs: RandomForestClassifier(
            n_estimators=params["n_estimators"],
            max_depth=params["max_depth"],
            min_samples_leaf=params["min_samples_leaf"],
            max_features=params.get("max_features", "sqrt"),
            random_state=rs,
            n_jobs=-1,
        ),
        "param_grid": [
            {
                "n_estimators": 100,
                "max_depth": 5,
                "min_samples_leaf": 20,
                "max_features": "sqrt",
            },
            {
                "n_estimators": 200,
                "max_depth": 8,
                "min_samples_leaf": 10,
                "max_features": "sqrt",
            },
            {
                "n_estimators": 150,
                "max_depth": 6,
                "min_samples_leaf": 15,
                "max_features": 0.5,
            },
            {
                "n_estimators": 300,
                "max_depth": 10,
                "min_samples_leaf": 5,
                "max_features": "sqrt",
            },
            {
                "n_estimators": 100,
                "max_depth": 4,
                "min_samples_leaf": 25,
                "max_features": 0.3,
            },
        ],
    },
}

if _HAS_XGBOOST:
    MODEL_REGISTRY["xgboost"] = {
        "display_name": "XGBClassifier",
        "factory": lambda params, rs: XGBClassifier(
            n_estimators=params["n_estimators"],
            max_depth=params["max_depth"],
            learning_rate=params["learning_rate"],
            subsample=params.get("subsample", 0.8),
            colsample_bytree=params.get("colsample_bytree", 0.8),
            min_child_weight=params.get("min_child_weight", 10),
            random_state=rs,
            use_label_encoder=False,
            eval_metric="logloss",
            tree_method="hist",
        ),
        "param_grid": [
            {
                "n_estimators": 100,
                "max_depth": 3,
                "learning_rate": 0.05,
                "subsample": 0.8,
                "colsample_bytree": 0.8,
                "min_child_weight": 10,
            },
            {
                "n_estimators": 200,
                "max_depth": 4,
                "learning_rate": 0.05,
                "subsample": 0.8,
                "colsample_bytree": 1.0,
                "min_child_weight": 5,
            },
            {
                "n_estimators": 150,
                "max_depth": 3,
                "learning_rate": 0.1,
                "subsample": 0.9,
                "colsample_bytree": 0.8,
                "min_child_weight": 10,
            },
            {
                "n_estimators": 200,
                "max_depth": 5,
                "learning_rate": 0.05,
                "subsample": 0.7,
                "colsample_bytree": 0.7,
                "min_child_weight": 5,
            },
            {
                "n_estimators": 100,
                "max_depth": 4,
                "learning_rate": 0.1,
                "subsample": 0.8,
                "colsample_bytree": 0.9,
                "min_child_weight": 15,
            },
        ],
    }


def available_model_types() -> list[str]:
    return list(MODEL_REGISTRY.keys())


def prepare_dataset(symbol: str, data_dir: str, timeframe: str = "H1", max_holding_bars: int = 50) -> pd.DataFrame:
    tf_map = {"M15": "M15", "H1": "H1", "H4": "H4", "D1": "D1"}
    tf_file = tf_map.get(timeframe, timeframe)

    csv_path = os.path.join(data_dir, "historical", f"{symbol}_{tf_file}.csv")
    df = load_csv(csv_path)

    features = build_feature_matrix(df)

    h4_path = os.path.join(data_dir, "historical", f"{symbol}_H4.csv")
    d1_path = os.path.join(data_dir, "historical", f"{symbol}_D1.csv")

    h4_df = load_csv(h4_path) if os.path.exists(h4_path) else None
    d1_df = load_csv(d1_path) if os.path.exists(d1_path) else None

    if h4_df is not None and d1_df is not None:
        features = add_multi_timeframe_features(features, h4_df, d1_df)

    [f for f in FEATURE_COLUMNS if f in features.columns]

    dataset = build_labeled_dataset(df, features, max_holding_bars)
    if dataset.empty:
        return pd.DataFrame()

    return dataset


def train_single_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    random_state: int = 42,
    model_type: str = MODEL_TYPE_DEFAULT,
) -> dict:
    if model_type not in MODEL_REGISTRY:
        raise ValueError(f"Unknown model type '{model_type}'. Available: {available_model_types()}")

    registry = MODEL_REGISTRY[model_type]
    best_model = None
    best_f1 = 0
    best_params = None

    for params in registry["param_grid"]:
        model = registry["factory"](params, random_state)
        model.fit(X_train, y_train)

        y_pred = model.predict(X_val)
        f1 = f1_score(y_val, y_pred, zero_division=0)

        if f1 > best_f1:
            best_f1 = f1
            best_model = model
            best_params = params

    return {
        "model": best_model,
        "params": best_params,
        "f1": best_f1,
        "model_type": model_type,
    }


def optimize_threshold(y_prob: np.ndarray, test_trades: pd.DataFrame, risk: np.ndarray) -> tuple[float, dict]:
    best_threshold = 0.5
    best_score = -999
    best_metrics = {}

    for threshold in np.arange(0.35, 0.85, 0.05):
        mask = y_prob >= threshold
        if mask.sum() < 10:
            continue

        mask_indices = np.where(mask)[0]
        filtered = test_trades.iloc[mask_indices]
        if len(filtered) < 10:
            continue

        wins = filtered[filtered["outcome"] == 1]
        losses = filtered[filtered["outcome"] == 0]

        if len(wins) == 0 or len(losses) == 0:
            continue

        total_win_pnl = wins["pnl"].sum()
        total_loss_pnl = abs(losses["pnl"].sum())
        pf = total_win_pnl / total_loss_pnl if total_loss_pnl > 0 else 0
        wr = len(wins) / len(filtered)
        total_pnl = filtered["pnl"].sum()
        trade_count = len(filtered)

        score = pf * np.log(trade_count + 1) + total_pnl * 0.001

        if score > best_score:
            best_score = score
            best_threshold = threshold
            best_metrics = {
                "threshold": round(float(threshold), 2),
                "win_rate": round(wr * 100, 2),
                "profit_factor": round(pf, 2),
                "total_pnl": round(float(total_pnl), 4),
                "trade_count": trade_count,
                "filter_rate": round(1 - mask.sum() / len(y_prob), 4),
            }

    return best_threshold, best_metrics


def evaluate_model(
    model,
    X_test: np.ndarray,
    y_test: np.ndarray,
    test_trades: pd.DataFrame,
    feature_names: list,
) -> dict:
    y_prob = model.predict_proba(X_test)[:, 1]

    opt_threshold, opt_metrics = optimize_threshold(y_prob, test_trades, np.array([]))
    y_pred_opt = (y_prob >= opt_threshold).astype(int)

    y_pred = (y_prob >= 0.5).astype(int)

    acc = accuracy_score(y_test, y_pred)
    prec = precision_score(y_test, y_pred, zero_division=0)
    rec = recall_score(y_test, y_pred, zero_division=0)
    f1 = f1_score(y_test, y_pred, zero_division=0)

    baseline_wr = test_trades["outcome"].mean() * 100
    filtered_mask = y_pred == 1
    filtered_wr = test_trades.loc[filtered_mask, "outcome"].mean() * 100 if filtered_mask.sum() > 0 else 0
    filter_rate = 1 - filtered_mask.sum() / len(y_pred) if len(y_pred) > 0 else 0

    filtered_trades = test_trades[filtered_mask]
    if len(filtered_trades) > 0:
        wins = filtered_trades[filtered_trades["outcome"] == 1]
        losses = filtered_trades[filtered_trades["outcome"] == 0]
        total_win_pnl = wins["pnl"].sum() if len(wins) > 0 else 0
        total_loss_pnl = abs(losses["pnl"].sum()) if len(losses) > 0 else 0
        pf = total_win_pnl / total_loss_pnl if total_loss_pnl > 0 else 999
        avg_rr = filtered_trades["rr_actual"].mean()
        total_pnl = filtered_trades["pnl"].sum()
    else:
        pf = 0
        avg_rr = 0
        total_pnl = 0

    all_wins = test_trades[test_trades["outcome"] == 1]
    all_losses = test_trades[test_trades["outcome"] == 0]
    baseline_pf = 0
    if len(all_wins) > 0 and len(all_losses) > 0:
        baseline_pf = all_wins["pnl"].sum() / abs(all_losses["pnl"].sum()) if abs(all_losses["pnl"].sum()) > 0 else 999

    try:
        perm_result = permutation_importance(model, X_test, y_test, n_repeats=10, random_state=42, n_jobs=-1)
        importance = dict(zip(feature_names, perm_result.importances_mean))  # noqa: B905
        importance = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True))
    except Exception:
        importance = dict(zip(feature_names, model.feature_importances_))  # noqa: B905

    cm = confusion_matrix(y_test, y_pred)

    filtered_mask_opt = y_pred_opt == 1
    opt_mask_indices = np.where(filtered_mask_opt)[0]
    filtered_trades_opt = test_trades.iloc[opt_mask_indices] if len(opt_mask_indices) > 0 else test_trades.iloc[:0]
    if len(filtered_trades_opt) > 0:
        wins_opt = filtered_trades_opt[filtered_trades_opt["outcome"] == 1]
        losses_opt = filtered_trades_opt[filtered_trades_opt["outcome"] == 0]
        total_win_pnl_opt = wins_opt["pnl"].sum() if len(wins_opt) > 0 else 0
        total_loss_pnl_opt = abs(losses_opt["pnl"].sum()) if len(losses_opt) > 0 else 0
        pf_opt = total_win_pnl_opt / total_loss_pnl_opt if total_loss_pnl_opt > 0 else 999
        wr_opt = len(wins_opt) / len(filtered_trades_opt) * 100
        pnl_opt = filtered_trades_opt["pnl"].sum()
    else:
        pf_opt = 0
        wr_opt = 0
        pnl_opt = 0

    return {
        "accuracy": round(acc, 4),
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(f1, 4),
        "baseline_win_rate": round(baseline_wr, 2),
        "filtered_win_rate": round(filtered_wr, 2),
        "filter_rate": round(filter_rate, 4),
        "filtered_profit_factor": round(pf, 2),
        "baseline_profit_factor": round(baseline_pf, 2),
        "filtered_avg_rr": round(avg_rr, 2),
        "filtered_total_pnl": round(total_pnl, 2),
        "n_total_trades": len(test_trades),
        "n_filtered_trades": int(filtered_mask.sum()),
        "n_wins_filtered": int((filtered_trades["outcome"] == 1).sum()) if len(filtered_trades) > 0 else 0,
        "confusion_matrix": cm.tolist(),
        "feature_importance": importance,
        "opt_threshold": opt_threshold,
        "opt_win_rate": round(wr_opt, 2),
        "opt_profit_factor": round(pf_opt, 2),
        "opt_total_pnl": round(float(pnl_opt), 4),
        "opt_filter_rate": round(1 - filtered_mask_opt.sum() / len(y_pred), 4) if len(y_pred) > 0 else 0,
        "opt_trade_count": len(filtered_trades_opt),
    }


def walk_forward_train(
    dataset: pd.DataFrame,
    n_folds: int = 5,
    test_ratio: float = 0.2,
    random_state: int = 42,
    model_types: list[str] | None = None,
) -> dict:
    if model_types is None:
        model_types = [MODEL_TYPE_DEFAULT]

    for mt in model_types:
        if mt not in MODEL_REGISTRY:
            raise ValueError(f"Unknown model type '{mt}'. Available: {available_model_types()}")

    feature_names = [f for f in FEATURE_COLUMNS if f in dataset.columns]
    X = dataset[feature_names].values
    y = dataset["outcome"].values

    valid_mask = ~(np.isnan(X).any(axis=1) | np.isinf(X).any(axis=1))
    X = X[valid_mask]
    y = y[valid_mask]
    dataset_clean = dataset[valid_mask].reset_index(drop=True)

    n = len(X)
    fold_size = n // n_folds

    model_results: dict[str, dict] = {}

    for mt in model_types:
        fold_metrics = []

        for fold in range(n_folds):
            train_end = (fold + 1) * fold_size
            test_start = train_end
            test_end = min(
                train_end + int(fold_size * (test_ratio / (1 - test_ratio / n_folds))),
                n,
            )

            if test_end <= test_start:
                continue

            X_train, y_train = X[:train_end], y[:train_end]
            X_test, y_test = X[test_start:test_end], y[test_start:test_end]

            if len(X_train) < 50 or len(X_test) < 20:
                continue

            if len(np.unique(y_train)) < 2:
                continue

            X_tr, X_val, y_tr, y_val = train_test_split(
                X_train,
                y_train,
                test_size=0.2,
                random_state=random_state + fold,
                stratify=y_train if len(np.unique(y_train)) >= 2 else None,
            )

            result = train_single_model(X_tr, y_tr, X_val, y_val, random_state + fold, model_type=mt)
            model = result["model"]
            test_trades = dataset_clean.iloc[test_start:test_end]

            metrics = evaluate_model(model, X_test, y_test, test_trades, feature_names)
            metrics["fold"] = fold
            metrics["train_size"] = len(X_train)
            metrics["test_size"] = len(X_test)
            metrics["model_type"] = mt
            fold_metrics.append(metrics)

        if not fold_metrics:
            model_results[mt] = {
                "folds": [],
                "error": "Insufficient data for walk-forward validation",
            }
            continue

        avg_metrics = {}
        numeric_keys = [
            k for k in fold_metrics[0] if isinstance(fold_metrics[0][k], (int, float)) and k not in ("fold",)
        ]
        for key in numeric_keys:
            values = [m[key] for m in fold_metrics if key in m]
            if values:
                avg_metrics[f"avg_{key}"] = round(float(np.mean(values)), 4)

        avg_metrics["n_folds_completed"] = len(fold_metrics)

        X_all_train = X[: n - fold_size]
        y_all_train = y[: n - fold_size]
        if len(X_all_train) >= 50 and len(np.unique(y_all_train)) >= 2:
            final_result = train_single_model(
                X_all_train,
                y_all_train,
                X[n - fold_size :],
                y[n - fold_size :],
                random_state,
                model_type=mt,
            )
            model_results[mt] = {
                "folds": fold_metrics,
                "summary": avg_metrics,
                "final_model": final_result["model"],
                "final_params": final_result["params"],
                "feature_names": feature_names,
                "model_type": mt,
            }
        else:
            model_results[mt] = {
                "folds": fold_metrics,
                "summary": avg_metrics,
                "model_type": mt,
            }

    comparison = build_comparison_table(model_results) if len(model_types) > 1 else None

    primary_mt = model_types[0]
    primary = model_results[primary_mt]

    output = {
        "folds": primary.get("folds", []),
        "summary": primary.get("summary", {}),
        "model_results": model_results,
        "model_types_trained": model_types,
    }

    if "final_model" in primary:
        output["final_model"] = primary["final_model"]
        output["final_params"] = primary["final_params"]
        output["feature_names"] = primary["feature_names"]

    if comparison is not None:
        output["comparison"] = comparison

    return output


def build_comparison_table(model_results: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for mt, results in model_results.items():
        summary = results.get("summary", {})
        display = MODEL_REGISTRY[mt]["display_name"]
        rows.append(
            {
                "model": display,
                "model_type": mt,
                "avg_f1": summary.get("avg_f1", 0),
                "avg_filtered_win_rate": summary.get("avg_filtered_win_rate", 0),
                "avg_filtered_profit_factor": summary.get("avg_filtered_profit_factor", 0),
                "avg_filtered_total_pnl": summary.get("avg_filtered_total_pnl", 0),
                "avg_opt_profit_factor": summary.get("avg_opt_profit_factor", 0),
                "avg_opt_win_rate": summary.get("avg_opt_win_rate", 0),
                "n_folds": summary.get("n_folds_completed", 0),
            }
        )
    return pd.DataFrame(rows)


def save_model(
    model,
    feature_names: list,
    metrics: dict,
    output_dir: str,
    model_type: str = MODEL_TYPE_DEFAULT,
) -> str:
    os.makedirs(output_dir, exist_ok=True)

    model_path = os.path.join(output_dir, "signal_filter.pkl")
    with open(model_path, "wb") as f:
        pickle.dump(model, f)

    meta = {
        "feature_names": feature_names,
        "metrics_summary": {k: v for k, v in metrics.get("summary", {}).items()},
        "n_features": len(feature_names),
        "model_type": model_type,
        "display_name": MODEL_REGISTRY.get(model_type, {}).get("display_name", model_type),
    }

    meta_path = os.path.join(output_dir, "signal_filter_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    return model_path


def load_model(model_dir: str) -> tuple:
    model_path = os.path.join(model_dir, "signal_filter.pkl")
    meta_path = os.path.join(model_dir, "signal_filter_meta.json")

    with open(model_path, "rb") as f:
        model = pickle.load(f)  # nosec B301  # noqa: S301

    with open(meta_path) as f:
        meta = json.load(f)

    return model, meta["feature_names"]


def run_full_pipeline(
    symbols: list[str],
    data_dir: str,
    output_dir: str,
    timeframe: str = "H1",
    max_holding_bars: int = 50,
    n_folds: int = 5,
    model_types: list[str] | None = None,
) -> dict:
    all_datasets = []

    for symbol in symbols:
        dataset = prepare_dataset(symbol, data_dir, timeframe, max_holding_bars)
        if not dataset.empty:
            all_datasets.append(dataset)
            print(f"  {symbol}: {len(dataset)} labeled trades")

    if not all_datasets:
        return {"error": "No trade data generated from any symbol"}

    combined = pd.concat(all_datasets, ignore_index=True)
    print(f"\nCombined dataset: {len(combined)} trades")
    print(f"  Win rate: {combined['outcome'].mean() * 100:.1f}%")
    print(f"  Strategies: {combined['strategy'].value_counts().to_dict()}")

    results = walk_forward_train(combined, n_folds=n_folds, model_types=model_types)

    if "final_model" in results:
        model_path = save_model(
            results["final_model"],
            results["feature_names"],
            results,
            output_dir,
            model_type=model_types[0] if model_types else MODEL_TYPE_DEFAULT,
        )
        results["model_path"] = model_path
        print(f"\nModel saved to: {model_path}")

    if "comparison" in results:
        print(f"\nModel comparison:\n{results['comparison'].to_string(index=False)}")

    return results
