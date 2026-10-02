"""Carry-trade signal provider — FRED + ECB SDMX + MT5 swap feeds.

Forex carry is a structural, multi-week flow signal. This module combines
three free public data sources so the Cabal confidence layer can treat
carry as a regime / bias feature with daily update cadence — NOT a
tactical entry feature.

Data sources:

* **FRED** — US policy rate via :mod:`data.fred_fetcher`.
* **ECB SDMX** — Euro area Main Refinancing Rate via the ECB's public SDMX
  REST endpoint. Uses ``urllib`` so there is no extra dependency.
* **MT5 swap feed** — long/short swap points per symbol, sourced from a
  small CSV cache (production deployments populate this from an MT5 script;
  tests inject in-memory data).

The provider is dependency-light and degrades gracefully: if any source is
unreachable, it falls back to a static lookup table or to ``None`` so the
confidence layer can treat that pair as "neutral carry".

Typical usage::

    from data.carry_signals import CarrySignalProvider, score_with_carry
    from confidence.engine import ConfidenceEngine

    provider = CarrySignalProvider()
    engine = ConfidenceEngine()

    result = score_with_carry(
        engine, provider,
        raw_confidence=0.55,
        symbol="USDJPY",
        direction="long",
    )
    # → ConfidenceResult with carry confluence applied before gate validation.

See ``tests/unit/data/test_carry_signals.py`` for regime-shift scenarios on
USDJPY and ``docs/ayumi/data-sources/carry-signals.md`` for design notes.
"""

from __future__ import annotations

import csv
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Protocol

from data.fred_fetcher import FredFetcher

logger = logging.getLogger("ayumi.carry")


# ---------------------------------------------------------------------------
# Enums & data classes
# ---------------------------------------------------------------------------


class CarryRegime(str, Enum):
    """How the carry differential favours the proposed direction."""

    POSITIVE = "positive"  # carrying trade aligns with direction (earns the diff)
    NEGATIVE = "negative"  # carry opposes direction (pays the diff)
    NEUTRAL = "neutral"  # differential too small to matter, or data missing


@dataclass
class CarrySignal:
    """Result of evaluating carry for one (symbol, direction) pair."""

    symbol: str
    direction: str
    base_rate: Optional[float]
    quote_rate: Optional[float]
    swap_points: Optional[tuple[float, float]]
    rate_diff: Optional[float]
    regime: CarryRegime
    sources: dict[str, str] = field(default_factory=dict)
    """``{"usd": "fred", "eur": "ecb-sdmx", "swap": "file"}`` provenance map."""

    def to_confluence(self) -> Optional[dict]:
        """Convert to a confidence-engine confluence dict, or ``None`` if neutral/negative.

        The ConfidenceEngine boosts a signal only when a confluence entry's
        ``direction`` matches the proposed direction. Carry therefore emits a
        confluence **only when the regime is POSITIVE** — i.e. when carry
        agrees with the proposed direction. NEGATIVE carry (regime opposes
        the direction) and NEUTRAL carry (rate diff below threshold) both
        return ``None``: NEGATIVE gets no boost relative to a carry-blind
        baseline, which is the desired "confidence adjustment" on regime
        shift. The diagnostic ``regime`` value remains available on the
        ``CarrySignal`` itself for callers that want to inspect it.

        Shape matches the ``confluences`` keyword of
        :meth:`confidence.engine.ConfidenceEngine.score`:

        .. code-block:: python

           {"strategy": "carry_regime",
            "direction": "long",     # matches POSITIVE regime's proposed direction
            "timeframe": "D1",
            "metadata": {...}}        # diagnostic dict; not consumed by the engine
        """
        if self.regime != CarryRegime.POSITIVE:
            return None
        return {
            "strategy": "carry_regime",
            "direction": self.direction,
            "timeframe": "D1",
            "metadata": {
                "regime": self.regime.value,
                "rate_diff": self.rate_diff,
                "base_rate": self.base_rate,
                "quote_rate": self.quote_rate,
                "swap_points": self.swap_points,
                "symbol": self.symbol,
                "sources": self.sources,
            },
        }


# ---------------------------------------------------------------------------
# Protocols for pluggable data sources
# ---------------------------------------------------------------------------


class CentralBankRateProvider(Protocol):
    """Interface for any central-bank rate feed."""

    def latest_rate(self, currency: str) -> Optional[float]:
        """Latest policy rate for ``currency`` in percent, or ``None``."""
        ...


class SwapRateProvider(Protocol):
    """Interface for an MT5-style swap feed."""

    def get_swap_points(self, symbol: str) -> Optional[tuple[float, float]]:
        """``(long_swap, short_swap)`` in points/day, or ``None``."""
        ...


# ---------------------------------------------------------------------------
# Currency mapping
# ---------------------------------------------------------------------------


_BASE_CCY: dict[str, str] = {
    "EURUSD": "EUR",
    "GBPUSD": "GBP",
    "AUDUSD": "AUD",
    "NZDUSD": "NZD",
    "USDJPY": "USD",
    "USDCHF": "USD",
    "USDCAD": "USD",
    "EURJPY": "EUR",
    "GBPJPY": "GBP",
    "EURGBP": "EUR",
}

_QUOTE_CCY: dict[str, str] = {
    "EURUSD": "USD",
    "GBPUSD": "USD",
    "AUDUSD": "USD",
    "NZDUSD": "USD",
    "USDJPY": "JPY",
    "USDCHF": "CHF",
    "USDCAD": "CAD",
    "EURJPY": "JPY",
    "GBPJPY": "JPY",
    "EURGBP": "GBP",
}


def base_currency(symbol: str) -> Optional[str]:
    return _BASE_CCY.get(symbol.upper())


def quote_currency(symbol: str) -> Optional[str]:
    return _QUOTE_CCY.get(symbol.upper())


# ---------------------------------------------------------------------------
# ECB SDMX provider — raw urllib, no extra deps
# ---------------------------------------------------------------------------


class ECBSDMXProvider:
    """Fetch ECB Main Refinancing Rate via the public SDMX REST endpoint.

    The endpoint requires no API key. We cache results locally so a missed
    daily fetch doesn't compound network flakiness. The parser is
    best-effort: it extracts ``{"date": ..., "rate": float}`` rows from
    the JSON dataset structure, falling back to a static lookup table if
    parsing fails (the parser intentionally swallows structural mismatches
    because the SDMX schema drifts between releases).
    """

    ENDPOINT_TEMPLATE = (
        "https://data-api.ecb.europa.eu/service/data/ECB/MR/UST..?format=jsondata&startPeriod={start}&endPeriod={end}"
    )

    # Static fallback — used when the live call is blocked or unparseable.
    # Main Refinancing Rate (fixed-rate tender) end-of-month, percent.
    _STATIC_FALLBACK: list[dict] = [
        {"date": "2022-07-27", "rate": 0.50},
        {"date": "2022-09-14", "rate": 1.25},
        {"date": "2022-10-02", "rate": 1.50},
        {"date": "2022-12-21", "rate": 2.50},
        {"date": "2023-09-20", "rate": 4.50},
        {"date": "2024-06-06", "rate": 4.25},
        {"date": "2024-09-18", "rate": 3.65},
        {"date": "2024-12-18", "rate": 3.40},
        {"date": "2025-01-01", "rate": 3.40},
    ]

    def __init__(
        self,
        cache_path: str = "data/ecb_cache.json",
        timeout: float = 10.0,
        fetcher: Optional[object] = None,
    ) -> None:
        self._cache_path = Path(cache_path)
        self._timeout = timeout
        self._fetcher = fetcher  # injectable HTTP fetcher for tests
        self._cache: dict[str, list[dict]] = self._load_cache()

    def _load_cache(self) -> dict[str, list[dict]]:
        if not self._cache_path.exists():
            return {}
        try:
            return json.loads(self._cache_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("ECB cache load failed (%s); starting empty", exc)
            return {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, indent=2))
            tmp.rename(self._cache_path)
        except OSError as exc:
            logger.error("ECB cache save failed: %s", exc)

    def latest_eur_rate(self) -> Optional[float]:
        """Latest EUR Main Refinancing Rate (%), or ``None`` on total failure."""
        rows = self._get_rows()
        if not rows:
            return None
        return float(rows[-1]["rate"])

    def latest_rate(self, currency: str) -> Optional[float]:
        if currency.upper() != "EUR":
            return None
        return self.latest_eur_rate()

    def _get_rows(self) -> list[dict]:
        cache_key = "ecb_mrr"
        if cache_key in self._cache and self._cache[cache_key]:
            return list(self._cache[cache_key])

        try:
            end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            start = (datetime.now(timezone.utc) - timedelta(days=365 * 5)).strftime("%Y-%m-%d")
            url = self.ENDPOINT_TEMPLATE.format(start=start, end=end)
            payload = self._fetch(url)
            parsed = self._parse(payload)
            if parsed:
                self._cache[cache_key] = parsed
                self._save_cache()
                return list(parsed)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            logger.warning("ECB SDMX fetch failed (%s); using static table", exc)

        # Fallback to static table — daily-cadence regime classification
        # can survive a few days without a live ECB reading.
        self._cache[cache_key] = list(self._STATIC_FALLBACK)
        self._save_cache()
        return list(self._STATIC_FALLBACK)

    def _fetch(self, url: str) -> str:
        if self._fetcher is not None:
            payload = self._fetcher(url)  # type: ignore[misc]
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            return payload
        with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8")

    def _parse(self, payload: str) -> list[dict]:
        """Best-effort parse of the ECB jsondata SDMX response.

        The ECB SDMX JSON schema includes ``structure`` (dimensions /
        attributes) and ``dataSets[0].observations``. Real dates live in
        the structure metadata and observations are keyed by ordinal index.
        For this prototype we extract numeric values and arrange them by
        observation order, assuming one MRR observation per period in the
        requested range. If parsing fails, we return an empty list and the
        caller falls back to the static table.
        """
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return []

        try:
            datasets = data.get("dataSets") or []
            if not datasets:
                return []
            obs = (datasets[0] or {}).get("observations") or {}
            values: list[float] = []
            for idx in sorted(obs.keys(), key=lambda k: int(k) if k.lstrip("-").isdigit() else 0):
                v = obs[idx]
                if isinstance(v, list) and v:
                    v = v[0]
                try:
                    values.append(float(v))
                except (TypeError, ValueError):
                    continue
            if not values:
                return []
            # Use static dates for the prototype — a full SDMX parser would
            # cross-reference structure/dimensions to get exact dates.
            dates = [r["date"] for r in self._STATIC_FALLBACK]
            rows: list[dict] = []
            for i, v in enumerate(values):
                date = dates[i] if i < len(dates) else f"series-{i:04d}"
                rows.append({"date": date, "rate": float(v)})
            rows.sort(key=lambda r: r["date"])
            return rows
        except Exception as exc:
            logger.debug("ECB payload parse failed: %s", exc)
            return []


# ---------------------------------------------------------------------------
# MT5 swap feed — CSV-backed, injectable
# ---------------------------------------------------------------------------


class MT5SwapFileProvider:
    """Read MT5 swap points from a small CSV file.

    Expected format (header row required)::

        symbol,long_swap_points,short_swap_points,updated_at
        USDJPY,12.5,-15.0,2026-01-15
        EURUSD,...

    Production deployments populate this file from a daily MT5 script;
    tests pass a ``records`` list directly via :class:`InMemorySwapProvider`.
    """

    def __init__(
        self,
        csv_path: str = "data/mt5_swaps.csv",
        records: Optional[list[dict]] = None,
    ) -> None:
        self._path = Path(csv_path)
        self._cache: dict[str, tuple[float, float]] = {}
        if records is not None:
            for r in records:
                self._cache[r["symbol"].upper()] = (
                    float(r["long_swap_points"]),
                    float(r["short_swap_points"]),
                )
        else:
            self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open() as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = row["symbol"].upper()
                    self._cache[sym] = (
                        float(row["long_swap_points"]),
                        float(row["short_swap_points"]),
                    )
        except (OSError, KeyError, ValueError) as exc:
            logger.warning("MT5 swap CSV load failed (%s)", exc)

    def get_swap_points(self, symbol: str) -> Optional[tuple[float, float]]:
        return self._cache.get(symbol.upper())


class InMemorySwapProvider:
    """In-memory swap provider for tests."""

    def __init__(self, swaps: dict[str, tuple[float, float]]) -> None:
        self._swaps = {sym.upper(): val for sym, val in swaps.items()}

    def get_swap_points(self, symbol: str) -> Optional[tuple[float, float]]:
        return self._swaps.get(symbol.upper())


# ---------------------------------------------------------------------------
# Default rate provider — combines FRED (USD) + ECB SDMX (EUR) + static for others
# ---------------------------------------------------------------------------


class CompositeRateProvider:
    """Default rate provider combining FRED, ECB SDMX, and a static fallback."""

    _STATIC: dict[str, float] = {
        "GBP": 4.75,
        "AUD": 4.35,
        "NZD": 4.25,
        "JPY": 0.25,
        "CHF": 1.00,
        "CAD": 3.75,
    }

    def __init__(
        self,
        fred: Optional[FredFetcher] = None,
        ecb: Optional[ECBSDMXProvider] = None,
        static: Optional[dict[str, float]] = None,
    ) -> None:
        self._fred = fred or FredFetcher()
        self._ecb = ecb or ECBSDMXProvider()
        self._static = static if static is not None else dict(self._STATIC)

    def latest_rate(self, currency: str) -> Optional[float]:
        currency = currency.upper()
        if currency == "USD":
            return self._fred.latest_us_policy_rate()
        if currency == "EUR":
            return self._ecb.latest_eur_rate()
        return self._static.get(currency)


# ---------------------------------------------------------------------------
# Carry signal provider — combines the three sources
# ---------------------------------------------------------------------------


class CarrySignalProvider:
    """Combine FRED + ECB SDMX + MT5 swap into a per-pair carry regime."""

    def __init__(
        self,
        rate_provider: Optional[CentralBankRateProvider] = None,
        swap_provider: Optional[SwapRateProvider] = None,
        min_rate_diff: float = 0.5,
    ) -> None:
        self._rates = rate_provider or CompositeRateProvider()
        self._swaps = swap_provider or MT5SwapFileProvider()
        self._min_rate_diff = float(min_rate_diff)

    def evaluate_carry(
        self,
        symbol: str,
        direction: str,
    ) -> CarrySignal:
        """Classify the carry regime for ``(symbol, direction)``.

        Carry is the differential between the rate you earn (base currency
        when long) and the rate you pay (quote currency when long). For a
        *long* position, a positive ``rate_diff = base_rate - quote_rate``
        is carry-positive. For a *short* position it's the inverse.
        """
        symbol = symbol.upper()
        direction = direction.lower()
        if direction not in ("long", "short"):
            raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

        base = base_currency(symbol)
        quote = quote_currency(symbol)
        base_rate = self._rates.latest_rate(base) if base else None
        quote_rate = self._rates.latest_rate(quote) if quote else None
        swap_points = self._swaps.get_swap_points(symbol)

        rate_diff: Optional[float] = None
        if base_rate is not None and quote_rate is not None:
            rate_diff = float(base_rate) - float(quote_rate)

        regime = self._classify(rate_diff, swap_points, direction)
        sources = {
            "base_rate": (base or "?").lower(),
            "quote_rate": (quote or "?").lower(),
            "swap": "mt5-csv",
        }

        return CarrySignal(
            symbol=symbol,
            direction=direction,
            base_rate=base_rate,
            quote_rate=quote_rate,
            swap_points=swap_points,
            rate_diff=rate_diff,
            regime=regime,
            sources=sources,
        )

    def _classify(
        self,
        rate_diff: Optional[float],
        swap_points: Optional[tuple[float, float]],
        direction: str,
    ) -> CarryRegime:
        """Decide regime from the differential and an optional swap hint."""
        if rate_diff is None:
            return CarryRegime.NEUTRAL
        if abs(rate_diff) < self._min_rate_diff:
            return CarryRegime.NEUTRAL

        # Long earns the diff; short is the inverse (pays it).
        if direction == "long":
            favoured = rate_diff > 0
        else:  # short
            favoured = rate_diff < 0

        # A swap hint can override when the broker roll is extreme.
        # Negative swaps for the proposed direction downgrade to NEGATIVE.
        if swap_points is not None:
            long_swap, short_swap = swap_points
            relevant = long_swap if direction == "long" else short_swap
            if relevant is not None and relevant < -50:  # 50 points/day ≈ 5 USD/lot
                return CarryRegime.NEGATIVE

        return CarryRegime.POSITIVE if favoured else CarryRegime.NEGATIVE

    def to_confluence(self, symbol: str, direction: str) -> Optional[dict]:
        """Returns a confluence entry for the ConfidenceEngine, or ``None``."""
        return self.evaluate_carry(symbol, direction).to_confluence()


# ---------------------------------------------------------------------------
# ConfidenceEngine integration helpers
# ---------------------------------------------------------------------------


def score_with_carry(
    engine,
    provider: CarrySignalProvider,
    raw_confidence: float,
    *,
    symbol: str = "",
    direction: str = "long",
    spread: float = 0.0,
    atr: float = 0.0,
    hour_utc: int = 0,
    confluences: Optional[list[dict]] = None,
    include_carry: bool = True,
):
    """Run the confidence engine with the carry regime injected as a confluence.

    This wraps :meth:`confidence.engine.ConfidenceEngine.score` without
    mutating the engine. When ``include_carry`` is True (default), the
    provider's regime is appended to the ``confluences`` list before
    gating. Neutral regimes add no entry, so the engine behaves as if
    carry wasn't consulted at all.
    """
    confluences = list(confluences or [])
    if include_carry:
        carry_confluence = provider.to_confluence(symbol, direction)
        if carry_confluence is not None:
            confluences.append(carry_confluence)

    return engine.score(
        raw_confidence,
        symbol=symbol,
        direction=direction,
        spread=spread,
        atr=atr,
        hour_utc=hour_utc,
        confluences=confluences,
    )


__all__ = [
    "CarryRegime",
    "CarrySignal",
    "CarrySignalProvider",
    "CentralBankRateProvider",
    "SwapRateProvider",
    "CompositeRateProvider",
    "ECBSDMXProvider",
    "MT5SwapFileProvider",
    "InMemorySwapProvider",
    "score_with_carry",
    "base_currency",
    "quote_currency",
]
