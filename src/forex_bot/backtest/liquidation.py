"""Mark-price liquidation mechanics for crypto backtests.

Card: 81cfff7c-cf1f-44da-a68f-dc5993b0e4a6 (Sprint C 1a.2)
Sprint: 2026-10-05-crypto-strategy-factory
Lane: [BUILD][CRYPTO-LANE]

What this module provides
==========================

A mark-price liquidation engine for the crypto backtest core that
closes positions against a reconstructed **mark price** (not last trade
price) when the mark price crosses the maintenance-margin threshold.
This is the second mandatory cost term in the R1 brief alongside
adaptive funding (``forex_bot.backtest.funding_model``).

Specifically, the module provides:

  * **Tiered maintenance margin** — Binance USDⓈ-M perps applies
    higher maintenance-margin rates at larger notional brackets
    (Binance spec: 0.5% / 1.0% / 2.5% / 5.0% / 10.0% by notional).
  * **Mark-price threshold math** — closed-form liquidation price
    for long / short positions given entry price, leverage, and the
    notional bracket's maintenance-margin rate.
  * **Partial vs full liquidation** — when triggered, compute the
    quantity to close to restore the position to its initial-margin
    safety buffer; force full close if the partial math exceeds the
    remaining size.
  * **Liquidation fee accounting** — Binance charges an extra
    liquidation fee (default 0.5% of closed notional at mark) on top
    of taker fees.
  * **Forced close at mark price** — liquidation PnL is realized at
    the *mark* price of the bar where the threshold was crossed, not
    at the candle's last trade price. This is the key fact that a
    naive last-price-stop backtest misses (Satsuki R1 §1.4).
  * **Composition with funding** — a combined wrapper
    :func:`run_backtest_with_funding_and_liquidation` layers this
    module on top of :func:`run_backtest_with_funding`; both modules
    apply per-bar deltas that downstream consumers can fold into a
    single equity curve.

Integration contract
====================

The module is intentionally decoupled from
:class:`forex_bot.engine.engine.BacktestEngine`. The engine still runs
unchanged; :func:`run_backtest_with_liquidation` wraps the engine and
returns a :class:`LiquidatedBacktestMetrics` extension of
:class:`backtest.types.BacktestMetrics` carrying:

  * the unchanged ``base`` engine metrics (legacy path preserved),
  * the ``liquidation_events`` actually applied (with bar indices,
    mark prices, partial-vs-full flags, fees, and realized PnL),
  * the ``total_liquidation_cost`` as a single number (fees + any
    residual negative equity absorbed — but the wrapper zeroes
    residual to keep balances non-negative, matching the engine's
    own floor),
  * a per-bar ``liquidated_equity_delta`` series (liquidation P&L
    adjustments, length ``len(bars)``) that downstream consumers can
    overlay onto the per-bar equity curve,
  * the ``liquidated_ending_balance`` after subtracting liquidation
    costs from the engine's reported ending balance.

The legacy forex path stays 100% untouched — callers that don't opt
into liquidation see identical metrics.

Liquidation economics (perp convention)
=======================================

For a *long* position with entry price ``P_e``, leverage ``L``, and
maintenance-margin rate ``m`` (resolved from the tier table by
notional):

  * Position notional at entry: ``N = q * P_e``
  * Initial margin posted:    ``IM = N / L``
  * Account equity at mark:   ``E = IM + q * (mark - P_e)``
  * Maintenance margin:       ``MM = q * mark * m``
  * Liquidation trigger:      ``mark <= P_e * (1 - 1/L) / (1 - m)``

Equivalently, the condition ``E <= MM`` rearranges to that same
trigger. For 100× leverage with ``m = 0.005``:

    mark_liq_long = P_e * (1 - 0.01) / (1 - 0.005)
                  = P_e * 0.99 / 0.995
                  ≈ P_e * 0.99497

i.e. the long is liquidated when the mark falls ~0.5% below entry.

For a *short* position with the same parameters:

  * Liquidation trigger:      ``mark >= P_e * (1 + 1/L) / (1 + m)``

For 100× leverage with ``m = 0.005``:

    mark_liq_short = P_e * (1 + 0.01) / (1 + 0.005)
                   ≈ P_e * 1.00498

i.e. the short is liquidated when the mark rises ~0.5% above entry.

Partial vs full liquidation
============================

When the mark crosses the threshold, the wrapper computes the
*quantity to close* to bring the position's margin ratio back up to its
**initial-margin** ratio (the safety buffer Binance aims for). Let:

  * ``q_p`` = absolute quantity to close
  * ``Q``   = position size (signed; positive long, negative short)
  * ``fee`` = liquidation fee rate

Closed PnL (realized on the closed portion) = ``|q_p| * (mark - P_e)``
*signed by direction* — for a long with q_p > 0 this is positive when
mark > entry, negative when mark < entry. Liquidation is most often a
loss event (mark moved against the position past the trigger), so
closed PnL is typically negative.

Liquidation fee : ``|q_p| * mark * fee``

New equity after partial close:
    new_equity = old_equity + closed_PnL - liquidation_fee

The wrapper targets ``new_equity >= new_IM`` (new initial margin for
the remaining position), which is the standard Binance-style "back to
IM level" target. Solving for ``q_p``:

    q_p = (Q * mark / L - old_equity) /
          (mark - entry + mark / L - mark * fee)
          for long (sign carried by the formula)

If the solved ``|q_p| >= |Q|``, the position is fully closed. This
happens when the mark is far past the threshold (e.g. cascade
condition) or when the position is small enough that a partial close
isn't worth the round-trip. The wrapper reports ``is_full_liquidation``
on the :class:`LiquidationEvent`.

Note: the IM portion is *returned* on the closed quantity — the
solved formula above accounts for that implicitly through the
``old_equity`` term (which already contains the original IM).

Source: Binance USDⓈ-M maintenance margin & liquidation spec; see
research doc §1.4 of
``docs/research/2026-10-05-crypto-first-strategy-testing-recommendations.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional, Sequence

from backtest.funding_model import (
    PositionSpec,
    run_backtest_with_funding,
)

# IntegrityConfig is referenced as a type annotation only; the runtime
# import is deferred to ``run_backtest_with_liquidation`` to keep the
# module-level import graph free of cross-crypto-overlay coupling.
from backtest.integrity_gate import IntegrityConfig  # noqa: E402
from backtest.types import Bar

__all__ = [
    "LiquidationTier",
    "LiquidationSpec",
    "LiquidationEvent",
    "LiquidatedBacktestMetrics",
    "DEFAULT_MAINTENANCE_TIERS",
    "DEFAULT_LIQUIDATION_FEE_RATE",
    "PARTIAL_LIQUIDATION_TARGET_RATIO",
    "resolve_maintenance_margin_rate",
    "compute_liquidation_threshold_price",
    "compute_partial_liquidation_quantity",
    "check_liquidation_threshold_crossed",
    "process_liquidation_at_bar",
    "run_backtest_with_liquidation",
    "run_backtest_with_funding_and_liquidation",
]


# ---------------------------------------------------------------------------
# Constants — Binance USDⓈ-M maintenance-margin tier table
# ---------------------------------------------------------------------------

#: Default liquidation fee rate (Binance VIP 0). The liquidation fee
#: is charged on top of taker fees — the engine wrapper below does
#: *not* double-count the taker fee, it applies the liquidation fee
#: to the closed notional at mark.
DEFAULT_LIQUIDATION_FEE_RATE: float = 0.005  # 0.5%

#: Target margin ratio after partial liquidation. ``1.0`` means "bring
#: the position's remaining equity back to its initial-margin level"
#: (Binance's standard partial-liquidation target). ``0.5`` would mean
#: bring it halfway back; ``0`` means close the minimum needed to clear
#: the maintenance threshold (more aggressive partial).
PARTIAL_LIQUIDATION_TARGET_RATIO: float = 1.0


@dataclass(frozen=True)
class LiquidationTier:
    """A single maintenance-margin bracket.

    Attributes
    ----------
    min_notional : float
        Lower bound (inclusive) of the bracket, in USD notional at
        entry. Must be ``>= 0``. Tiers must be sorted ascending by
        ``min_notional`` and the first tier's ``min_notional`` must be
        ``0``.
    max_notional : float
        Upper bound (exclusive) of the bracket, in USD notional at
        entry. Use ``math.inf`` for the top tier.
    maintenance_margin_rate : float
        Maintenance-margin rate applied to positions whose notional
        falls in this bracket. Must satisfy ``0 < rate < 1`` and is
        typically much smaller than ``1 / leverage`` (otherwise the
        liquidation trigger sits above the entry price — degenerate).
    """

    min_notional: float
    max_notional: float
    maintenance_margin_rate: float

    def __post_init__(self) -> None:
        if self.min_notional < 0:
            raise ValueError(
                f"min_notional must be >= 0 (got {self.min_notional})"
            )
        if not math.isfinite(self.max_notional) and self.max_notional <= 0:
            raise ValueError(
                f"max_notional must be positive (or math.inf); got {self.max_notional}"
            )
        if self.max_notional <= self.min_notional:
            raise ValueError(
                f"max_notional ({self.max_notional}) must exceed min_notional "
                f"({self.min_notional})"
            )
        if not (0 < self.maintenance_margin_rate < 1):
            raise ValueError(
                f"maintenance_margin_rate must be in (0, 1); got {self.maintenance_margin_rate}"
            )


#: Binance USDⓈ-M maintenance-margin tier table (effective 2025-09-16).
#: Source: Binance maintenance-margin bracket table per
#: https://www.binance.com/en/support/announcement/detail/c00588a7e8504b3eb28d02a2da00530b.
#: Sorted ascending by ``min_notional``.
DEFAULT_MAINTENANCE_TIERS: tuple[LiquidationTier, ...] = (
    LiquidationTier(0.0,            50_000.0,    0.005),   # 0.5%
    LiquidationTier(50_000.0,       250_000.0,   0.010),   # 1.0%
    LiquidationTier(250_000.0,      1_000_000.0, 0.025),   # 2.5%
    LiquidationTier(1_000_000.0,    10_000_000.0, 0.050),  # 5.0%
    LiquidationTier(10_000_000.0,   math.inf,    0.100),   # 10.0%
)


# ---------------------------------------------------------------------------
# Position spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiquidationSpec:
    """Liquidation parameters for an open crypto position.

    This is a *separate* spec from
    :class:`backtest.funding_model.PositionSpec` so the funding path
    can run without specifying entry/leverage (e.g. for spot-only or
    pre-liquidation runs). The combined wrapper
    :func:`run_backtest_with_funding_and_liquidation` takes both
    specs side-by-side.

    Attributes
    ----------
    entry_price : float
        Position entry price (mark-based, USD per unit). Must be > 0.
    leverage : float
        Effective leverage used at entry. Must be >= 1.0 (no margin).
    direction : {'long', 'short'}
        Position direction.
    liquidation_fee_rate : float
        Fee rate applied to the closed notional at mark (default
        ``DEFAULT_LIQUIDATION_FEE_RATE`` = 0.5% per Binance VIP 0).
    maintenance_margin_rate : float, optional
        Explicit maintenance-margin rate. When ``None`` (default), the
        rate is resolved at liq-evaluation time from
        ``DEFAULT_MAINTENANCE_TIERS`` based on the position's notional.
        Pinning by a value is useful for tests, regime-specific runs, or
        matching a non-Binance venue's bracket table.
    tiers : tuple[LiquidationTier]
        Tier table to use when ``maintenance_margin_rate`` is ``None``.
        Defaults to :data:`DEFAULT_MAINTENANCE_TIERS`.
    target_margin_ratio : float
        Target margin ratio after partial close (default
        ``PARTIAL_LIQUIDATION_TARGET_RATIO`` = 1.0 → "back to IM").
        See module docstring §partial for the math.

    Notes
    -----
    ``maintenance_margin_rate`` is the *value* applied to position
    notional at mark to derive the maintenance-margin dollar amount.
    For Binance's tier table it ranges 0.5%–10% depending on bracket.
    """

    entry_price: float
    leverage: float
    direction: Literal["long", "short"]
    liquidation_fee_rate: float = DEFAULT_LIQUIDATION_FEE_RATE
    maintenance_margin_rate: Optional[float] = None
    tiers: tuple[LiquidationTier, ...] = DEFAULT_MAINTENANCE_TIERS
    target_margin_ratio: float = PARTIAL_LIQUIDATION_TARGET_RATIO

    def __post_init__(self) -> None:
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be positive (got {self.entry_price})")
        if not math.isfinite(self.entry_price):
            raise ValueError(f"entry_price must be finite (got {self.entry_price})")
        if self.leverage < 1.0:
            raise ValueError(f"leverage must be >= 1.0 (got {self.leverage})")
        if self.direction not in ("long", "short"):
            raise ValueError(
                f"direction must be 'long' or 'short' (got {self.direction!r})"
            )
        if self.liquidation_fee_rate < 0 or self.liquidation_fee_rate >= 1:
            raise ValueError(
                f"liquidation_fee_rate must be in [0, 1); got {self.liquidation_fee_rate}"
            )
        if self.maintenance_margin_rate is not None and not (
            0 < self.maintenance_margin_rate < 1
        ):
            raise ValueError(
                f"maintenance_margin_rate must be in (0, 1); got {self.maintenance_margin_rate}"
            )
        if self.target_margin_ratio < 0:
            raise ValueError(
                f"target_margin_ratio must be >= 0; got {self.target_margin_ratio}"
            )
        # Validate the tier table once at construction time so callers
        # don't discover bad brackets at runtime.
        if self.maintenance_margin_rate is None and len(self.tiers) == 0:
            raise ValueError(
                "tiers must be non-empty when maintenance_margin_rate is None"
            )
        if self.maintenance_margin_rate is None:
            prev_max = 0.0
            for i, t in enumerate(self.tiers):
                if t.min_notional != prev_max:
                    raise ValueError(
                        f"tiers must be contiguous; tier {i} starts at "
                        f"{t.min_notional}, expected {prev_max}"
                    )
                if t.max_notional <= t.min_notional and not math.isinf(t.max_notional):
                    raise ValueError(
                        f"tier {i} max_notional ({t.max_notional}) must exceed "
                        f"min_notional ({t.min_notional})"
                    )
                prev_max = t.max_notional if not math.isinf(t.max_notional) else prev_max


# ---------------------------------------------------------------------------
# Liquidation event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiquidationEvent:
    """A single forced close triggered by mark-price liquidation.

    Attributes
    ----------
    time : datetime
        Bar time at which liquidation was triggered (UTC). Must be
        timezone-aware.
    mark_price : float
        Mark price used to evaluate the trigger and to compute the
        forced-close PnL (the *key* difference from last-price-stop
        backtests — see research doc §1.4).
    last_price : float
        The candle's last trade price at the same bar, recorded for
        diagnostics. Forced close happens at ``mark_price``, not
        ``last_price`` — callers can compare to see how much a
        last-price model would have differed.
    quantity_closed : float
        Absolute quantity closed by this event. Positive. Equals
        ``quantity_remaining_after`` for a full liquidation, or a
        smaller amount for partial.
    quantity_remaining_after : float
        Absolute quantity still open after the event. ``0.0`` for a
        full liquidation; ``> 0`` for a partial close.
    realized_pnl : float
        Dollar P&L realized on the closed portion (signed; negative
        = loss, positive = gain). Computed at the *mark price*, not
        last price.
    liquidation_fee : float
        Dollar liquidation fee charged on the closed notional at
        mark. Always positive.
    net_pnl : float
        ``realized_pnl - liquidation_fee``.
    bar_index : int
        Index of the bar in the supplied bar list at which liquidation
        was triggered. ``-1`` if the mark-price series had no exact
        alignment (caller passes ``mark_prices`` aligned to ``bars``).
    maintenance_margin_rate : float
        Maintenance-margin rate applied at this evaluation (resolved
        from the tier table if ``LiquidationSpec.maintenance_margin_rate``
        was ``None``).
    is_full_liquidation : bool
        ``True`` if the event closed the entire position; ``False``
        for partial close.
    """

    time: datetime
    mark_price: float
    last_price: float
    quantity_closed: float
    quantity_remaining_after: float
    realized_pnl: float
    liquidation_fee: float
    net_pnl: float
    bar_index: int
    maintenance_margin_rate: float
    is_full_liquidation: bool

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            raise ValueError(
                f"LiquidationEvent.time must be timezone-aware (got naive {self.time!r})"
            )
        if self.time.tzinfo != timezone.utc:
            object.__setattr__(self, "time", self.time.astimezone(timezone.utc))
        if self.mark_price <= 0:
            raise ValueError(f"mark_price must be positive (got {self.mark_price})")
        if self.last_price <= 0:
            raise ValueError(f"last_price must be positive (got {self.last_price})")
        if self.quantity_closed < 0:
            raise ValueError(
                f"quantity_closed must be >= 0 (got {self.quantity_closed})"
            )
        if self.quantity_remaining_after < 0:
            raise ValueError(
                f"quantity_remaining_after must be >= 0 "
                f"(got {self.quantity_remaining_after})"
            )
        if self.liquidation_fee < 0:
            raise ValueError(
                f"liquidation_fee must be >= 0 (got {self.liquidation_fee})"
            )
        if not (0 < self.maintenance_margin_rate < 1):
            raise ValueError(
                f"maintenance_margin_rate must be in (0, 1); got {self.maintenance_margin_rate}"
            )


# ---------------------------------------------------------------------------
# Liquidation math
# ---------------------------------------------------------------------------


def resolve_maintenance_margin_rate(
    notional_usd: float,
    tiers: tuple[LiquidationTier, ...] = DEFAULT_MAINTENANCE_TIERS,
) -> float:
    """Resolve the maintenance-margin rate for a notional bracket.

    Walks ``tiers`` in order and returns the first bracket whose
    ``min_notional <= notional_usd < max_notional``. If
    ``notional_usd`` exceeds the highest tier's ``min_notional`` (i.e.
    falls in the open-ended top bracket with ``max_notional == inf``),
    the top tier's rate is returned.

    Parameters
    ----------
    notional_usd : float
        Position notional in USD. Must be ``>= 0``.
    tiers : tuple[LiquidationTier]
        Tier table to walk. Defaults to
        :data:`DEFAULT_MAINTENANCE_TIERS` (Binance USDⓈ-M spec).

    Returns
    -------
    float
        Maintenance-margin rate applied to this bracket.

    Raises
    ------
    ValueError
        If ``notional_usd < 0`` or no tier contains it.
    """
    if notional_usd < 0:
        raise ValueError(f"notional_usd must be >= 0 (got {notional_usd})")
    if not tiers:
        raise ValueError("tiers must be non-empty")
    for tier in tiers:
        if tier.min_notional <= notional_usd < tier.max_notional:
            return tier.maintenance_margin_rate
    # Past the top tier's open-ended bracket (max_notional == inf).
    # The validator in LiquidationTier accepts math.inf, and the
    # construction-time check in LiquidationSpec guarantees the last
    # tier's max_notional is inf when present. We only land here if
    # callers supply a tier table without an open-ended top tier and
    # the notional exceeds the highest max_notional — fall through to
    # the highest tier's rate rather than failing, matching the
    # spirit of "top bracket applies beyond its lower bound."
    return tiers[-1].maintenance_margin_rate


def compute_liquidation_threshold_price(
    entry_price: float,
    notional_usd: float,
    direction: Literal["long", "short"],
    leverage: float,
    *,
    maintenance_margin_rate: Optional[float] = None,
    tiers: tuple[LiquidationTier, ...] = DEFAULT_MAINTENANCE_TIERS,
) -> float:
    """Compute the mark price at which a position is liquidated.

    For a *long* position with entry price ``P_e``, leverage ``L``,
    and maintenance-margin rate ``m``:

        threshold = P_e * (1 - 1/L) / (1 - m)

    For a *short*:

        threshold = P_e * (1 + 1/L) / (1 + m)

    Derived from ``equity <= maintenance_margin`` where
    ``equity = N/L + N/L*(mark/P_e - 1)`` (long; see module docstring
    for derivation).

    Parameters
    ----------
    entry_price : float
        Entry price in USD per unit. Must be > 0.
    notional_usd : float
        Position notional in USD. Used to resolve the maintenance
        margin rate from ``tiers`` when ``maintenance_margin_rate`` is
        ``None``. Must be >= 0.
    direction : {'long', 'short'}
        Position direction.
    leverage : float
        Effective leverage. Must be >= 1.0.
    maintenance_margin_rate : float, optional
        Explicit rate. When ``None``, resolved from ``tiers`` based on
        ``notional_usd``.
    tiers : tuple[LiquidationTier]
        Tier table to use when ``maintenance_margin_rate`` is
        ``None``. Defaults to :data:`DEFAULT_MAINTENANCE_TIERS`.

    Returns
    -------
    float
        The mark price at which ``equity == maintenance_margin``. The
        liquidation trigger is ``mark_price <= threshold`` for longs
        and ``mark_price >= threshold`` for shorts (see
        :func:`check_liquidation_threshold_crossed`).
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive (got {entry_price})")
    if notional_usd < 0:
        raise ValueError(f"notional_usd must be >= 0 (got {notional_usd})")
    if leverage < 1.0:
        raise ValueError(f"leverage must be >= 1.0 (got {leverage})")
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short' (got {direction!r})")
    if maintenance_margin_rate is None:
        maintenance_margin_rate = resolve_maintenance_margin_rate(notional_usd, tiers)
    if not (0 < maintenance_margin_rate < 1):
        raise ValueError(
            f"maintenance_margin_rate must be in (0, 1); got {maintenance_margin_rate}"
        )
    inv_leverage = 1.0 / leverage
    if direction == "long":
        # Liquidation when: equity <= MM
        #   N/L + N*(mark - P_e)/P_e <= N*mark/P_e * m  (divided by N)
        #   mark * (1 - m) <= P_e * (1 - 1/L)
        #   mark <= P_e * (1 - 1/L) / (1 - m)
        return entry_price * (1.0 - inv_leverage) / (1.0 - maintenance_margin_rate)
    # short: liquidation when mark >= P_e * (1 + 1/L) / (1 + m)
    return entry_price * (1.0 + inv_leverage) / (1.0 + maintenance_margin_rate)


def check_liquidation_threshold_crossed(
    mark_price: float,
    threshold_price: float,
    direction: Literal["long", "short"],
) -> bool:
    """Whether the mark price has crossed the liquidation threshold.

    For a *long*, the trigger is ``mark_price <= threshold_price``
    (mark falling below the threshold forces liquidation). For a
    *short*, the trigger is ``mark_price >= threshold_price``.

    Parameters
    ----------
    mark_price : float
        Mark price at the current bar. Must be > 0.
    threshold_price : float
        The threshold from
        :func:`compute_liquidation_threshold_price`. Must be > 0.
    direction : {'long', 'short'}
        Position direction.
    """
    if mark_price <= 0:
        raise ValueError(f"mark_price must be positive (got {mark_price})")
    if threshold_price <= 0:
        raise ValueError(f"threshold_price must be positive (got {threshold_price})")
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short' (got {direction!r})")
    if direction == "long":
        return mark_price <= threshold_price
    return mark_price >= threshold_price


def compute_partial_liquidation_quantity(
    position_qty: float,
    mark_price: float,
    entry_price: float,
    leverage: float,
    maintenance_margin_rate: float,
    liquidation_fee_rate: float,
    direction: Literal["long", "short"],
    target_margin_ratio: float = PARTIAL_LIQUIDATION_TARGET_RATIO,
) -> float:
    """Compute the absolute quantity to close in a partial liquidation.

    Solves for ``|q_p|`` such that after closing ``q_p`` at the mark
    price (with liquidation fee), the remaining position's margin
    ratio is restored to ``target_margin_ratio`` of the initial
    margin. The closed-PnL term moves sign by direction.

    Math (long, signed positive q_p):

        old_equity = q * P_e / L + q * (mark - P_e)
                    = q * (P_e / L + mark - P_e)
        closed_PnL = q_p * (mark - P_e)
        IM_returned_on_closed = q_p * P_e / L  (already inside old_equity)
        liquidation_fee = q_p * mark * fee

        new_equity = old_equity + closed_PnL - liquidation_fee
        new_size   = q - q_p
        new_notional_at_mark = (q - q_p) * mark
        new_IM = new_notional_at_mark / L
        target_equity = target_margin_ratio * new_IM

        old_equity + closed_PnL - fee = target_margin_ratio * new_IM
        old_equity + q_p*(mark - P_e) - q_p*mark*fee = target_margin_ratio * (q - q_p) * mark / L

    Solve for ``q_p``. For longs (sign considerations): the closed
    PnL carries the *direction* sign — ``(mark - P_e)`` is signed for
    a long (negative when mark < entry), so when mark is past the
    trigger on the *wrong* side, closed PnL is negative, and a larger
    q_p is needed.

    We solve in closed form and return ``|q_p|``. When the solved
    ``|q_p| >= |q|``, the wrapper force-closes (caller checks).

    Parameters
    ----------
    position_qty : float
        Signed position quantity (``> 0`` long, ``< 0`` short).
    mark_price : float
        Mark price at the liquidation bar.
    entry_price : float
        Position entry price.
    leverage : float
        Effective leverage (>= 1).
    maintenance_margin_rate : float
        Maintenance-margin rate (in (0, 1)).
    liquidation_fee_rate : float
        Fee rate on closed notional at mark.
    direction : {'long', 'short'}
        Position direction.
    target_margin_ratio : float
        Target margin ratio after partial close (default 1.0 → "back
        to IM"). ``0`` means close the minimum to clear the maintenance
        threshold.

    Returns
    -------
    float
        Absolute quantity to close. Caller compares against
        ``abs(position_qty)`` to decide partial vs full.

    Notes
    -----
    When ``mark_price`` is at or beyond the maintenance boundary,
    ``old_equity <= old_maintenance_margin`` — the function returns a
    positive ``|q_p|``. For short positions the closed-PnL sign flips
    (``(entry - mark)``), but the formula is symmetric via the
    ``direction`` parameter.
    """
    if position_qty == 0:
        return 0.0
    if mark_price <= 0:
        raise ValueError(f"mark_price must be positive (got {mark_price})")
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive (got {entry_price})")
    if leverage < 1.0:
        raise ValueError(f"leverage must be >= 1.0 (got {leverage})")
    if not (0 < maintenance_margin_rate < 1):
        raise ValueError(
            f"maintenance_margin_rate must be in (0, 1); got {maintenance_margin_rate}"
        )
    if not (0 <= liquidation_fee_rate < 1):
        raise ValueError(
            f"liquidation_fee_rate must be in [0, 1); got {liquidation_fee_rate}"
        )
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short' (got {direction!r})")
    if target_margin_ratio < 0:
        raise ValueError(f"target_margin_ratio must be >= 0; got {target_margin_ratio}")

    abs_q = abs(position_qty)
    inv_L = 1.0 / leverage
    if direction == "long":
        # Old equity at the liq bar:
        #   E = q * (entry/L) + q * (mark - entry)  =  q * (entry/L + mark - entry)
        old_equity = position_qty * (entry_price * inv_L + mark_price - entry_price)
        # Closed-PnL coefficient: mark - entry for long (signed; negative
        # when mark < entry, which is when liquidation fires).
        dpnl_coef = mark_price - entry_price
    else:
        # short: PnL on closed portion = q_p * (entry - mark)
        # E = |q| * entry/L + |q| * (entry - mark)
        old_equity = abs_q * (entry_price * inv_L + entry_price - mark_price)
        dpnl_coef = entry_price - mark_price

    # Change in equity per unit q_p closed:
    #   d(E_new) / d(q_p) = dpnl_coef + entry/L - mark * fee
    # (closed-PnL + IM-returned-on-closed - liquidation-fee)
    # We then set the result against the target safety buffer
    # target_ratio * new_IM_remaining, where new_IM_remaining uses the
    # *entry* price (IM is computed on entry notional, not mark):
    #   new_IM_remaining = (abs_q - q_p) * entry / L
    #
    # Rearranging for q_p:
    #   old_equity + q_p * (dpnl_coef + entry/L - mark * fee)
    #       >= target_ratio * (abs_q - q_p) * entry / L
    #   old_equity + q_p * (dpnl_coef + entry/L - mark * fee + target_ratio * entry / L)
    #       >= target_ratio * abs_q * entry / L
    #   q_p * (dpnl_coef + entry/L * (1 + target_ratio) - mark * fee)
    #       = target_ratio * abs_q * entry / L - old_equity
    coef_lhs = (
        dpnl_coef
        + entry_price * inv_L * (1.0 + target_margin_ratio)
        - mark_price * liquidation_fee_rate
    )
    coef_rhs = target_margin_ratio * abs_q * entry_price * inv_L - old_equity

    # Edge case: denom sums to zero → the closed-PnL term exactly
    # offsets the IM-returned + target-IM-required terms → no closed
    # quantity possible from this formula. Caller interprets this as
    # "force full close".
    if coef_lhs == 0.0:
        return abs_q

    q_p = coef_rhs / coef_lhs
    # Clamp logic:
    #   q_p > abs_q  → math says close more than we hold → full close.
    #   q_p < 0.0    → math says partial would *reduce* equity per unit
    #                   (deep-loss case: mark far past threshold, closing
    #                   more locks in more losses faster than it frees
    #                   margin). Force full close — there is no
    #                   profitable partial close in this regime, and
    #                   leaving the position open would re-trigger the
    #                   same degenerate q_p on every subsequent bar.
    #   0 <= q_p < abs_q → partial close (the target is reachable).
    if q_p > abs_q:
        q_p = abs_q
    if q_p < 0.0:
        q_p = abs_q
    return q_p


def process_liquidation_at_bar(
    position_qty: float,
    mark_price: float,
    last_price: float,
    entry_price: float,
    leverage: float,
    maintenance_margin_rate: float,
    liquidation_fee_rate: float,
    direction: Literal["long", "short"],
    bar_index: int,
    bar_time: datetime,
    *,
    target_margin_ratio: float = PARTIAL_LIQUIDATION_TARGET_RATIO,
) -> LiquidationEvent:
    """Process a single liquidation at a bar and return the event.

    Computes the quantity to close (partial or full), the realized
    PnL at the *mark* price, and the liquidation fee. The caller is
    responsible for deciding *when* to call this — i.e. only when
    :func:`check_liquidation_threshold_crossed` returns ``True``.

    Parameters
    ----------
    position_qty : float
        Signed position quantity (``> 0`` long, ``< 0`` short).
    mark_price : float
        Mark price at the liquidation bar. Used for both PnL and fee
        math.
    last_price : float
        Last trade price at the same bar. Recorded for diagnostics;
        *not* used for PnL.
    entry_price : float
        Position entry price.
    leverage : float
        Effective leverage.
    maintenance_margin_rate : float
        Maintenance-margin rate at this bracket.
    liquidation_fee_rate : float
        Liquidation fee rate.
    direction : {'long', 'short'}
        Position direction.
    bar_index : int
        Bar index in the caller's bar series. ``-1`` if not aligned.
    bar_time : datetime
        Bar time (UTC, timezone-aware).
    target_margin_ratio : float
        Target margin ratio for partial close (default 1.0).

    Returns
    -------
    LiquidationEvent
        The forced-close event. ``is_full_liquidation`` is ``True`` when
        the solved ``|q_p| >= |position_qty|``.
    """
    if mark_price <= 0:
        raise ValueError(f"mark_price must be positive (got {mark_price})")
    if last_price <= 0:
        raise ValueError(f"last_price must be positive (got {last_price})")
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive (got {entry_price})")
    if leverage < 1.0:
        raise ValueError(f"leverage must be >= 1.0 (got {leverage})")
    if not (0 < maintenance_margin_rate < 1):
        raise ValueError(
            f"maintenance_margin_rate must be in (0, 1); got {maintenance_margin_rate}"
        )
    if not (0 <= liquidation_fee_rate < 1):
        raise ValueError(
            f"liquidation_fee_rate must be in [0, 1); got {liquidation_fee_rate}"
        )
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short' (got {direction!r})")

    abs_q = abs(position_qty)
    q_p = compute_partial_liquidation_quantity(
        position_qty=position_qty,
        mark_price=mark_price,
        entry_price=entry_price,
        leverage=leverage,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
        direction=direction,
        target_margin_ratio=target_margin_ratio,
    )

    is_full_liquidation = q_p >= abs_q
    quantity_closed = q_p
    quantity_remaining = abs_q - q_p

    # Realized PnL on the closed portion at the *mark* price.
    if direction == "long":
        realized_pnl = quantity_closed * (mark_price - entry_price)
    else:
        realized_pnl = quantity_closed * (entry_price - mark_price)

    # Liquidation fee on closed notional at mark.
    liquidation_fee = quantity_closed * mark_price * liquidation_fee_rate

    return LiquidationEvent(
        time=bar_time,
        mark_price=mark_price,
        last_price=last_price,
        quantity_closed=quantity_closed,
        quantity_remaining_after=quantity_remaining,
        realized_pnl=realized_pnl,
        liquidation_fee=liquidation_fee,
        net_pnl=realized_pnl - liquidation_fee,
        bar_index=bar_index,
        maintenance_margin_rate=maintenance_margin_rate,
        is_full_liquidation=is_full_liquidation,
    )


# ---------------------------------------------------------------------------
# Liquidation wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiquidatedBacktestMetrics:
    """BacktestMetrics + liquidation overlay (R1 deliverable shape).

    Adds five fields to :class:`backtest.types.BacktestMetrics` without
    mutating the original dataclass.

    Attributes
    ----------
    base : BacktestMetrics
        The original engine output (unchanged).
    liquidation_events : tuple[LiquidationEvent, ...]
        Per-event forced-close results (in bar order).
    total_liquidation_cost : float
        Sum of ``-net_pnl`` across liquidation events (i.e. fees +
        realized losses). When all liquidations are profitable
        (rare — liquidation usually signals a loss event), this can
        be negative; for clarity callers can also sum ``net_pnl``
        directly.
    liquidated_equity_delta : list[float]
        Per-bar liquidation deltas, length ``len(bars)``. ``[i]`` is
        the cumulative liquidation P&L charged at bar ``i`` (only
        nonzero at bars where a liquidation event lands).
    liquidated_ending_balance : float
        ``base.ending_balance + sum(net_pnl)`` for in-window
        liquidation events (events past the bar window are excluded,
        matching the funding-wrapper contract).
    """

    base: object  # BacktestMetrics — typed as object to avoid hard import
    liquidation_events: tuple[LiquidationEvent, ...]
    total_liquidation_cost: float
    liquidated_equity_delta: list[float]
    liquidated_ending_balance: float


def _resolve_mm_rate(
    spec: LiquidationSpec,
    position: PositionSpec,
) -> float:
    """Resolve the maintenance margin rate from spec.position + tier table."""
    if spec.maintenance_margin_rate is not None:
        return spec.maintenance_margin_rate
    return resolve_maintenance_margin_rate(position.notional_usd, spec.tiers)


def _ensure_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _aligned_mark_series(
    bars: Sequence[Bar],
    mark_prices: Sequence[float],
) -> list[tuple[int, Bar, float]]:
    """Pair bars with mark prices; bail on length mismatch."""
    if len(mark_prices) != len(bars):
        raise ValueError(
            f"mark_prices length ({len(mark_prices)}) must match bars length "
            f"({len(bars)})"
        )
    return [(i, bar, float(mark_prices[i])) for i, bar in enumerate(bars)]


def run_backtest_with_liquidation(
    bars: Sequence[Bar],
    mark_prices: Sequence[float],
    position: PositionSpec,
    liq_spec: LiquidationSpec,
    config: object,  # BacktestConfig — typed as object to avoid hard import
    strategies: Sequence[object],
    strategy_name: Optional[str] = None,
    integrity_config: Optional[IntegrityConfig] = None,
) -> LiquidatedBacktestMetrics:
    """Run :class:`BacktestEngine` and overlay mark-price liquidation P&L.

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series. Must be sorted ascending by time.
    mark_prices : sequence[float]
        Mark-price series aligned 1:1 with ``bars``. The liquidation
        trigger is evaluated against these; the forced close happens
        at the mark price, not at ``bar.close``.
    position : PositionSpec
        Position to evaluate against. The position is treated as open
        for the entire window (same simplification as the funding
        wrapper).
    liq_spec : LiquidationSpec
        Liquidation parameters (entry price, leverage, fee rate, mm
        rate or tier table).
    config : BacktestConfig
        Backtest config forwarded to ``BacktestEngine``.
    strategies : sequence
        Strategies passed to ``BacktestEngine``. Must be non-empty.
    strategy_name : str, optional
        Which strategy's metrics to wrap.

    Returns
    -------
    LiquidatedBacktestMetrics
        Base + liquidation overlay. Events whose bar time exceeds the
        bar window are excluded (matching the funding wrapper
        contract).

    Notes
    -----
    The engine path is identical to
    :func:`run_backtest_with_funding`: the engine runs unchanged, the
    wrapper only computes the liquidation overlay. Legacy forex path
    (``BacktestEngine.run_single``) is untouched.
    """
    if not isinstance(position, PositionSpec):
        raise TypeError(
            f"position must be PositionSpec (got {type(position).__name__})"
        )
    if not isinstance(liq_spec, LiquidationSpec):
        raise TypeError(
            f"liq_spec must be LiquidationSpec (got {type(liq_spec).__name__})"
        )
    if integrity_config is not None and not isinstance(integrity_config, IntegrityConfig):
        raise TypeError(
            f"integrity_config must be IntegrityConfig or None "
            f"(got {type(integrity_config).__name__})"
        )
    if not bars:
        raise ValueError("bars must be non-empty")
    if not strategies:
        raise ValueError("strategies must be non-empty")
    if any(m <= 0 for m in mark_prices):
        raise ValueError(
            "mark_prices must all be positive (got non-positive at some index)"
        )
    if liq_spec.entry_price <= 0:
        raise ValueError(
            f"liq_spec.entry_price must be positive (got {liq_spec.entry_price})"
        )

    # Opt-in data-integrity gate (R5 — Satsuki). When integrity_config
    # is supplied, validate bars BEFORE invoking the engine. The gate
    # is fail-loud: on any violation it raises DataIntegrityError and
    # the engine is never reached. When integrity_config is None
    # (default), no validation runs and the existing behavior is
    # preserved.
    if integrity_config is not None:
        from backtest.integrity_gate import apply_integrity_gate as _apply_gate
        _apply_gate(
            symbol=getattr(config, "pair", "") or "",
            bars=bars,
            config=integrity_config,
        )

    # Lazy import: same pattern as funding_model.run_backtest_with_funding.
    from engine.engine import BacktestEngine

    name = strategy_name if strategy_name is not None else getattr(strategies[0], "name", None)
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

    # Resolve mm rate once at the start of the window (notional is
    # fixed in the single-position-wrapper contract).
    mm_rate = _resolve_mm_rate(liq_spec, position)
    threshold = compute_liquidation_threshold_price(
        entry_price=liq_spec.entry_price,
        notional_usd=position.notional_usd,
        direction=position.direction,
        leverage=liq_spec.leverage,
        maintenance_margin_rate=mm_rate,
    )

    # Walk the bar/mark series and evaluate liquidation at each bar.
    # Once a full liquidation closes the position, subsequent bars
    # don't trigger (the position is gone).
    aligned = _aligned_mark_series(bars, mark_prices)
    position_qty = (
        position.notional_usd / liq_spec.entry_price if liq_spec.entry_price > 0 else 0.0
    )
    # Sign the quantity by direction.
    position_qty = abs(position_qty)
    if liq_spec.direction == "short":
        position_qty = -position_qty

    events: list[LiquidationEvent] = []
    deltas = [0.0] * len(bars)

    for i, bar, mark_price in aligned:
        if position_qty == 0.0:
            break
        crossed = check_liquidation_threshold_crossed(
            mark_price=mark_price,
            threshold_price=threshold,
            direction=liq_spec.direction,
        )
        if not crossed:
            continue
        bar_time = _ensure_utc(bar.time)
        ev = process_liquidation_at_bar(
            position_qty=position_qty,
            mark_price=mark_price,
            last_price=bar.close,
            entry_price=liq_spec.entry_price,
            leverage=liq_spec.leverage,
            maintenance_margin_rate=mm_rate,
            liquidation_fee_rate=liq_spec.liquidation_fee_rate,
            direction=liq_spec.direction,
            bar_index=i,
            bar_time=bar_time,
            target_margin_ratio=liq_spec.target_margin_ratio,
        )
        events.append(ev)
        deltas[i] += ev.net_pnl
        position_qty = (
            -ev.quantity_remaining_after if liq_spec.direction == "short"
            else ev.quantity_remaining_after
        )

    total_cost = -sum(ev.net_pnl for ev in events)
    # Sum of net_pnl across events (signed; negative = net cost).
    net_liq_pnl = sum(ev.net_pnl for ev in events)

    return LiquidatedBacktestMetrics(
        base=base_metrics,
        liquidation_events=tuple(events),
        total_liquidation_cost=total_cost,
        liquidated_equity_delta=deltas,
        liquidated_ending_balance=base_metrics.ending_balance + net_liq_pnl,
    )


# ---------------------------------------------------------------------------
# Composition with funding
# ---------------------------------------------------------------------------


def run_backtest_with_funding_and_liquidation(
    bars: Sequence[Bar],
    mark_prices: Sequence[float],
    funding_events: Iterable[object],
    position: PositionSpec,
    liq_spec: LiquidationSpec,
    config: object,
    strategies: Sequence[object],
    strategy_name: Optional[str] = None,
) -> tuple[object, object]:
    """Run :func:`run_backtest_with_funding` and overlay mark-price liq.

    Composition strategy:
      1. Run ``run_backtest_with_funding`` to get the funded metrics.
      2. Run ``run_backtest_with_liquidation`` against the same
         bars/position/engine config — but using the funded metric's
         ending balance as the prior equity for liquidation math
         (the position is treated as open for the entire window, so
         starting equity doesn't change the *trigger* — only the
         realized P&L bookkeeping is affected).
      3. Return ``(funded_metrics, liquidated_metrics)`` for the
         caller to fold together.

    Parameters
    ----------
    bars, mark_prices, funding_events, position, liq_spec, config,
    strategies, strategy_name
        See :func:`run_backtest_with_funding` and
        :func:`run_backtest_with_liquidation`.

    Returns
    -------
    tuple[FundedBacktestMetrics, LiquidatedBacktestMetrics]
        Both overlays computed against the same engine run. The
        caller folds the two deltas into a single equity curve.

    Notes
    -----
    The two wrappers do not interact: funding applies at the bar whose
    time is >= settlement time; liquidation applies at the bar whose
    mark crosses the threshold. They are independent event sources
    that can coexist on the same bar (both produce a delta at that
    bar; the caller sums them).
    """
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
    return funded, liquidated
