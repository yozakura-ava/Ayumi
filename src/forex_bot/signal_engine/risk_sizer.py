"""Confidence-based position sizing for risk management."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfidenceTier:
    min_confidence: float
    max_confidence: float
    risk_pct: float


DEFAULT_TIERS: list[ConfidenceTier] = [
    ConfidenceTier(0.85, 1.00, 0.0150),  # 85-100%: 1.50% risk (T5)
    ConfidenceTier(0.70, 0.85, 0.0075),  # 70-84%:  0.75% risk (T4)
    ConfidenceTier(0.50, 0.70, 0.0050),  # 50-69%:  0.50% risk (T3)
    ConfidenceTier(0.35, 0.50, 0.0025),  # 35-49%:  0.25% risk (T2)
    ConfidenceTier(0.20, 0.35, 0.0010),  # 20-34%:  0.10% risk (T1)
]


class ConfidencePositionSizer:
    """Sizes position based on confidence level and account size."""

    def __init__(
        self,
        account_size: float = 10000.0,
        tiers: list[ConfidenceTier] | None = None,
    ):
        self.account = account_size
        self.tiers = tiers if tiers is not None else DEFAULT_TIERS

    def get_risk_pct(self, confidence: float) -> float:
        """Map confidence to risk percentage of account."""
        for tier in self.tiers:
            if tier.min_confidence <= confidence < tier.max_confidence:
                return tier.risk_pct
        # Confidence >= top tier max or below bottom tier min
        if confidence >= self.tiers[0].max_confidence:
            return self.tiers[0].risk_pct
        return self.tiers[-1].risk_pct

    def get_risk_amount(self, confidence: float) -> float:
        """Return dollar amount to risk based on confidence."""
        return self.account * self.get_risk_pct(confidence)

    def get_lot_size(
        self,
        confidence: float,
        stop_pips: float,
        pip_size: float,
    ) -> float:
        """Calculate lot size for a given confidence level and stop distance."""
        risk_dollar = self.get_risk_amount(confidence)
        stop_price_distance = stop_pips * pip_size
        if stop_price_distance == 0:
            return 0.0
        return risk_dollar / stop_price_distance

    def get_tier_label(self, confidence: float) -> str:
        """Return a human-readable label for the confidence tier."""
        for tier in self.tiers:
            if tier.min_confidence <= confidence < tier.max_confidence:
                return f"{tier.min_confidence:.0%}-{tier.max_confidence:.0%}"
        if confidence >= self.tiers[0].max_confidence:
            return f"{self.tiers[0].min_confidence:.0%}+"
        return f"<{self.tiers[-1].min_confidence:.0%}"


def parse_tiers(tiers_json: str) -> list[ConfidenceTier]:
    """Parse a JSON list of [min_conf, max_conf, risk_pct] tuples."""
    import json

    raw = json.loads(tiers_json)
    return [ConfidenceTier(min_c, max_c, risk) for min_c, max_c, risk in raw]
