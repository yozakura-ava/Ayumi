"""FRED (Federal Reserve Economic Data) policy-rate fetcher.

Fetches US policy rate history from the St. Louis Fed's FRED API using the
optional `fredapi` library, with an offline fallback table when the API key or
library is unavailable. Designed for daily-cadence consumption by the Cabal
confidence layer's carry signal — not for high-frequency / real-time use.

Typical usage::

    from data.fred_fetcher import FredFetcher
    fetcher = FredFetcher()                       # uses FRED_API_KEY env var
    history = fetcher.get_us_policy_rate_history(start_date="2010-01-01")
    latest = fetcher.latest_us_policy_rate()

For tests, pass an explicit ``StaticFredRates`` source or set
``FRED_OFFLINE=1`` to force the fallback path without needing the library.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger("ayumi.fred")

# FRED series IDs (stable; do not change without updating downstream consumers)
SERIES_FED_FUNDS = "FEDFUNDS"  # Effective Federal Funds Rate (monthly, %)
SERIES_DGS10 = "DGS10"  # 10-Year Treasury (daily, %)
SERIES_DGS2 = "DGS2"  # 2-Year Treasury (daily, %)

# Manual fallback table for the synthetic-history path. Values are end-of-year
# effective federal funds rate in percent. Updated quarterly by Ava/Tsukasa.
_FALLBACK_FED_FUNDS: dict[int, float] = {
    2010: 0.18,
    2011: 0.07,
    2012: 0.14,
    2013: 0.09,
    2014: 0.12,
    2015: 0.24,
    2016: 0.54,
    2017: 1.30,
    2018: 2.27,
    2019: 1.55,
    2020: 0.09,
    2021: 0.08,
    2022: 4.10,
    2023: 5.33,
    2024: 4.48,
    2025: 4.58,
}


def _has_fredapi() -> bool:
    """Check if the optional fredapi library is available."""
    try:
        # Side-effect import: only used to probe optional dependency availability.
        import fredapi  # noqa: F401

        return True
    except ImportError:
        return False


class FredSource(Protocol):
    """Interface for any FRED-like rate source (real or test double)."""

    def get_series(self, series_id: str, start_date: str, end_date: str) -> list[dict]: ...


class StaticFredRates:
    """Static US policy-rate source suitable for tests and offline operation.

    Mirrors the ``fredapi.Fred.get_series(...)`` return shape consumed by
    :class:`FredFetcher.get_us_policy_rate_history`: a list of
    ``{"date": "YYYY-MM-DD", "rate": float}`` dicts, sorted oldest → newest.
    """

    def __init__(self, history: Optional[list[dict]] = None) -> None:
        if history is None:
            history = [{"date": f"{year}-01-01", "rate": rate} for year, rate in _FALLBACK_FED_FUNDS.items()]
        self._history = sorted(history, key=lambda r: r["date"])

    def get_series(self, series_id: str, start_date: str, end_date: str) -> list[dict]:
        del series_id  # static source ignores series; one effective rate per year
        return [r for r in self._history if start_date <= r["date"] <= end_date]


class FredFetcher:
    """FRED policy-rate fetcher with offline fallback.

    Parameters
    ----------
    api_key:
        FRED API key. Defaults to ``FRED_API_KEY`` environment variable. If
        unset or empty, the offline fallback path is used.
    cache_path:
        On-disk JSON cache path. Writes are atomic via a temporary file +
        rename. Set to ``None`` to disable persistence.
    force_offline:
        If ``True``, never attempt the real FRED endpoint even when fredapi is
        installed. Useful for testing and for the carry signal's daily fetch
        when running on a sandbox without network egress.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_path: str = "data/fred_cache.json",
        force_offline: bool = False,
        source: Optional[FredSource] = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY", "")
        self._cache_path = Path(cache_path) if cache_path else None
        self._force_offline = force_offline or os.environ.get("FRED_OFFLINE", "") not in ("", "0", "false", "False")
        self._cache: dict[str, list[dict]] = self._load_cache()
        self._client = None
        self._explicit_source = source

        if self._explicit_source is None and not self._force_offline:
            if _has_fredapi() and self._api_key:
                try:
                    import fredapi  # type: ignore

                    self._client = fredapi.Fred(api_key=self._api_key)
                except Exception as exc:  # pragma: no cover - network init
                    logger.warning("fredapi init failed (%s); falling back to synthetic rates", exc)

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------

    def _load_cache(self) -> dict[str, list[dict]]:
        if self._cache_path is None or not self._cache_path.exists():
            return {}
        try:
            return json.loads(self._cache_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("FRED cache load failed (%s); starting empty", exc)
            return {}

    def _save_cache(self) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, indent=2))
            tmp.rename(self._cache_path)
        except OSError as exc:
            logger.error("FRED cache save failed: %s", exc)

    @staticmethod
    def _cache_key(series_id: str, start: str, end: str) -> str:
        return f"{series_id}:{start}:{end}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_us_policy_rate_history(
        self,
        start_date: str = "2010-01-01",
        end_date: Optional[str] = None,
        series_id: str = SERIES_FED_FUNDS,
    ) -> list[dict]:
        """Fetch US policy rate history.

        Returns a list of ``{"date": "YYYY-MM-DD", "rate": float}`` dicts,
        sorted oldest → newest. Each ``rate`` is in percent (e.g. ``5.33`` = 5.33%).

        Results are cached on disk so repeat daily fetches don't hit FRED.
        """
        end_date = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = self._cache_key(series_id, start_date, end_date)
        if key in self._cache:
            return list(self._cache[key])

        rows: Optional[list[dict]] = None
        if self._explicit_source is not None:
            rows = self._explicit_source.get_series(series_id, start_date, end_date)
        elif self._client is not None:
            try:
                df = self._client.get_series(series_id, start_date, end_date)
                rows = [
                    {"date": ts.strftime("%Y-%m-%d"), "rate": float(value)}
                    for ts, value in df.items()
                    if value is not None and not (isinstance(value, float) and value != value)  # NaN check
                ]
            except Exception as exc:  # pragma: no cover - network path
                logger.warning("FRED live fetch failed (%s); using synthetic history", exc)

        if rows is None:
            rows = self._synthetic_us_rates(start_date, end_date)

        rows.sort(key=lambda r: r["date"])
        self._cache[key] = rows
        self._save_cache()
        return list(rows)

    def latest_us_policy_rate(self) -> Optional[float]:
        """Most recent US policy rate (percent). ``None`` when no history."""
        rows = self.get_us_policy_rate_history()
        if not rows:
            return None
        return float(rows[-1]["rate"])

    # ------------------------------------------------------------------
    # Synthetic fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _synthetic_us_rates(start: str, end: str) -> list[dict]:
        """Annual-policy-rate fallback when fredapi/network unavailable."""
        try:
            start_year = int(start[:4])
            end_year = int(end[:4])
        except (TypeError, ValueError):
            start_year, end_year = 2010, datetime.now(timezone.utc).year

        rows: list[dict] = []
        for year, rate in _FALLBACK_FED_FUNDS.items():
            if start_year <= year <= end_year:
                rows.append({"date": f"{year}-01-01", "rate": float(rate)})
        rows.sort(key=lambda r: r["date"])
        return rows


__all__ = [
    "FredFetcher",
    "FredSource",
    "StaticFredRates",
    "SERIES_FED_FUNDS",
    "SERIES_DGS10",
    "SERIES_DGS2",
]
