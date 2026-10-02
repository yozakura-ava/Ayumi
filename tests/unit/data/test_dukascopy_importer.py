"""Unit tests for the Dukascopy bi5 tick importer.

Covers the three concerns that previously lived as inline script logic:

* Binary format parsing (LZMA + ``>IIIff`` struct).
* Time / price conversion against the Dukascopy scaling rules.
* The high-level CSV writer and URL builder, exercised with no network.

The HTTP fetcher is mocked so the suite runs offline.
"""

from __future__ import annotations

import csv
import lzma
import struct
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

# Ensure the package root is importable when running this file in isolation.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "src" / "forex_bot"))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from data.dukascopy_importer import (  # type: ignore  # noqa: E402, I001
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT_RPS,
    DEFAULT_RETRY_BACKOFF,
    DukascopyFetchError,
    DukascopyImporter,
    Tick,
    _is_weekend,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_bi5_record(
    time_ms: int,
    ask_scaled: int,
    bid_scaled: int,
    ask_vol: float,
    bid_vol: float,
) -> bytes:
    """Pack a single 20-byte bi5 record."""
    return struct.pack(">IIIff", time_ms, ask_scaled, bid_scaled, ask_vol, bid_vol)


def _make_bi5_blob(records: list[tuple]) -> bytes:
    """LZMA-compress a list of bi5 records into a single blob."""
    raw = b"".join(_make_bi5_record(*r) for r in records)
    return lzma.compress(raw)


# Reference: 2024-06-03 12:00:00 UTC = 1717416000 epoch seconds
_HOUR_START_EPOCH = int(datetime(2024, 6, 3, 12, tzinfo=timezone.utc).timestamp())


# ── Test cases ─────────────────────────────────────────────────────────────


class TestTickDataclass(unittest.TestCase):
    def test_is_frozen_and_slotted(self):
        t = Tick(
            timestamp_ms=1717416000123,
            symbol="EURUSD",
            bid=1.085,
            ask=1.0852,
            bid_vol=1.5,
            ask_vol=2.0,
        )
        self.assertEqual(t.timestamp_ms, 1717416000123)
        self.assertEqual(t.symbol, "EURUSD")
        self.assertEqual(t.bid, 1.085)
        self.assertEqual(t.ask, 1.0852)
        self.assertEqual(t.bid_vol, 1.5)
        self.assertEqual(t.ask_vol, 2.0)
        with self.assertRaises(Exception):  # noqa: B017
            t.bid = 9.999  # type: ignore[misc]

    def test_equality_by_value(self):
        a = Tick(1, "X", 1.0, 1.1, 0.0, 0.0)
        b = Tick(1, "X", 1.0, 1.1, 0.0, 0.0)
        self.assertEqual(a, b)


class TestIsWeekend(unittest.TestCase):
    def test_saturday_and_sunday(self):
        # 2024-06-01 is a Saturday
        self.assertTrue(_is_weekend(date(2024, 6, 1)))
        self.assertTrue(_is_weekend(date(2024, 6, 2)))

    def test_weekday(self):
        # 2024-06-03 is a Monday
        self.assertFalse(_is_weekend(date(2024, 6, 3)))
        self.assertFalse(_is_weekend(date(2024, 6, 7)))  # Friday


class TestUrlConstruction(unittest.TestCase):
    def test_hour_url_format(self):
        importer = DukascopyImporter(output_dir=None, base_url="https://example.test/datafeed")
        url = importer._hour_url("EURUSD", date(2024, 6, 3), 12)
        self.assertEqual(url, "https://example.test/datafeed/EURUSD/2024/06/03/12h_ticks.bi5")

    def test_hour_url_pads_month_day_hour(self):
        importer = DukascopyImporter(output_dir=None, base_url="https://x.test/d")
        # January 5th, hour 3
        url = importer._hour_url("GBPUSD", date(2024, 1, 5), 3)
        self.assertEqual(url, "https://x.test/d/GBPUSD/2024/01/05/03h_ticks.bi5")

    def test_hour_url_strips_trailing_slash(self):
        importer = DukascopyImporter(output_dir=None, base_url="https://x.test/d/")
        url = importer._hour_url("EURUSD", date(2024, 6, 3), 0)
        self.assertEqual(url, "https://x.test/d/EURUSD/2024/06/03/00h_ticks.bi5")


class TestParseBi5(unittest.TestCase):
    def test_parses_single_record(self):
        # ask_raw=1_100_000 → 1.1, bid_raw=1_080_000 → 1.08
        blob = _make_bi5_blob([(1500, 1_100_000, 1_080_000, 2.5, 1.5)])
        ticks = DukascopyImporter.parse_bi5(blob, "EURUSD", _HOUR_START_EPOCH)
        self.assertEqual(len(ticks), 1)
        t = ticks[0]
        self.assertEqual(t.symbol, "EURUSD")
        self.assertEqual(t.timestamp_ms, _HOUR_START_EPOCH * 1000 + 1500)
        self.assertAlmostEqual(t.ask, 1.1)
        self.assertAlmostEqual(t.bid, 1.08)
        self.assertAlmostEqual(t.ask_vol, 2.5, places=5)
        self.assertAlmostEqual(t.bid_vol, 1.5, places=5)

    def test_parses_multiple_records_in_order(self):
        records = [
            (100, 1_100_000, 1_080_000, 1.0, 1.0),
            (200, 1_100_500, 1_080_500, 2.0, 2.0),
            (300, 1_101_000, 1_081_000, 3.0, 3.0),
        ]
        blob = _make_bi5_blob(records)
        ticks = DukascopyImporter.parse_bi5(blob, "GBPUSD", _HOUR_START_EPOCH)
        self.assertEqual(len(ticks), 3)
        # Timestamps are absolute ms and monotonic
        timestamps = [t.timestamp_ms for t in ticks]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual(
            timestamps,
            [
                _HOUR_START_EPOCH * 1000 + 100,
                _HOUR_START_EPOCH * 1000 + 200,
                _HOUR_START_EPOCH * 1000 + 300,
            ],
        )

    def test_handles_empty_blob(self):
        # An empty file (no records) is represented as an LZMA-compressed empty payload.
        ticks = DukascopyImporter.parse_bi5(lzma.compress(b""), "EURUSD", _HOUR_START_EPOCH)
        self.assertEqual(ticks, [])

    def test_handles_raw_empty_bytes(self):
        ticks = DukascopyImporter.parse_bi5(b"", "EURUSD", _HOUR_START_EPOCH)
        self.assertEqual(ticks, [])

    def test_returns_empty_on_lzma_error(self):
        # Garbage that is not valid LZMA
        ticks = DukascopyImporter.parse_bi5(b"not a real xz stream", "EURUSD", _HOUR_START_EPOCH)
        self.assertEqual(ticks, [])

    def test_xau_uses_larger_scaling(self):
        # Gold is priced around 2000; raw values are ~2_000_000_000 with 1M divisor.
        blob = _make_bi5_blob([(0, 2_000_500_000, 2_000_000_000, 0.0, 0.0)])
        ticks = DukascopyImporter.parse_bi5(blob, "XAUUSD", _HOUR_START_EPOCH)
        self.assertEqual(len(ticks), 1)
        self.assertAlmostEqual(ticks[0].ask, 2000.5)
        self.assertAlmostEqual(ticks[0].bid, 2000.0)

    def test_ignores_trailing_partial_record(self):
        # 41 bytes of data: 2 full 20-byte records + 1 leftover byte.
        raw = b"".join(_make_bi5_record(0, 1, 1, 0.0, 0.0) for _ in range(2)) + b"\x00"
        blob = lzma.compress(raw)
        ticks = DukascopyImporter.parse_bi5(blob, "EURUSD", _HOUR_START_EPOCH)
        self.assertEqual(len(ticks), 2)


class TestFetchBytes(unittest.TestCase):
    def _make_importer(self) -> DukascopyImporter:
        return DukascopyImporter(
            output_dir=None,
            base_url="https://example.test",
            rate_limit_rps=DEFAULT_RATE_LIMIT_RPS,
            max_retries=3,
            retry_backoff=(0, 0, 0),  # no sleep during tests
        )

    def test_returns_bytes_on_success(self):
        importer = self._make_importer()
        with mock.patch.object(importer, "_fetch_bytes", wraps=importer._fetch_bytes) as _:
            pass  # not used; see real mock below

        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = b"hello"
        fake_resp.__enter__ = lambda s: fake_resp
        fake_resp.__exit__ = lambda s, *a: False
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            result = importer._fetch_bytes("https://example.test/x")
        self.assertEqual(result, b"hello")

    def test_returns_empty_on_404(self):
        importer = self._make_importer()
        err = urllib_error_404()
        with mock.patch("urllib.request.urlopen", side_effect=err):
            result = importer._fetch_bytes("https://example.test/x")
        self.assertEqual(result, b"")

    def test_retries_then_raises_after_max_attempts(self):
        importer = self._make_importer()
        # urllib raises ConnectionError, not HTTPError, so retries apply.
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=ConnectionError("boom"),
        ):
            result = importer._fetch_bytes("https://example.test/x")
        self.assertIsNone(result)


def urllib_error_404():
    import urllib.error

    return urllib.error.HTTPError(url="https://example.test/x", code=404, msg="Not Found", hdrs={}, fp=None)


class TestFetchHourTicks(unittest.TestCase):
    def test_returns_parsed_ticks(self):
        importer = DukascopyImporter(
            output_dir=None,
            base_url="https://example.test",
            retry_backoff=(0, 0, 0),
        )
        blob = _make_bi5_blob([(1500, 1_100_000, 1_080_000, 2.5, 1.5)])
        with mock.patch.object(importer, "_fetch_bytes", return_value=blob):
            ticks = importer.fetch_hour_ticks("EURUSD", date(2024, 6, 3), 12)
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0].timestamp_ms, _HOUR_START_EPOCH * 1000 + 1500)

    def test_raises_fetch_error_on_total_failure(self):
        importer = DukascopyImporter(
            output_dir=None,
            base_url="https://example.test",
            retry_backoff=(0, 0, 0),
        )
        with mock.patch.object(importer, "_fetch_bytes", return_value=None):
            with self.assertRaises(DukascopyFetchError):
                importer.fetch_hour_ticks("EURUSD", date(2024, 6, 3), 12)

    def test_returns_empty_on_404(self):
        importer = DukascopyImporter(
            output_dir=None,
            base_url="https://example.test",
            retry_backoff=(0, 0, 0),
        )
        with mock.patch.object(importer, "_fetch_bytes", return_value=b""):
            ticks = importer.fetch_hour_ticks("EURUSD", date(2024, 6, 3), 12)
        self.assertEqual(ticks, [])


class TestDownloadDay(unittest.TestCase):
    def test_skips_weekend_without_network(self):
        importer = DukascopyImporter(
            output_dir=None,
            base_url="https://example.test",
            retry_backoff=(0, 0, 0),
        )
        with mock.patch.object(importer, "fetch_hour_ticks") as mock_fetch:
            # 2024-06-01 is Saturday
            ticks = importer.download_day("EURUSD", date(2024, 6, 1))
        self.assertEqual(ticks, [])
        mock_fetch.assert_not_called()

    def test_calls_fetch_for_each_hour_on_weekday(self):
        importer = DukascopyImporter(
            output_dir=None,
            base_url="https://example.test",
            retry_backoff=(0, 0, 0),
        )
        with mock.patch.object(
            importer,
            "fetch_hour_ticks",
            return_value=[
                Tick(1, "EURUSD", 1.0, 1.1, 0.0, 0.0),
            ],
        ) as mock_fetch:
            ticks = importer.download_day("EURUSD", date(2024, 6, 3))  # Monday
        self.assertEqual(mock_fetch.call_count, 24)
        self.assertEqual(len(ticks), 24)  # 1 tick per hour

    def test_continues_after_fetch_error(self):
        importer = DukascopyImporter(
            output_dir=None,
            base_url="https://example.test",
            retry_backoff=(0, 0, 0),
        )
        # Make the first call fail, the rest succeed.
        side_effects = [DukascopyFetchError("network down")] + [
            [Tick(1, "EURUSD", 1.0, 1.1, 0.0, 0.0)] for _ in range(23)
        ]
        with mock.patch.object(importer, "fetch_hour_ticks", side_effect=side_effects):
            ticks = importer.download_day("EURUSD", date(2024, 6, 3))
        # 23 successful hours, each yielding 1 tick.
        self.assertEqual(len(ticks), 23)


class TestWriteCsv(unittest.TestCase):
    def test_writes_csv_with_expected_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            importer = DukascopyImporter(output_dir=outdir)
            ticks = [
                Tick(1717416000123, "EURUSD", 1.085, 1.0852, 1.5, 2.0),
                Tick(1717416001456, "EURUSD", 1.0851, 1.0853, 1.0, 1.5),
            ]
            outpath = importer.write_csv("EURUSD", date(2024, 6, 3), ticks)

            assert outpath is not None
            self.assertEqual(outpath.name, "EURUSD_20240603.csv")
            with open(outpath, newline="") as fh:
                rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["timestamp", "instrument", "bid", "ask", "bidVol", "askVol"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][0], "1717416000123")
        self.assertEqual(rows[1][1], "EURUSD")
        self.assertEqual(rows[2][0], "1717416001456")

    def test_returns_none_when_output_dir_unset(self):
        importer = DukascopyImporter(output_dir=None)
        ticks = [Tick(1, "EURUSD", 1.0, 1.1, 0.0, 0.0)]
        self.assertIsNone(importer.write_csv("EURUSD", date(2024, 6, 3), ticks))

    def test_returns_none_for_empty_tick_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            importer = DukascopyImporter(output_dir=outdir)
            self.assertIsNone(importer.write_csv("EURUSD", date(2024, 6, 3), []))


class TestDownloadRangeRestart(unittest.TestCase):
    def test_skips_days_with_existing_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            # Pre-create the day 1 CSV; day 2 should be downloaded.
            (outdir / "EURUSD_20240603.csv").write_text("placeholder\n")

            importer = DukascopyImporter(output_dir=outdir, base_url="https://x.test")
            with mock.patch.object(
                importer,
                "download_day",
                return_value=[Tick(1, "EURUSD", 1.0, 1.1, 0.0, 0.0)],
            ) as mock_dl:
                _days = list(importer.download_range("EURUSD", date(2024, 6, 3), date(2024, 6, 4)))
            self.assertEqual(mock_dl.call_count, 1)
            self.assertEqual(mock_dl.call_args.args, ("EURUSD", date(2024, 6, 4)))


class TestRetryBackoffIsConfigurable(unittest.TestCase):
    def test_custom_retry_backoff_is_stored(self):
        importer = DukascopyImporter(
            output_dir=None,
            retry_backoff=(1, 2, 3),
        )
        self.assertEqual(importer._retry_backoff, (1, 2, 3))

    def test_default_constants_hold(self):
        # Pin the public defaults so accidental changes show up as test failures.
        self.assertEqual(DEFAULT_RATE_LIMIT_RPS, 4.0)
        self.assertEqual(DEFAULT_MAX_RETRIES, 3)
        self.assertEqual(DEFAULT_RETRY_BACKOFF, (5, 15, 30))


if __name__ == "__main__":
    unittest.main()
