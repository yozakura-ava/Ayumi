"""Tests for ``forex_bot.backtest.universe`` — point-in-time crypto universe.

Card: b127a6aa-b897-45b2-86e7-5678e6df4e4f (Sprint C 1a.5)
Sprint: 2026-10-05-crypto-strategy-factory
Lane: [BUILD][CRYPTO-LANE]

Scope (HR5 — targeted tests only):

  * Inclusivity convention at the boundaries
      - listed_from inclusive (symbol enters on listed_from)
      - delisted_at exclusive (symbol exits on delisted_at)
      - documented via tests on the exact boundary dates
  * Never-delisted majors (delisted_at=None) live from
    listed_from forward indefinitely
  * Future-dated queries (as_of > today) do not raise
  * Empty-universe errors
      - resolve_universe on a date before any listing → EmptyUniverseError
      - resolve_universe with empty Universe → EmptyUniverseError
      - resolve_universe_window with empty Universe → () (no raise)
  * Delisting-record validation
      - duplicate symbols rejected
      - listed_from not before delisted_at rejected
      - non-string symbol rejected
      - empty / whitespace-only symbol rejected
      - non-date listed_from rejected
      - non-date delisted_at (when supplied) rejected
  * Config loading
      - missing keys / wrong types / non-ISO datetimes
      - empty entries list rejected
      - bool-as-str / int-as-str traps (none here, but type discipline)
      - full mapping round-trip preserves semantics
  * Integration with integrity gate
      - symbol in universe, in data → passes
      - symbol in universe, NOT in data → missing_symbol violation
      - symbol NOT in universe but in data → fails loud via
        assert_symbol_in_universe (not silently dropped)
      - build_universe_integrity_config wires universe_symbols
  * Legacy FX path untouched
      - data_loader.CsvDataLoader not modified
      - abstract_data_loader unchanged
      - integrity gate's universe_symbols still accepts an empty
        tuple (default) — no breaking signature changes.

The tests use synthetic fixtures only — no live exchange calls,
no IO, no randomness.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup: src/forex_bot on sys.path so ``backtest.universe`` and
# ``engine.engine`` resolve. Mirrors test_integrity_gate / test_venue_costs
# / test_liquidation / test_adaptive_funding setup. MUST run before any
# imports below.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_FX = _PROJECT_ROOT / "src" / "forex_bot"
_SRC_ROOT = _PROJECT_ROOT / "src"
for p in (_SRC_FX, _SRC_ROOT):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from datetime import date, datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from backtest.integrity_gate import (  # noqa: E402
    IntegrityConfig,
    enforce_integrity_gate,
    validate_crypto_bars,
)
from backtest.types import Bar  # noqa: E402
from backtest.universe import (  # noqa: E402
    DEFAULT_CRYPTO_UNIVERSE,
    DEFAULT_LISTED_FROM,
    DEFAULT_NEVER_DELISTED_MAJORS,
    EmptyUniverseError,
    SymbolNotInUniverseError,
    Universe,
    UniverseConfigError,
    UniverseEntry,
    UniverseError,
    UniverseIntegrityError,
    assert_symbol_in_universe,
    build_universe_integrity_config,
    load_universe_config,
    resolve_universe,
    resolve_universe_window,
    validate_universe_entries,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _entry(
    symbol: str,
    listed_from: date,
    delisted_at: date | None = None,
) -> UniverseEntry:
    """Shorthand for building a single UniverseEntry."""
    return UniverseEntry(
        symbol=symbol, listed_from=listed_from, delisted_at=delisted_at
    )


def _flat_bars(
    n: int,
    start: datetime,
    step_minutes: int = 60,
    base_price: float = 100.0,
    volume: float = 100.0,
) -> list[Bar]:
    """Build N flat OHLC bars at the given cadence (UTC). Mirrors the
    helper used in test_integrity_gate / test_venue_costs / etc.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    bars: list[Bar] = []
    for i in range(n):
        t = start + timedelta(minutes=step_minutes * i)
        bars.append(
            Bar(
                time=t,
                open=base_price,
                high=base_price + 0.5,
                low=base_price - 0.5,
                close=base_price,
                volume=volume,
                spread_pips=0.0,
            )
        )
    return bars


# ──────────────────────────────────────────────────────────────────────
# UniverseEntry validation
# ──────────────────────────────────────────────────────────────────────


class TestUniverseEntryValidation:
    """UniverseEntry field discipline."""

    def test_minimal_entry_is_valid(self):
        e = _entry("BTCUSDT", date(2020, 1, 1))
        assert e.symbol == "BTCUSDT"
        assert e.listed_from == date(2020, 1, 1)
        assert e.delisted_at is None

    def test_entry_with_delisted_at_is_valid(self):
        e = _entry("XRPUSDT", date(2020, 1, 1), date(2024, 6, 15))
        assert e.delisted_at == date(2024, 6, 15)

    def test_empty_symbol_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="symbol"):
            _entry("", date(2020, 1, 1))

    def test_whitespace_symbol_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="symbol"):
            _entry("   ", date(2020, 1, 1))

    def test_non_string_symbol_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="symbol"):
            _entry(123, date(2020, 1, 1))  # type: ignore[arg-type]
        with pytest.raises(UniverseIntegrityError, match="symbol"):
            _entry(None, date(2020, 1, 1))  # type: ignore[arg-type]

    def test_non_date_listed_from_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="listed_from"):
            _entry("BTCUSDT", "2020-01-01")  # type: ignore[arg-type]

    def test_non_date_delisted_at_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="delisted_at"):
            _entry("BTCUSDT", date(2020, 1, 1), "2024-06-15")  # type: ignore[arg-type]

    def test_delisted_at_equal_to_listed_from_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="delisted_at"):
            _entry("BTCUSDT", date(2024, 6, 15), date(2024, 6, 15))

    def test_delisted_at_before_listed_from_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="delisted_at"):
            _entry("BTCUSDT", date(2024, 6, 15), date(2024, 6, 14))


# ──────────────────────────────────────────────────────────────────────
# Inclusivity convention (the critical bit per spec)
# ──────────────────────────────────────────────────────────────────────


class TestInclusivityConvention:
    """The boundary semantics are part of the public contract.

    A symbol is live on date D iff listed_from <= D < delisted_at:

      * listed_from is inclusive (enters the universe on listing day)
      * delisted_at is exclusive (last active day is delisted_at - 1)

    These tests pin the convention so any future change is forced
    to update the test (and the operator who breaks it gets a clear
    test failure naming the convention).
    """

    def test_listed_from_inclusive_symbol_live_on_listing_date(self):
        e = _entry("NEWUSDT", date(2024, 6, 1))
        assert e.is_live_on(date(2024, 6, 1)) is True

    def test_listed_from_inclusive_symbol_not_live_before_listing_date(self):
        e = _entry("NEWUSDT", date(2024, 6, 1))
        assert e.is_live_on(date(2024, 5, 31)) is False

    def test_delisted_at_exclusive_symbol_live_on_day_before(self):
        e = _entry("DEADUSDT", date(2020, 1, 1), date(2024, 6, 15))
        assert e.is_live_on(date(2024, 6, 14)) is True

    def test_delisted_at_exclusive_symbol_not_live_on_delist_day(self):
        e = _entry("DEADUSDT", date(2020, 1, 1), date(2024, 6, 15))
        assert e.is_live_on(date(2024, 6, 15)) is False

    def test_delisted_at_exclusive_symbol_not_live_after_delist_day(self):
        e = _entry("DEADUSDT", date(2020, 1, 1), date(2024, 6, 15))
        assert e.is_live_on(date(2024, 6, 16)) is False
        assert e.is_live_on(date(2025, 1, 1)) is False

    def test_full_lifecycle_live_then_delisted(self):
        e = _entry("XRPUSDT", date(2020, 1, 1), date(2024, 6, 15))
        # Before listing: not live
        assert e.is_live_on(date(2019, 12, 31)) is False
        # On listing day: live
        assert e.is_live_on(date(2020, 1, 1)) is True
        # Mid-life: live
        assert e.is_live_on(date(2022, 6, 15)) is True
        # Day before delisting: live (last active day)
        assert e.is_live_on(date(2024, 6, 14)) is True
        # On delisting day: not live
        assert e.is_live_on(date(2024, 6, 15)) is False
        # After delisting: not live
        assert e.is_live_on(date(2024, 6, 16)) is False

    def test_never_delisted_live_indefinitely(self):
        e = _entry("BTCUSDT", date(2020, 1, 1), delisted_at=None)
        assert e.is_live_on(date(2020, 1, 1)) is True
        assert e.is_live_on(date(2024, 6, 15)) is True
        assert e.is_live_on(date(2099, 12, 31)) is True


# ──────────────────────────────────────────────────────────────────────
# resolve_universe — as-of resolution
# ──────────────────────────────────────────────────────────────────────


class TestResolveUniverse:
    """As-of resolution against the full Universe."""

    def test_default_universe_resolves_three_majors(self):
        result = resolve_universe(DEFAULT_CRYPTO_UNIVERSE, date(2024, 6, 1))
        assert result == ("BTCUSDT", "ETHUSDT", "SOLUSDT")

    def test_default_universe_resolves_on_listing_date(self):
        # The default listing date is the inclusive boundary; all
        # three must be live.
        result = resolve_universe(DEFAULT_CRYPTO_UNIVERSE, DEFAULT_LISTED_FROM)
        assert result == ("BTCUSDT", "ETHUSDT", "SOLUSDT")

    def test_default_universe_before_listing_raises_empty(self):
        with pytest.raises(EmptyUniverseError) as exc_info:
            resolve_universe(DEFAULT_CRYPTO_UNIVERSE, date(2019, 12, 31))
        assert exc_info.value.as_of == date(2019, 12, 31)

    def test_empty_universe_raises_empty(self):
        with pytest.raises(EmptyUniverseError) as exc_info:
            resolve_universe(Universe(), date(2024, 6, 1))
        assert exc_info.value.as_of == date(2024, 6, 1)

    def test_resolve_skips_delisted(self):
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("DEADUSDT", date(2020, 1, 1), date(2024, 6, 15)),
            )
        )
        # On 2024-06-14: both live (DEADUSDT's last day)
        assert resolve_universe(u, date(2024, 6, 14)) == ("BTCUSDT", "DEADUSDT")
        # On 2024-06-15: DEADUSDT removed (delisted_at exclusive)
        assert resolve_universe(u, date(2024, 6, 15)) == ("BTCUSDT",)
        # After delist: only BTC
        assert resolve_universe(u, date(2025, 1, 1)) == ("BTCUSDT",)

    def test_resolve_includes_symbol_on_its_listing_date(self):
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("NEWUSDT", date(2024, 6, 1)),
            )
        )
        # 2024-05-31: only BTC
        assert resolve_universe(u, date(2024, 5, 31)) == ("BTCUSDT",)
        # 2024-06-01: BTC + NEW (listing day inclusive)
        assert resolve_universe(u, date(2024, 6, 1)) == ("BTCUSDT", "NEWUSDT")

    def test_resolve_excludes_symbol_on_its_delisting_date(self):
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("DEADUSDT", date(2020, 1, 1), date(2024, 6, 15)),
            )
        )
        # 2024-06-14: DEADUSDT's last active day (delisted_at - 1)
        assert resolve_universe(u, date(2024, 6, 14)) == ("BTCUSDT", "DEADUSDT")
        # 2024-06-15: DEADUSDT removed
        assert resolve_universe(u, date(2024, 6, 15)) == ("BTCUSDT",)

    def test_resolve_returns_sorted_tuple(self):
        # Order of insertion is BTC, NEW, ETH, SOL — sort by symbol
        # (case-sensitive) should give the same alphabetical order.
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("NEWUSDT", date(2024, 6, 1)),
                _entry("ETHUSDT", date(2020, 1, 1)),
                _entry("SOLUSDT", date(2020, 1, 1)),
            )
        )
        assert resolve_universe(u, date(2024, 6, 1)) == (
            "BTCUSDT",
            "ETHUSDT",
            "NEWUSDT",
            "SOLUSDT",
        )

    def test_resolve_with_naive_datetime_rejected(self):
        # date is the only acceptable input; datetime is rejected.
        with pytest.raises(TypeError, match="as_of"):
            resolve_universe(DEFAULT_CRYPTO_UNIVERSE, datetime(2024, 6, 1))  # type: ignore[arg-type]

    def test_resolve_with_non_universe_rejected(self):
        with pytest.raises(TypeError, match="universe"):
            resolve_universe("not a universe", date(2024, 6, 1))  # type: ignore[arg-type]

    def test_resolve_with_naive_date_works(self):
        # date objects have no tzinfo; they are always treated as UTC.
        # (datetime.date is the spec input — datetime objects are not.)
        result = resolve_universe(DEFAULT_CRYPTO_UNIVERSE, date(2024, 6, 1))
        assert "BTCUSDT" in result


# ──────────────────────────────────────────────────────────────────────
# Future-dated queries
# ──────────────────────────────────────────────────────────────────────


class TestFutureDatedQueries:
    """Future as_of values are permitted (no implicit today() clamp)."""

    def test_resolve_universe_accepts_future_date(self):
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        far_future = date(2099, 12, 31)
        assert resolve_universe(u, far_future) == ("BTCUSDT",)

    def test_resolve_universe_window_accepts_future_window(self):
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("ETHUSDT", date(2020, 1, 1)),
            )
        )
        future_window = resolve_universe_window(
            u, date(2099, 1, 1), date(2099, 12, 31)
        )
        assert future_window == ("BTCUSDT", "ETHUSDT")


# ──────────────────────────────────────────────────────────────────────
# resolve_universe_window
# ──────────────────────────────────────────────────────────────────────


class TestResolveUniverseWindow:
    """Window-resolution returns the union of symbols live at any
    point in [start, end]."""

    def test_window_with_single_never_delisted_major(self):
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        assert resolve_universe_window(
            u, date(2024, 1, 1), date(2024, 12, 31)
        ) == ("BTCUSDT",)

    def test_window_includes_symbols_listed_during_window(self):
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("NEWUSDT", date(2024, 6, 1)),
            )
        )
        # NEWUSDT listed mid-window; window-resolution must include it.
        assert resolve_universe_window(
            u, date(2024, 1, 1), date(2024, 12, 31)
        ) == ("BTCUSDT", "NEWUSDT")

    def test_window_includes_symbols_delisted_during_window(self):
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("DEADUSDT", date(2020, 1, 1), date(2024, 6, 15)),
            )
        )
        # DEADUSDT delisted mid-window; still live at start.
        assert resolve_universe_window(
            u, date(2024, 1, 1), date(2024, 12, 31)
        ) == ("BTCUSDT", "DEADUSDT")

    def test_window_excludes_symbols_outside_window(self):
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("OLDUSDT", date(2018, 1, 1), date(2019, 12, 31)),
            )
        )
        # Window in 2024: OLDUSDT delisted before window start.
        assert resolve_universe_window(
            u, date(2024, 1, 1), date(2024, 12, 31)
        ) == ("BTCUSDT",)

    def test_window_empty_universe_returns_empty(self):
        # Note: empty window-resolution does NOT raise — it's a
        # legitimate answer ("no symbol was ever live here").
        assert resolve_universe_window(Universe(), date(2024, 1, 1), date(2024, 12, 31)) == ()

    def test_window_before_any_listing_returns_empty(self):
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        assert resolve_universe_window(u, date(2018, 1, 1), date(2019, 12, 31)) == ()

    def test_window_start_after_end_rejected(self):
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        with pytest.raises(UniverseError, match="start"):
            resolve_universe_window(u, date(2024, 12, 31), date(2024, 1, 1))

    def test_window_single_day_window_inclusive(self):
        u = Universe(
            entries=(
                _entry("A", date(2024, 1, 1), date(2024, 1, 5)),
            )
        )
        # Single-day window on the listing date: symbol live.
        assert resolve_universe_window(u, date(2024, 1, 1), date(2024, 1, 1)) == ("A",)
        # Single-day window on the last active day (delisted_at - 1): live.
        assert resolve_universe_window(u, date(2024, 1, 4), date(2024, 1, 4)) == ("A",)
        # Single-day window on the delisting date: not live.
        assert resolve_universe_window(u, date(2024, 1, 5), date(2024, 1, 5)) == ()


# ──────────────────────────────────────────────────────────────────────
# Universe container behavior
# ──────────────────────────────────────────────────────────────────────


class TestUniverseContainer:
    """Universe dataclass: sorted, hashable, contains."""

    def test_universe_sort_stable(self):
        # Insertion order: SOL, BTC, ETH — Universe sorts by symbol.
        u = Universe(
            entries=(
                _entry("SOLUSDT", date(2020, 1, 1)),
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("ETHUSDT", date(2020, 1, 1)),
            )
        )
        symbols = [e.symbol for e in u]
        assert symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def test_universe_length(self):
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("ETHUSDT", date(2020, 1, 1)),
            )
        )
        assert len(u) == 2

    def test_universe_iteration(self):
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        assert list(u) == [_entry("BTCUSDT", date(2020, 1, 1))]

    def test_universe_contains_symbol(self):
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        assert "BTCUSDT" in u
        assert "ETHUSDT" not in u

    def test_universe_contains_non_string_returns_false(self):
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        assert (123 in u) is False
        assert (None in u) is False

    def test_universe_empty_is_constructable(self):
        u = Universe()
        assert len(u) == 0

    def test_universe_duplicate_symbol_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="duplicate"):
            Universe(
                entries=(
                    _entry("BTCUSDT", date(2020, 1, 1)),
                    _entry("BTCUSDT", date(2024, 1, 1)),
                )
            )

    def test_universe_equality(self):
        # Dataclass(frozen=True) gives value equality; sort must be stable.
        u1 = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        u2 = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        assert u1 == u2

    def test_universe_hashable(self):
        # frozen=True makes the dataclass hashable; entries must be
        # tuple (not list) to be hashable.
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        assert hash(u) == hash(u)

    def test_universe_entries_is_tuple(self):
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        assert isinstance(u.entries, tuple)


# ──────────────────────────────────────────────────────────────────────
# Delisting-record validation (semantic checks)
# ──────────────────────────────────────────────────────────────────────


class TestDelistingRecordValidation:
    """Semantic validation of universe entries."""

    def test_duplicate_symbols_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="duplicate"):
            validate_universe_entries(
                (
                    _entry("BTCUSDT", date(2020, 1, 1)),
                    _entry("BTCUSDT", date(2024, 1, 1)),
                )
            )

    def test_listed_from_not_before_delisted_at_rejected(self):
        # Equal → already covered in entry-level validation; the
        # semantic check is for the case where listed_from is
        # strictly after delisted_at (entry-level rejects this
        # too, but the validator should also surface it).
        with pytest.raises(UniverseIntegrityError):
            validate_universe_entries(
                (_entry("BTCUSDT", date(2024, 6, 15), date(2024, 6, 14)),)
            )

    def test_validator_accepts_valid_collection(self):
        # No exception.
        validate_universe_entries(
            (
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("DEADUSDT", date(2020, 1, 1), date(2024, 6, 15)),
            )
        )

    def test_validator_rejects_non_entry(self):
        with pytest.raises(UniverseIntegrityError, match="UniverseEntry"):
            validate_universe_entries(({"symbol": "BTCUSDT"},))  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# Config loading
# ──────────────────────────────────────────────────────────────────────


class TestLoadUniverseConfig:
    """Fail-loud YAML-style config parser."""

    def test_empty_mapping_rejected(self):
        with pytest.raises(UniverseConfigError, match="entries"):
            load_universe_config({})

    def test_non_mapping_rejected(self):
        with pytest.raises(UniverseConfigError, match="mapping"):
            load_universe_config("not a mapping")  # type: ignore[arg-type]
        with pytest.raises(UniverseConfigError, match="mapping"):
            load_universe_config(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_empty_entries_rejected(self):
        with pytest.raises(UniverseConfigError, match="non-empty"):
            load_universe_config({"entries": []})

    def test_non_list_entries_rejected(self):
        with pytest.raises(UniverseConfigError, match="entries"):
            load_universe_config({"entries": "BTCUSDT"})

    def test_entry_missing_symbol_rejected(self):
        with pytest.raises(UniverseConfigError, match="symbol"):
            load_universe_config(
                {
                    "entries": [
                        {"listed_from": "2020-01-01"},
                    ]
                }
            )

    def test_entry_missing_listed_from_rejected(self):
        with pytest.raises(UniverseConfigError, match="listed_from"):
            load_universe_config(
                {
                    "entries": [
                        {"symbol": "BTCUSDT"},
                    ]
                }
            )

    def test_entry_non_string_symbol_rejected(self):
        with pytest.raises(UniverseConfigError, match="symbol"):
            load_universe_config(
                {
                    "entries": [
                        {"symbol": 123, "listed_from": "2020-01-01"},
                    ]
                }
            )

    def test_entry_empty_string_symbol_rejected(self):
        with pytest.raises(UniverseConfigError, match="symbol"):
            load_universe_config(
                {
                    "entries": [
                        {"symbol": "", "listed_from": "2020-01-01"},
                    ]
                }
            )

    def test_entry_whitespace_string_symbol_rejected(self):
        with pytest.raises(UniverseConfigError, match="symbol"):
            load_universe_config(
                {
                    "entries": [
                        {"symbol": "   ", "listed_from": "2020-01-01"},
                    ]
                }
            )

    def test_entry_non_date_listed_from_rejected(self):
        with pytest.raises(UniverseConfigError, match="listed_from"):
            load_universe_config(
                {
                    "entries": [
                        {"symbol": "BTCUSDT", "listed_from": "not-a-date"},
                    ]
                }
            )

    def test_entry_non_date_delisted_at_rejected(self):
        with pytest.raises(UniverseConfigError, match="delisted_at"):
            load_universe_config(
                {
                    "entries": [
                        {
                            "symbol": "BTCUSDT",
                            "listed_from": "2020-01-01",
                            "delisted_at": "not-a-date",
                        },
                    ]
                }
            )

    def test_entry_not_a_mapping_rejected(self):
        with pytest.raises(UniverseConfigError, match="mapping"):
            load_universe_config(
                {
                    "entries": [
                        "BTCUSDT",
                    ]
                }
            )

    def test_full_mapping_round_trips(self):
        u = load_universe_config(
            {
                "entries": [
                    {
                        "symbol": "BTCUSDT",
                        "listed_from": "2020-01-01",
                        "delisted_at": None,
                    },
                    {
                        "symbol": "DEADUSDT",
                        "listed_from": "2020-01-01",
                        "delisted_at": "2024-06-15",
                    },
                ]
            }
        )
        assert len(u) == 2
        assert "BTCUSDT" in u
        assert "DEADUSDT" in u
        # Resolve on delist day - 1 → both live
        assert resolve_universe(u, date(2024, 6, 14)) == ("BTCUSDT", "DEADUSDT")
        # Resolve on delist day → DEADUSDT gone
        assert resolve_universe(u, date(2024, 6, 15)) == ("BTCUSDT",)

    def test_duplicate_symbols_in_config_rejected(self):
        with pytest.raises(UniverseIntegrityError, match="duplicate"):
            load_universe_config(
                {
                    "entries": [
                        {"symbol": "BTCUSDT", "listed_from": "2020-01-01"},
                        {"symbol": "BTCUSDT", "listed_from": "2024-01-01"},
                    ]
                }
            )

    def test_delisted_at_not_after_listed_from_rejected_in_config(self):
        with pytest.raises(UniverseIntegrityError, match="delisted_at"):
            load_universe_config(
                {
                    "entries": [
                        {
                            "symbol": "BTCUSDT",
                            "listed_from": "2024-06-15",
                            "delisted_at": "2024-06-14",
                        },
                    ]
                }
            )

    def test_aware_datetime_strings_normalized_to_utc_date(self):
        # An ISO 8601 string with timezone offset must be converted
        # to UTC date before extraction. "2020-01-01T20:00:00-05:00"
        # is 2020-01-02 in UTC.
        u = load_universe_config(
            {
                "entries": [
                    {
                        "symbol": "BTCUSDT",
                        "listed_from": "2020-01-01T20:00:00-05:00",
                    },
                ]
            }
        )
        assert u.entries[0].listed_from == date(2020, 1, 2)

    def test_naive_datetime_treated_as_utc_date(self):
        # A naive datetime string ("2020-01-01T00:00:00") is treated
        # as UTC for date extraction (the time portion is discarded).
        u = load_universe_config(
            {
                "entries": [
                    {
                        "symbol": "BTCUSDT",
                        "listed_from": "2020-01-01T00:00:00",
                    },
                ]
            }
        )
        assert u.entries[0].listed_from == date(2020, 1, 1)


# ──────────────────────────────────────────────────────────────────────
# assert_symbol_in_universe — "skip loudly" contract
# ──────────────────────────────────────────────────────────────────────


class TestAssertSymbolInUniverse:
    """The fail-loud-skip helper for "data present but not in universe"."""

    def test_assert_pass_for_live_symbol(self):
        # No exception.
        assert_symbol_in_universe("BTCUSDT", DEFAULT_CRYPTO_UNIVERSE, date(2024, 6, 1))

    def test_assert_fails_for_unknown_symbol(self):
        with pytest.raises(SymbolNotInUniverseError) as exc_info:
            assert_symbol_in_universe("GHOSTUSDT", DEFAULT_CRYPTO_UNIVERSE, date(2024, 6, 1))
        assert exc_info.value.symbol == "GHOSTUSDT"
        assert exc_info.value.as_of == date(2024, 6, 1)

    def test_assert_fails_for_delisted_symbol_after_delisting(self):
        # Universe includes a never-delisted major (BTCUSDT) so
        # resolve_universe returns non-empty; the assert fires
        # SymbolNotInUniverseError for DEADUSDT specifically.
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("DEADUSDT", date(2020, 1, 1), date(2024, 6, 15)),
            )
        )
        with pytest.raises(SymbolNotInUniverseError):
            assert_symbol_in_universe("DEADUSDT", u, date(2024, 6, 16))

    def test_assert_fails_for_symbol_before_listing(self):
        # Use a universe with a never-delisted major (BTCUSDT) so
        # resolve_universe doesn't raise EmptyUniverseError; the
        # assert then fires SymbolNotInUniverseError because NEWUSDT
        # is not in the resolved set.
        u = Universe(
            entries=(
                _entry("BTCUSDT", date(2020, 1, 1)),
                _entry("NEWUSDT", date(2024, 6, 1)),
            )
        )
        with pytest.raises(SymbolNotInUniverseError):
            assert_symbol_in_universe("NEWUSDT", u, date(2024, 5, 31))

    def test_assert_type_error_for_non_string_symbol(self):
        with pytest.raises(TypeError, match="symbol"):
            assert_symbol_in_universe(123, DEFAULT_CRYPTO_UNIVERSE, date(2024, 6, 1))  # type: ignore[arg-type]

    def test_assert_type_error_for_empty_symbol(self):
        with pytest.raises(TypeError, match="symbol"):
            assert_symbol_in_universe("", DEFAULT_CRYPTO_UNIVERSE, date(2024, 6, 1))

    def test_assert_type_error_for_non_universe(self):
        with pytest.raises(TypeError, match="universe"):
            assert_symbol_in_universe("BTCUSDT", "not a universe", date(2024, 6, 1))  # type: ignore[arg-type]

    def test_assert_type_error_for_non_date_as_of(self):
        with pytest.raises(TypeError, match="as_of"):
            assert_symbol_in_universe(
                "BTCUSDT", DEFAULT_CRYPTO_UNIVERSE, datetime(2024, 6, 1)  # type: ignore[arg-type]
            )


# ──────────────────────────────────────────────────────────────────────
# Integration with integrity gate
# ──────────────────────────────────────────────────────────────────────


class TestIntegrityGateIntegration:
    """PIT universe + integrity gate = survivorship-bias-free validation."""

    def test_build_universe_integrity_config_sets_universe_symbols(self):
        cfg = build_universe_integrity_config(
            DEFAULT_CRYPTO_UNIVERSE, date(2024, 6, 1)
        )
        assert isinstance(cfg, IntegrityConfig)
        assert cfg.universe_symbols == ("BTCUSDT", "ETHUSDT", "SOLUSDT")

    def test_build_universe_integrity_config_propagates_window_end(self):
        end = datetime(2024, 6, 1, tzinfo=timezone.utc)
        cfg = build_universe_integrity_config(
            DEFAULT_CRYPTO_UNIVERSE,
            date(2024, 6, 1),
            expected_window_end=end,
        )
        assert cfg.expected_window_end == end

    def test_build_universe_integrity_config_propagates_cadence(self):
        cfg = build_universe_integrity_config(
            DEFAULT_CRYPTO_UNIVERSE,
            date(2024, 6, 1),
            expected_cadence_minutes=240,
            gap_tolerance_multiplier=2.0,
            max_zero_volume_stretch=3,
        )
        assert cfg.expected_cadence_minutes == 240
        assert cfg.gap_tolerance_multiplier == 2.0
        assert cfg.max_zero_volume_stretch == 3

    def test_build_universe_integrity_config_type_error_for_universe(self):
        with pytest.raises(TypeError, match="universe"):
            build_universe_integrity_config("not a universe", date(2024, 6, 1))  # type: ignore[arg-type]

    def test_build_universe_integrity_config_type_error_for_as_of(self):
        with pytest.raises(TypeError, match="as_of"):
            build_universe_integrity_config(
                DEFAULT_CRYPTO_UNIVERSE, datetime(2024, 6, 1)  # type: ignore[arg-type]
            )

    def test_build_universe_integrity_config_empty_universe_raises(self):
        with pytest.raises(EmptyUniverseError):
            build_universe_integrity_config(Universe(), date(2024, 6, 1))

    def test_integrity_gate_with_pit_universe_passes_for_present_symbols(self):
        # Universe has only BTCUSDT (a single never-delisted major);
        # we load BTCUSDT bars and the gate should pass cleanly.
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        bars = _flat_bars(10, datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc))
        cfg = build_universe_integrity_config(u, date(2024, 6, 1))
        # BTC is in the universe and the bars are valid → passes.
        report = enforce_integrity_gate(
            symbol="BTCUSDT",
            bars=bars,
            config=IntegrityConfig(
                expected_cadence_minutes=60,
                gap_tolerance_multiplier=1.5,
                universe_symbols=cfg.universe_symbols,
            ),
            loaded_symbol="BTCUSDT",
        )
        assert report.passed

    def test_integrity_gate_flags_missing_symbol_when_in_universe(self):
        # Universe has BTC, ETH, SOL. We validate against the loaded
        # bars for ETH only; SOL is in the universe but no bars were
        # loaded for it. The gate should emit a missing_symbol
        # violation for SOL.
        bars = _flat_bars(10, datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc))
        cfg = IntegrityConfig(
            expected_cadence_minutes=60,
            gap_tolerance_multiplier=1.5,
            universe_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        )
        report = validate_crypto_bars(
            symbol="ETHUSDT",
            bars=bars,
            config=cfg,
            loaded_symbol="ETHUSDT",
        )
        assert not report.passed
        kinds = [v.kind for v in report.violations]
        assert "missing_symbol" in kinds
        missing_symbols = [v.symbol for v in report.violations if v.kind == "missing_symbol"]
        assert "BTCUSDT" in missing_symbols
        assert "SOLUSDT" in missing_symbols

    def test_integrity_gate_flags_window_end_delisting_for_dropped_symbol(self):
        # Universe has BTC, ETH, SOL. SOL's bars end at 2024-06-01
        # 09:00 UTC, but expected_window_end is 2024-06-01 12:00 UTC.
        # The gate's window-end check should flag a delisting.
        start = datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(10, start)  # last bar at 2024-06-01 09:00 UTC
        cfg = IntegrityConfig(
            expected_cadence_minutes=60,
            gap_tolerance_multiplier=1.5,
            expected_window_end=datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc),
            universe_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        )
        report = validate_crypto_bars(
            symbol="SOLUSDT",
            bars=bars,
            config=cfg,
            loaded_symbol="SOLUSDT",
        )
        assert not report.passed
        assert any(v.kind == "delisting" for v in report.violations)

    def test_assert_symbol_in_universe_is_the_loud_skip_helper(self):
        # The complementary case: symbol in data but NOT in the
        # universe. assert_symbol_in_universe is the loud-skip
        # contract; the integrity gate does NOT do this check
        # (it's a data-shape check, not a data-content check).
        u = Universe(entries=(_entry("BTCUSDT", date(2020, 1, 1)),))
        # ETHUSDT is in our (hypothetical) loaded data but not in
        # the as-of universe → must raise.
        with pytest.raises(SymbolNotInUniverseError):
            assert_symbol_in_universe("ETHUSDT", u, date(2024, 6, 1))


# ──────────────────────────────────────────────────────────────────────
# Legacy FX path untouched
# ──────────────────────────────────────────────────────────────────────


class TestLegacyFxPathUntouched:
    """The universe module must not touch the legacy FX loaders or
    the engine's run_single signature."""

    def test_universe_module_does_not_import_data_loader(self):
        # Inspect the module's __dict__ for data_loader symbols.
        import backtest.universe as u_mod
        assert "CsvDataLoader" not in u_mod.__dict__
        assert "AbstractDataLoader" not in u_mod.__dict__

    def test_universe_module_does_not_import_engine(self):
        import backtest.universe as u_mod
        assert "BacktestEngine" not in u_mod.__dict__
        assert "engine" not in u_mod.__dict__

    def test_universe_module_does_not_mutate_engine_run_single(self):
        # Sanity: the real engine's run_single signature is unchanged
        # (positional bars, no PIT universe kwarg). The real engine
        # lives at engine.engine (backtest.types has only a stub
        # BacktestEngine with ``run``, not ``run_single``).
        try:
            from engine.engine import BacktestEngine  # type: ignore
        except Exception:
            # If the real engine module is not importable in the
            # test environment, skip rather than fail — the legacy
            # FX-path integrity is enforced by other tests.
            import pytest

            pytest.skip("engine.engine not importable in this env")
            return
        import inspect

        sig = inspect.signature(BacktestEngine.run_single)
        param_names = list(sig.parameters.keys())
        # run_single is the per-strategy entrypoint; the universe
        # is wired at the wrapper level (integrity_config), not here.
        assert "universe" not in param_names
        assert "as_of" not in param_names
        assert "integrity_config" not in param_names

    def test_universe_does_not_import_subclasses_of_BacktestEngine(self):
        # The universe module is pure data construction; it must
        # not import any engine subclass (engine_enhanced,
        # multi_strategy_engine, simple_engine, etc.).
        import backtest.universe as u_mod
        forbidden = (
            "EnhancedBacktestEngine",
            "MultiStrategyEngine",
            "SimpleBacktestEngine",
            "WalkForwardEngine",
        )
        for name in forbidden:
            assert name not in u_mod.__dict__, (
                f"universe module must not import {name}"
            )

    def test_legacy_csv_data_loader_still_imports(self):
        # The legacy FX loader must still work — no breaking
        # changes to its public surface.
        from backtest.data_loader import CsvDataLoader  # noqa: F401

    def test_legacy_abstract_data_loader_still_imports(self):
        from backtest.abstract_data_loader import (  # noqa: F401
            AbstractDataLoader,
        )


# ──────────────────────────────────────────────────────────────────────
# Default universe sanity
# ──────────────────────────────────────────────────────────────────────


class TestDefaultUniverseSanity:
    """The seed (DEFAULT_CRYPTO_UNIVERSE) must satisfy the spec
    contract: BTC/ETH/SOL, never-delisted, listed_from >= 2020-01-01."""

    def test_default_universe_has_three_majors(self):
        assert len(DEFAULT_CRYPTO_UNIVERSE) == 3
        assert tuple(sorted(DEFAULT_NEVER_DELISTED_MAJORS)) == (
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
        )

    def test_default_majors_are_never_delisted(self):
        for entry in DEFAULT_CRYPTO_UNIVERSE:
            assert entry.delisted_at is None

    def test_default_majors_have_listed_from_default(self):
        for entry in DEFAULT_CRYPTO_UNIVERSE:
            assert entry.listed_from == DEFAULT_LISTED_FROM

    def test_default_listed_from_is_jan_1_2020(self):
        assert DEFAULT_LISTED_FROM == date(2020, 1, 1)

    def test_default_universe_resolves_before_2020_raises(self):
        # Default listing is 2020-01-01; any date before that has
        # zero live symbols.
        with pytest.raises(EmptyUniverseError):
            resolve_universe(DEFAULT_CRYPTO_UNIVERSE, date(2019, 12, 31))

    def test_default_universe_resolves_far_future(self):
        # Future-dated queries are allowed.
        assert resolve_universe(DEFAULT_CRYPTO_UNIVERSE, date(2099, 12, 31)) == (
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
        )


# ──────────────────────────────────────────────────────────────────────
# Error hierarchy
# ──────────────────────────────────────────────────────────────────────


class TestErrorHierarchy:
    """All universe errors inherit from ValueError so existing
    ``pytest.raises(ValueError)`` callers catch them."""

    def test_universe_error_is_value_error(self):
        assert issubclass(UniverseError, ValueError)

    def test_config_error_is_universe_error(self):
        assert issubclass(UniverseConfigError, UniverseError)

    def test_empty_error_is_universe_error(self):
        assert issubclass(EmptyUniverseError, UniverseError)

    def test_symbol_not_in_universe_is_universe_error(self):
        assert issubclass(SymbolNotInUniverseError, UniverseError)

    def test_integrity_error_is_universe_error(self):
        assert issubclass(UniverseIntegrityError, UniverseError)

    def test_empty_error_carries_as_of(self):
        e = EmptyUniverseError("test", as_of=date(2024, 6, 1))
        assert e.as_of == date(2024, 6, 1)

    def test_symbol_not_in_universe_carries_symbol_and_as_of(self):
        e = SymbolNotInUniverseError(
            "test", symbol="BTCUSDT", as_of=date(2024, 6, 1)
        )
        assert e.symbol == "BTCUSDT"
        assert e.as_of == date(2024, 6, 1)
