"""BTC Macro Regime Overlay (BQ-508).

Reads BTC regime labels from the crypto_monitor pipeline and classifies
them into a simplified macro regime: trending_up, trending_down, high_vol,
or neutral. Falls back to "neutral" if the data file is missing or
unreadable.

Usage:
    from quant.btc_regime_overlay import BtcRegimeOverlay

    overlay = BtcRegimeOverlay()  # loads default path
    regime = overlay.regime_at_timestamp(ts_ms)
    regime = overlay.regime_for_window(start_ms, end_ms)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

# Default path to BTC regime labels from crypto_monitor pipeline
DEFAULT_BTC_REGIME_PATH = "/root/.openclaw/workspace/src/crypto_monitor/data/crypto/regime_labels.jsonl"

# Raw crypto_monitor regimes → simplified macro classes
_REGIME_MAP: dict[str, str] = {
    "trending_up_low_vol": "trending_up",
    "trending_up_high_vol": "high_vol",
    "trending_down_low_vol": "trending_down",
    "trending_down_high_vol": "high_vol",
    "sideways_high_vol": "high_vol",
    "sideways_low_vol": "neutral",
}


class BtcRegimeOverlay:
    """BTC macro regime overlay for walk-forward backtests.

    Loads BTC regime labels lazily and provides timestamp-based lookups.
    Degrades gracefully to "neutral" if data is unavailable.

    Optionally accepts an ``hmm_signal_source`` — a fitted
    ``CorrelationRegimeHMM`` — to blend HMM regime detection with
    label-based lookups via :meth:`regime_with_hmm`.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        hmm_signal_source=None,
    ) -> None:
        self._path = Path(path) if path else Path(DEFAULT_BTC_REGIME_PATH)
        self._entries: list[dict] | None = None  # lazy load
        self._timestamps: list[int] | None = None
        self._hmm_source = hmm_signal_source

    def _ensure_loaded(self) -> bool:
        """Lazily load the JSONL file. Returns True if data is available."""
        if self._entries is not None:
            return len(self._entries) > 0

        self._entries = []
        self._timestamps = []

        if not self._path.exists():
            logger.warning(
                "BTC regime file not found: %s — falling back to neutral",
                self._path,
            )
            return False

        try:
            with self._path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    self._entries.append(entry)
                    self._timestamps.append(entry["ts"])

            if self._entries:
                logger.info(
                    "Loaded %d BTC regime entries from %s",
                    len(self._entries),
                    self._path,
                )
                return True
            else:
                logger.warning("BTC regime file is empty: %s", self._path)
                return False

        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.error(
                "Failed to read BTC regime file %s: %s — falling back to neutral",
                self._path,
                exc,
            )
            self._entries = []
            self._timestamps = []
            return False

    @staticmethod
    def _classify(raw_regime: str) -> str:
        """Map crypto_monitor regime label to simplified macro regime."""
        return _REGIME_MAP.get(raw_regime, "neutral")

    def regime_at_timestamp(self, ts_ms: int) -> str:
        """Get BTC macro regime at a specific timestamp (epoch ms).

        Uses nearest-prior entry. Returns "neutral" if no data.
        """
        if not self._ensure_loaded():
            return "neutral"

        assert self._timestamps is not None  # noqa: S101 — invariant after `_ensure_loaded()` returns True; type-narrowing aid for mypy, intentionally silenced under `python -O`
        assert self._entries is not None  # noqa: S101 — invariant after `_ensure_loaded()` returns True; type-narrowing aid for mypy, intentionally silenced under `python -O`

        # Binary search for nearest-prior timestamp
        import bisect

        idx = bisect.bisect_right(self._timestamps, ts_ms) - 1

        if idx < 0:
            # Before first entry — use earliest
            idx = 0

        raw = self._entries[idx].get("regime", "sideways_low_vol")
        return self._classify(raw)

    def regime_for_window(
        self,
        start_ms: int,
        end_ms: int,
    ) -> str:
        """Get dominant BTC regime across a time window.

        Returns the most frequent regime within [start_ms, end_ms].
        Falls back to regime_at_timestamp(start_ms) if no entries in range.
        """
        if not self._ensure_loaded():
            return "neutral"

        assert self._timestamps is not None  # noqa: S101 — invariant after `_ensure_loaded()` returns True; type-narrowing aid for mypy, intentionally silenced under `python -O`
        assert self._entries is not None  # noqa: S101 — invariant after `_ensure_loaded()` returns True; type-narrowing aid for mypy, intentionally silenced under `python -O`

        import bisect
        from collections import Counter

        lo = bisect.bisect_left(self._timestamps, start_ms)
        hi = bisect.bisect_right(self._timestamps, end_ms)

        if lo >= hi:
            # No entries in window — use nearest prior
            return self.regime_at_timestamp(start_ms)

        in_range = self._entries[lo:hi]
        classified = [self._classify(e.get("regime", "sideways_low_vol")) for e in in_range]

        most_common = Counter(classified).most_common(1)
        return most_common[0][0] if most_common else "neutral"

    def regime_for_bars(self, bars: Sequence) -> str:
        """Get BTC regime for a window defined by Bar-like objects.

        Uses first bar's time as start, last bar's time as end.
        Bars must have a ``time`` attribute with a ``timestamp()`` method
        (i.e., a datetime).
        """
        if not bars:
            return "neutral"

        start_ms = int(bars[0].time.timestamp() * 1000)
        end_ms = int(bars[-1].time.timestamp() * 1000)
        return self.regime_for_window(start_ms, end_ms)

    # ------------------------------------------------------------------
    # HMM blend (BQ-1240a — additive, does not change existing behavior)
    # ------------------------------------------------------------------

    _HMM_REGIME_MAP: dict[str, str] = {
        "STABLE": "neutral",
        "BREAKDOWN": "high_vol",
        "TRANSITION": "neutral",
    }

    def regime_with_hmm(
        self,
        ts_ms: int,
        features_df=None,
    ) -> str:
        """Blend label-based regime with HMM signal.

        If the HMM source is available and predicts BREAKDOWN with high
        confidence, escalate the blended regime to ``high_vol`` regardless
        of what the label says. Otherwise, return the label-based regime.

        Args:
            ts_ms: Epoch-millisecond timestamp.
            features_df: Optional pre-computed HMM feature DataFrame.
                If not provided and the HMM source has its own feature
                engineer, this method falls back to label-only.

        Returns:
            Blended regime string (same vocabulary as
            :meth:`regime_at_timestamp`).
        """
        label_regime = self.regime_at_timestamp(ts_ms)

        if self._hmm_source is None:
            return label_regime

        if features_df is not None and not features_df.empty:
            try:
                hmm_regime, confidence = self._hmm_source.predict_current(
                    features_df,
                )
            except Exception:
                return label_regime

            hmm_mapped = self._HMM_REGIME_MAP.get(hmm_regime, "neutral")

            # If HMM is confident about BREAKDOWN, escalate
            if hmm_regime == "BREAKDOWN" and confidence >= 0.65:
                return "high_vol"

            # If HMM says STABLE and label agrees, keep label
            # If HMM says STABLE but label says high_vol, defer to label
            return label_regime if hmm_mapped == label_regime else label_regime

        return label_regime

    @property
    def entry_count(self) -> int:
        """Number of loaded entries (0 if not loadable)."""
        if not self._ensure_loaded():
            return 0
        assert self._entries is not None  # noqa: S101 — invariant after `_ensure_loaded()` returns True; type-narrowing aid for mypy, intentionally silenced under `python -O`
        return len(self._entries)
