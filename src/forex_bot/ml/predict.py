import numpy as np
import pandas as pd

from .train_model import load_model


class SignalFilter:
    def __init__(self, model_dir: str):
        self.model, self.feature_names = load_model(model_dir)
        self._feature_index = {name: i for i, name in enumerate(self.feature_names)}

    def predict(self, feature_row: pd.Series) -> dict:
        values = np.full(len(self.feature_names), np.nan)
        for name in self.feature_names:
            if name in feature_row.index:
                values[self._feature_index[name]] = feature_row[name]

        if np.isnan(values).any():
            return {"approve": False, "confidence": 0.0, "reason": "missing features"}

        X = values.reshape(1, -1)
        prediction = self.model.predict(X)[0]
        probability = self.model.predict_proba(X)[0]

        return {
            "approve": bool(prediction == 1),
            "confidence": round(float(probability[1]), 4),
            "reject_probability": round(float(probability[0]), 4),
            "reason": "approved" if prediction == 1 else "rejected by ML filter",
        }

    def predict_batch(self, features_df: pd.DataFrame) -> pd.DataFrame:
        available = [f for f in self.feature_names if f in features_df.columns]
        if len(available) < len(self.feature_names):
            missing = set(self.feature_names) - set(available)
            raise ValueError(f"Missing features: {missing}")

        X = features_df[self.feature_names].values
        if np.isnan(X).any() or np.isinf(X).any():
            raise ValueError("Features contain NaN or Inf values")

        predictions = self.model.predict(X)
        probabilities = self.model.predict_proba(X)

        results = pd.DataFrame(
            {
                "approve": predictions == 1,
                "confidence": probabilities[:, 1],
                "reject_probability": probabilities[:, 0],
            },
            index=features_df.index,
        )

        return results

    def filter_signals(self, signals_df: pd.DataFrame, features_df: pd.DataFrame) -> pd.DataFrame:
        merged = signals_df.join(features_df, how="inner")
        if merged.empty:
            return pd.DataFrame()

        predictions = self.predict_batch(merged[self.feature_names])
        approved = merged[predictions["approve"]].copy()
        approved["ml_confidence"] = predictions.loc[predictions["approve"], "confidence"]

        return approved


def create_filter_integration_stub(model_dir: str, output_path: str):
    code = f'''import json
import numpy as np

MODEL_DIR = "{model_dir}"

def filter_signal(feature_dict: dict) -> dict:
    """Filter a single signal. Returns {{"approve": bool, "confidence": float}}."""
    import pickle
    import os

    model_path = os.path.join(MODEL_DIR, "signal_filter.pkl")
    meta_path = os.path.join(MODEL_DIR, "signal_filter_meta.json")

    with open(meta_path, "r") as f:
        meta = json.load(f)

    with open(model_path, "rb") as f:
        model = pickle.load(f)

    feature_names = meta["feature_names"]
    values = np.full(len(feature_names), np.nan)
    for i, name in enumerate(feature_names):
        if name in feature_dict:
            values[i] = feature_dict[name]

    if np.isnan(values).any():
        return {{"approve": False, "confidence": 0.0}}

    X = values.reshape(1, -1)
    pred = model.predict(X)[0]
    prob = model.predict_proba(X)[0]

    return {{"approve": bool(pred == 1), "confidence": float(prob[1])}}
'''

    with open(output_path, "w") as f:
        f.write(code)
