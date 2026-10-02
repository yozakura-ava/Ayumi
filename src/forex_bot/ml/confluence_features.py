"""Feature engineering for confluence-based trade prediction.

Extracts binary feature vectors from trade records indicating which
confluence factors were present at signal time.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class ConfluenceFeatureExtractor:
    """Extract feature vector from a trade's confluence signals."""

    # Boost names that match TTSStrategy ConfidenceBuilder.add_boost() calls.
    # Each maps to a canonical feature name.
    BOOST_TO_FEATURE: dict[str, str] = {
        "rsi_divergence": "rsi_divergence",
        "htf_trend_aligned": "htf_trend_aligned",
        "svc_at_peak": "svc_at_peak",
        "consolidation": "consolidation",
        "asia_gap_favorable": "asia_gap_favorable",
        "ilod_ihod_at_boundary": "ilod_ihod_at_boundary",
        "vwap_rejection": "vwap_rejection",
        "kill_zone_active": "kill_zone_active",
        "quality_gate": "quality_gate",
        "confluence_scorer": "confluence_scorer",
        "htf_opposing": "htf_opposing",
        "htf_conflicting": "htf_conflicting",
        "htf_consolidating": "htf_consolidating",
    }

    FEATURE_NAMES = [
        "rsi_divergence",
        "htf_trend_aligned",
        "svc_at_peak",
        "consolidation",
        "asia_gap_favorable",
        "ilod_ihod_at_boundary",
        "vwap_rejection",
        "kill_zone_active",
        "quality_gate",
        "confluence_scorer",
        # Penalty features (negative boosts → presence is bad signal)
        "htf_opposing",
        "htf_conflicting",
        "htf_consolidating",
        # Numeric features
        "confidence_score",
        "base_confidence",  # raw base confidence before boosts
        "confluence_count",
    ]

    NUMERIC_FEATURES = {"confidence_score", "base_confidence", "confluence_count"}

    def extract(self, trade_record: dict) -> list[float]:
        """Convert a trade record to a feature vector.

        Args:
            trade_record: dict with keys 'rationale' (str), 'confidence_score' (float),
                          'confluence_count' (int).

        Returns:
            List of float values matching FEATURE_NAMES order.
        """
        rationale = trade_record.get("rationale", "")
        active_boosts = self._parse_boosts(rationale)

        features: list[float] = []
        for feat_name in self.FEATURE_NAMES:
            if feat_name in self.NUMERIC_FEATURES:
                val = trade_record.get(feat_name)
                if val is None:
                    logger.warning("Missing feature '%s' in trade record, using 0.0", feat_name)
                    features.append(0.0)
                else:
                    features.append(float(val))
            else:
                features.append(1.0 if feat_name in active_boosts else 0.0)

        return features

    def extract_batch(self, trade_records: list[dict]) -> list[list[float]]:
        """Extract feature vectors for a batch of trade records."""
        return [self.extract(tr) for tr in trade_records]

    def _parse_boosts(self, rationale: str) -> set[str]:
        """Parse boost names from a TTSStrategy rationale string.

        Expected format: "...boosts=[('rsi_divergence', 0.05), ('kill_zone_active', 0.08)]"
        """
        boosts: set[str] = set()
        # Match tuples like ('name', value)
        matches = re.findall(r"\('([^']+)',\s*[-\d.]+\)", rationale)
        for name in matches:
            canonical = self.BOOST_TO_FEATURE.get(name, name)
            boosts.add(canonical)
        return boosts
