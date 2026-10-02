"""ATR provider stub — JSON-backed cache for rolling ATR values."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ayumi.providers")


class ATRProvider:
    """Provides rolling ATR values. Stub — reads from cache/JSON until market feed integrated."""

    def __init__(self, cache_path: str = "data/atr_cache.json"):
        self._cache_path = Path(cache_path)
        self._cache: dict[str, float] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """Load ATR cache from disk."""
        if self._cache_path.exists():
            try:
                self._cache = json.loads(self._cache_path.read_text())
                logger.info("Loaded ATR cache with %d symbols", len(self._cache))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load ATR cache: %s", e)
                self._cache = {}

    def _save_cache(self) -> None:
        """Persist ATR cache to disk."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._cache, indent=2))
            tmp.rename(self._cache_path)
        except OSError as e:
            logger.error("Failed to save ATR cache: %s", e)

    def get_atr(self, symbol: str) -> Optional[float]:
        """Get current ATR for a symbol."""
        return self._cache.get(symbol)

    def update_atr(self, symbol: str, value: float) -> None:
        """Update ATR for a symbol and persist."""
        self._cache[symbol] = value
        self._save_cache()

    def __call__(self, symbol: str) -> Optional[float]:
        """Callable interface for VolatilityGate compatibility."""
        return self.get_atr(symbol)
