"""CFTC Commitments of Traders (COT) data fetcher.

Fetches weekly COT reports from the CFTC in Legacy and Disaggregated
formats. COT data reflects futures market positioning and is used as
a weekly confidence multiplier — not an entry signal.

Key constraints:
  - 3-day publication lag (Friday release for Tuesday close)
  - Futures positioning, not spot FX — divergences during regime
    changes ARE the signal
  - Useless for entry timing; valuable for weekly regime classification

Data sources:
  Legacy:         https://www.cftc.gov/files/dea/history/fut_txt_YYYY.zip
  Disaggregated:  https://www.cftc.gov/files/dea/history/com_dis_txt_YYYY.zip
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

CFTC_BASE = "https://www.cftc.gov/files/dea/history"

LEGACY_TEMPLATE = f"{CFTC_BASE}/fut_txt_{{year}}.zip"
DISAGG_TEMPLATE = f"{CFTC_BASE}/com_dis_txt_{{year}}.zip"

# Forex futures market name mappings (CFTC market names → ISO pairs)
FOREX_MARKET_MAP = {
    "JAPANESE YEN": "JPY",
    "EURO FX": "EUR",
    "BRITISH POUND": "GBP",
    "SWISS FRANC": "CHF",
    "CANADIAN DOLLAR": "CAD",
    "AUSTRALIAN DOLLAR": "AUD",
    "NEW ZEALAND DOLLAR": "NZD",
}

# USD-quoted pairs derived from individual currency positioning
USD_QUOTED_PAIRS = {
    "USDJPY": "JAPANESE YEN",
    "EURUSD": "EURO FX",
    "GBPUSD": "BRITISH POUND",
    "USDCHF": "SWISS FRANC",
    "USDCAD": "CANADIAN DOLLAR",
    "AUDUSD": "AUSTRALIAN DOLLAR",
    "NZDUSD": "NEW ZEALAND DOLLAR",
}

USER_AGENT = "Mozilla/5.0 (Ayumi COT Fetcher)"


class COTFormat(str, Enum):
    """Supported CFTC COT report formats."""

    LEGACY = "legacy"
    DISAGGREGATED = "disaggregated"


@dataclass
class COTPositioning:
    """Parsed positioning snapshot for a single currency/market."""

    market_name: str
    report_date: str  # ISO date string (Tuesday close)
    format: COTFormat

    # Legacy fields
    long_open_interest: float = 0.0
    short_open_interest: float = 0.0

    # Non-commercial (speculative) positioning
    non_comm_long: float = 0.0
    non_comm_short: float = 0.0
    non_comm_spread: float = 0.0

    # Commercial (hedger) positioning
    comm_long: float = 0.0
    comm_short: float = 0.0

    # Derived metrics
    net_position: float = 0.0  # non-commercial net
    total_open_interest: float = 0.0

    def __post_init__(self) -> None:
        self.net_position = self.non_comm_long - self.non_comm_short
        self.total_open_interest = self.non_comm_long + self.non_comm_short + self.comm_long + self.comm_short

    @property
    def net_ratio(self) -> float:
        """Net non-commercial position as fraction of total OI."""
        if self.total_open_interest == 0:
            return 0.0
        return self.net_position / self.total_open_interest


@dataclass
class COTDivergenceSignal:
    """Directional bias derived from COT positioning shifts.

    Used as a confidence multiplier: when positioning aligns with
    trade direction, confidence gets a small boost. When divergent,
    confidence is penalised.
    """

    currency: str
    signal_date: str  # ISO date
    bias: str  # "long", "short", "neutral"
    strength: float  # -1.0 (max short) to +1.0 (max long)
    confidence_adjustment: float  # multiplier delta, e.g. -0.05 to +0.05
    rationale: str = ""


# ─── Fetcher ─────────────────────────────────────────────────────────────────


class COTFetcher:
    """Fetch and parse CFTC COT reports in Legacy and Disaggregated formats.

    Usage:
        fetcher = COTFetcher()
        jpy_data = fetcher.get_positioning("USDJPY", fmt=COTFormat.LEGACY)
        signal = fetcher.get_divergence_signal("USDJPY")
    """

    def __init__(self, cache=None, timeout: int = 30):
        self._cache = cache
        self._timeout = timeout

    # ── Public API ────────────────────────────────────────────────────────────

    def get_positioning(
        self,
        pair: str,
        fmt: COTFormat = COTFormat.LEGACY,
        report_date: Optional[str] = None,
    ) -> Optional[COTPositioning]:
        """Get latest (or specific) COT positioning for a forex pair.

        Args:
            pair: FX pair, e.g. "USDJPY", "EURUSD".
            fmt: Legacy or Disaggregated format.
            report_date: Optional ISO date for a specific report.

        Returns:
            COTPositioning or None if not found.
        """
        market_name = USD_QUOTED_PAIRS.get(pair.upper())
        if not market_name:
            logger.warning("Unknown FX pair: %s", pair)
            return None

        records = self._load_reports(fmt, report_date)
        if not records:
            return None

        # Filter to this market, sorted by date descending
        matching = [r for r in records if r.market_name.upper() == market_name.upper()]
        if not matching:
            logger.debug("No COT data for %s (%s)", market_name, fmt.value)
            return None

        matching.sort(key=lambda r: r.report_date, reverse=True)
        if report_date:
            for r in matching:
                if r.report_date == report_date:
                    return r
            return None

        return matching[0]  # latest

    def get_divergence_signal(
        self,
        pair: str,
        lookback_weeks: int = 4,
        fmt: COTFormat = COTFormat.LEGACY,
    ) -> COTDivergenceSignal:
        """Compute directional bias from COT positioning trend.

        Compares recent net positioning against the lookback average.
        A regime change (shift from net-long to net-short or vice versa)
        produces a stronger signal.

        Args:
            pair: FX pair.
            lookback_weeks: Number of weeks for trend comparison.
            fmt: Report format.

        Returns:
            COTDivergenceSignal with bias and confidence adjustment.
        """
        market_name = USD_QUOTED_PAIRS.get(pair.upper(), pair)
        records = self._load_reports(fmt)
        matching = [r for r in records if r.market_name.upper() == market_name.upper()]
        matching.sort(key=lambda r: r.report_date, reverse=True)

        if len(matching) < 2:
            return COTDivergenceSignal(
                currency=pair,
                signal_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                bias="neutral",
                strength=0.0,
                confidence_adjustment=0.0,
                rationale="Insufficient COT history",
            )

        recent = matching[: min(lookback_weeks, len(matching))]
        current = recent[0]
        prior_avg_net = sum(r.net_position for r in recent[1:]) / max(len(recent) - 1, 1)

        # For USD-quoted pairs where the CFTC reports the non-USD currency:
        # JPY long = USDJPY short, so invert the signal
        invert = pair.upper().startswith("USD") and pair.upper().endswith(("JPY", "CHF", "CAD"))
        raw_net = current.net_position * (-1 if invert else 1)

        # Prior average on same (possibly inverted) basis as raw_net
        prior_inverted = prior_avg_net * (-1 if invert else 1)

        # Normalise: compare current to prior average
        shift = raw_net - prior_inverted
        total_oi = current.total_open_interest or 1.0
        normalised_shift = max(-1.0, min(1.0, shift / total_oi))

        # Determine bias from current net positioning direction
        if raw_net > 0:
            bias = "long"
        elif raw_net < 0:
            bias = "short"
        else:
            bias = "neutral"

        # Confidence adjustment: scale the normalised shift
        # Cap at ±0.05 to act as a multiplier, not a dominant signal
        confidence_adjustment = max(-0.05, min(0.05, normalised_shift * 0.1))

        # Detect regime change (on same basis as bias)
        prior_bias = "long" if prior_inverted > 0 else ("short" if prior_inverted < 0 else "neutral")
        regime_change = bias != prior_bias and bias != "neutral"

        rationale_parts = [f"Current net: {current.net_position:,.0f}"]
        if regime_change:
            rationale_parts.append(f"Regime change: {prior_bias} → {bias}")
            confidence_adjustment *= 1.5  # amplify regime changes
            confidence_adjustment = max(-0.05, min(0.05, confidence_adjustment))

        rationale_parts.append(f"Prior {lookback_weeks}w avg: {prior_avg_net:,.0f}")

        return COTDivergenceSignal(
            currency=pair,
            signal_date=current.report_date,
            bias=bias,
            strength=normalised_shift,
            confidence_adjustment=confidence_adjustment,
            rationale="; ".join(rationale_parts),
        )

    def apply_to_confidence(
        self,
        raw_confidence: float,
        pair: str,
        direction: str = "long",
    ) -> float:
        """Apply COT divergence signal as a confidence multiplier.

        This is the integration contract for the ConfidenceEngine: callers
        feed in the strategy's raw confidence score and the trade's
        direction, and get back the COT-adjusted confidence. The
        multiplier is intentionally small (±0.05 max) — COT is a
        structural weekly indicator, not a tactical signal.

        Alignment logic:
          - signal.bias == direction → COT confirms trade → boost confidence
          - signal.bias != direction → COT warns against trade → reduce
            confidence
          - signal.bias == "neutral" → insufficient data → no change

        Args:
            raw_confidence: Strategy's raw confidence score (0.0-1.0).
            pair: FX pair being traded.
            direction: Trade direction ("long" or "short").

        Returns:
            Adjusted confidence score clamped to [0.0, 1.0].
        """
        signal = self.get_divergence_signal(pair)

        # Neutral COT (insufficient data) → no adjustment
        if signal.bias == "neutral":
            return max(0.0, min(1.0, raw_confidence))

        # Magnitude of the multiplier (always non-negative)
        magnitude = abs(signal.confidence_adjustment)

        if signal.bias == direction:
            # Aligned: boost confidence by COT shift magnitude
            adjusted = raw_confidence + magnitude
        else:
            # Opposed: penalise confidence by COT shift magnitude
            adjusted = raw_confidence - magnitude

        return max(0.0, min(1.0, adjusted))

    # ── Internal: Download & Parse ────────────────────────────────────────────

    def _load_reports(
        self,
        fmt: COTFormat,
        report_date: Optional[str] = None,
    ) -> list[COTPositioning]:
        """Load reports from cache or fetch from CFTC."""
        year = report_date[:4] if report_date else str(datetime.now(timezone.utc).year)

        # Try cache first
        if self._cache is not None:
            cached = self._cache.get(fmt, year)
            if cached is not None:
                return cached

        # Fetch from CFTC
        try:
            records = self._download_and_parse(year, fmt)
        except Exception as exc:
            logger.error("Failed to fetch COT %s %s: %s", fmt.value, year, exc)
            # Try previous year as fallback
            prev_year = str(int(year) - 1)
            try:
                records = self._download_and_parse(prev_year, fmt)
            except Exception as exc2:
                logger.error("Fallback fetch also failed: %s", exc2)
                return []

        if records and self._cache is not None:
            self._cache.put(fmt, year, records)

        return records

    def _download_and_parse(self, year: str, fmt: COTFormat) -> list[COTPositioning]:
        """Download ZIP from CFTC and parse CSV contents."""
        url = self._build_url(year, fmt)
        logger.info("Fetching COT %s %s from %s", fmt.value, year, url)

        raw = self._http_get(url)
        csv_data = self._extract_csv_from_zip(raw, fmt)
        return self._parse_csv(csv_data, fmt)

    def _build_url(self, year: str, fmt: COTFormat) -> str:
        if fmt == COTFormat.LEGACY:
            return LEGACY_TEMPLATE.format(year=year)
        return DISAGG_TEMPLATE.format(year=year)

    def _http_get(self, url: str) -> bytes:
        """Download content from URL."""
        req = Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
        with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
            return resp.read()

    @staticmethod
    def _extract_csv_from_zip(zip_data: bytes, fmt: COTFormat) -> str:
        """Extract the main CSV text from a CFTC ZIP archive."""
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            names = zf.namelist()
            if not names:
                raise ValueError("Empty ZIP archive")
            # CFTC ZIPs contain a single .txt file
            target = names[0]
            for name in names:
                if name.lower().endswith(".txt"):
                    target = name
                    break
            with zf.open(target) as f:
                return f.read().decode("utf-8", errors="replace")

    @staticmethod
    def _parse_csv(csv_text: str, fmt: COTFormat) -> list[COTPositioning]:
        """Parse CFTC COT CSV text into COTPositioning records.

        Only forex futures markets are extracted (filtered by FOREX_MARKET_MAP).
        """
        records: list[COTPositioning] = []
        reader = csv.reader(io.StringIO(csv_text))

        for row in reader:
            if len(row) < 6:
                continue

            market_name = row[0].strip().upper() if row[0] else ""
            if market_name not in FOREX_MARKET_MAP:
                continue

            try:
                if fmt == COTFormat.LEGACY:
                    rec = COTFetcher._parse_legacy_row(row, market_name)
                else:
                    rec = COTFetcher._parse_disaggregated_row(row, market_name)
                if rec:
                    records.append(rec)
            except (ValueError, IndexError) as exc:
                logger.debug("Skip row for %s: %s", market_name, exc)

        logger.info("Parsed %d COT %s records", len(records), fmt.value)
        return records

    @staticmethod
    def _parse_legacy_row(row: list[str], market_name: str) -> Optional[COTPositioning]:
        """Parse a Legacy format row.

        Columns (0-indexed):
          0: Market Name
          1: CFTC Contract Market Code
          ...
          2: Report Date (MM/DD/YYYY)
          4: Non-Commercial Long
          5: Non-Commercial Short
          6: Non-Commercial Spreading
          7: Commercial Long
          8: Commercial Short
        """
        report_date = COTFetcher._normalise_date(row[2] if len(row) > 2 else "")
        if not report_date:
            return None

        def _f(idx: int) -> float:
            if idx >= len(row):
                return 0.0
            val = (row[idx] or "").strip().replace(",", "")
            try:
                return float(val) if val else 0.0
            except ValueError:
                return 0.0

        return COTPositioning(
            market_name=market_name,
            report_date=report_date,
            format=COTFormat.LEGACY,
            non_comm_long=_f(4),
            non_comm_short=_f(5),
            non_comm_spread=_f(6),
            comm_long=_f(7),
            comm_short=_f(8),
        )

    @staticmethod
    def _parse_disaggregated_row(row: list[str], market_name: str) -> Optional[COTPositioning]:
        """Parse a Disaggregated format row.

        Columns differ from Legacy:
          0: Market Name
          ...
          2: Report Date (MM/DD/YYYY)
          4: Producer/Merchant/Processor Long
          5: Producer/Merchant/Processor Short
          ...
          8: Managed Money Long
          9: Managed Money Short
        Managed Money ≈ non-commercial for our purposes.
        """
        report_date = COTFetcher._normalise_date(row[2] if len(row) > 2 else "")
        if not report_date:
            return None

        def _f(idx: int) -> float:
            if idx >= len(row):
                return 0.0
            val = (row[idx] or "").strip().replace(",", "")
            try:
                return float(val) if val else 0.0
            except ValueError:
                return 0.0

        return COTPositioning(
            market_name=market_name,
            report_date=report_date,
            format=COTFormat.DISAGGREGATED,
            non_comm_long=_f(8),  # Managed Money Long
            non_comm_short=_f(9),  # Managed Money Short
            non_comm_spread=_f(10) if len(row) > 10 else 0.0,
            comm_long=_f(4),  # Producer/Merchant Long
            comm_short=_f(5),  # Producer/Merchant Short
        )

    @staticmethod
    def _normalise_date(raw: str) -> str:
        """Convert MM/DD/YYYY to YYYY-MM-DD."""
        raw = raw.strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""
