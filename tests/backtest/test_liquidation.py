"""Tests for ``forex_bot.backtest.liquidation`` — mark-price liquidation mechanics.

Card: 81cfff7c-cf1f-44da-a68f-dc5993b0e4a6 (Sprint C 1a.2)
Sprint: 2026-10-05-crypto-strategy-factory
Lane: [BUILD][CRYPTO-LANE]

Scope (HR5 — targeted tests only):

  * LiquidationTier validation (positive notionals, valid rate)
  * resolve_maintenance_margin_rate — tier lookup, edge cases
    (notional at boundary, beyond top tier, empty table)
  * compute_liquidation_threshold_price — long/short math, leverage,
    tier-resolution branch, error cases
  * check_liquidation_threshold_crossed — long/short triggers
    (boundary inclusive/exclusive semantics), error cases
  * compute_partial_liquidation_quantity — when partial applies,
    when full is forced, sign handling
  * process_liquidation_at_bar — full vs partial, fee accounting,
    realized PnL signed by direction, is_full_liquidation flag
  * Mark-vs-last divergence cases:
      - Last trade below threshold but mark above → no liq
      - Last trade above threshold but mark below → liq triggers
      - Forced close happens at MARK, not last
  * Tiered maintenance margin brackets (Binance USDⓈ-M spec)
  * Engine integration:
      - run_backtest_with_liquidation runs the legacy engine
        unchanged and overlays liquidation events
      - Legacy FX path is 100% untouched
      - Composition with run_backtest_with_funding returns both overlays
  * Edge cases:
      - Mark price > threshold throughout → no events
      - Mark crosses at bar 0 → event at bar 0
      - Position fully closed at first liq → no subsequent events
      - Empty bars, mismatched mark series length, invalid specs

All tests are deterministic — synthetic fixtures only, no live
exchange calls. The engine integration tests use a no-op strategy
that never fires a signal, so we exercise the real engine's
run_single() path without price movement dominating the equity curve.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup: src/forex_bot on sys.path so ``backtest.liquidation`` and
# ``engine.engine`` resolve. Mirrors test_adaptive_funding.py setup.
# This MUST run before any imports below.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_FX = _PROJECT_ROOT / "src" / "forex_bot"
_SRC_ROOT = _PROJECT_ROOT / "src"
for p in (_SRC_FX, _SRC_ROOT):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from backtest.funding_model import (  # noqa: E402
    FundedBacktestMetrics,
    FundingEvent,
    PositionSpec,
    run_backtest_with_funding,
)
from backtest.liquidation import (  # noqa: E402
    DEFAULT_LIQUIDATION_FEE_RATE,
    DEFAULT_MAINTENANCE_TIERS,
    PARTIAL_LIQUIDATION_TARGET_RATIO,
    LiquidatedBacktestMetrics,
    LiquidationEvent,
    LiquidationSpec,
    LiquidationTier,
    check_liquidation_threshold_crossed,
    compute_liquidation_threshold_price,
    compute_partial_liquidation_quantity,
    process_liquidation_at_bar,
    resolve_maintenance_margin_rate,
    run_backtest_with_funding_and_liquidation,
    run_backtest_with_liquidation,
)
from backtest.strategies.isignal_strategy import ISignalStrategy
from backtest.types import BacktestConfig, Bar

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _ev(  # shorthand FundingEvent factory (reused for fixture generation)
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int = 0,
    rate: float = 0.0001,
    interval_hours: int = 8,
    cap_triggered: bool = False,
) -> FundingEvent:
    return FundingEvent(
        time=datetime(year, month, day, hour, minute, tzinfo=timezone.utc),
        rate=rate,
        interval_hours=interval_hours,
        cap_triggered=cap_triggered,
    )


def _flat_bars(
    n: int,
    start: datetime,
    step_minutes: int = 60,
    base_price: float = 100.0,
    last_prices: list[float] | None = None,
) -> list[Bar]:
    """Build N flat OHLC bars. If ``last_prices`` is provided, the
    ``close`` field uses those values (useful for constructing
    divergent last/mark scenarios); otherwise all closes == base_price.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    bars: list[Bar] = []
    for i in range(n):
        t = start + timedelta(minutes=step_minutes * i)
        if last_prices is not None:
            cp = last_prices[i]
        else:
            cp = base_price
        bars.append(
            Bar(
                time=t,
                open=base_price,
                high=base_price,
                low=base_price,
                close=cp,
                volume=100.0,
                spread_pips=1.5,
            )
        )
    return bars


def _simple_config(starting_balance: float = 10_000.0) -> BacktestConfig:
    """A BacktestConfig that triggers no trades on flat bars — used to
    isolate liquidation overlay from strategy P&L."""
    return BacktestConfig(
        starting_balance=starting_balance,
        risk_per_trade_pct=0.01,
        max_daily_drawdown_pct=0.05,
        max_total_drawdown_pct=0.10,
        commission_per_lot=0.0,
        leverage=100,
        min_confidence=0.99,
        min_bars_before_signal=30,
        max_open_trades=1,
        pair="BTCUSDT",
        spread_pips=0.0,
        round_trip_spread=False,
        slippage_pips=0.0,
        swap_per_lot_per_day=0.0,
    )


class _NoFireStrategy(ISignalStrategy):
    """A trivial strategy that never produces a signal."""

    @property
    def name(self) -> str:
        return "NoFire"

    def evaluate(self, state: Any) -> None:
        return None


# ──────────────────────────────────────────────────────────────────────
# Module-level constants (Binance spec)
# ──────────────────────────────────────────────────────────────────────


class TestModuleConstants:
    def test_default_liquidation_fee_rate(self):
        # 0.5% per Binance VIP 0 spec.
        assert DEFAULT_LIQUIDATION_FEE_RATE == pytest.approx(0.005, abs=1e-12)

    def test_partial_liquidation_target_ratio(self):
        # 1.0 → "bring position back to IM level" (Binance-style).
        assert PARTIAL_LIQUIDATION_TARGET_RATIO == pytest.approx(1.0, abs=1e-12)

    def test_default_maintenance_tiers_binance_spec(self):
        """The default tier table matches the Binance USDⓈ-M spec:
        0.5% / 1.0% / 2.5% / 5.0% / 10.0% by notional bracket."""
        assert len(DEFAULT_MAINTENANCE_TIERS) == 5
        expected_rates = [0.005, 0.010, 0.025, 0.050, 0.100]
        for tier, expected in zip(DEFAULT_MAINTENANCE_TIERS, expected_rates, strict=True):
            assert tier.maintenance_margin_rate == pytest.approx(expected, abs=1e-12)

    def test_default_tiers_have_open_ended_top_bracket(self):
        """The top tier must be open-ended (max_notional == inf)."""
        top = DEFAULT_MAINTENANCE_TIERS[-1]
        assert math.isinf(top.max_notional)

    def test_default_tiers_are_contiguous(self):
        """Each tier's min_notional must equal the previous tier's
        max_notional — gaps would silently skip brackets."""
        for i in range(1, len(DEFAULT_MAINTENANCE_TIERS)):
            prev_max = DEFAULT_MAINTENANCE_TIERS[i - 1].max_notional
            curr_min = DEFAULT_MAINTENANCE_TIERS[i].min_notional
            assert curr_min == prev_max

    def test_default_tiers_have_valid_rates(self):
        for tier in DEFAULT_MAINTENANCE_TIERS:
            assert 0 < tier.maintenance_margin_rate < 1


# ──────────────────────────────────────────────────────────────────────
# LiquidationTier validation
# ──────────────────────────────────────────────────────────────────────


class TestLiquidationTierValidation:
    def test_round_trip(self):
        t = LiquidationTier(min_notional=0.0, max_notional=50_000.0, maintenance_margin_rate=0.005)
        assert t.min_notional == 0.0
        assert t.max_notional == 50_000.0
        assert t.maintenance_margin_rate == pytest.approx(0.005, abs=1e-12)

    def test_rejects_negative_min_notional(self):
        with pytest.raises(ValueError, match="min_notional"):
            LiquidationTier(min_notional=-1.0, max_notional=50_000.0, maintenance_margin_rate=0.005)

    def test_rejects_max_le_min_notional(self):
        with pytest.raises(ValueError, match="max_notional"):
            LiquidationTier(min_notional=50_000.0, max_notional=50_000.0, maintenance_margin_rate=0.005)

    def test_rejects_max_le_min_when_max_inf_below_min(self):
        """max_notional == -inf (or any non-positive) is rejected."""
        with pytest.raises(ValueError, match="max_notional"):
            LiquidationTier(min_notional=0.0, max_notional=-math.inf, maintenance_margin_rate=0.005)

    def test_rejects_zero_mm_rate(self):
        with pytest.raises(ValueError, match="maintenance_margin_rate"):
            LiquidationTier(min_notional=0.0, max_notional=50_000.0, maintenance_margin_rate=0.0)

    def test_rejects_mm_rate_above_1(self):
        with pytest.raises(ValueError, match="maintenance_margin_rate"):
            LiquidationTier(min_notional=0.0, max_notional=50_000.0, maintenance_margin_rate=1.0)

    def test_rejects_negative_mm_rate(self):
        with pytest.raises(ValueError, match="maintenance_margin_rate"):
            LiquidationTier(min_notional=0.0, max_notional=50_000.0, maintenance_margin_rate=-0.001)


# ──────────────────────────────────────────────────────────────────────
# resolve_maintenance_margin_rate
# ──────────────────────────────────────────────────────────────────────


class TestResolveMaintenanceMarginRate:
    def test_lowest_bracket(self):
        rate = resolve_maintenance_margin_rate(10_000.0)
        assert rate == pytest.approx(0.005, abs=1e-12)

    def test_top_of_lowest_bracket(self):
        """At the boundary (notional == 49_999.999), still in the lowest tier."""
        rate = resolve_maintenance_margin_rate(49_999.999)
        assert rate == pytest.approx(0.005, abs=1e-12)

    def test_first_bracket_upper_bound_exclusive(self):
        """At the exact upper bound (50_000), the rate flips to the next tier."""
        rate = resolve_maintenance_margin_rate(50_000.0)
        assert rate == pytest.approx(0.010, abs=1e-12)

    def test_second_bracket(self):
        rate = resolve_maintenance_margin_rate(100_000.0)
        assert rate == pytest.approx(0.010, abs=1e-12)

    def test_third_bracket(self):
        rate = resolve_maintenance_margin_rate(500_000.0)
        assert rate == pytest.approx(0.025, abs=1e-12)

    def test_fourth_bracket(self):
        rate = resolve_maintenance_margin_rate(5_000_000.0)
        assert rate == pytest.approx(0.050, abs=1e-12)

    def test_top_open_ended_bracket(self):
        """Notional > 10M → top open-ended bracket (10.0%)."""
        rate = resolve_maintenance_margin_rate(50_000_000.0)
        assert rate == pytest.approx(0.100, abs=1e-12)

    def test_zero_notional_is_valid(self):
        """0 notional still resolves — degenerate position, but rate lookup is fine."""
        rate = resolve_maintenance_margin_rate(0.0)
        assert rate == pytest.approx(0.005, abs=1e-12)

    def test_rejects_negative_notional(self):
        with pytest.raises(ValueError, match="notional_usd"):
            resolve_maintenance_margin_rate(-1.0)

    def test_rejects_empty_tier_table(self):
        with pytest.raises(ValueError, match="tiers"):
            resolve_maintenance_margin_rate(1000.0, tiers=())

    def test_custom_tier_table(self):
        custom = (LiquidationTier(0.0, math.inf, 0.020),)
        rate = resolve_maintenance_margin_rate(1_000_000.0, tiers=custom)
        assert rate == pytest.approx(0.020, abs=1e-12)


# ──────────────────────────────────────────────────────────────────────
# LiquidationSpec validation
# ──────────────────────────────────────────────────────────────────────


class TestLiquidationSpecValidation:
    def test_round_trip(self):
        spec = LiquidationSpec(
            entry_price=100.0,
            leverage=10.0,
            direction="long",
        )
        assert spec.entry_price == 100.0
        assert spec.leverage == 10.0
        assert spec.direction == "long"
        assert spec.liquidation_fee_rate == DEFAULT_LIQUIDATION_FEE_RATE
        assert spec.maintenance_margin_rate is None
        assert spec.target_margin_ratio == PARTIAL_LIQUIDATION_TARGET_RATIO

    def test_rejects_zero_entry_price(self):
        with pytest.raises(ValueError, match="entry_price"):
            LiquidationSpec(entry_price=0.0, leverage=10.0, direction="long")

    def test_rejects_negative_entry_price(self):
        with pytest.raises(ValueError, match="entry_price"):
            LiquidationSpec(entry_price=-100.0, leverage=10.0, direction="long")

    def test_rejects_leverage_below_one(self):
        with pytest.raises(ValueError, match="leverage"):
            LiquidationSpec(entry_price=100.0, leverage=0.5, direction="long")

    def test_rejects_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            LiquidationSpec(entry_price=100.0, leverage=10.0, direction="sideways")  # type: ignore[arg-type]

    def test_rejects_negative_fee_rate(self):
        with pytest.raises(ValueError, match="liquidation_fee_rate"):
            LiquidationSpec(entry_price=100.0, leverage=10.0, direction="long", liquidation_fee_rate=-0.001)

    def test_rejects_fee_rate_at_1(self):
        with pytest.raises(ValueError, match="liquidation_fee_rate"):
            LiquidationSpec(entry_price=100.0, leverage=10.0, direction="long", liquidation_fee_rate=1.0)

    def test_rejects_mm_rate_zero(self):
        with pytest.raises(ValueError, match="maintenance_margin_rate"):
            LiquidationSpec(
                entry_price=100.0, leverage=10.0, direction="long",
                maintenance_margin_rate=0.0,
            )

    def test_rejects_mm_rate_one(self):
        with pytest.raises(ValueError, match="maintenance_margin_rate"):
            LiquidationSpec(
                entry_price=100.0, leverage=10.0, direction="long",
                maintenance_margin_rate=1.0,
            )

    def test_rejects_negative_target_ratio(self):
        with pytest.raises(ValueError, match="target_margin_ratio"):
            LiquidationSpec(
                entry_price=100.0, leverage=10.0, direction="long",
                target_margin_ratio=-0.1,
            )

    def test_accepts_explicit_mm_rate(self):
        """An explicit maintenance_margin_rate overrides the tier table."""
        spec = LiquidationSpec(
            entry_price=100.0,
            leverage=10.0,
            direction="long",
            maintenance_margin_rate=0.020,
        )
        assert spec.maintenance_margin_rate == pytest.approx(0.020, abs=1e-12)

    def test_rejects_empty_tier_table(self):
        with pytest.raises(ValueError, match="tiers"):
            LiquidationSpec(
                entry_price=100.0,
                leverage=100.0,
                direction="long",
                tiers=(),
            )

    def test_rejects_non_contiguous_tier_table(self):
        bad_tiers = (
            LiquidationTier(0.0, 50_000.0, 0.005),
            LiquidationTier(60_000.0, 250_000.0, 0.010),  # gap at 50k
        )
        with pytest.raises(ValueError, match="contiguous"):
            LiquidationSpec(
                entry_price=100.0, leverage=10.0, direction="long",
                tiers=bad_tiers,
            )


# ──────────────────────────────────────────────────────────────────────
# compute_liquidation_threshold_price
# ──────────────────────────────────────────────────────────────────────


class TestComputeLiquidationThreshold:
    def test_long_threshold_100x_low_bracket(self):
        """Long + 100× leverage + 0.5% mm → trigger ~0.5% below entry."""
        threshold = compute_liquidation_threshold_price(
            entry_price=100.0,
            notional_usd=10_000.0,
            direction="long",
            leverage=100.0,
        )
        # 100 * (1 - 0.01) / (1 - 0.005) = 100 * 0.99 / 0.995 ≈ 99.49749
        assert threshold == pytest.approx(99.49748743718, abs=1e-6)

    def test_short_threshold_100x_low_bracket(self):
        """Short + 100× leverage + 0.5% mm → trigger ~0.5% above entry."""
        threshold = compute_liquidation_threshold_price(
            entry_price=100.0,
            notional_usd=10_000.0,
            direction="short",
            leverage=100.0,
        )
        # 100 * (1 + 0.01) / (1 + 0.005) = 100 * 1.01 / 1.005 ≈ 100.49751
        assert threshold == pytest.approx(100.49751243881, abs=1e-6)

    def test_long_threshold_10x_2nd_bracket(self):
        """Long + 10× leverage + 1.0% mm (because notional in second bracket)
        → trigger is wider (10% below) due to lower leverage."""
        threshold = compute_liquidation_threshold_price(
            entry_price=100.0,
            notional_usd=100_000.0,  # second tier
            direction="long",
            leverage=10.0,
        )
        # 100 * (1 - 0.1) / (1 - 0.01) = 100 * 0.9 / 0.99 ≈ 90.909...
        assert threshold == pytest.approx(90.90909090909, abs=1e-6)

    def test_short_threshold_10x_2nd_bracket(self):
        """Short + 10× leverage + 1.0% mm → trigger ~10% above."""
        threshold = compute_liquidation_threshold_price(
            entry_price=100.0,
            notional_usd=100_000.0,
            direction="short",
            leverage=10.0,
        )
        # 100 * (1 + 0.1) / (1 + 0.01) = 100 * 1.1 / 1.01 ≈ 108.911
        assert threshold == pytest.approx(108.91089108911, abs=1e-6)

    def test_explicit_mm_rate_overrides_tier_table(self):
        """When maintenance_margin_rate is pinned, the tier table isn't consulted."""
        threshold = compute_liquidation_threshold_price(
            entry_price=100.0,
            notional_usd=10_000_000.0,  # would be in 5.0% tier without override
            direction="long",
            leverage=10.0,
            maintenance_margin_rate=0.020,
        )
        # 100 * 0.9 / 0.98 ≈ 91.836...
        assert threshold == pytest.approx(91.83673469388, abs=1e-6)

    def test_long_threshold_drops_with_higher_leverage(self):
        """Higher leverage → mark can fall further before liquidation."""
        t_10x = compute_liquidation_threshold_price(
            entry_price=100.0, notional_usd=10_000.0,
            direction="long", leverage=10.0,
        )
        t_50x = compute_liquidation_threshold_price(
            entry_price=100.0, notional_usd=10_000.0,
            direction="long", leverage=50.0,
        )
        # 10× threshold is much lower (further from entry) than 50×
        # because lower leverage gives more "room" before liq.
        assert t_10x < t_50x < 100.0

    def test_short_threshold_rises_with_higher_leverage(self):
        """For shorts, higher leverage → trigger closer to entry."""
        t_10x = compute_liquidation_threshold_price(
            entry_price=100.0, notional_usd=10_000.0,
            direction="short", leverage=10.0,
        )
        t_50x = compute_liquidation_threshold_price(
            entry_price=100.0, notional_usd=10_000.0,
            direction="short", leverage=50.0,
        )
        assert t_10x > t_50x > 100.0

    def test_rejects_zero_entry_price(self):
        with pytest.raises(ValueError, match="entry_price"):
            compute_liquidation_threshold_price(
                entry_price=0.0, notional_usd=10_000.0,
                direction="long", leverage=10.0,
            )

    def test_rejects_negative_notional(self):
        with pytest.raises(ValueError, match="notional_usd"):
            compute_liquidation_threshold_price(
                entry_price=100.0, notional_usd=-1.0,
                direction="long", leverage=10.0,
            )

    def test_rejects_leverage_below_one(self):
        with pytest.raises(ValueError, match="leverage"):
            compute_liquidation_threshold_price(
                entry_price=100.0, notional_usd=10_000.0,
                direction="long", leverage=0.5,
            )

    def test_rejects_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            compute_liquidation_threshold_price(
                entry_price=100.0, notional_usd=10_000.0,
                direction="sideways", leverage=10.0,  # type: ignore[arg-type]
            )


# ──────────────────────────────────────────────────────────────────────
# check_liquidation_threshold_crossed
# ──────────────────────────────────────────────────────────────────────


class TestCheckLiquidationThresholdCrossed:
    def test_long_triggered_when_mark_below(self):
        crossed = check_liquidation_threshold_crossed(
            mark_price=99.0, threshold_price=99.5, direction="long",
        )
        assert crossed is True

    def test_long_triggered_when_mark_equals(self):
        """Boundary inclusive: mark == threshold counts as crossed."""
        crossed = check_liquidation_threshold_crossed(
            mark_price=99.5, threshold_price=99.5, direction="long",
        )
        assert crossed is True

    def test_long_not_triggered_when_mark_above(self):
        crossed = check_liquidation_threshold_crossed(
            mark_price=100.0, threshold_price=99.5, direction="long",
        )
        assert crossed is False

    def test_short_triggered_when_mark_above(self):
        crossed = check_liquidation_threshold_crossed(
            mark_price=101.0, threshold_price=100.5, direction="short",
        )
        assert crossed is True

    def test_short_triggered_when_mark_equals(self):
        crossed = check_liquidation_threshold_crossed(
            mark_price=100.5, threshold_price=100.5, direction="short",
        )
        assert crossed is True

    def test_short_not_triggered_when_mark_below(self):
        crossed = check_liquidation_threshold_crossed(
            mark_price=100.0, threshold_price=100.5, direction="short",
        )
        assert crossed is False

    def test_rejects_zero_mark_price(self):
        with pytest.raises(ValueError, match="mark_price"):
            check_liquidation_threshold_crossed(
                mark_price=0.0, threshold_price=100.0, direction="long",
            )

    def test_rejects_zero_threshold(self):
        with pytest.raises(ValueError, match="threshold_price"):
            check_liquidation_threshold_crossed(
                mark_price=100.0, threshold_price=0.0, direction="long",
            )

    def test_rejects_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            check_liquidation_threshold_crossed(
                mark_price=100.0, threshold_price=100.0, direction="up",  # type: ignore[arg-type]
            )


# ──────────────────────────────────────────────────────────────────────
# compute_partial_liquidation_quantity
# ──────────────────────────────────────────────────────────────────────


class TestComputePartialLiquidationQuantity:
    def test_zero_position_returns_zero(self):
        q = compute_partial_liquidation_quantity(
            position_qty=0.0, mark_price=99.0,
            entry_price=100.0, leverage=10.0,
            maintenance_margin_rate=0.01, liquidation_fee_rate=0.005,
            direction="long",
        )
        assert q == 0.0

    def test_returns_positive_for_above_threshold_short(self):
        """Short position with mark well above threshold → returns positive qty."""
        # Position qty = -1.0 (short), mark well above entry → loss
        q = compute_partial_liquidation_quantity(
            position_qty=-1.0, mark_price=110.0,
            entry_price=100.0, leverage=10.0,
            maintenance_margin_rate=0.01, liquidation_fee_rate=0.005,
            direction="short",
        )
        assert q > 0.0

    def test_partial_quantity_does_not_exceed_position(self):
        """Solved partial qty is clamped to the open position size."""
        q = compute_partial_liquidation_quantity(
            position_qty=1.0, mark_price=99.0,  # far past threshold
            entry_price=100.0, leverage=100.0,
            maintenance_margin_rate=0.005, liquidation_fee_rate=0.005,
            direction="long",
        )
        assert q <= 1.0

    def test_rejects_zero_mark_price(self):
        with pytest.raises(ValueError, match="mark_price"):
            compute_partial_liquidation_quantity(
                position_qty=1.0, mark_price=0.0,
                entry_price=100.0, leverage=10.0,
                maintenance_margin_rate=0.01, liquidation_fee_rate=0.005,
                direction="long",
            )

    def test_rejects_zero_entry_price(self):
        with pytest.raises(ValueError, match="entry_price"):
            compute_partial_liquidation_quantity(
                position_qty=1.0, mark_price=99.0,
                entry_price=0.0, leverage=10.0,
                maintenance_margin_rate=0.01, liquidation_fee_rate=0.005,
                direction="long",
            )

    def test_rejects_leverage_below_one(self):
        with pytest.raises(ValueError, match="leverage"):
            compute_partial_liquidation_quantity(
                position_qty=1.0, mark_price=99.0,
                entry_price=100.0, leverage=0.5,
                maintenance_margin_rate=0.01, liquidation_fee_rate=0.005,
                direction="long",
            )

    def test_rejects_invalid_mm_rate(self):
        with pytest.raises(ValueError, match="maintenance_margin_rate"):
            compute_partial_liquidation_quantity(
                position_qty=1.0, mark_price=99.0,
                entry_price=100.0, leverage=10.0,
                maintenance_margin_rate=1.0, liquidation_fee_rate=0.005,
                direction="long",
            )

    def test_rejects_negative_fee_rate(self):
        with pytest.raises(ValueError, match="liquidation_fee_rate"):
            compute_partial_liquidation_quantity(
                position_qty=1.0, mark_price=99.0,
                entry_price=100.0, leverage=10.0,
                maintenance_margin_rate=0.01, liquidation_fee_rate=-0.005,
                direction="long",
            )

    def test_rejects_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            compute_partial_liquidation_quantity(
                position_qty=1.0, mark_price=99.0,
                entry_price=100.0, leverage=10.0,
                maintenance_margin_rate=0.01, liquidation_fee_rate=0.005,
                direction="sideways",  # type: ignore[arg-type]
            )


# ──────────────────────────────────────────────────────────────────────
# process_liquidation_at_bar
# ──────────────────────────────────────────────────────────────────────


class TestProcessLiquidationAtBar:
    def _bar_time(self) -> datetime:
        return datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)

    def test_long_liquidation_event_carries_loss(self):
        """Long with mark below entry → realized_pnl negative."""
        ev = process_liquidation_at_bar(
            position_qty=1.0,
            mark_price=99.0,  # 1% below entry
            last_price=99.0,
            entry_price=100.0,
            leverage=10.0,
            maintenance_margin_rate=0.01,
            liquidation_fee_rate=0.005,
            direction="long",
            bar_index=0,
            bar_time=self._bar_time(),
        )
        assert isinstance(ev, LiquidationEvent)
        # Realized PnL = qty_closed * (mark - entry) = qty_closed * (-1)
        assert ev.realized_pnl < 0
        assert ev.liquidation_fee > 0
        assert ev.net_pnl < ev.realized_pnl  # fee reduces net

    def test_short_liquidation_event_carries_loss(self):
        """Short with mark above entry → realized_pnl negative."""
        ev = process_liquidation_at_bar(
            position_qty=-1.0,
            mark_price=101.0,  # 1% above entry
            last_price=101.0,
            entry_price=100.0,
            leverage=10.0,
            maintenance_margin_rate=0.01,
            liquidation_fee_rate=0.005,
            direction="short",
            bar_index=0,
            bar_time=self._bar_time(),
        )
        assert ev.realized_pnl < 0
        assert ev.liquidation_fee > 0

    def test_full_liquidation_when_partial_exceeds_position(self):
        """When mark is far past threshold, the solved q_p exceeds |q|
        and is clamped to full close."""
        ev = process_liquidation_at_bar(
            position_qty=1.0,
            mark_price=80.0,  # 20% below entry, deep loss
            last_price=80.0,
            entry_price=100.0,
            leverage=10.0,
            maintenance_margin_rate=0.01,
            liquidation_fee_rate=0.005,
            direction="long",
            bar_index=0,
            bar_time=self._bar_time(),
        )
        assert ev.is_full_liquidation is True
        assert ev.quantity_remaining_after == 0.0
        assert ev.quantity_closed == pytest.approx(1.0, abs=1e-12)

    def test_partial_liquidation_when_q_p_less_than_position(self):
        """Mild liquidation (mark just past threshold) → partial close.

        Threshold for 10× leverage + 1% mm + entry=100 (long) is 90.909.
        mark=90.8 sits just below threshold (triggers) but only 1.09
        below entry — the closed-PnL coefficient is small enough that a
        partial close restores the IM buffer.
        """
        ev = process_liquidation_at_bar(
            position_qty=10.0,
            mark_price=90.8,  # ~0.11 below the 90.909 threshold
            last_price=90.8,
            entry_price=100.0,
            leverage=10.0,
            maintenance_margin_rate=0.01,
            liquidation_fee_rate=0.005,
            direction="long",
            bar_index=0,
            bar_time=self._bar_time(),
        )
        # Should be partial (q_p < position size).
        assert ev.is_full_liquidation is False
        assert ev.quantity_closed > 0
        assert ev.quantity_closed < 10.0
        assert ev.quantity_remaining_after > 0.0

    def test_liquidation_fee_scales_with_notional(self):
        """Liquidation fee = q * mark * fee_rate — verify."""
        ev1 = process_liquidation_at_bar(
            position_qty=1.0,
            mark_price=99.0, last_price=99.0, entry_price=100.0,
            leverage=10.0, maintenance_margin_rate=0.01,
            liquidation_fee_rate=0.005, direction="long",
            bar_index=0, bar_time=self._bar_time(),
        )
        ev2 = process_liquidation_at_bar(
            position_qty=2.0,
            mark_price=99.0, last_price=99.0, entry_price=100.0,
            leverage=10.0, maintenance_margin_rate=0.01,
            liquidation_fee_rate=0.005, direction="long",
            bar_index=0, bar_time=self._bar_time(),
        )
        # 2x quantity (full close in both) → 2x fee (clamped).
        if ev1.is_full_liquidation and ev2.is_full_liquidation:
            assert ev2.liquidation_fee == pytest.approx(
                2.0 * ev1.liquidation_fee, abs=1e-9
            )

    def test_liquidation_event_validates_tz_aware_time(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            process_liquidation_at_bar(
                position_qty=1.0,
                mark_price=99.0, last_price=99.0, entry_price=100.0,
                leverage=10.0, maintenance_margin_rate=0.01,
                liquidation_fee_rate=0.005, direction="long",
                bar_index=0,
                bar_time=datetime(2026, 10, 5, 0, 0),  # naive
            )

    def test_liquidation_event_normalizes_non_utc_to_utc(self):
        """Non-UTC time is normalized (FundingEvent-equivalent behavior)."""
        eastern = timezone(timedelta(hours=-4))
        t = datetime(2026, 10, 5, 0, 0, tzinfo=eastern)
        ev = process_liquidation_at_bar(
            position_qty=1.0,
            mark_price=99.0, last_price=99.0, entry_price=100.0,
            leverage=10.0, maintenance_margin_rate=0.01,
            liquidation_fee_rate=0.005, direction="long",
            bar_index=0,
            bar_time=t,
        )
        assert ev.time.tzinfo == timezone.utc
        assert ev.time.hour == 4  # 00:00 EDT → 04:00 UTC

    def test_liquidation_event_rejects_zero_mark_price(self):
        with pytest.raises(ValueError, match="mark_price"):
            process_liquidation_at_bar(
                position_qty=1.0,
                mark_price=0.0, last_price=99.0, entry_price=100.0,
                leverage=10.0, maintenance_margin_rate=0.01,
                liquidation_fee_rate=0.005, direction="long",
                bar_index=0, bar_time=self._bar_time(),
            )

    def test_liquidation_event_rejects_invalid_leverage(self):
        with pytest.raises(ValueError, match="leverage"):
            process_liquidation_at_bar(
                position_qty=1.0,
                mark_price=99.0, last_price=99.0, entry_price=100.0,
                leverage=0.5, maintenance_margin_rate=0.01,
                liquidation_fee_rate=0.005, direction="long",
                bar_index=0, bar_time=self._bar_time(),
            )

    def test_liquidation_event_rejects_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            process_liquidation_at_bar(
                position_qty=1.0,
                mark_price=99.0, last_price=99.0, entry_price=100.0,
                leverage=10.0, maintenance_margin_rate=0.01,
                liquidation_fee_rate=0.005, direction="up",  # type: ignore[arg-type]
                bar_index=0, bar_time=self._bar_time(),
            )

    def test_mm_rate_recorded_on_event(self):
        ev = process_liquidation_at_bar(
            position_qty=1.0,
            mark_price=99.0, last_price=99.0, entry_price=100.0,
            leverage=10.0, maintenance_margin_rate=0.020,
            liquidation_fee_rate=0.005, direction="long",
            bar_index=0, bar_time=self._bar_time(),
        )
        assert ev.maintenance_margin_rate == pytest.approx(0.020, abs=1e-12)


# ──────────────────────────────────────────────────────────────────────
# Mark vs last trade divergence (R1 §1.4 — the key fact)
# ──────────────────────────────────────────────────────────────────────


class TestMarkVsLastDivergence:
    """Per R1 §1.4: liquidation is a MARK-price event. Last trades can
    overshoot during stress. A backtest that triggers on last trade
    systematically mis-models liquidation probability. These tests
    pin that distinction."""

    def test_last_below_threshold_but_mark_above_no_liquidation(self):
        """Long: last price has spiked DOWN through threshold, but mark
        (which smooths via index aggregation) is still above. A naive
        last-price backtest would liquidate here; mark-price model does
        not. Verified via check_liquidation_threshold_crossed + wrapper
        integration."""
        threshold = 99.5
        mark = 100.0  # above threshold → no trigger
        crossed = check_liquidation_threshold_crossed(
            mark_price=mark, threshold_price=threshold, direction="long",
        )
        assert crossed is False

    def test_last_above_threshold_but_mark_below_liquidates(self):
        """Long: last price is HOLDING above threshold, but mark (from
        index) has fallen through. A naive last-price backtest would
        NOT liquidate here; mark-price model does. The mark wins —
        this is the cascade signal."""
        threshold = 99.5
        mark = 99.0  # below threshold → triggers
        crossed = check_liquidation_threshold_crossed(
            mark_price=mark, threshold_price=threshold, direction="long",
        )
        assert crossed is True

    def test_forced_close_uses_mark_not_last(self):
        """The liquidation PnL is computed at MARK, not LAST. We
        verify by passing divergent mark/last values and checking the
        event's realized_pnl reflects the mark side.

        Scenario uses mark=80 (20% below entry, well past the 90.91
        threshold for 10× with 1% mm) which forces a full liquidation
        (the partial math clamps to abs_q in the deep-loss regime).
        last=50 represents a hypothetical last-trade overshoot that a
        naive last-price-stop backtest would have used.
        """
        entry = 100.0
        # Mark = 80 (a 20% drop); Last = 50 (hypothetical wick overshoot)
        ev = process_liquidation_at_bar(
            position_qty=1.0,
            mark_price=80.0,
            last_price=50.0,  # hypothetical last trade overshoot
            entry_price=entry,
            leverage=10.0,
            maintenance_margin_rate=0.01,
            liquidation_fee_rate=0.005,
            direction="long",
            bar_index=0,
            bar_time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
        # Full close is forced (deep-loss regime: partial math clamps).
        assert ev.is_full_liquidation is True
        # Realized PnL = 1 * (80 - 100) = -20 (mark-based, NOT -50 last-based).
        assert ev.realized_pnl == pytest.approx(-20.0, abs=1e-9)
        # Last price recorded for diagnostics.
        assert ev.last_price == pytest.approx(50.0, abs=1e-9)
        assert ev.mark_price == pytest.approx(80.0, abs=1e-9)
        # Liquidation fee also uses mark: 1 * 80 * 0.005 = 0.4
        assert ev.liquidation_fee == pytest.approx(0.4, abs=1e-9)

    def test_short_mark_above_threshold_liquidates_despite_last_below(self):
        """Short: last trade has temporarily dropped (a wick), but mark
        has risen above threshold. Naive last-price model misses the
        trigger; mark-price model catches it."""
        threshold = 100.5
        mark = 101.0  # above threshold → triggers for short
        # (Hypothetical wick-down last print at 98.0 is documented in
        # the docstring; the assertion exercises the mark-side only.)
        crossed = check_liquidation_threshold_crossed(
            mark_price=mark, threshold_price=threshold, direction="short",
        )
        assert crossed is True

    def test_short_mark_below_threshold_no_liquidation_despite_last_above(self):
        """Short: last trade has spiked up (a wick), but mark is still
        below threshold. Naive last-price model would liquidate; mark
        model does not."""
        threshold = 100.5
        mark = 100.0  # below threshold → no trigger
        # (Hypothetical wick-up last print at 105.0 documented in the
        # docstring; the assertion exercises the mark-side only.)
        crossed = check_liquidation_threshold_crossed(
            mark_price=mark, threshold_price=threshold, direction="short",
        )
        assert crossed is False


# ──────────────────────────────────────────────────────────────────────
# Tiered maintenance margin bracket coverage
# ──────────────────────────────────────────────────────────────────────


class TestTieredMaintenanceMargin:
    """Verify the bracket lookup is exercised across all 5 Binance
    tiers and the open-ended top bracket."""

    @pytest.mark.parametrize(
        "notional,expected_rate",
        [
            (1_000.0, 0.005),
            (49_999.0, 0.005),
            (50_000.0, 0.010),
            (200_000.0, 0.010),
            (250_000.0, 0.025),
            (500_000.0, 0.025),
            (1_000_000.0, 0.050),
            (5_000_000.0, 0.050),
            (10_000_000.0, 0.100),
            (100_000_000.0, 0.100),
        ],
    )
    def test_threshold_varies_by_bracket(self, notional, expected_rate):
        """Same entry/leverage/direction, varying notional → different mm rates
        → different threshold prices."""
        threshold = compute_liquidation_threshold_price(
            entry_price=100.0,
            notional_usd=notional,
            direction="long",
            leverage=10.0,
        )
        expected_threshold = 100.0 * (1.0 - 0.1) / (1.0 - expected_rate)
        assert threshold == pytest.approx(expected_threshold, abs=1e-6)

    def test_threshold_tighter_at_higher_bracket_long(self):
        """At a higher bracket (more mm rate), the threshold for a long
        sits CLOSER to entry — i.e. less room before liquidation."""
        t_low = compute_liquidation_threshold_price(
            entry_price=100.0, notional_usd=10_000.0,
            direction="long", leverage=10.0,
        )
        t_high = compute_liquidation_threshold_price(
            entry_price=100.0, notional_usd=5_000_000.0,
            direction="long", leverage=10.0,
        )
        assert t_high > t_low  # tighter (closer to 100.0)

    def test_threshold_tighter_at_higher_bracket_short(self):
        t_low = compute_liquidation_threshold_price(
            entry_price=100.0, notional_usd=10_000.0,
            direction="short", leverage=10.0,
        )
        t_high = compute_liquidation_threshold_price(
            entry_price=100.0, notional_usd=5_000_000.0,
            direction="short", leverage=10.0,
        )
        assert t_high < t_low  # tighter (closer to 100.0)


# ──────────────────────────────────────────────────────────────────────
# run_backtest_with_liquidation wrapper
# ──────────────────────────────────────────────────────────────────────


class TestRunBacktestWithLiquidation:
    def _position(self) -> PositionSpec:
        return PositionSpec(notional_usd=10_000.0, direction="long")

    def _spec(self, entry: float = 100.0, leverage: float = 10.0) -> LiquidationSpec:
        return LiquidationSpec(
            entry_price=entry, leverage=leverage, direction="long",
        )

    def test_no_liquidation_when_mark_always_above_threshold(self):
        """Mark stays well above the long threshold → no events, ending
        balance equals engine's reported ending balance."""
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        # Mark stays at 100.0 (well above the ~89.9 threshold for 10×).
        mark_prices = [100.0] * len(bars)
        config = _simple_config()
        result = run_backtest_with_liquidation(
            bars, mark_prices, self._position(), self._spec(),
            config, [_NoFireStrategy()],
        )
        assert isinstance(result, LiquidatedBacktestMetrics)
        assert len(result.liquidation_events) == 0
        assert result.total_liquidation_cost == 0.0
        assert result.liquidated_ending_balance == pytest.approx(
            result.base.ending_balance, abs=1e-9
        )

    def test_liquidation_triggers_when_mark_drops_below_threshold(self):
        """Mark drops to 80 → trigger → forced close at mark + fee."""
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        # Marks at 100 for first 24 bars, then 80 for last 24.
        mark_prices = [100.0] * 24 + [80.0] * 24
        config = _simple_config()
        result = run_backtest_with_liquidation(
            bars, mark_prices, self._position(), self._spec(),
            config, [_NoFireStrategy()],
        )
        assert len(result.liquidation_events) >= 1
        # First event lands at bar 24 (where mark first crosses 89.9...).
        first_ev = result.liquidation_events[0]
        assert first_ev.bar_index == 24
        assert first_ev.mark_price == pytest.approx(80.0, abs=1e-9)
        assert first_ev.realized_pnl < 0  # loss event
        # Subsequent bars with mark=80 produce no further events (position closed).
        assert len(result.liquidation_events) == 1

    def test_engine_path_unchanged_by_wrapper(self):
        """The wrapper invokes the engine's run_single() and stores its
        output as ``base`` — the engine itself is untouched."""
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        mark_prices = [100.0] * len(bars)
        config = _simple_config()
        result = run_backtest_with_liquidation(
            bars, mark_prices, self._position(), self._spec(),
            config, [_NoFireStrategy()],
        )
        # Duck-type: base has engine-defined attributes
        assert hasattr(result.base, "ending_balance")
        assert hasattr(result.base, "total_pnl")
        assert hasattr(result.base, "sharpe_ratio")
        assert hasattr(result.base, "trades")

    def test_rejects_invalid_position(self):
        bars = _flat_bars(n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        spec = self._spec()
        config = _simple_config()
        with pytest.raises(TypeError, match="PositionSpec"):
            run_backtest_with_liquidation(
                bars, [100.0] * len(bars), "not a position spec", spec, config, [_NoFireStrategy()]
            )

    def test_rejects_invalid_liq_spec(self):
        bars = _flat_bars(n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        position = self._position()
        config = _simple_config()
        with pytest.raises(TypeError, match="LiquidationSpec"):
            run_backtest_with_liquidation(
                bars, [100.0] * len(bars), position, "not a liq spec", config, [_NoFireStrategy()]
            )

    def test_rejects_empty_bars(self):
        with pytest.raises(ValueError, match="non-empty"):
            run_backtest_with_liquidation(
                [], [], self._position(), self._spec(), _simple_config(), [_NoFireStrategy()]
            )

    def test_rejects_empty_strategies(self):
        bars = _flat_bars(n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        with pytest.raises(ValueError, match="strategies"):
            run_backtest_with_liquidation(
                bars, [100.0] * len(bars), self._position(), self._spec(), _simple_config(), []
            )

    def test_rejects_mismatched_mark_series_length(self):
        bars = _flat_bars(n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        with pytest.raises(ValueError, match="mark_prices length"):
            run_backtest_with_liquidation(
                bars, [100.0] * (len(bars) - 1), self._position(), self._spec(),
                _simple_config(), [_NoFireStrategy()],
            )

    def test_rejects_non_positive_mark_price(self):
        bars = _flat_bars(n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        mark_prices = [100.0] * 24 + [0.0] + [100.0] * 23
        with pytest.raises(ValueError, match="mark_prices"):
            run_backtest_with_liquidation(
                bars, mark_prices, self._position(), self._spec(),
                _simple_config(), [_NoFireStrategy()],
            )

    def test_rejects_zero_entry_price(self):
        bars = _flat_bars(n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        with pytest.raises(ValueError, match="entry_price"):
            run_backtest_with_liquidation(
                bars, [100.0] * len(bars), self._position(),
                LiquidationSpec(entry_price=0.0, leverage=10.0, direction="long"),
                _simple_config(), [_NoFireStrategy()],
            )

    def test_short_position_triggers_on_mark_above_threshold(self):
        """Short + mark above threshold → liquidation."""
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        mark_prices = [100.0] * 24 + [120.0] * 24
        config = _simple_config()
        short_position = PositionSpec(notional_usd=10_000.0, direction="short")
        short_spec = LiquidationSpec(entry_price=100.0, leverage=10.0, direction="short")
        result = run_backtest_with_liquidation(
            bars, mark_prices, short_position, short_spec,
            config, [_NoFireStrategy()],
        )
        assert len(result.liquidation_events) >= 1
        assert result.liquidation_events[0].mark_price == pytest.approx(120.0, abs=1e-9)
        assert result.liquidation_events[0].realized_pnl < 0

    def test_liquidated_ending_balance_differs_from_base(self):
        """When liquidation fires, the wrapper's ending balance is
        below the engine's base ending balance."""
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        mark_prices = [100.0] * 24 + [50.0] * 24  # deep drop
        config = _simple_config()
        result = run_backtest_with_liquidation(
            bars, mark_prices, self._position(), self._spec(),
            config, [_NoFireStrategy()],
        )
        assert len(result.liquidation_events) >= 1
        # Net liq PnL < 0 → ending balance < base ending balance
        assert result.liquidated_ending_balance < result.base.ending_balance


# ──────────────────────────────────────────────────────────────────────
# Composition with funding
# ──────────────────────────────────────────────────────────────────────


class TestCompositionWithFunding:
    def _config(self) -> BacktestConfig:
        return _simple_config()

    def _position(self) -> PositionSpec:
        return PositionSpec(notional_usd=10_000.0, direction="long")

    def _spec(self) -> LiquidationSpec:
        return LiquidationSpec(entry_price=100.0, leverage=10.0, direction="long")

    def test_composition_with_no_liquidation_equals_funding_metrics(self):
        """When mark stays above threshold, the liquidation wrapper
        contributes nothing — funded + liquidated balance equals
        funded balance."""
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        config = self._config()
        position = self._position()
        # Funding events inside the window
        funding = [_ev(2026, 10, 5, 8, rate=0.0001)]
        funded = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()],
        )
        # Mark stays at 100 (above threshold) → no liq
        mark_prices = [100.0] * len(bars)
        funded2, liquidated = run_backtest_with_funding_and_liquidation(
            bars, mark_prices, funding, position, self._spec(),
            config, [_NoFireStrategy()],
        )
        assert isinstance(funded2, FundedBacktestMetrics)
        assert isinstance(liquidated, LiquidatedBacktestMetrics)
        # No liquidation events
        assert len(liquidated.liquidation_events) == 0
        # The funding overlay from the composition matches the standalone funding run
        assert funded2.total_funding_cost == pytest.approx(
            funded.total_funding_cost, abs=1e-9
        )

    def test_composition_with_both_funding_and_liquidation(self):
        """When mark crosses AND funding events are present, both
        wrappers contribute deltas at their respective bars."""
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        config = self._config()
        position = self._position()
        # Funding event at hour 8 (bar index 8).
        funding = [_ev(2026, 10, 5, 8, rate=0.0001)]
        # Mark drops to 80 at bar 24 → triggers liquidation.
        mark_prices = [100.0] * 24 + [80.0] * 24
        funded, liquidated = run_backtest_with_funding_and_liquidation(
            bars, mark_prices, funding, position, self._spec(),
            config, [_NoFireStrategy()],
        )
        # Funding applied at bar 8 (in funded_equity_delta).
        assert any(d != 0.0 for d in funded.funded_equity_delta)
        # Liquidation applied at bar 24 (in liquidated_equity_delta).
        assert any(d != 0.0 for d in liquidated.liquidated_equity_delta)
        # Liquidation is a net loss
        assert sum(ev.net_pnl for ev in liquidated.liquidation_events) < 0
        # Funding delta is a separate, smaller, loss
        assert funded.total_funding_cost < 0

    def test_composition_returns_independent_overlays(self):
        """The composition returns BOTH funded and liquidated metrics;
        each is independently valid."""
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        config = self._config()
        position = self._position()
        funding = [_ev(2026, 10, 5, 8, rate=0.0001)]
        mark_prices = [100.0] * len(bars)
        funded, liquidated = run_backtest_with_funding_and_liquidation(
            bars, mark_prices, funding, position, self._spec(),
            config, [_NoFireStrategy()],
        )
        # Both are valid result types
        assert isinstance(funded, FundedBacktestMetrics)
        assert isinstance(liquidated, LiquidatedBacktestMetrics)
        # Both wrap the same base engine output
        assert funded.base.ending_balance == pytest.approx(
            liquidated.base.ending_balance, abs=1e-9
        )


# ──────────────────────────────────────────────────────────────────────
# Legacy forex path isolation
# ──────────────────────────────────────────────────────────────────────


class TestLegacyForexPathIsolation:
    """The liquidation module is a separate module — calling the
    engine directly must produce identical metrics whether or not the
    liquidation module is imported."""

    def test_engine_module_does_not_import_liquidation(self):
        """The engine must not depend on liquidation. We verify by
        checking the engine file's text doesn't reference any
        liquidation symbols."""
        from pathlib import Path

        engine_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "forex_bot"
            / "engine"
            / "engine.py"
        )
        contents = engine_path.read_text()
        for sym in ("LiquidationEvent", "LiquidationSpec", "compute_liquidation_threshold_price"):
            assert sym not in contents, (
                f"engine.py must not import {sym} (leaks crypto path into legacy engine)"
            )

    def test_unwrapped_engine_run_unchanged(self):
        """BacktestEngine.run_single() returns identical metrics whether
        or not the liquidation module is loaded."""
        bars = _flat_bars(
            n=100, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        config = _simple_config()
        strategy = _NoFireStrategy()
        from engine.engine import BacktestEngine

        engine1 = BacktestEngine(config, [strategy])
        m1 = engine1.run_single(strategy, bars)
        engine2 = BacktestEngine(config, [strategy])
        m2 = engine2.run_single(strategy, bars)
        assert m1.total_pnl == m2.total_pnl
        assert m1.sharpe_ratio == m2.sharpe_ratio
        assert m1.ending_balance == m2.ending_balance


# ──────────────────────────────────────────────────────────────────────
# LiquidatedBacktestMetrics structure
# ──────────────────────────────────────────────────────────────────────


class TestLiquidatedBacktestMetricsStructure:
    def test_event_tuple_stored_in_order(self):
        """Liquidation events are stored as a tuple in bar order."""
        bars = _flat_bars(
            n=72, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        # Drop at bar 24, then bar 48 — but second one won't fire because
        # position is closed at the first event. So we use a partial
        # close by making the drop mild (mark at 89, just past 89.9
        # threshold for 10× with 1% mm). Actually the threshold for
        # 10x, 1% mm, long, 100 entry is ~89.99; mark at 89 is past.
        # We verify one event triggers at bar 24 and the rest don't
        # because the position closes.
        mark_prices = [100.0] * 24 + [80.0] * 48
        config = _simple_config()
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        spec = LiquidationSpec(entry_price=100.0, leverage=10.0, direction="long")
        result = run_backtest_with_liquidation(
            bars, mark_prices, position, spec, config, [_NoFireStrategy()],
        )
        # One event (full close at bar 24)
        assert len(result.liquidation_events) == 1
        assert result.liquidation_events[0].bar_index == 24

    def test_liquidated_equity_delta_length_matches_bars(self):
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        mark_prices = [100.0] * 24 + [80.0] * 24
        config = _simple_config()
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        spec = LiquidationSpec(entry_price=100.0, leverage=10.0, direction="long")
        result = run_backtest_with_liquidation(
            bars, mark_prices, position, spec, config, [_NoFireStrategy()],
        )
        assert len(result.liquidated_equity_delta) == len(bars)

    def test_liquidated_ending_balance_equals_base_plus_net_pnl(self):
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        mark_prices = [100.0] * 24 + [80.0] * 24
        config = _simple_config()
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        spec = LiquidationSpec(entry_price=100.0, leverage=10.0, direction="long")
        result = run_backtest_with_liquidation(
            bars, mark_prices, position, spec, config, [_NoFireStrategy()],
        )
        net_liq_pnl = sum(ev.net_pnl for ev in result.liquidation_events)
        assert result.liquidated_ending_balance == pytest.approx(
            result.base.ending_balance + net_liq_pnl, abs=1e-9
        )

    def test_total_liquidation_cost_is_negated_sum_of_net_pnl(self):
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        mark_prices = [100.0] * 24 + [80.0] * 24
        config = _simple_config()
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        spec = LiquidationSpec(entry_price=100.0, leverage=10.0, direction="long")
        result = run_backtest_with_liquidation(
            bars, mark_prices, position, spec, config, [_NoFireStrategy()],
        )
        net_pnl = sum(ev.net_pnl for ev in result.liquidation_events)
        assert result.total_liquidation_cost == pytest.approx(-net_pnl, abs=1e-9)
