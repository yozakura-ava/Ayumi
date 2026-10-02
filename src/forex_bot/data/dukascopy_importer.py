#!/usr/bin/env python3
"""Dukascopy bi5 tick data importer.

Wraps the Dukascopy REST datafeed API into a clean, testable module.
Handles bi5 binary format parsing (LZMA-compressed ``>IIIff`` records),
price/time conversion, CSV writing, and DuckDB bulk import.

Format reference
----------------
Each bi5 file contains one hour of tick data.  After LZMA decompression,
every 20-byte record is::

    >IIIff
    ├── time_ms      (uint32, big-endian) — milliseconds since the hour start
    ├── ask_scaled   (uint32, big-endian) — ask price × 1_000_000
    ├── bid_scaled   (uint32, big-endian) — bid price × 1_000_000
    ├── ask_vol      (float32, big-endian) — ask volume
    └── bid_vol      (float32, big-endian) — bid volume

Usage
-----
::

    from forex_bot.data.dukascopy_importer import DukascopyImporter, Tick

    importer = DukascopyImporter(output_dir=Path("output"))
    ticks = importer.download_day("EURUSD", date(2024, 6, 3))
    importer.write_csv("EURUSD", date(2024, 6, 3), ticks)

Existing scripts (``harvest_ticks_overnight.py``, ``import_ticks.py``)
contain the same logic inline; this module exposes it through a stable
class-based API so it can be unit-tested and reused by the backtest
pipeline.
"""

from __future__ import annotations

import csv
import logging
import lzma
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

_BASE_URL = "https://datafeed.dukascopy.com/datafeed"
_TICK_SIZE = 20  # bytes per bi5 record
_RECORD_FORMAT = ">IIIff"  # big-endian: uint32, uint32, uint32, float32, float32
_PRICE_DIVISOR = 1_000_000  # raw int → real price

DEFAULT_RATE_LIMIT_RPS = 4.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF: tuple[int, ...] = (5, 15, 30)


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Tick:
    """Normalised tick record.

    ``timestamp_ms`` is wall-clock UTC milliseconds (epoch).
    Prices are real (already divided by the Dukascopy scaling factor).
    """

    timestamp_ms: int
    symbol: str
    bid: float
    ask: float
    bid_vol: float
    ask_vol: float


# ── Exceptions ──────────────────────────────────────────────────────────────


class DukascopyFetchError(Exception):
    """Raised when the Dukascopy API cannot be reached after all retries."""


# ── Public API ──────────────────────────────────────────────────────────────


class DukascopyImporter:
    """Download, parse, and persist Dukascopy bi5 tick data.

    Parameters
    ----------
    output_dir
        Directory where CSV files are written.  Created if it doesn't exist.
    rate_limit_rps
        Maximum requests per second to the Dukascopy API.
    max_retries
        Number of retry attempts on transient network errors.
    retry_backoff
        Seconds to wait between retries (indexed by attempt number).
    base_url
        Base URL for the Dukascopy datafeed (overridable for testing).
    """

    def __init__(
        self,
        output_dir: Path | None = None,
        *,
        rate_limit_rps: float = DEFAULT_RATE_LIMIT_RPS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: Sequence[int] = DEFAULT_RETRY_BACKOFF,
        base_url: str = _BASE_URL,
    ) -> None:
        self.output_dir = output_dir
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)

        self._min_interval = 1.0 / rate_limit_rps
        self._max_retries = max_retries
        self._retry_backoff = tuple(retry_backoff)
        self._base_url = base_url.rstrip("/")

    # -- URL construction ---------------------------------------------------

    def _hour_url(self, symbol: str, dt: date, hour: int) -> str:
        """Build the bi5 download URL for one hour of one day."""
        return f"{self._base_url}/{symbol}/{dt.year}/{dt.month:02d}/{dt.day:02d}/{hour:02d}h_ticks.bi5"

    # -- Network ------------------------------------------------------------

    def _fetch_bytes(self, url: str) -> bytes | None:
        """Fetch *url* with retries.

        Returns
        -------
        bytes
            The raw (still compressed) response body.
        b""
            The server returned 404 (no data for this slot — normal).
        None
            All retries exhausted.
        """
        for attempt in range(self._max_retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310
                with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return b""
                logger.warning(
                    "HTTP %d for %s (attempt %d/%d)",
                    exc.code,
                    url,
                    attempt + 1,
                    self._max_retries,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "%s: %s (attempt %d/%d)",
                    exc.__class__.__name__,
                    exc,
                    attempt + 1,
                    self._max_retries,
                )

            if attempt < self._max_retries - 1:
                wait = self._retry_backoff[min(attempt, len(self._retry_backoff) - 1)]
                time.sleep(wait)

        return None

    # -- bi5 parsing --------------------------------------------------------

    @staticmethod
    def parse_bi5(raw: bytes, symbol: str, hour_start_epoch: int) -> list[Tick]:
        """Decompress and parse a bi5 blob into :class:`Tick` records.

        Parameters
        ----------
        raw
            Raw LZMA-compressed bytes as returned by the API.
        symbol
            Instrument symbol (e.g. ``"EURUSD"``).
        hour_start_epoch
            Unix timestamp (seconds) for the start of the hour.

        Returns
        -------
        list[Tick]
            May be empty if the blob contained no records.
        """
        if not raw:
            return []

        try:
            decompressed = lzma.decompress(raw)
        except lzma.LZMAError as exc:
            logger.error("LZMA decompress failed: %s", exc)
            return []

        ticks: list[Tick] = []
        n_records = len(decompressed) // _TICK_SIZE
        base_ms = hour_start_epoch * 1000

        for i in range(n_records):
            offset = i * _TICK_SIZE
            ms_within_hour, ask_raw, bid_raw, ask_vol, bid_vol = struct.unpack_from(
                _RECORD_FORMAT, decompressed, offset
            )

            ticks.append(
                Tick(
                    timestamp_ms=base_ms + ms_within_hour,
                    symbol=symbol,
                    bid=bid_raw / _PRICE_DIVISOR,
                    ask=ask_raw / _PRICE_DIVISOR,
                    bid_vol=bid_vol,
                    ask_vol=ask_vol,
                )
            )

        return ticks

    # -- High-level download ------------------------------------------------

    def fetch_hour_ticks(self, symbol: str, dt: date, hour: int) -> list[Tick]:
        """Download and parse a single hour of ticks.

        Raises :class:`DukascopyFetchError` on total network failure.
        """
        url = self._hour_url(symbol, dt, hour)
        last = time.monotonic()

        raw = self._fetch_bytes(url)
        if raw is None:
            raise DukascopyFetchError(f"Failed to fetch {url} after {self._max_retries} retries")

        hour_start = int(datetime(dt.year, dt.month, dt.day, hour, tzinfo=timezone.utc).timestamp())
        ticks = self.parse_bi5(raw, symbol, hour_start)

        # Throttle
        elapsed = time.monotonic() - last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        return ticks

    def download_day(self, symbol: str, dt: date) -> list[Tick]:
        """Download all 24 hours of tick data for one day.

        Weekend days (*Saturday* / *Sunday*) return an empty list without
        hitting the network.
        """
        if _is_weekend(dt):
            return []

        day_ticks: list[Tick] = []
        for hour in range(24):
            try:
                day_ticks.extend(self.fetch_hour_ticks(symbol, dt, hour))
            except DukascopyFetchError as exc:
                logger.error("Skipping hour %02d: %s", hour, exc)
        return day_ticks

    def download_range(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> Iterator[list[Tick]]:
        """Yield one day's worth of ticks at a time.

        Skips days whose CSV already exists in ``output_dir`` when it is
        set, enabling safe restarts.
        """
        current = start
        while current <= end:
            if self.output_dir is not None:
                fname = f"{symbol}_{current.year}{current.month:02d}{current.day:02d}.csv"
                if (self.output_dir / fname).exists():
                    current += timedelta(days=1)
                    continue
            yield self.download_day(symbol, current)
            current += timedelta(days=1)

    # -- CSV writing --------------------------------------------------------

    def write_csv(self, symbol: str, dt: date, ticks: Sequence[Tick]) -> Path | None:
        """Write ticks for one day to CSV.

        Returns the path written, or ``None`` if ``output_dir`` was not
        configured or *ticks* is empty.
        """
        if self.output_dir is None or not ticks:
            return None

        filename = f"{symbol}_{dt.year}{dt.month:02d}{dt.day:02d}.csv"
        outpath = self.output_dir / filename

        with open(outpath, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["timestamp", "instrument", "bid", "ask", "bidVol", "askVol"])
            for t in ticks:
                writer.writerow([t.timestamp_ms, t.symbol, t.bid, t.ask, t.bid_vol, t.ask_vol])

        return outpath


# ── Helpers ─────────────────────────────────────────────────────────────────


def _is_weekend(dt: date) -> bool:
    """Return *True* for Saturday or Sunday."""
    return dt.weekday() >= 5
