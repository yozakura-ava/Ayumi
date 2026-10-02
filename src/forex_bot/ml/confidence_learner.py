"""Confidence learner — trains a model to predict trade outcomes from confluence features.

Per (symbol, timeframe), a RandomForest learns which confluence factors
correlate with wins vs losses. The learned feature importances serve as
data-driven confidence weight adjustments.
"""

from __future__ import annotations

import numpy as np
from sklearn.ensemble import RandomForestClassifier


class ConfidenceLearner:
    """Learns optimal confidence weights from historical trade outcomes."""

    def __init__(self, symbol: str, timeframe: str, min_trades: int = 20):
        self.symbol = symbol
        self.timeframe = timeframe
        self.model: RandomForestClassifier | None = None
        self.feature_names: list[str] = []
        self.is_trained = False
        self.learned_weights: dict[str, float] = {}
        self.min_trades = min_trades
        self.train_size = 0

    def train(
        self,
        trade_records: list[dict],
        feature_names: list[str],
    ) -> dict[str, float]:
        """Train a RandomForest classifier on trade records.

        Args:
            trade_records: list of dicts with 'features' (list[float]) and
                           'outcome' (1=win, 0=loss).
            feature_names: names corresponding to feature vector indices.

        Returns:
            Dict mapping feature_name → importance.

        Raises:
            ValueError: if fewer than min_trades records.
        """
        if len(trade_records) < self.min_trades:
            raise ValueError(
                f"[{self.symbol}/{self.timeframe}] Need at least "
                f"{self.min_trades} trades to train, got {len(trade_records)}"
            )

        X = np.array([r["features"] for r in trade_records])
        y = np.array([r["outcome"] for r in trade_records], dtype=np.int32)

        self.feature_names = feature_names
        self.train_size = len(trade_records)

        # Class-weighted to handle imbalanced win/loss ratio
        n_wins = int(y.sum())
        n_losses = len(y) - n_wins
        class_weight = "balanced" if n_losses > 0 and n_wins > 0 else None

        self.model = RandomForestClassifier(
            n_estimators=100,
            max_depth=5,
            min_samples_leaf=5,
            random_state=42,
            class_weight=class_weight,
        )
        self.model.fit(X, y)
        self.is_trained = True

        importances = self.model.feature_importances_
        self.learned_weights = dict(zip(feature_names, importances.tolist()))  # noqa: B905

        return self.learned_weights

    def predict_proba(self, features: list[float]) -> float:
        """Predict win probability for a trade given its feature vector.

        Returns 0.5 (50/50) if not trained.
        """
        if not self.is_trained or self.model is None:
            return 0.5
        return float(self.model.predict_proba([features])[0][1])

    def get_top_features(self, n: int = 5) -> list[tuple[str, float]]:
        """Return top n features by importance, descending."""
        sorted_weights = sorted(self.learned_weights.items(), key=lambda x: -x[1])
        return sorted_weights[:n]
