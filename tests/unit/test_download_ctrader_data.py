"""Unit tests for scripts/download_ctrader_data.py — card 291de428.

Covers:
  - argparse: --symbols / --timeframes / --date-range parsing
  - whitelist enforcement (G6)
  - date range sanity
  - ISO-date validation rejects malformed dates
  - csv-list parser uppercases and trims
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

import pytest

# Make scripts/ importable
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from download_ctrader_data import (  # noqa: E402
    ALLOWED_SYMBOLS,
    ALLOWED_TIMEFRAMES,
    _csv,
    _iso_date,
    build_parser,
    validate_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ns(**overrides):
    """Build a Namespace with the same defaults as build_parser()."""
    parser = build_parser()
    defaults = {
        "symbols": ["XAUUSD"],
        "timeframes": ["M3", "M5", "M15", "M30", "H1"],
        "start": "2026-07-13",
        "end": "2026-10-04",
        "csv_dir": SCRIPTS_DIR.parent / "data" / "forex" / "historical",
        "db": SCRIPTS_DIR.parent / "data" / "ayumi_market.duckdb",
        "dry_run": True,
        "allow_ms": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# csv-list parser
# ---------------------------------------------------------------------------


class TestCsvListParser:
    def test_simple(self):
        assert _csv("M3,M5,M15") == ["M3", "M5", "M15"]

    def test_uppercases(self):
        assert _csv("xauusd,m3") == ["XAUUSD", "M3"]

    def test_trims_whitespace(self):
        assert _csv(" M3 , M5 ,  M15 ") == ["M3", "M5", "M15"]

    def test_empty_returns_empty(self):
        assert _csv("") == []

    def test_single(self):
        assert _csv("XAUUSD") == ["XAUUSD"]


# ---------------------------------------------------------------------------
# ISO-date parser
# ---------------------------------------------------------------------------


class TestIsoDateParser:
    def test_valid(self):
        assert _iso_date("2026-07-13") == "2026-07-13"

    def test_invalid_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _iso_date("2026-13-99")

    def test_invalid_format_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _iso_date("07/13/2026")


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_required_start_and_end(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])  # --start and --end are required

    def test_default_symbol_and_timeframes(self):
        parser = build_parser()
        ns = parser.parse_args(["--start", "2026-07-13", "--end", "2026-10-04"])
        assert ns.symbols == ["XAUUSD"]
        assert ns.timeframes == ["M3", "M5", "M15", "M30", "H1"]
        # default mode is dry-run
        assert ns.dry_run is True

    def test_apply_flag_flips_dry_run(self):
        parser = build_parser()
        ns = parser.parse_args(
            ["--start", "2026-07-13", "--end", "2026-10-04", "--apply"]
        )
        assert ns.dry_run is False

    def test_dry_run_explicit(self):
        parser = build_parser()
        ns = parser.parse_args(
            ["--start", "2026-07-13", "--end", "2026-10-04", "--dry-run"]
        )
        assert ns.dry_run is True

    def test_allow_ms_flag(self):
        parser = build_parser()
        ns = parser.parse_args(
            ["--start", "2026-07-13", "--end", "2026-10-04", "--allow-ms"]
        )
        assert ns.allow_ms is True

    def test_multi_symbol_csv_parsing(self):
        parser = build_parser()
        ns = parser.parse_args(
            [
                "--symbols", "XAUUSD,EURUSD",
                "--timeframes", "M3,M15",
                "--start", "2026-07-13",
                "--end", "2026-10-04",
            ]
        )
        assert ns.symbols == ["XAUUSD", "EURUSD"]
        assert ns.timeframes == ["M3", "M15"]


# ---------------------------------------------------------------------------
# Whitelist + date sanity (G6, G4)
# ---------------------------------------------------------------------------


class TestValidateRequest:
    def test_default_xauusd_request_is_valid(self):
        validate_request(_ns())  # must not raise

    def test_eurusd_symbol_rejected(self):
        with pytest.raises(ValueError, match="not in hard whitelist"):
            validate_request(_ns(symbols=["EURUSD"]))

    def test_unknown_symbol_rejected(self):
        with pytest.raises(ValueError, match="not in hard whitelist"):
            validate_request(_ns(symbols=["BTCUSD"]))

    def test_unknown_timeframe_rejected(self):
        with pytest.raises(ValueError, match="not in hard whitelist"):
            validate_request(_ns(timeframes=["H4"]))

    def test_d1_timeframe_rejected(self):
        # H4/D1 excluded from matrix per pivot Q2
        with pytest.raises(ValueError, match="not in hard whitelist"):
            validate_request(_ns(timeframes=["D1"]))

    def test_start_after_end_rejected(self):
        with pytest.raises(ValueError, match="must be <="):
            validate_request(_ns(start="2026-10-04", end="2026-07-13"))

    def test_equal_start_end_allowed(self):
        validate_request(_ns(start="2026-10-04", end="2026-10-04"))


# ---------------------------------------------------------------------------
# Constants — sanity check (defensive)
# ---------------------------------------------------------------------------


class TestWhitelistConstants:
    def test_xauusd_only(self):
        assert ALLOWED_SYMBOLS == frozenset({"XAUUSD"})

    def test_timeframes_match_pivot_q2(self):
        # Pivot Q2: M3, M5, M15, M30, H1 (H4/D1 excluded)
        assert ALLOWED_TIMEFRAMES == frozenset({"M3", "M5", "M15", "M30", "H1"})