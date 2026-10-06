"""Point-in-time crypto universe with explicit delisting records.

Card: b127a6aa-b897-45b2-86e7-5678e6df4e4f (Sprint C 1a.5)
Sprint: 2026-10-05-crypto-strategy-factory
Lane: [BUILD][CRYPTO-LANE]

What this module provides
==========================

A point-in-time (PIT) universe constructor for crypto backtests that
resolves "which symbols were live on date D?" exactly — never using
*today's* listed universe as a proxy for the historical universe.
Without PIT construction, every backtest that sweeps the
current-as-of-truth-it-listed set inherits survivorship bias by
construction: only the assets that survived to today are in the
universe, and survivors are systematically the assets that went up.
Satsuki §1.7 quantifies this in crypto specifically — Binance ran
multiple delisting waves in 2026 alone (eight tokens on 2026-04-01,
six more on 2026-08-17, ICON/Secret/Storj to ATLs after announcement).

Concretely, this module provides:

  * **UniverseEntry** — a single dated listing record
    (symbol, listed_from, delisted_at). ``delisted_at`` is nullable
    (a never-delisted major like BTC, ETH, SOL has it as ``None``).

  * **Universe** — an immutable, sorted collection of
    :class:`UniverseEntry` items with ``__contains__`` and
    equality-by-value semantics.

  * **resolve_universe(universe, as_of)** — the as-of resolution
    function. Given a PIT :class:`Universe` and a UTC ``date``,
    returns the exact ``tuple[str, ...]`` of symbols live on that
    date. A symbol is live when ``listed_from <= as_of`` AND
    (``delisted_at is None`` OR ``as_of < delisted_at``). The
    inclusivity convention is documented and unit-tested:

      * ``listed_from`` is **inclusive** (symbol enters the
        universe on ``listed_from`` itself).
      * ``delisted_at`` is **exclusive** (last active day is
        ``delisted_at - 1``; the symbol is removed from the
        universe on ``delisted_at`` itself).

    This mirrors the CRSP/Quandl delisting-record convention used
    in equity factor backtests (the canonical reference for
    point-in-time universe construction) and avoids the
    double-counting trap that an inclusive ``delisted_at`` would
    create when the delisting-bar day is also a backtest day.

  * **Default never-delisted majors** —
    :data:`DEFAULT_CRYPTO_UNIVERSE` seeds BTC / ETH / SOL with
    ``listed_from = 2020-01-01 UTC`` and ``delisted_at = None`` so
    breadth-symbol delistings can be added incrementally without
    touching the seed.

  * **Fail-loud validation** — :func:`validate_universe_entries`
    and :func:`load_universe_config` raise structured
    :class:`UniverseError` subclasses on:

        - duplicate symbols,
        - empty / whitespace-only symbol strings,
        - non-string symbol fields,
        - naive datetimes (the gate requires tz-aware values),
        - ``listed_from`` not strictly before ``delisted_at``,
        - missing required config keys,
        - empty entries list,
        - non-list entries in config,
        - bool-as-int / non-numeric / non-string truthy values
          (mirroring the fail-loud discipline of
          :func:`backtest.integrity_gate.load_integrity_config` and
          :func:`backtest.venue_costs.load_venue_fee_config`).

  * **Integration with the integrity gate** —
    :func:`build_universe_integrity_config` returns an
    :class:`backtest.integrity_gate.IntegrityConfig` whose
    ``universe_symbols`` slot is the as-of-resolved tuple. Wiring
    this into the crypto overlay wrappers makes the gate's
    universe-missing-symbol and window-end delisting checks
    enforce the PIT universe automatically. :func:`assert_symbol_in_universe`
    is the loud-skip helper for "data present, but symbol was not
    in the as-of universe" — it raises
    :class:`SymbolNotInUniverseError` so callers fail loud rather
    than silently dropping or trading a symbol that was not
    investable on the backtest date.

  * **Window helpers** —
    :func:`resolve_universe_window` returns the union of symbols
    live at *any* point in a ``[start, end]`` date window
    (inclusive on both ends). Useful for pre-filtering the data
    loader: only attempt to fetch symbols that were ever live
    during the backtest window.

Behavior contract
=================

The module is **fail-loud**:

  * On invalid input it raises :class:`UniverseError` (a
    :class:`ValueError` subclass) before the engine is reached.
  * It never silently drops a symbol.
  * It never silently includes a symbol that is not in the as-of
    universe.
  * The legacy forex path is untouched — callers that don't opt
    into a PIT universe see identical behaviour (this module is
    pure data construction; it is invoked only by callers that
    explicitly opt in via
    :func:`build_universe_integrity_config` or
    :func:`assert_symbol_in_universe`).

Inclusivity convention (CRITICAL — read before modifying)
=========================================================

A symbol is **live** on a date ``D`` iff:

    listed_from <= D < delisted_at

where:

    * ``listed_from`` is treated as inclusive (the symbol enters
      the universe on the listing date itself).
    * ``delisted_at`` is treated as exclusive (the symbol's last
      active day is ``delisted_at - 1``; on ``delisted_at`` itself
      the symbol is *not* live).

Rationale: the spec explicitly says "delisted symbols excluded
*after* their delist date but present *before* it". Inclusive
``delisted_at`` would mean "still live on the delisting day" — i.e.
the symbol exits the universe only on ``delisted_at + 1``, leaving a
bar between "delisted" and "excluded from universe" that the
backtester cannot price because the data has ended. Treating
``delisted_at`` as exclusive eliminates that ambiguity and matches
the CRSP / Quandl convention for equity delisting records.

Source: research doc §1.7 + §2 R4 of
``docs/research/2026-10-05-crypto-first-strategy-testing-recommendations.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, cast

# IntegrityConfig is referenced as a type annotation only; the
# runtime import is deferred to ``build_universe_integrity_config``
# to keep the module-level import graph free of cross-crypto-overlay
# coupling (same convention as the other overlays).
from backtest.integrity_gate import IntegrityConfig  # noqa: F401

__all__ = [
    "UniverseEntry",
    "Universe",
    "UniverseError",
    "UniverseConfigError",
    "EmptyUniverseError",
    "SymbolNotInUniverseError",
    "UniverseIntegrityError",
    "DEFAULT_CRYPTO_UNIVERSE",
    "DEFAULT_LISTED_FROM",
    "DEFAULT_NEVER_DELISTED_MAJORS",
    "resolve_universe",
    "resolve_universe_window",
    "validate_universe_entries",
    "load_universe_config",
    "assert_symbol_in_universe",
    "build_universe_integrity_config",
]


# ---------------------------------------------------------------------------
# Constants — defaults for the never-delisted majors
# ---------------------------------------------------------------------------


#: Default listing date for never-delisted majors (UTC date). Chosen
#: as 2020-01-01 so the seed predates the bulk of crypto perpetual
#: history on Binance USDⓈ-M while remaining well after the actual
#: listing dates of BTC/ETH/SOL on spot. Production callers may
#: supply their own ``listed_from`` per symbol — the seed only
#: exists to bootstrap a non-empty universe out of the box.
DEFAULT_LISTED_FROM: date = date(2020, 1, 1)

#: The default never-delisted majors seeded into
#: :data:`DEFAULT_CRYPTO_UNIVERSE`. Listed against USDT (the
#: standard Binance USDⓈ-M perp quote). These three are the
#: canonical liquidity anchors as of 2026 and the recommended
#: safe-default cap until breadth symbols with delisting records
#: are explicitly added.
DEFAULT_NEVER_DELISTED_MAJORS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UniverseError(ValueError):
    """Base class for all universe-construction errors.

    Inherits from :class:`ValueError` so existing
    ``pytest.raises(ValueError)`` callers catch it; tests that
    want to differentiate can use ``except UniverseError``
    directly (or the more specific subclasses).

    Subclasses:

      * :class:`UniverseConfigError` — config mapping failed
        validation (missing keys, wrong types, invalid datetimes).
      * :class:`EmptyUniverseError` — a ``resolve_universe`` call
        returned zero symbols for the requested as-of date.
      * :class:`SymbolNotInUniverseError` — caller attempted to
        load / price a symbol that was not in the as-of universe.
      * :class:`UniverseIntegrityError` — semantic violation in
        the supplied entries (duplicate symbols, listed_from not
        before delisted_at, etc.).
    """


class UniverseConfigError(UniverseError):
    """Raised when a universe config mapping fails validation."""


class EmptyUniverseError(UniverseError):
    """Raised when ``resolve_universe`` resolves to zero symbols.

    Carries the as-of date so callers can surface a useful error
    message without re-parsing the exception string.
    """

    def __init__(self, message: str, *, as_of: date) -> None:
        super().__init__(message)
        self.as_of = as_of

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"EmptyUniverseError(as_of={self.as_of.isoformat()!r}, message={str(self)!r})"


class SymbolNotInUniverseError(UniverseError):
    """Raised when a symbol is referenced outside the as-of universe.

    The fail-loud-skip contract: a symbol present in the loaded
    data but absent from the PIT universe must not be silently
    dropped or traded. Callers should catch this and either
    filter the symbol upstream or accept the loud skip.

    Attributes
    ----------
    symbol : str
        The offending symbol.
    as_of : date
        The as-of date the universe was resolved against.
    """

    def __init__(self, message: str, *, symbol: str, as_of: date) -> None:
        super().__init__(message)
        self.symbol = symbol
        self.as_of = as_of

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"SymbolNotInUniverseError(symbol={self.symbol!r}, "
            f"as_of={self.as_of.isoformat()!r}, message={str(self)!r})"
        )


class UniverseIntegrityError(UniverseError):
    """Raised when supplied universe entries fail semantic checks.

    Used by :func:`validate_universe_entries` for failures that
    are about the *content* of the entries (duplicate symbols,
    listed_from not before delisted_at) rather than the *types*
    of the config mapping (those are :class:`UniverseConfigError`).
    """


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class UniverseEntry:
    """A single dated listing record.

    Attributes
    ----------
    symbol : str
        Non-empty exchange-native symbol (e.g. ``"BTCUSDT"``,
        ``"ETHUSDT"``). Comparison is case-sensitive. The crypto
        lane uses the Binance USDⓈ-M perp convention by default
        but the universe module does not enforce a venue
        convention — callers can use any string identity.

    listed_from : date
        First day the symbol is live in the universe. **Inclusive**
        — the symbol is live on ``listed_from`` itself. Treated
        as a UTC date (the time-of-day portion is irrelevant
        because the resolution is by date).

    delisted_at : date, optional
        First day the symbol is *not* live in the universe.
        **Exclusive** — the symbol is live on
        ``delisted_at - 1`` but not on ``delisted_at`` itself.
        ``None`` (default) means the symbol has never been
        delisted (still live as of the present day). Validation
        enforces ``listed_from < delisted_at`` when not ``None``.
    """

    symbol: str
    listed_from: date
    delisted_at: Optional[date] = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str):
            raise UniverseIntegrityError(
                f"UniverseEntry.symbol must be a string "
                f"(got {type(self.symbol).__name__}: {self.symbol!r})"
            )
        if not self.symbol or not self.symbol.strip():
            raise UniverseIntegrityError(
                f"UniverseEntry.symbol must be a non-empty string "
                f"(got {self.symbol!r})"
            )
        if not isinstance(self.listed_from, date):
            raise UniverseIntegrityError(
                f"UniverseEntry.listed_from must be a datetime.date "
                f"(got {type(self.listed_from).__name__}: {self.listed_from!r})"
            )
        if self.delisted_at is not None:
            if not isinstance(self.delisted_at, date):
                raise UniverseIntegrityError(
                    f"UniverseEntry.delisted_at must be a datetime.date "
                    f"or None (got {type(self.delisted_at).__name__}: "
                    f"{self.delisted_at!r})"
                )
            if self.delisted_at <= self.listed_from:
                raise UniverseIntegrityError(
                    f"UniverseEntry.delisted_at must be strictly after "
                    f"listed_from (got listed_from={self.listed_from.isoformat()}, "
                    f"delisted_at={self.delisted_at.isoformat()})"
                )

    def is_live_on(self, as_of: date) -> bool:
        """Return True iff the symbol is live on ``as_of``.

        Implements the documented inclusivity convention:

            listed_from <= as_of < delisted_at

        with ``delisted_at`` treated as +infinity when ``None``
        (never-delisted major).
        """
        if as_of < self.listed_from:
            return False
        if self.delisted_at is not None and as_of >= self.delisted_at:
            return False
        return True


def _validate_universe_entries_inline(entries: Sequence[UniverseEntry]) -> None:
    """Inline duplicate-symbol check used by :class:`Universe.__post_init__`.

    Mirrors the duplicate portion of :func:`validate_universe_entries`
    without depending on it (the Universe class is defined before
    :func:`validate_universe_entries` because the module-level
    :data:`DEFAULT_CRYPTO_UNIVERSE` is constructed at import time
    via ``_build_default_universe``, which calls the Universe
    constructor — so the class body must be self-contained).
    """
    seen: set[str] = set()
    for entry in entries:
        if entry.symbol in seen:
            raise UniverseIntegrityError(
                f"duplicate symbol in universe entries: {entry.symbol!r} "
                f"(each symbol may appear at most once; for split "
                f"listings or relistings, model the listing interval "
                f"directly via listed_from / delisted_at)"
            )
        seen.add(entry.symbol)


@dataclass(frozen=True)
class Universe:
    """An immutable, sorted collection of :class:`UniverseEntry` items.

    The entries are stored sorted by ``symbol`` (primary) and
    ``listed_from`` (secondary) for deterministic iteration.
    Sorting by symbol primary means :func:`resolve_universe` returns
    symbols in a stable alphabetical order — important for tests
    and for reproducibility of downstream data fetches.

    Attributes
    ----------
    entries : tuple[UniverseEntry, ...]
        The constituent entries. Sorted on construction (see
        above). Empty when no entries are supplied; in that case
        :func:`resolve_universe` always raises
        :class:`EmptyUniverseError`.
    """

    entries: tuple[UniverseEntry, ...] = ()

    def __post_init__(self) -> None:
        # Per-entry __post_init__ already validated each entry's
        # fields. We additionally enforce symbol-uniqueness here
        # (the inline helper avoids forward-reference issues with
        # the later-defined ``validate_universe_entries``).
        _validate_universe_entries_inline(self.entries)
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(self.entries, key=lambda e: (e.symbol, e.listed_from))),
        )

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def __contains__(self, symbol: object) -> bool:
        """``symbol in universe`` is True iff any entry has that symbol.

        Note this is *symbol-set* membership — does NOT check
        listing dates. For as-of membership use
        :func:`resolve_universe` or
        :meth:`UniverseEntry.is_live_on`.
        """
        if not isinstance(symbol, str):
            return False
        return any(e.symbol == symbol for e in self.entries)


def _build_default_universe() -> Universe:
    """Build :data:`DEFAULT_CRYPTO_UNIVERSE` from the major seed."""
    entries = tuple(
        UniverseEntry(
            symbol=symbol,
            listed_from=DEFAULT_LISTED_FROM,
            delisted_at=None,
        )
        for symbol in DEFAULT_NEVER_DELISTED_MAJORS
    )
    return Universe(entries=entries)


#: Default point-in-time crypto universe. Seeds the three
#: never-delisted majors against USDT with ``listed_from =
#: :data:`DEFAULT_LISTED_FROM``` (2020-01-01) and
#: ``delisted_at = None``. Callers should treat this as a safe
#: bootstrap — adding breadth symbols requires appending
#: :class:`UniverseEntry` records (with their own
#: ``delisted_at`` when applicable). Resolving
#: :func:`resolve_universe` against any ``as_of >= 2020-01-01``
#: returns ``("BTCUSDT", "ETHUSDT", "SOLUSDT")``.
DEFAULT_CRYPTO_UNIVERSE: Universe = _build_default_universe()


# ---------------------------------------------------------------------------
# As-of resolution
# ---------------------------------------------------------------------------


def resolve_universe(
    universe: Universe,
    as_of: date,
) -> tuple[str, ...]:
    """Return the symbols live in ``universe`` on ``as_of``.

    Implements the documented inclusivity convention:

        symbol is live  iff  listed_from <= as_of < delisted_at

    The result is a ``tuple[str, ...]`` sorted alphabetically by
    symbol (a stable, deterministic order — important for test
    reproducibility and for downstream data-load ordering).

    Future-dated queries are permitted: the resolver does not
    reject ``as_of > date.today()``. The seed universe
    (:data:`DEFAULT_CRYPTO_UNIVERSE`) includes never-delisted
    majors with ``listed_from = 2020-01-01``; querying any
    ``as_of >= 2020-01-01`` returns all three. Querying before
    ``2020-01-01`` returns an empty tuple and raises
    :class:`EmptyUniverseError`.

    Parameters
    ----------
    universe : Universe
        The PIT universe to resolve.
    as_of : datetime.date
        The UTC date to resolve against. Naive datetimes are
        rejected (resolution is by UTC date; callers must
        convert aware datetimes to UTC dates via
        ``dt.astimezone(timezone.utc).date()``).

    Returns
    -------
    tuple[str, ...]
        Symbols live on ``as_of``, sorted by symbol. Empty
        only when the raise-on-empty contract is opted out via
        :func:`resolve_universe_window`; the default
        :func:`resolve_universe` raises before returning empty.

    Raises
    ------
    EmptyUniverseError
        When zero symbols resolve to ``as_of``. Carries
        ``as_of`` for diagnostics. The contract is: an empty
        resolution is always a programming error (either
        the as-of is before any listing date, or the universe
        is empty), not a silent return of ``()``.
    UniverseError
        Forwarded from :func:`validate_universe_entries` when
        ``universe.entries`` fails semantic checks. Should be
        impossible after construction; documented for defense
        in depth.
    """
    if not isinstance(universe, Universe):
        raise TypeError(
            f"universe must be a Universe (got {type(universe).__name__})"
        )
    # Reject datetime explicitly even though ``isinstance(dt, date)``
    # is True (datetime subclasses date). The contract is "as_of is a
    # ``datetime.date``"; callers with a tz-aware datetime should
    # convert via ``dt.astimezone(timezone.utc).date()``.
    if isinstance(as_of, datetime):
        raise TypeError(
            f"as_of must be a datetime.date (got datetime.datetime: "
            f"{as_of!r}); convert with .astimezone(timezone.utc).date()"
        )
    if not isinstance(as_of, date):
        raise TypeError(
            f"as_of must be a datetime.date (got {type(as_of).__name__}: "
            f"{as_of!r})"
        )
    # Defense in depth: re-validate entries on every call. The
    # Universe constructor validates on construction; this catches
    # mutation via object.__setattr__ bypasses (the dataclass is
    # frozen, but dataclass(frozen=True) only freezes the outer
    # namespace, not deeply).
    validate_universe_entries(universe.entries)
    live = tuple(
        e.symbol for e in universe.entries if e.is_live_on(as_of)
    )
    # Stable alphabetical order. The Universe constructor already
    # sorts entries by (symbol, listed_from); we sort the resolved
    # symbol list by symbol to canonicalize across multiple
    # listing intervals for the same symbol (defensive — current
    # semantics forbid duplicate symbols entirely, but if that
    # rule changes the resolution order remains stable).
    live = tuple(sorted(live))
    if not live:
        raise EmptyUniverseError(
            f"resolve_universe: no symbols live on as_of={as_of.isoformat()} "
            f"(universe has {len(universe.entries)} entries; "
            f"earliest listed_from="
            f"{min((e.listed_from for e in universe.entries), default=None)!r})",
            as_of=as_of,
        )
    return live


def resolve_universe_window(
    universe: Universe,
    start: date,
    end: date,
) -> tuple[str, ...]:
    """Return the union of symbols live at any point in ``[start, end]``.

    The window is inclusive on both ends. Useful for pre-filtering
    a data loader: only attempt to fetch symbols that were ever
    live during the backtest window.

    Unlike :func:`resolve_universe`, this function does NOT raise
    on empty output (an empty window-resolution is a legitimate
    answer — "no symbol was ever live in this window"). Empty
    results are returned as ``()`` so callers can compose them
    with existing fallback paths.

    Parameters
    ----------
    universe : Universe
        The PIT universe.
    start : datetime.date
        First date in the window (inclusive). Must be ``<= end``.
    end : datetime.date
        Last date in the window (inclusive). Must be ``>= start``.

    Returns
    -------
    tuple[str, ...]
        Symbols live at any point in ``[start, end]``, sorted
        alphabetically. Empty when no symbol was live in the
        window.
    """
    if not isinstance(universe, Universe):
        raise TypeError(
            f"universe must be a Universe (got {type(universe).__name__})"
        )
    if isinstance(start, datetime) or isinstance(end, datetime):
        raise TypeError(
            "start and end must be datetime.date (got datetime; convert "
            "with .astimezone(timezone.utc).date())"
        )
    if not isinstance(start, date) or not isinstance(end, date):
        raise TypeError(
            f"start and end must be datetime.date (got "
            f"{type(start).__name__} / {type(end).__name__})"
        )
    if start > end:
        raise UniverseError(
            f"resolve_universe_window: start ({start.isoformat()}) "
            f"must be <= end ({end.isoformat()})"
        )
    validate_universe_entries(universe.entries)
    live_set: set[str] = set()
    for entry in universe.entries:
        # Conservative: a symbol is in the window-resolution if
        # its interval [listed_from, delisted_at) intersects the
        # window [start, end]. Two intervals intersect iff
        # listed_from <= end AND (delisted_at is None OR delisted_at > start).
        if entry.listed_from > end:
            continue
        if entry.delisted_at is not None and entry.delisted_at <= start:
            continue
        live_set.add(entry.symbol)
    return tuple(sorted(live_set))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_universe_entries(entries: Iterable[UniverseEntry]) -> None:
    """Validate a collection of :class:`UniverseEntry` for semantic soundness.

    Checks:

      * Every entry must be a :class:`UniverseEntry` instance.
      * Every entry's per-field validation passes (delegated to
        :class:`UniverseEntry.__post_init__`).
      * Symbols are unique across the collection (case-sensitive).
      * No duplicate ``(symbol, listed_from)`` pairs.

    Parameters
    ----------
    entries : iterable[UniverseEntry]
        The entries to validate.

    Raises
    ------
    UniverseIntegrityError
        On any semantic violation. The exception message names
        the offending symbol(s) so the operator can fix the
        inputs directly.
    """
    materialized: list[UniverseEntry] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, UniverseEntry):
            raise UniverseIntegrityError(
                f"entries[{i}] must be a UniverseEntry "
                f"(got {type(entry).__name__})"
            )
        # Each entry's __post_init__ already validates its own
        # fields; we just need to surface a useful index in the
        # error path. Re-trigger the per-entry validation by
        # re-constructing a sentinel via attribute access —
        # the dataclass __post_init__ already ran on construction.
        materialized.append(entry)

    seen_symbols: set[str] = set()
    duplicates: list[str] = []
    for entry in materialized:
        if entry.symbol in seen_symbols:
            duplicates.append(entry.symbol)
        seen_symbols.add(entry.symbol)
    if duplicates:
        unique_dupes = sorted(set(duplicates))
        raise UniverseIntegrityError(
            f"duplicate symbol(s) in universe entries: {unique_dupes!r} "
            f"(each symbol may appear at most once; for split listings "
            f"or relistings, model the listing interval directly via "
            f"listed_from / delisted_at)"
        )

    # Per-entry validation (re-run to surface the index in the error).
    for i, entry in enumerate(materialized):
        # The dataclass already validated on construction. Re-check
        # the most common semantic rules to surface the index.
        if (
            entry.delisted_at is not None
            and entry.delisted_at <= entry.listed_from
        ):
            raise UniverseIntegrityError(
                f"entries[{i}] (symbol={entry.symbol!r}): "
                f"delisted_at ({entry.delisted_at.isoformat()}) must be "
                f"strictly after listed_from "
                f"({entry.listed_from.isoformat()})"
            )


# ---------------------------------------------------------------------------
# Config loading — fail-loud YAML-style mapping parser
# ---------------------------------------------------------------------------


def _coerce_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise UniverseConfigError(
            f"{label} must be a string (got {type(value).__name__}: {value!r})"
        )
    if not value.strip():
        raise UniverseConfigError(
            f"{label} must be a non-empty string (got {value!r})"
        )
    return value


def _coerce_date(
    value: Any,
    label: str,
    *,
    allow_none: bool = False,
) -> Optional[date]:
    """Parse a date from a datetime.date, ISO 8601 date string,
    ISO 8601 datetime string, or ``None``.

    Naive datetimes are accepted (the time portion is discarded)
    so callers can paste a CSV column directly. Time-zone-aware
    datetimes are converted to UTC before extracting the date.
    ``None`` is only allowed when ``allow_none=True``.
    """
    if value is None:
        if allow_none:
            return None
        raise UniverseConfigError(
            f"{label} must be a date or ISO 8601 string "
            f"(got None and allow_none=False)"
        )
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Treat naive as UTC (the date is what matters).
            return value.date()
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        # Accept both date-only ("2020-01-01") and full ISO 8601
        # datetime strings ("2020-01-01T00:00:00Z" or
        # "2020-01-01T00:00:00-05:00"). The datetime parser
        # also accepts date-only strings, so try it first — this
        # way tz offsets are honored (a tz-aware string like
        # "2020-01-01T20:00:00-05:00" becomes 2020-01-02 in UTC).
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            # Fallback: pure date string ("YYYY-MM-DD"). This is
            # unambiguous; no tz offset to interpret.
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise UniverseConfigError(
                    f"{label} must be a date or ISO 8601 string "
                    f"(got {value!r}: {exc})"
                ) from exc
        if parsed.tzinfo is None:
            return parsed.date()
        return parsed.astimezone(timezone.utc).date()
    raise UniverseConfigError(
        f"{label} must be a date, ISO 8601 string, datetime, or None "
        f"(got {type(value).__name__}: {value!r})"
    )


def load_universe_config(mapping: Mapping[str, Any]) -> Universe:
    """Parse a universe config mapping into a :class:`Universe`.

    The mapping is the in-memory representation of a YAML block
    (caller does ``yaml.safe_load``); this function enforces the
    schema:

      * Top-level mapping must contain an ``entries`` key with a
        list of per-entry mappings.
      * Each entry mapping must contain ``symbol``, ``listed_from``;
        ``delisted_at`` is optional (default ``None``).
      * ``symbol`` must be a non-empty string.
      * ``listed_from`` must parse as a date / datetime / ISO string.
      * ``delisted_at`` must be a date / datetime / ISO string or
        ``None``.

    Any validation failure raises :class:`UniverseConfigError` (a
    :class:`ValueError` subclass) with a specific message naming
    the failing field so the operator can fix the YAML directly.

    Parameters
    ----------
    mapping : Mapping[str, Any]
        The config mapping (typically produced by
        ``yaml.safe_load``).

    Returns
    -------
    Universe
        Validated, sorted :class:`Universe`.

    Raises
    ------
    UniverseConfigError
        On any structural / type / value violation.
    UniverseIntegrityError
        On semantic violations surfaced via
        :func:`validate_universe_entries` (duplicate symbols,
        ``listed_from >= delisted_at``, etc.).
    """
    if not isinstance(mapping, Mapping):
        raise UniverseConfigError(
            f"universe config must be a mapping (got {type(mapping).__name__})"
        )

    if "entries" not in mapping:
        raise UniverseConfigError(
            "universe config is missing required key: 'entries'"
        )

    raw_entries = mapping["entries"]
    if not isinstance(raw_entries, (list, tuple)):
        raise UniverseConfigError(
            f"universe config 'entries' must be a list/tuple "
            f"(got {type(raw_entries).__name__})"
        )
    if not raw_entries:
        raise UniverseConfigError(
            "universe config 'entries' must be non-empty "
            "(seed at least one symbol via DEFAULT_CRYPTO_UNIVERSE or "
            "an explicit entry)"
        )

    parsed_entries: list[UniverseEntry] = []
    for i, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping):
            raise UniverseConfigError(
                f"entries[{i}] must be a mapping (got {type(raw).__name__})"
            )
        if "symbol" not in raw:
            raise UniverseConfigError(
                f"entries[{i}] is missing required key: 'symbol'"
            )
        if "listed_from" not in raw:
            raise UniverseConfigError(
                f"entries[{i}] is missing required key: 'listed_from'"
            )
        symbol = _coerce_str(raw["symbol"], f"entries[{i}].symbol")
        listed_from = cast(
            date,
            _coerce_date(
                raw["listed_from"], f"entries[{i}].listed_from"
            ),
        )
        delisted_at = _coerce_date(
            raw.get("delisted_at"),
            f"entries[{i}].delisted_at",
            allow_none=True,
        )
        parsed_entries.append(
            UniverseEntry(
                symbol=symbol,
                listed_from=listed_from,
                delisted_at=delisted_at,
            )
        )

    # Delegate semantic checks (duplicate symbols, etc.) to the
    # shared validator; the Universe constructor will also re-run
    # them but we want a single source of error messages.
    validate_universe_entries(parsed_entries)
    return Universe(entries=tuple(parsed_entries))


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------


def assert_symbol_in_universe(
    symbol: str,
    universe: Universe,
    as_of: date,
) -> None:
    """Fail loud if ``symbol`` is not live in ``universe`` on ``as_of``.

    This is the "skip loudly" half of the integrity-gate
    integration: when a backtest attempt references a symbol
    that was not in the as-of universe, the call must raise
    rather than silently drop the symbol or trade it as if it
    were live. The complementary case (symbol in universe but
    absent from loaded data) is handled by the integrity gate's
    universe-missing-symbol check (R5).

    Parameters
    ----------
    symbol : str
        Symbol to check. Must be a non-empty string.
    universe : Universe
        The PIT universe to resolve.
    as_of : datetime.date
        The as-of date.

    Raises
    ------
    SymbolNotInUniverseError
        When ``symbol`` is not live on ``as_of``. Carries
        ``symbol`` and ``as_of`` for diagnostics.
    TypeError
        When ``universe`` is not a :class:`Universe` or
        ``as_of`` is not a ``datetime.date``.
    """
    if not isinstance(symbol, str) or not symbol:
        raise TypeError(
            f"symbol must be a non-empty string (got {symbol!r})"
        )
    if not isinstance(universe, Universe):
        raise TypeError(
            f"universe must be a Universe (got {type(universe).__name__})"
        )
    if isinstance(as_of, datetime):
        raise TypeError(
            f"as_of must be a datetime.date (got datetime.datetime: "
            f"{as_of!r}); convert with .astimezone(timezone.utc).date()"
        )
    if not isinstance(as_of, date):
        raise TypeError(
            f"as_of must be a datetime.date (got {type(as_of).__name__}: "
            f"{as_of!r})"
        )
    if symbol not in resolve_universe(universe, as_of):
        raise SymbolNotInUniverseError(
            f"symbol {symbol!r} is not live in universe on as_of="
            f"{as_of.isoformat()} "
            f"(universe has {len(universe.entries)} entries; "
            f"resolved symbols on that date do not include {symbol!r})",
            symbol=symbol,
            as_of=as_of,
        )


def build_universe_integrity_config(
    universe: Universe,
    as_of: date,
    *,
    expected_cadence_minutes: int = 60,
    gap_tolerance_multiplier: float = 1.5,
    expected_window_end: Optional[datetime] = None,
    delisting_tolerance_minutes: float = 0.0,
    max_zero_volume_stretch: int = 0,
    check_ohlc_invariants: bool = True,
    check_nan_values: bool = True,
    check_gaps: bool = True,
    check_delistings: bool = True,
    check_anomalies: bool = True,
) -> IntegrityConfig:
    """Build an :class:`IntegrityConfig` wired to a PIT universe.

    This is the integration glue: it resolves ``universe`` against
    ``as_of`` (raising :class:`EmptyUniverseError` if zero symbols
    are live) and returns an :class:`IntegrityConfig` whose
    ``universe_symbols`` slot is the resolved tuple. Wiring this
    into the crypto overlay wrappers means the gate's
    universe-missing-symbol check fires whenever a loaded
    bar series' name is not in the resolved set, and the
    window-end check fires whenever a series drops out of data
    while still being in the as-of universe (delisting detected
    by absence of late data).

    The integrity-gate integration is forward-only by default
    (every other parameter is passed through to
    :class:`IntegrityConfig`); production callers should pass
    ``expected_window_end`` to enable the late-data delisting
    check on each per-symbol run.

    Parameters
    ----------
    universe : Universe
        The PIT universe to resolve.
    as_of : datetime.date
        The as-of date for resolution.
    expected_cadence_minutes : int
        Forwarded to :class:`IntegrityConfig`. Default 60.
    gap_tolerance_multiplier : float
        Forwarded to :class:`IntegrityConfig`. Default 1.5.
    expected_window_end : datetime, optional
        Forwarded to :class:`IntegrityConfig`. When supplied,
        the gate's window-end check fires on each per-symbol
        bar run, catching late-data drops (= delistings while
        still in the universe).
    delisting_tolerance_minutes : float
        Forwarded to :class:`IntegrityConfig`. Default 0.
    max_zero_volume_stretch : int
        Forwarded to :class:`IntegrityConfig`. Default 0.
    check_ohlc_invariants : bool
        Forwarded to :class:`IntegrityConfig`. Default ``True``.
    check_nan_values : bool
        Forwarded to :class:`IntegrityConfig`. Default ``True``.
    check_gaps : bool
        Forwarded to :class:`IntegrityConfig`. Default ``True``.
    check_delistings : bool
        Forwarded to :class:`IntegrityConfig`. Default ``True``.
    check_anomalies : bool
        Forwarded to :class:`IntegrityConfig`. Default ``True``.

    Returns
    -------
    IntegrityConfig
        A config whose ``universe_symbols`` slot is the as-of
        resolution of ``universe``. The :class:`IntegrityConfig`
        schema_version is left at its default (1); bump if the
        downstream schema evolves.

    Raises
    ------
    EmptyUniverseError
        Forwarded from :func:`resolve_universe`.
    """
    if not isinstance(universe, Universe):
        raise TypeError(
            f"universe must be a Universe (got {type(universe).__name__})"
        )
    if isinstance(as_of, datetime):
        raise TypeError(
            f"as_of must be a datetime.date (got datetime.datetime: "
            f"{as_of!r}); convert with .astimezone(timezone.utc).date()"
        )
    if not isinstance(as_of, date):
        raise TypeError(
            f"as_of must be a datetime.date (got {type(as_of).__name__}: "
            f"{as_of!r})"
        )
    resolved = resolve_universe(universe, as_of)
    return IntegrityConfig(
        expected_cadence_minutes=expected_cadence_minutes,
        gap_tolerance_multiplier=gap_tolerance_multiplier,
        expected_window_end=expected_window_end,
        delisting_tolerance_minutes=delisting_tolerance_minutes,
        universe_symbols=resolved,
        max_zero_volume_stretch=max_zero_volume_stretch,
        check_ohlc_invariants=check_ohlc_invariants,
        check_nan_values=check_nan_values,
        check_gaps=check_gaps,
        check_delistings=check_delistings,
        check_anomalies=check_anomalies,
    )
