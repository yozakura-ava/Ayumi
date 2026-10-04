"""Mandatory spread / commission / slippage defaults (Liora ground rule).

Per the spec §1 cross-cutting concerns:

    Spread costs mandatory: XAUUSD 3.0 pips, EURUSD/GBPUSD 1.5 pips,
                             USDJPY 1.2 pips
    Commission: $3.5/lot, slippage 0.2 pips (Liora ground rule)

This module is the single source of truth for those defaults — they MUST
NOT be inlined anywhere else in the pipeline (no magic numbers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


# Fixed ground-rule constants (never mutate, never inline elsewhere).
COMMISSION_PER_LOT_USD: float = 3.5
PIP_SLIPPAGE: float = 0.2


@dataclass(frozen=True)
class SpreadCosts:
    """One pair's spread + commission + slippage.

    All values are in **price-pips** except :attr:`commission_per_lot_usd`,
    which is in USD per round-turn lot.  ``slippage_pips`` is shared across
    pairs by default (Liora ground rule) but is exposed per-pair so a future
    symbol-class override can override it without touching this module.
    """

    symbol: str
    spread_pips: float
    commission_per_lot_usd: float = COMMISSION_PER_LOT_USD
    slippage_pips: float = PIP_SLIPPAGE

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("SpreadCosts.symbol must be non-empty")
        if self.spread_pips < 0:
            raise ValueError(
                f"SpreadCosts({self.symbol!r}) spread_pips must be >= 0"
            )
        if self.slippage_pips < 0:
            raise ValueError(
                f"SpreadCosts({self.symbol!r}) slippage_pips must be >= 0"
            )
        if self.commission_per_lot_usd < 0:
            raise ValueError(
                f"SpreadCosts({self.symbol!r}) commission_per_lot_usd must be >= 0"
            )


@dataclass(frozen=True)
class SpreadCostTable:
    """Immutable per-symbol spread cost lookup.

    Use :meth:`get` / :meth:`for_symbol` to look up a pair; :meth:`symbols`
    enumerates the registered pairs.  Construction is purely declarative —
    this class does no I/O and can be unit-tested without market data.
    """

    entries: tuple[SpreadCosts, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for entry in self.entries:
            key = entry.symbol.upper()
            if key in seen:
                raise ValueError(f"Duplicate symbol in SpreadCostTable: {key!r}")
            seen.add(key)

    @classmethod
    def from_mapping(
        cls, mapping: Mapping[str, float], *, slippage_pips: float = PIP_SLIPPAGE
    ) -> "SpreadCostTable":
        """Build from a ``{SYMBOL: spread_pips}`` dict (canonical defaults).

        Commission and slippage use the Liora-ground-rule defaults; override
        the resulting :class:`SpreadCostTable` only when a symbol-class
        deviation is explicitly approved (none exists in SFA-1).
        """
        return cls(
            entries=tuple(
                SpreadCosts(
                    symbol=symbol,
                    spread_pips=float(spread_pips),
                    slippage_pips=slippage_pips,
                )
                for symbol, spread_pips in mapping.items()
            )
        )

    def get(self, symbol: str) -> SpreadCosts:
        """Look up by symbol (case-insensitive); raise ``KeyError`` if unknown."""
        key = symbol.upper()
        for entry in self.entries:
            if entry.symbol.upper() == key:
                return entry
        raise KeyError(
            f"No spread cost entry for symbol {symbol!r} "
            f"(registered: {sorted(e.symbol for e in self.entries)!r})"
        )

    def for_symbol(self, symbol: str) -> SpreadCosts:
        """Alias for :meth:`get` (call-site readability)."""
        return self.get(symbol)

    @property
    def symbols(self) -> tuple[str, ...]:
        """All registered symbols (uppercased)."""
        return tuple(entry.symbol.upper() for entry in self.entries)


def default_spread_costs() -> SpreadCostTable:
    """Return the Liora-ground-rule spread cost table.

    Mirrors spec §1 cross-cutting concerns verbatim — keep these numbers in
    lock-step with the spec, never duplicate them elsewhere.
    """
    canonical: dict[str, float] = {
        "XAUUSD": 3.0,
        "EURUSD": 1.5,
        "GBPUSD": 1.5,
        "USDJPY": 1.2,
    }
    return SpreadCostTable.from_mapping(canonical)


__all__ = [
    "COMMISSION_PER_LOT_USD",
    "PIP_SLIPPAGE",
    "SpreadCosts",
    "SpreadCostTable",
    "default_spread_costs",
]