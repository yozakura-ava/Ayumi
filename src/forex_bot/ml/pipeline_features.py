"""ML feature extraction from the signal pipeline for blend optimizer."""

from __future__ import annotations

MAX_LOT = 0.5  # Normalization ceiling


class PipelineFeatureExtractor:
    """Extracts features from the new signal pipeline for ML models."""

    CONFIDENCE_THRESHOLDS = (0.3, 0.7)

    def extract(self, trade: dict) -> dict[str, float]:
        """Extract feature vector from a trade record."""
        confidence = float(trade.get("confidence", 0.5))
        account_balance = float(trade.get("account_balance", 10000.0))
        lots = float(trade.get("lots", 0.0))
        risk_amount = float(trade.get("risk_amount", 0.0))
        sl_distance = float(trade.get("sl_distance_pips", 0.0))

        # Confidence bucket
        low, high = self.CONFIDENCE_THRESHOLDS
        if confidence < low:
            bucket = "low"
        elif confidence < high:
            bucket = "med"
        else:
            bucket = "high"

        return {
            "profile_sniper": 1.0 if trade.get("profile") == "sniper" else 0.0,
            "confidence_score": max(0.0, min(confidence, 1.0)),
            "confidence_bucket_low": 1.0 if bucket == "low" else 0.0,
            "confidence_bucket_med": 1.0 if bucket == "med" else 0.0,
            "confidence_bucket_high": 1.0 if bucket == "high" else 0.0,
            "gate_spread_pass": 1.0 if trade.get("gate_spread_pass") else 0.0,
            "gate_session_pass": 1.0 if trade.get("gate_session_pass") else 0.0,
            "gate_volatility_pass": 1.0 if trade.get("gate_volatility_pass") else 0.0,
            "lots_normalized": min(lots / MAX_LOT, 1.0) if MAX_LOT > 0 else 0.0,
            "risk_amount_normalized": min(risk_amount / account_balance, 1.0) if account_balance > 0 else 0.0,
            "sl_distance_pips_normalized": min(sl_distance / 50.0, 1.0),
            "confluence_score": max(0.0, min(float(trade.get("confluence_score", 0.0)), 1.0)),
            "num_agreeing_strategies": min(float(trade.get("num_agreeing_strategies", 0)), 1.0),
        }

    @property
    def feature_names(self) -> list[str]:
        """Ordered list of feature names for ML model input."""
        return [
            "profile_sniper",
            "confidence_score",
            "confidence_bucket_low",
            "confidence_bucket_med",
            "confidence_bucket_high",
            "gate_spread_pass",
            "gate_session_pass",
            "gate_volatility_pass",
            "lots_normalized",
            "risk_amount_normalized",
            "sl_distance_pips_normalized",
            "confluence_score",
            "num_agreeing_strategies",
        ]
