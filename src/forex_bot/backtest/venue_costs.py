"""Venue/tier-aware cost model with maker/taker branching for crypto backtests.

Card: 8f8a54e6-96cb-45f9-8de8-08b742dc246f (Sprint C 1a.3)
Sprint: 2026-10-05-crypto-strategy-factory
Lane: [BUILD][CRYPTO-LANE]

What this module provides
==========================

A venue/tier-aware trading-fee cost model for the crypto backtest core
that prices each order fill against the maker/taker schedule of the
*actual* exchange account being modeled. This is the third mandatory
cost term in the R1/R2 brief alongside adaptive funding
(``forex_bot.backtest.funding_model``) and mark-price liquidation
(``forex_bot.backtest.liquidation``).

Concretely, the module provides:

  * **Venue tier parameterization** — fee schedules are tables of
    (min 30-day volume, maker fee, taker fee) brackets, supplied via
    config and resolved at fill time. The tier tables are NOT
    hardcoded in this module — they are *pinned from the account's
    live fee page* per the R2 brief ("pin from the actual account's
    fee page at backtest time, do not hardcode a spot constant the
    way XAUUSD 3.0 / FX 1.5 are hardcoded today"). A default
    Binance USDⓈ-M VIP schedule is exposed as
    :data:`DEFAULT_BINANCE_USDM_TIERS` for tests and onboarding, but
    production callers must load their own.

  * **Maker/taker branching** — every fill records whether it was a
    *maker* (resting limit order that earned the maker rebate) or
    a *taker* (aggressive order that crossed the spread). The fee
    applied is the per-fill resolved rate (no rounding, no per-bar
    averaging) — this matters because a strategy's maker-taker mix is
    a *first-class* signal of execution quality and a single
    mis-classified fill materially biases the cost line for
    high-frequency templates.

  * **Fail-loud config validation** — :func:`load_venue_fee_config`
    parses a config mapping (e.g. loaded from YAML) into a
    :class:`VenueFeeConfig`, raising a structured :class:`ValueError`
    on:

        - missing ``name`` / ``tiers`` keys,
        - empty ``tiers``,
        - non-contiguous tier brackets (each tier's min must equal
          the previous tier's max),
        - negative 30-day volume,
        - negative or non-finite fee rates,
        - fee rates ``>= 1`` (sentinel for "decimal misformatted",
          e.g. ``0.05`` vs ``5.0``),
        - non-numeric types in any numeric field.

    The R2 brief's "fees pinned from live account values via config
    with fail-loud error if missing or malformed" requirement is met
    by this single function. The :class:`VenueFeeConfig` constructor
    also re-validates so callers that bypass the loader (e.g. inline
    constructions in synthetic tests) get the same guarantees.

  * **Per-fill overlay** — :func:`compute_venue_cost_lines` walks
    an externally-supplied list of ``VenueOrderFill`` events
    (time, fill price, quantity, intent, optional per-fill 30d
    volume override) and produces :class:`VenueCostLine` items
    annotated with the bar index and applied fee rate/amount.

  * **Engine wrapper** — :func:`run_backtest_with_venue_costs`
    runs :class:`BacktestEngine` unchanged (legacy path preserved)
    and overlays the per-fill venue cost onto the engine's metrics
    via a :class:`VenueCostBacktestMetrics` extension of
    :class:`backtest.types.BacktestMetrics`.

  * **3-way composition** —
    :func:`run_backtest_with_full_crypto_overlay` composes this
    module with :func:`run_backtest_with_funding` and
    :func:`run_backtest_with_liquidation`, returning all three
    overlays in one call. The three wrappers operate independently
    on the same engine run (per-bar deltas are additive at the
    caller's equity layer).

The legacy forex path (``BacktestEngine.run_single``) stays untouched.
Callers that don't opt into venue costs see identical metrics.

Fee economics
=============

A *maker* fill at price ``P`` with quantity ``q`` charges::

    fee = q * P * maker_fee_rate

A *taker* fill at price ``P`` with quantity ``q`` charges::

    fee = q * P * taker_fee_rate

Fees are always *positive* in the cost line (they reduce P&L);
the wrapper's :attr:`VenueCostBacktestMetrics.total_venue_cost`
sums all ``fee_amount`` values; the engine's ``ending_balance``
is reduced by this total in :attr:`venue_ending_balance`.

The fee rate is resolved per-fill by walking the venue tier table
against the fill's effective 30-day volume:

  * When the fill (or the config) supplies
    ``current_min_volume_30d_usd``, that value is used to look up
    the tier.
  * When the fill omits it, the config's value is used.
  * When neither is supplied, the *lowest* tier (typically VIP 0)
    is used — matching the most conservative (highest-fee) default.

Maker/taker intent is supplied per-fill via the ``intent`` field;
the module does NOT derive intent from order type (limit vs market)
because the backtest core doesn't model order-book interaction.

Source: research doc §1.5 + §2 R2 of
``docs/research/2026-10-05-crypto-first-strategy-testing-recommendations.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional, Sequence

from backtest.funding_model import (
    PositionSpec,
    run_backtest_with_funding,
)
from backtest.liquidation import (
    LiquidationSpec,
    run_backtest_with_liquidation,
)
from backtest.types import Bar

__all__ = [
    "VenueTier",
    "VenueFeeConfig",
    "VenueOrderFill",
    "VenueCostLine",
    "VenueCostBacktestMetrics",
    "DEFAULT_BINANCE_USDM_TIERS",
    "DEFAULT_BINANCE_USDM_FEE_CONFIG",
    "VenueConfigError",
    "resolve_venue_tier",
    "compute_fill_fee",
    "compute_venue_cost_lines",
    "load_venue_fee_config",
    "run_backtest_with_venue_costs",
    "run_backtest_with_full_crypto_overlay",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VenueConfigError(ValueError):
    """Raised when a venue fee config mapping fails validation.

    Inherits from :class:`ValueError` so existing ``pytest.raises(
    ValueError)`` callers catch it; tests that want to differentiate
    can use ``except VenueConfigError`` directly.
    """


# ---------------------------------------------------------------------------
# Default tier table — Binance USDⓈ-M (reported best-published VIP schedule)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueTier:
    """A single venue fee tier bracket.

    Attributes
    ----------
    min_volume_30d_usd : float
        Lower bound (inclusive) of the bracket, measured as 30-day
        trailing volume in either notional traded or quote-currency
        turnover (the venue's own definition). Must be ``>= 0``.
        The first tier's ``min_volume_30d_usd`` must be ``0`` and
        each subsequent tier's ``min_volume_30d_usd`` must equal
        the previous tier's ``max_volume_30d_usd`` (contiguous
        brackets — gaps would silently skip fee schedules).
    max_volume_30d_usd : float
        Upper bound (exclusive) of the bracket. Use ``math.inf``
        for the top tier. Validated in :class:`VenueFeeConfig`.
    maker_fee_rate : float
        Maker fee rate as a decimal (e.g. ``0.0002`` = 0.02%).
        Must be in ``[0, 1)``. Values ``>= 1`` are rejected as a
        sentinel for decimal-place errors (e.g. ``5.0`` instead of
        ``0.05``).
    taker_fee_rate : float
        Taker fee rate as a decimal (same validation as
        ``maker_fee_rate``). Taker is usually higher than maker;
        a taker rate below the maker rate is permitted (some
        venues do this for rebates on top tiers) but flagged in
        the docstring so reviewers catch it.
    """

    min_volume_30d_usd: float
    max_volume_30d_usd: float
    maker_fee_rate: float
    taker_fee_rate: float

    def __post_init__(self) -> None:
        if self.min_volume_30d_usd < 0:
            raise VenueConfigError(
                f"min_volume_30d_usd must be >= 0 (got {self.min_volume_30d_usd})"
            )
        if not math.isfinite(self.max_volume_30d_usd) and self.max_volume_30d_usd <= 0:
            raise VenueConfigError(
                f"max_volume_30d_usd must be positive (or math.inf); got "
                f"{self.max_volume_30d_usd}"
            )
        if self.max_volume_30d_usd <= self.min_volume_30d_usd and not math.isinf(
            self.max_volume_30d_usd
        ):
            raise VenueConfigError(
                f"max_volume_30d_usd ({self.max_volume_30d_usd}) must exceed "
                f"min_volume_30d_usd ({self.min_volume_30d_usd})"
            )
        _validate_fee_rate(self.maker_fee_rate, "maker_fee_rate")
        _validate_fee_rate(self.taker_fee_rate, "taker_fee_rate")


def _validate_fee_rate(rate: float, label: str) -> None:
    if not math.isfinite(rate):
        raise VenueConfigError(f"{label} must be finite (got {rate})")
    if rate < 0:
        raise VenueConfigError(f"{label} must be >= 0 (got {rate})")
    if rate >= 1:
        raise VenueConfigError(
            f"{label} must be < 1 (got {rate}); fees are decimals "
            f"e.g. 0.0005 for 0.05% — values >= 1 almost always mean "
            f"a decimal-place error"
        )


#: Default Binance USDⓈ-M VIP fee schedule (reported best-published
#: values; pinned at config-load). Sorted ascending by ``min_volume_30d_usd``.
#: Sources: Binance delivery-constant fee schedule pages, 2026-09 snapshot
#: cross-checked with two practitioner fee-tracking posts cited in
#: research doc §1.5.
DEFAULT_BINANCE_USDM_TIERS: tuple[VenueTier, ...] = (
    VenueTier(0.0,            1_000_000.0,    0.000200, 0.000500),  # VIP 0
    VenueTier(1_000_000.0,    5_000_000.0,    0.000180, 0.000450),  # VIP 1
    VenueTier(5_000_000.0,    25_000_000.0,   0.000160, 0.000400),  # VIP 2
    VenueTier(25_000_000.0,   100_000_000.0,  0.000140, 0.000350),  # VIP 3
    VenueTier(100_000_000.0,  250_000_000.0,  0.000120, 0.000300),  # VIP 4
    VenueTier(250_000_000.0,  math.inf,       0.000080, 0.000270),  # VIP 5+
)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueFeeConfig:
    """Top-level venue fee configuration.

    Attributes
    ----------
    venue_name : str
        Human-readable venue identifier (e.g. ``"Binance USDⓈ-M"``).
        Used in error messages and logged in metrics for traceability
        of the fee schedule that produced a result.
    tiers : tuple[VenueTier, ...]
        Sorted, contiguous tier table. Validated at construction.
    current_min_volume_30d_usd : float, optional
        The account's *current* 30-day volume. When supplied, it is
        used to resolve the tier for any input that doesn't carry its
        own per-fill volume override. When ``None``, the lowest tier
        (typically VIP 0) is used as the most conservative default.
    schema_version : int
        Config schema version. Bumped when the on-disk YAML structure
        changes; callers can refuse to load a config with a newer
        schema than they recognize. Defaults to ``1``.
    """

    venue_name: str
    tiers: tuple[VenueTier, ...]
    current_min_volume_30d_usd: Optional[float] = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.venue_name, str) or not self.venue_name.strip():
            raise VenueConfigError(
                f"venue_name must be a non-empty string (got {self.venue_name!r})"
            )
        if not self.tiers:
            raise VenueConfigError("tiers must be non-empty")
        if self.current_min_volume_30d_usd is not None:
            if not math.isfinite(self.current_min_volume_30d_usd):
                raise VenueConfigError(
                    f"current_min_volume_30d_usd must be finite (got "
                    f"{self.current_min_volume_30d_usd})"
                )
            if self.current_min_volume_30d_usd < 0:
                raise VenueConfigError(
                    f"current_min_volume_30d_usd must be >= 0 (got "
                    f"{self.current_min_volume_30d_usd})"
                )
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise VenueConfigError(
                f"schema_version must be a positive int (got {self.schema_version})"
            )
        # Validate tier table contiguity once at construction so
        # callers don't discover bad brackets at runtime.
        prev_max: Optional[float] = None
        for i, tier in enumerate(self.tiers):
            if i == 0 and tier.min_volume_30d_usd != 0.0:
                raise VenueConfigError(
                    f"first tier must start at min_volume_30d_usd == 0 (got "
                    f"{tier.min_volume_30d_usd})"
                )
            if prev_max is not None and tier.min_volume_30d_usd != prev_max:
                raise VenueConfigError(
                    f"tiers must be contiguous; tier {i} starts at "
                    f"{tier.min_volume_30d_usd}, expected {prev_max}"
                )
            if not math.isinf(tier.max_volume_30d_usd) and tier.max_volume_30d_usd <= tier.min_volume_30d_usd:
                raise VenueConfigError(
                    f"tier {i} max_volume_30d_usd ({tier.max_volume_30d_usd}) "
                    f"must exceed min_volume_30d_usd ({tier.min_volume_30d_usd})"
                )
            prev_max = tier.max_volume_30d_usd


#: Convenience default config constructed from
#: :data:`DEFAULT_BINANCE_USDM_TIERS`. Tests and onboarding use this;
#: production callers must pin their own fees from the live fee page.
DEFAULT_BINANCE_USDM_FEE_CONFIG: VenueFeeConfig = VenueFeeConfig(
    venue_name="Binance USDⓈ-M",
    tiers=DEFAULT_BINANCE_USDM_TIERS,
    current_min_volume_30d_usd=None,  # most conservative (VIP 0)
)


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


def resolve_venue_tier(
    min_volume_30d_usd: float,
    tiers: tuple[VenueTier, ...] = DEFAULT_BINANCE_USDM_TIERS,
) -> VenueTier:
    """Resolve the venue tier for a given 30-day volume.

    Walks ``tiers`` in order and returns the first tier whose
    ``min_volume_30d_usd <= volume < max_volume_30d_usd``. If the
    volume exceeds the highest tier's ``min_volume_30d_usd`` (i.e.
    falls in the open-ended top tier with ``max_volume_30d_usd ==
    inf``), the top tier is returned.

    Parameters
    ----------
    min_volume_30d_usd : float
        Account's 30-day trailing volume in USD. Must be ``>= 0``.
    tiers : tuple[VenueTier, ...]
        Tier table to walk. Defaults to
        :data:`DEFAULT_BINANCE_USDM_TIERS`.

    Returns
    -------
    VenueTier
        The tier containing this volume.

    Raises
    ------
    VenueConfigError
        If ``min_volume_30d_usd < 0`` or ``tiers`` is empty.
    """
    if min_volume_30d_usd < 0:
        raise VenueConfigError(
            f"min_volume_30d_usd must be >= 0 (got {min_volume_30d_usd})"
        )
    if not tiers:
        raise VenueConfigError("tiers must be non-empty")
    for tier in tiers:
        if tier.min_volume_30d_usd <= min_volume_30d_usd < tier.max_volume_30d_usd:
            return tier
    # Past the top tier's open-ended bracket (max == inf).
    # Same fallback contract as resolve_maintenance_margin_rate: when
    # a non-inf top tier was supplied and the volume exceeds it, fall
    # through to the highest tier's rate rather than failing.
    return tiers[-1]


# ---------------------------------------------------------------------------
# Per-fill fee calculation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueOrderFill:
    """A single order fill event with maker/taker intent.

    Attributes
    ----------
    time : datetime
        Fill time in UTC. Must be timezone-aware (validated by the
        line-item constructor; naive datetimes are normalized to UTC
        so downstream arithmetic is consistent).
    fill_price : float
        Price at which the order filled (USD per unit). Must be > 0.
    quantity : float
        Absolute quantity filled (in base-asset units). Must be > 0.
        Direction is intentionally NOT tracked — fees are symmetric
        in qty across buy/hold (no taker direction premium in the
        base model; rebate / rate-by-direction is a future extension).
    intent : {'maker', 'taker'}
        Whether the fill was a *maker* (resting limit order filled
        by an incoming order) or a *taker* (aggressive order that
        crossed the spread). The fee applied is the per-fill
        resolved rate — there is no averaging, no rounding.
    fill_min_volume_30d_usd : float, optional
        Per-fill override for the 30-day volume used to resolve the
        fee tier. When ``None``, the config's
        ``current_min_volume_30d_usd`` is used; when that is also
        ``None``, the lowest tier is used. Per-fill overrides exist
        for strategies whose 30-day volume shifts within a backtest
        (e.g. a strategy that's well-known to trade more during
        high-volume regimes — the caller may pin a higher tier for
        those fills).

    Notes
    -----
    Constructing a :class:`VenueOrderFill` directly is supported for
    tests and synthetic fixtures. Production callers typically derive
    fill events from the engine's per-trade book + order routing
    policy.
    """

    time: datetime
    fill_price: float
    quantity: float
    intent: Literal["maker", "taker"]
    fill_min_volume_30d_usd: Optional[float] = None

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            raise VenueConfigError(
                f"VenueOrderFill.time must be timezone-aware (got naive {self.time!r})"
            )
        if self.time.tzinfo != timezone.utc:
            object.__setattr__(self, "time", self.time.astimezone(timezone.utc))
        if self.fill_price <= 0:
            raise VenueConfigError(
                f"fill_price must be positive (got {self.fill_price})"
            )
        if not math.isfinite(self.fill_price):
            raise VenueConfigError(
                f"fill_price must be finite (got {self.fill_price})"
            )
        if self.quantity <= 0:
            raise VenueConfigError(
                f"quantity must be positive (got {self.quantity})"
            )
        if self.intent not in ("maker", "taker"):
            raise VenueConfigError(
                f"intent must be 'maker' or 'taker' (got {self.intent!r})"
            )
        if self.fill_min_volume_30d_usd is not None:
            if not math.isfinite(self.fill_min_volume_30d_usd):
                raise VenueConfigError(
                    f"fill_min_volume_30d_usd must be finite (got "
                    f"{self.fill_min_volume_30d_usd})"
                )
            if self.fill_min_volume_30d_usd < 0:
                raise VenueConfigError(
                    f"fill_min_volume_30d_usd must be >= 0 (got "
                    f"{self.fill_min_volume_30d_usd})"
                )


def compute_fill_fee(
    fill_price: float,
    quantity: float,
    intent: Literal["maker", "taker"],
    config: VenueFeeConfig,
    fill_min_volume_30d_usd: Optional[float] = None,
) -> tuple[float, float]:
    """Compute the fee (rate, amount) for a single fill against a venue config.

    Parameters
    ----------
    fill_price : float
        Fill price (USD per unit). Must be > 0.
    quantity : float
        Absolute quantity filled (base-asset units). Must be > 0.
    intent : {'maker', 'taker'}
        Maker/taker branch selector.
    config : VenueFeeConfig
        Venue fee config to resolve the tier from.
    fill_min_volume_30d_usd : float, optional
        Per-call override for the 30-day volume used to resolve the
        tier. When ``None``, ``config.current_min_volume_30d_usd`` is
        used; when that is also ``None``, the lowest tier is used.

    Returns
    -------
    tuple[float, float]
        ``(fee_rate, applied_amount)`` where:

        * ``fee_rate`` is the per-unit fee rate (e.g. ``0.0005`` for
          0.05% taker);
        * ``applied_amount`` is ``quantity * fill_price * fee_rate``
          (i.e. the dollar fee).

    Notes
    -----
    This is the *core* per-fill primitive. Every other fee path
    (line-item aggregation, wrapper overlay, composition) bottoms
    out in this function so the math is auditable in one place.
    """
    if fill_price <= 0:
        raise VenueConfigError(f"fill_price must be positive (got {fill_price})")
    if quantity <= 0:
        raise VenueConfigError(f"quantity must be positive (got {quantity})")
    if intent not in ("maker", "taker"):
        raise VenueConfigError(f"intent must be 'maker' or 'taker' (got {intent!r})")
    if not isinstance(config, VenueFeeConfig):
        raise TypeError(
            f"config must be VenueFeeConfig (got {type(config).__name__})"
        )
    # Per-fill volume pre-check: surface the per-fill label in the
    # error message rather than the generic "min_volume_30d_usd" label
    # that resolve_venue_tier raises.
    if fill_min_volume_30d_usd is not None:
        if not math.isfinite(fill_min_volume_30d_usd):
            raise VenueConfigError(
                f"fill_min_volume_30d_usd must be finite (got {fill_min_volume_30d_usd})"
            )
        if fill_min_volume_30d_usd < 0:
            raise VenueConfigError(
                f"fill_min_volume_30d_usd must be >= 0 (got {fill_min_volume_30d_usd})"
            )

    effective_volume = fill_min_volume_30d_usd
    if effective_volume is None:
        effective_volume = config.current_min_volume_30d_usd
    # When neither is set, resolve_venue_tier against volume == 0
    # returns the lowest tier (the conservative default).
    if effective_volume is None:
        effective_volume = 0.0

    tier = resolve_venue_tier(effective_volume, config.tiers)
    fee_rate = tier.maker_fee_rate if intent == "maker" else tier.taker_fee_rate
    fee_amount = quantity * fill_price * fee_rate
    return fee_rate, fee_amount


# ---------------------------------------------------------------------------
# Per-fill aggregation → cost line items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueCostLine:
    """A single per-fill venue fee line item.

    Attributes
    ----------
    time : datetime
        Fill time (UTC, timezone-aware).
    fill_price : float
        Price at which the order filled.
    intent : {'maker', 'taker'}
        Whether the fill was a maker or taker.
    quantity : float
        Absolute quantity filled.
    fee_rate : float
        Fee rate applied (e.g. ``0.0005`` for 0.05% taker).
    fee_amount : float
        Dollar fee applied (``quantity * fill_price * fee_rate``).
    bar_index : int
        Index of the bar in the supplied bar list at which this
        fill lands (first bar whose time is >= the fill time).
        ``-1`` if the fill is past the bar window.
    """

    time: datetime
    fill_price: float
    intent: Literal["maker", "taker"]
    quantity: float
    fee_rate: float
    fee_amount: float
    bar_index: int

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            raise VenueConfigError(
                f"VenueCostLine.time must be timezone-aware (got naive {self.time!r})"
            )
        if self.time.tzinfo != timezone.utc:
            object.__setattr__(self, "time", self.time.astimezone(timezone.utc))
        if self.fill_price <= 0:
            raise VenueConfigError(
                f"fill_price must be positive (got {self.fill_price})"
            )
        if self.quantity <= 0:
            raise VenueConfigError(
                f"quantity must be positive (got {self.quantity})"
            )
        if self.intent not in ("maker", "taker"):
            raise VenueConfigError(
                f"intent must be 'maker' or 'taker' (got {self.intent!r})"
            )
        _validate_fee_rate(self.fee_rate, "fee_rate")
        if self.fee_amount < 0:
            raise VenueConfigError(
                f"fee_amount must be >= 0 (got {self.fee_amount})"
            )


def compute_venue_cost_lines(
    fills: Sequence[VenueOrderFill],
    config: VenueFeeConfig,
    bars: Optional[Sequence[Bar]] = None,
) -> list[VenueCostLine]:
    """Aggregate per-fill venue cost line items.

    Walks ``fills`` in order and produces a :class:`VenueCostLine`
    for each fill. The sum of ``fee_amount`` is the total venue cost
    applied to the backtest period.

    Parameters
    ----------
    fills : sequence[VenueOrderFill]
        Fill events. Order is preserved in the output (the caller
        may sort before passing if they want chronological output —
        the engine uses ascending time for bar alignment, but cost
        line items keep input order for traceability).
    config : VenueFeeConfig
        Venue fee config.
    bars : sequence[Bar], optional
        When supplied, each line is annotated with the bar index
        where the fill lands (first bar whose time is >= fill time).
        ``-1`` for fills past the bar window (excluded from
        ``venue_equity_delta``).

    Returns
    -------
    list[VenueCostLine]
        Per-fill line items in input order.
    """
    if not isinstance(config, VenueFeeConfig):
        raise TypeError(
            f"config must be VenueFeeConfig (got {type(config).__name__})"
        )
    out: list[VenueCostLine] = []
    for fill in fills:
        fee_rate, fee_amount = compute_fill_fee(
            fill_price=fill.fill_price,
            quantity=fill.quantity,
            intent=fill.intent,
            config=config,
            fill_min_volume_30d_usd=fill.fill_min_volume_30d_usd,
        )
        bar_idx = -1
        if bars is not None and len(bars) > 0:
            bar_idx = _find_bar_index(bars, fill.time)
            if bar_idx >= len(bars):
                bar_idx = -1
        out.append(
            VenueCostLine(
                time=fill.time,
                fill_price=fill.fill_price,
                intent=fill.intent,
                quantity=fill.quantity,
                fee_rate=fee_rate,
                fee_amount=fee_amount,
                bar_index=bar_idx,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Config loading — fail-loud YAML-style mapping parser
# ---------------------------------------------------------------------------


def _require_keys(mapping: Mapping[str, Any], required: Sequence[str]) -> None:
    """Raise VenueConfigError listing every missing required key."""
    missing = [k for k in required if k not in mapping]
    if missing:
        keys_str = ", ".join(repr(k) for k in missing)
        raise VenueConfigError(
            f"venue fee config is missing required key(s): {keys_str}"
        )


def _coerce_float(value: Any, label: str) -> float:
    """Coerce a config value to float; fail loud on non-numeric input."""
    if isinstance(value, bool):
        # bool is a subclass of int — guard against True/False silently
        # passing as numeric.
        raise VenueConfigError(
            f"{label} must be numeric (got bool {value})"
        )
    if not isinstance(value, (int, float)):
        raise VenueConfigError(
            f"{label} must be numeric (got {type(value).__name__}: {value!r})"
        )
    result = float(value)
    if not math.isfinite(result):
        raise VenueConfigError(f"{label} must be finite (got {value!r})")
    return result


def _coerce_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise VenueConfigError(
            f"{label} must be a string (got {type(value).__name__}: {value!r})"
        )
    if not value.strip():
        raise VenueConfigError(f"{label} must be a non-empty string (got {value!r})")
    return value


def load_venue_fee_config(mapping: Mapping[str, Any]) -> VenueFeeConfig:
    """Parse a venue fee config mapping into a :class:`VenueFeeConfig`.

    The mapping is the in-memory representation of a YAML block
    (caller does ``yaml.safe_load``); this function enforces the
    schema:

      * Required top-level keys: ``venue_name`` (or ``name``) and
        ``tiers``.
      * ``tiers`` must be a non-empty sequence of mappings, each
        with ``min_volume_30d_usd``, ``max_volume_30d_usd``,
        ``maker_fee_rate``, ``taker_fee_rate``.
      * All numeric fields must be numeric (not bool, not string).
      * All fee rates must be in ``[0, 1)``.
      * Tier brackets must be contiguous and ascending.

    Any validation failure raises :class:`VenueConfigError` (a
    :class:`ValueError` subclass) with a specific message naming the
    failing field.

    Parameters
    ----------
    mapping : Mapping[str, Any]
        The config mapping (typically produced by ``yaml.safe_load``).

    Returns
    -------
    VenueFeeConfig
        Validated config object.

    Raises
    ------
    VenueConfigError
        On any structural / type / value violation. The message is
        designed to identify the failing field so the operator can fix
        the YAML directly.
    """
    if not isinstance(mapping, Mapping):
        raise VenueConfigError(
            f"venue fee config must be a mapping (got {type(mapping).__name__})"
        )

    # venue_name / name are accepted aliases — pin a YAML with EITHER
    # but normalize on venue_name.
    _require_keys(mapping, ["tiers"])
    if "venue_name" in mapping:
        venue_name = _coerce_str(mapping["venue_name"], "venue_name")
    elif "name" in mapping:
        venue_name = _coerce_str(mapping["name"], "name")
    else:
        raise VenueConfigError(
            "venue fee config is missing required key(s): 'venue_name' or 'name'"
        )

    tiers_raw = mapping["tiers"]
    if not isinstance(tiers_raw, (list, tuple)):
        raise VenueConfigError(
            f"tiers must be a list/tuple (got {type(tiers_raw).__name__})"
        )
    if not tiers_raw:
        raise VenueConfigError("tiers must be non-empty")

    parsed_tiers: list[VenueTier] = []
    for i, tier_raw in enumerate(tiers_raw):
        if not isinstance(tier_raw, Mapping):
            raise VenueConfigError(
                f"tiers[{i}] must be a mapping (got {type(tier_raw).__name__})"
            )
        _require_keys(
            tier_raw,
            [
                "min_volume_30d_usd",
                "max_volume_30d_usd",
                "maker_fee_rate",
                "taker_fee_rate",
            ],
        )
        # 'inf' / 'Infinity' as strings — accept these for the top tier.
        max_vol_raw = tier_raw["max_volume_30d_usd"]
        if isinstance(max_vol_raw, str) and max_vol_raw.strip().lower() in (
            "inf",
            "infinity",
        ):
            max_vol = math.inf
        else:
            max_vol = _coerce_float(max_vol_raw, f"tiers[{i}].max_volume_30d_usd")
        tier = VenueTier(
            min_volume_30d_usd=_coerce_float(
                tier_raw["min_volume_30d_usd"], f"tiers[{i}].min_volume_30d_usd"
            ),
            max_volume_30d_usd=max_vol,
            maker_fee_rate=_coerce_float(
                tier_raw["maker_fee_rate"], f"tiers[{i}].maker_fee_rate"
            ),
            taker_fee_rate=_coerce_float(
                tier_raw["taker_fee_rate"], f"tiers[{i}].taker_fee_rate"
            ),
        )
        parsed_tiers.append(tier)

    current_vol: Optional[float] = None
    if "current_min_volume_30d_usd" in mapping:
        current_vol = _coerce_float(
            mapping["current_min_volume_30d_usd"], "current_min_volume_30d_usd"
        )

    schema_version = 1
    if "schema_version" in mapping:
        schema_raw = mapping["schema_version"]
        if isinstance(schema_raw, bool) or not isinstance(schema_raw, int):
            raise VenueConfigError(
                f"schema_version must be an int (got "
                f"{type(schema_raw).__name__}: {schema_raw!r})"
            )
        schema_version = schema_raw

    return VenueFeeConfig(
        venue_name=venue_name,
        tiers=tuple(parsed_tiers),
        current_min_volume_30d_usd=current_vol,
        schema_version=schema_version,
    )


# ---------------------------------------------------------------------------
# Backtest integration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueCostBacktestMetrics:
    """BacktestMetrics + venue cost overlay (R1/R2 deliverable shape).

    Adds four fields to :class:`backtest.types.BacktestMetrics`
    without mutating the original dataclass.

    Attributes
    ----------
    base : BacktestMetrics
        The original engine output (unchanged).
    cost_lines : tuple[VenueCostLine, ...]
        Per-fill venue cost line items (in input order).
    total_venue_cost : float
        Sum of ``fee_amount`` across ``cost_lines`` (always >= 0;
        fees are a reduction, never an inflow).
    venue_equity_delta : list[float]
        Per-bar venue cost deltas, length ``len(bars)``. ``[i]`` is
        the cumulative fee charged at bar ``i`` (only nonzero at
        bars where a fill lands). Downstream consumers subtract this
        from their own per-bar equity curve.
    venue_ending_balance : float
        ``base.ending_balance - total_venue_cost``.

    The shape is intentionally parallel to
    :class:`backtest.funding_model.FundedBacktestMetrics` and
    :class:`backtest.liquidation.LiquidatedBacktestMetrics` so a
    caller that composites all three overlays can sum their
    ``*_equity_delta`` series and their ``total_*_cost`` / net_pnl
    contributions against the same engine ``base.ending_balance``.
    """

    base: object  # BacktestMetrics — typed as object to avoid hard import
    cost_lines: tuple[VenueCostLine, ...]
    total_venue_cost: float
    venue_equity_delta: list[float]
    venue_ending_balance: float


def _find_bar_index(bars: Sequence[Bar], event_time: datetime) -> int:
    """Find the first bar whose time is >= event_time.

    Returns ``len(bars)`` if no such bar exists (event is past the
    backtest window — fill is excluded from ``venue_equity_delta``).
    """
    for i, bar in enumerate(bars):
        bt = bar.time
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=timezone.utc)
        else:
            bt = bt.astimezone(timezone.utc)
        if bt >= event_time:
            return i
    return len(bars)


def run_backtest_with_venue_costs(
    bars: Sequence[Bar],
    fills: Sequence[VenueOrderFill],
    venue_config: VenueFeeConfig,
    config: object,
    strategies: Sequence[object],
    strategy_name: Optional[str] = None,
) -> VenueCostBacktestMetrics:
    """Run :class:`BacktestEngine` and overlay venue/tier-aware fees.

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series. Must be sorted ascending by time.
    fills : sequence[VenueOrderFill]
        Per-fill events with maker/taker intent. Order is preserved
        in the output (``cost_lines``); fills past the bar window
        are excluded from ``venue_equity_delta``.
    venue_config : VenueFeeConfig
        Venue fee config (tiers + current 30d volume).
    config : BacktestConfig
        Backtest config forwarded to ``BacktestEngine``.
    strategies : sequence
        Strategies passed to ``BacktestEngine``. Must be non-empty.
    strategy_name : str, optional
        Which strategy's metrics to wrap. Defaults to the first
        strategy in the list.

    Returns
    -------
    VenueCostBacktestMetrics
        Base + venue cost overlay.

    Notes
    -----
    The engine path is identical to the funding and liquidation
    wrappers: the engine runs unchanged, the wrapper only computes
    the fee overlay. The legacy FX path
    (``BacktestEngine.run_single``) is untouched.
    """
    if not isinstance(venue_config, VenueFeeConfig):
        raise TypeError(
            f"venue_config must be VenueFeeConfig (got "
            f"{type(venue_config).__name__})"
        )
    if not bars:
        raise ValueError("bars must be non-empty")
    if not strategies:
        raise ValueError("strategies must be non-empty")

    # Lazy import: same pattern as funding_model / liquidation.
    from engine.engine import BacktestEngine

    name = (
        strategy_name
        if strategy_name is not None
        else getattr(strategies[0], "name", None)
    )
    engine = BacktestEngine(config, list(strategies))
    if name is not None:
        for s in strategies:
            if getattr(s, "name", None) == name:
                base_metrics = engine.run_single(s, list(bars))
                break
        else:
            base_metrics = engine.run_single(strategies[0], list(bars))
    else:
        base_metrics = engine.run_single(strategies[0], list(bars))

    lines = compute_venue_cost_lines(fills, venue_config, bars=bars)

    # Build per-bar venue cost delta series. Length = len(bars).
    # Each entry is the sum of fee amounts charged at that bar.
    deltas = [0.0] * len(bars)
    for line in lines:
        if 0 <= line.bar_index < len(bars):
            deltas[line.bar_index] += line.fee_amount

    total_venue_cost = sum(line.fee_amount for line in lines)

    return VenueCostBacktestMetrics(
        base=base_metrics,
        cost_lines=tuple(lines),
        total_venue_cost=total_venue_cost,
        venue_equity_delta=deltas,
        venue_ending_balance=base_metrics.ending_balance - total_venue_cost,
    )


# ---------------------------------------------------------------------------
# 3-way composition: venue + funding + liquidation
# ---------------------------------------------------------------------------


def run_backtest_with_full_crypto_overlay(
    bars: Sequence[Bar],
    mark_prices: Sequence[float],
    funding_events: Sequence[object],
    fills: Sequence[VenueOrderFill],
    venue_config: VenueFeeConfig,
    position: PositionSpec,
    liq_spec: LiquidationSpec,
    config: object,
    strategies: Sequence[object],
    strategy_name: Optional[str] = None,
) -> tuple[object, object, VenueCostBacktestMetrics]:
    """Run venue + funding + liquidation overlays in one call.

    Composition strategy:
      1. Run :func:`run_backtest_with_venue_costs` against the
         engine — produces :class:`VenueCostBacktestMetrics`.
      2. Run :func:`run_backtest_with_funding` against the engine
         — produces :class:`FundedBacktestMetrics`.
      3. Run :func:`run_backtest_with_liquidation` against the
         engine — produces :class:`LiquidatedBacktestMetrics`.
      4. Return all three.

    All three wrappers share the same engine run shape (same bars,
    same position, same strategies). They produce independent
    per-bar delta series; the caller sums them into a single equity
    curve.

    Parameters
    ----------
    bars, mark_prices, funding_events, position, liq_spec, config,
    strategies, strategy_name
        Forwarded to :func:`run_backtest_with_venue_costs`,
        :func:`run_backtest_with_funding`, and
        :func:`run_backtest_with_liquidation`.
    fills, venue_config
        Forwarded to :func:`run_backtest_with_venue_costs`.

    Returns
    -------
    tuple[FundedBacktestMetrics, LiquidatedBacktestMetrics,
          VenueCostBacktestMetrics]
        All three overlays. The caller sums their ``*_equity_delta``
        series for a single equity curve and folds their net
        contributions against ``base.ending_balance``.

    Notes
    -----
    Each wrapper runs the engine independently. This means the
    engine runs three times — once per overlay. The engine is
    deterministic on (config, bars, strategies), so the
    ``base.ending_balance`` is identical across the three runs and
    the overlays are additive on top. If the engine becomes expensive
    to run three times, a shared-engine wrapper can be added later
    without changing the public shape.
    """
    venue_metrics = run_backtest_with_venue_costs(
        bars=bars,
        fills=fills,
        venue_config=venue_config,
        config=config,
        strategies=strategies,
        strategy_name=strategy_name,
    )
    funded = run_backtest_with_funding(
        bars=bars,
        funding_events=funding_events,
        position=position,
        config=config,
        strategies=strategies,
        strategy_name=strategy_name,
    )
    liquidated = run_backtest_with_liquidation(
        bars=bars,
        mark_prices=mark_prices,
        position=position,
        liq_spec=liq_spec,
        config=config,
        strategies=strategies,
        strategy_name=strategy_name,
    )
    return funded, liquidated, venue_metrics
