"""Tests for ``forex_bot.backtest.vol_target`` — vol-target + DD ladder overlays.

Card: 37443f97-ddea-4f5b-b280-fc7b3d968d98 (Sprint D 4)
Sprint: 2026-10-06-avaos-ui-rework
Lane: [BUILD]

Scope (HR5 — targeted tests only):

  * Module constants — annualization factors, default DD tiers,
    default vol target.
  * VolTargetConfig validation — target, lookback, annualization,
    realized-vol floor, scale clamps, warmup, name.
  * DrawdownTier validation — threshold, fraction.
  * DrawdownLadderConfig validation — tier contiguity, starting
    equity, min_risk_fraction floor, name.
  * Math primitives:
      - compute_log_returns: empty, single, ascending, descending,
        non-positive/NaN rejection.
      - infer_annualization_factor: 1h, 4h, daily, weekly, sub-hourly.
      - compute_realized_vol_series: zero returns, window too short,
        scaling, warmup.
      - compute_vol_scale_series: null path (all 1.0), clamp at
        max_scale, clamp at min_scale, warmup gate.
      - compute_high_water_mark_series: monotonic up, monotonic
        down, mixed, starting_equity anchor.
      - compute_dd_series: at HWM (0%), full loss (100%), clamping
        above zero.
      - lookup_dd_risk_fraction: tier boundaries (inclusive /
        exclusive semantics), min_risk_fraction floor.
      - compute_dd_risk_series: null path (all 1.0), tier
        transitions across the equity curve.
      - compute_composite_risk_scale_series: multiplicative
        composition, length mismatch rejection.
  * Config loaders (fail-loud):
      - load_vol_target_config: required keys, type errors,
        clamping failures.
      - load_drawdown_ladder_config: tier parsing, first tier
        must start at 0.0, contiguity.
  * Engine integration:
      - run_backtest_with_vol_target: legacy FX path (None config,
        all-1.0 scale), active config produces scale series,
        engine metrics byte-identical to the no-overlay run.
      - run_backtest_with_drawdown_ladder: legacy FX path,
        active config produces tier-driven risk series.
      - run_backtest_with_vol_target_and_dd: multiplicative
        composition, null case (both configs None).
  * Composition with full_crypto_overlay (byte-equality):
      - When vol_target_config is None and dd_config is None,
        the wrapper's base.ending_balance matches the full overlay's
        base.ending_balance.
      - The cost overlays' series are unaffected by vol-target/DD
        state (byte-identical with and without vol-target/DD).
  * Degenerate inputs:
      - Empty bars, empty strategies, mismatched lengths.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup: src/forex_bot on sys.path so ``backtest.vol_target`` and
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

from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from backtest.funding_model import FundingEvent, PositionSpec  # noqa: E402
from backtest.integrity_gate import (  # noqa: E402
    IntegrityConfig,
)
from backtest.liquidation import LiquidationSpec  # noqa: E402
from backtest.strategies.isignal_strategy import ISignalStrategy  # noqa: E402
from backtest.types import BacktestConfig, Bar  # noqa: E402
from backtest.venue_costs import (  # noqa: E402
    DEFAULT_BINANCE_USDM_FEE_CONFIG,
    VenueOrderFill,
    run_backtest_with_full_crypto_overlay,
)
from backtest.vol_target import (  # noqa: E402
    DEFAULT_ANNUALIZATION_FACTOR_1H,
    DEFAULT_ANNUALIZATION_FACTOR_4H,
    DEFAULT_ANNUALIZATION_FACTOR_DAILY,
    DEFAULT_DD_TIERS,
    DEFAULT_LOOKBACK_1H,
    DEFAULT_LOOKBACK_4H,
    DEFAULT_LOOKBACK_DAILY,
    DEFAULT_VOL_TARGET,
    DrawdownLadderBacktestMetrics,
    DrawdownLadderConfig,
    DrawdownTier,
    VolTargetBacktestMetrics,
    VolTargetConfig,
    VolTargetDDBacktestMetrics,
    VolTargetError,
    compute_composite_risk_scale_series,
    compute_dd_risk_series,
    compute_dd_series,
    compute_high_water_mark_series,
    compute_log_returns,
    compute_realized_vol_series,
    compute_vol_scale_series,
    infer_annualization_factor,
    load_drawdown_ladder_config,
    load_vol_target_config,
    lookup_dd_risk_fraction,
    run_backtest_with_drawdown_ladder,
    run_backtest_with_full_crypto_overlay_and_vol_target,
    run_backtest_with_vol_target,
    run_backtest_with_vol_target_and_dd,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _flat_bars(
    n: int,
    start: datetime,
    step_minutes: int = 60,
    base_price: float = 100.0,
    closes: list[float] | None = None,
) -> list[Bar]:
    """Build N bars with constant OHLC. ``closes`` (when supplied)
    lets a test build a bar series with a specific close trajectory."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    bars: list[Bar] = []
    for i in range(n):
        t = start + timedelta(minutes=step_minutes * i)
        if closes is not None:
            cp = closes[i]
        else:
            cp = base_price
        bars.append(
            Bar(
                time=t,
                open=base_price,
                high=max(base_price, cp),
                low=min(base_price, cp),
                close=cp,
                volume=100.0,
                spread_pips=1.5,
            )
        )
    return bars


def _uptrend_bars(
    n: int,
    start: datetime,
    step_minutes: int = 60,
    base_price: float = 100.0,
    pct_per_bar: float = 0.01,
) -> list[Bar]:
    """N bars with exponentially growing close prices."""
    closes = [base_price * (1.0 + pct_per_bar) ** i for i in range(n)]
    return _flat_bars(
        n=n,
        start=start,
        step_minutes=step_minutes,
        base_price=base_price,
        closes=closes,
    )


def _vol_bars(
    n: int,
    start: datetime,
    step_minutes: int = 60,
    base_price: float = 100.0,
    sigma: float = 0.02,
    seed: int = 42,
) -> list[Bar]:
    """N bars with random log-normal returns (deterministic seed)."""
    import random
    rng = random.Random(seed)  # noqa: S311 — deterministic test fixtures
    closes = [base_price]
    for _ in range(n - 1):
        ret = rng.gauss(0.0, sigma)
        closes.append(closes[-1] * math.exp(ret))
    return _flat_bars(
        n=n,
        start=start,
        step_minutes=step_minutes,
        base_price=base_price,
        closes=closes,
    )


def _simple_config(
    starting_balance: float = 10_000.0,
    pair: str = "BTCUSDT",
    min_bars_before_signal: int = 30,
) -> BacktestConfig:
    """A BacktestConfig that triggers no trades on flat bars — used to
    isolate vol-target / DD overlays from strategy P&L."""
    return BacktestConfig(
        starting_balance=starting_balance,
        risk_per_trade_pct=0.01,
        max_daily_drawdown_pct=0.05,
        max_total_drawdown_pct=0.10,
        commission_per_lot=0.0,
        leverage=100,
        min_confidence=0.99,
        min_bars_before_signal=min_bars_before_signal,
        max_open_trades=1,
        pair=pair,
        spread_pips=0.0,
        round_trip_spread=False,
        slippage_pips=0.0,
        swap_per_lot_per_day=0.0,
    )


def _integrity_config() -> IntegrityConfig:
    """An IntegrityConfig that passes for clean bar series."""
    return IntegrityConfig(
        expected_cadence_minutes=60,
        gap_tolerance_multiplier=1.5,
        check_ohlc_invariants=True,
        check_nan_values=True,
        check_gaps=True,
        check_delistings=False,  # no expected_window_end
        check_anomalies=True,
    )


class _NoFireStrategy(ISignalStrategy):
    """A trivial strategy that never produces a signal."""

    @property
    def name(self) -> str:
        return "NoFire"

    def evaluate(self, state: Any) -> None:
        return None


class _AlwaysLongStrategy(ISignalStrategy):
    """A trivial strategy that always signals long at every bar.

    Used to exercise the engine's trade path with controlled P&L.
    """

    def __init__(self, name: str = "AlwaysLong") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def evaluate(self, state: Any) -> Any:
        from backtest.types import StrategySignal, TradeDirection
        bar = state.latest_bar
        return StrategySignal(
            direction=TradeDirection.LONG,
            confidence=0.99,
            entry_price=bar.close,
            stop_loss=bar.close * 0.99,
            take_profit_1=bar.close * 1.005,
            take_profit_2=bar.close * 1.01,
            take_profit_3=bar.close * 1.02,
            rationale="always-long-test",
        )


# ──────────────────────────────────────────────────────────────────────
# Module-level constants
# ──────────────────────────────────────────────────────────────────────


class TestModuleConstants:
    def test_default_annualization_factors(self):
        assert DEFAULT_ANNUALIZATION_FACTOR_1H == 8760   # 24 * 365
        assert DEFAULT_ANNUALIZATION_FACTOR_4H == 2190   # 6 * 365
        assert DEFAULT_ANNUALIZATION_FACTOR_DAILY == 365

    def test_default_lookback_windows(self):
        assert DEFAULT_LOOKBACK_1H == 60
        assert DEFAULT_LOOKBACK_4H == 60
        assert DEFAULT_LOOKBACK_DAILY == 30

    def test_default_vol_target_is_10pct(self):
        assert DEFAULT_VOL_TARGET == pytest.approx(0.10, abs=1e-12)

    def test_default_dd_tiers_three_tiers(self):
        assert len(DEFAULT_DD_TIERS) == 3

    def test_default_dd_tiers_match_spec(self):
        """Spec example: full risk < 5%, 50% at 5-10%, 25% beyond."""
        expected = [(0.00, 1.00), (0.05, 0.50), (0.10, 0.25)]
        actual = [(t.dd_threshold, t.risk_fraction) for t in DEFAULT_DD_TIERS]
        assert actual == expected

    def test_default_dd_tiers_first_threshold_is_zero(self):
        assert DEFAULT_DD_TIERS[0].dd_threshold == 0.0

    def test_default_dd_tiers_strictly_ascending(self):
        thresholds = [t.dd_threshold for t in DEFAULT_DD_TIERS]
        for i in range(1, len(thresholds)):
            assert thresholds[i] > thresholds[i - 1]

    def test_default_dd_tiers_all_fractions_in_unit_interval(self):
        for t in DEFAULT_DD_TIERS:
            assert 0.0 <= t.risk_fraction <= 1.0


# ──────────────────────────────────────────────────────────────────────
# VolTargetConfig validation
# ──────────────────────────────────────────────────────────────────────


class TestVolTargetConfigValidation:
    def test_default_construction(self):
        cfg = VolTargetConfig()
        assert cfg.target_annualized_vol == DEFAULT_VOL_TARGET
        assert cfg.lookback_window == DEFAULT_LOOKBACK_1H
        assert cfg.annualization_factor == DEFAULT_ANNUALIZATION_FACTOR_1H

    def test_round_trip(self):
        cfg = VolTargetConfig(
            target_annualized_vol=0.15,
            lookback_window=30,
            annualization_factor=2190,
            min_realized_vol=1e-3,
            max_scale=2.0,
            min_scale=0.5,
            warmup_bars=20,
            name="custom",
        )
        assert cfg.target_annualized_vol == 0.15
        assert cfg.lookback_window == 30
        assert cfg.annualization_factor == 2190
        assert cfg.min_realized_vol == 1e-3
        assert cfg.max_scale == 2.0
        assert cfg.min_scale == 0.5
        assert cfg.warmup_bars == 20
        assert cfg.name == "custom"

    def test_rejects_zero_target(self):
        with pytest.raises(VolTargetError, match="target_annualized_vol"):
            VolTargetConfig(target_annualized_vol=0.0)

    def test_rejects_negative_target(self):
        with pytest.raises(VolTargetError, match="target_annualized_vol"):
            VolTargetConfig(target_annualized_vol=-0.01)

    def test_rejects_nonfinite_target(self):
        with pytest.raises(VolTargetError, match="target_annualized_vol"):
            VolTargetConfig(target_annualized_vol=math.inf)
        with pytest.raises(VolTargetError, match="target_annualized_vol"):
            VolTargetConfig(target_annualized_vol=math.nan)

    def test_rejects_lookback_lt_2(self):
        with pytest.raises(VolTargetError, match="lookback_window"):
            VolTargetConfig(lookback_window=1)
        with pytest.raises(VolTargetError, match="lookback_window"):
            VolTargetConfig(lookback_window=0)

    def test_rejects_annualization_factor_lt_1(self):
        with pytest.raises(VolTargetError, match="annualization_factor"):
            VolTargetConfig(annualization_factor=0)

    def test_rejects_zero_min_realized_vol(self):
        with pytest.raises(VolTargetError, match="min_realized_vol"):
            VolTargetConfig(min_realized_vol=0.0)

    def test_rejects_negative_min_realized_vol(self):
        with pytest.raises(VolTargetError, match="min_realized_vol"):
            VolTargetConfig(min_realized_vol=-0.001)

    def test_rejects_max_scale_lt_1(self):
        with pytest.raises(VolTargetError, match="max_scale"):
            VolTargetConfig(max_scale=0.99)

    def test_rejects_negative_min_scale(self):
        with pytest.raises(VolTargetError, match="min_scale"):
            VolTargetConfig(min_scale=-0.1)

    def test_rejects_max_lt_min(self):
        with pytest.raises(VolTargetError, match="max_scale"):
            VolTargetConfig(min_scale=1.0, max_scale=0.5)

    def test_rejects_negative_warmup_bars(self):
        with pytest.raises(VolTargetError, match="warmup_bars"):
            VolTargetConfig(warmup_bars=-1)

    def test_rejects_empty_name(self):
        with pytest.raises(VolTargetError, match="name"):
            VolTargetConfig(name="")
        with pytest.raises(VolTargetError, match="name"):
            VolTargetConfig(name="   ")

    def test_nonfinite_min_scale_rejected(self):
        with pytest.raises(VolTargetError, match="min_scale"):
            VolTargetConfig(min_scale=math.inf)

    def test_nonfinite_max_scale_rejected(self):
        with pytest.raises(VolTargetError, match="max_scale"):
            VolTargetConfig(max_scale=math.inf)


# ──────────────────────────────────────────────────────────────────────
# DrawdownTier validation
# ──────────────────────────────────────────────────────────────────────


class TestDrawdownTierValidation:
    def test_round_trip(self):
        t = DrawdownTier(dd_threshold=0.05, risk_fraction=0.5)
        assert t.dd_threshold == pytest.approx(0.05)
        assert t.risk_fraction == pytest.approx(0.5)

    def test_zero_threshold_is_valid(self):
        """The first tier in a ladder must have dd_threshold == 0."""
        t = DrawdownTier(dd_threshold=0.0, risk_fraction=1.0)
        assert t.dd_threshold == 0.0

    def test_one_threshold_is_valid(self):
        """100% drawdown is allowed (degenerate edge)."""
        t = DrawdownTier(dd_threshold=1.0, risk_fraction=0.0)
        assert t.risk_fraction == 0.0

    def test_rejects_negative_threshold(self):
        with pytest.raises(VolTargetError, match="dd_threshold"):
            DrawdownTier(dd_threshold=-0.01, risk_fraction=0.5)

    def test_rejects_fraction_above_one(self):
        with pytest.raises(VolTargetError, match="risk_fraction"):
            DrawdownTier(dd_threshold=0.0, risk_fraction=1.5)

    def test_rejects_fraction_below_zero(self):
        with pytest.raises(VolTargetError, match="risk_fraction"):
            DrawdownTier(dd_threshold=0.0, risk_fraction=-0.1)

    def test_rejects_nonfinite_threshold(self):
        with pytest.raises(VolTargetError, match="dd_threshold"):
            DrawdownTier(dd_threshold=math.inf, risk_fraction=0.5)

    def test_rejects_nonfinite_fraction(self):
        with pytest.raises(VolTargetError, match="risk_fraction"):
            DrawdownTier(dd_threshold=0.0, risk_fraction=math.nan)


# ──────────────────────────────────────────────────────────────────────
# DrawdownLadderConfig validation
# ──────────────────────────────────────────────────────────────────────


class TestDrawdownLadderConfigValidation:
    def test_default_construction(self):
        cfg = DrawdownLadderConfig()
        assert cfg.tiers == DEFAULT_DD_TIERS

    def test_round_trip(self):
        tiers = (
            DrawdownTier(0.0, 1.0),
            DrawdownTier(0.05, 0.5),
        )
        cfg = DrawdownLadderConfig(
            tiers=tiers,
            starting_equity=10_000.0,
            min_risk_fraction=0.1,
            name="custom",
        )
        assert cfg.tiers == tiers
        assert cfg.starting_equity == 10_000.0
        assert cfg.min_risk_fraction == pytest.approx(0.1)
        assert cfg.name == "custom"

    def test_rejects_empty_tiers(self):
        with pytest.raises(VolTargetError, match="tiers"):
            DrawdownLadderConfig(tiers=())

    def test_rejects_first_tier_nonzero_threshold(self):
        with pytest.raises(VolTargetError, match="first tier"):
            DrawdownLadderConfig(tiers=(DrawdownTier(0.01, 1.0),))

    def test_rejects_nonascending_tiers(self):
        with pytest.raises(VolTargetError, match="ascending"):
            DrawdownLadderConfig(
                tiers=(
                    DrawdownTier(0.0, 1.0),
                    DrawdownTier(0.05, 0.5),
                    DrawdownTier(0.03, 0.25),  # out of order
                )
            )

    def test_rejects_equal_tier_thresholds(self):
        """Two tiers with the same dd_threshold would overlap at the
        boundary — the config validator requires strictly ascending."""
        with pytest.raises(VolTargetError, match="ascending"):
            DrawdownLadderConfig(
                tiers=(
                    DrawdownTier(0.0, 1.0),
                    DrawdownTier(0.05, 0.5),
                    DrawdownTier(0.05, 0.25),
                )
            )

    def test_rejects_negative_min_risk_fraction(self):
        with pytest.raises(VolTargetError, match="min_risk_fraction"):
            DrawdownLadderConfig(min_risk_fraction=-0.1)

    def test_rejects_min_risk_fraction_above_one(self):
        with pytest.raises(VolTargetError, match="min_risk_fraction"):
            DrawdownLadderConfig(min_risk_fraction=1.5)

    def test_rejects_nonfinite_min_risk_fraction(self):
        with pytest.raises(VolTargetError, match="min_risk_fraction"):
            DrawdownLadderConfig(min_risk_fraction=math.nan)

    def test_rejects_zero_starting_equity(self):
        with pytest.raises(VolTargetError, match="starting_equity"):
            DrawdownLadderConfig(starting_equity=0.0)

    def test_rejects_negative_starting_equity(self):
        with pytest.raises(VolTargetError, match="starting_equity"):
            DrawdownLadderConfig(starting_equity=-1.0)

    def test_rejects_nonfinite_starting_equity(self):
        with pytest.raises(VolTargetError, match="starting_equity"):
            DrawdownLadderConfig(starting_equity=math.inf)

    def test_rejects_empty_name(self):
        with pytest.raises(VolTargetError, match="name"):
            DrawdownLadderConfig(name="")


# ──────────────────────────────────────────────────────────────────────
# compute_log_returns
# ──────────────────────────────────────────────────────────────────────


class TestComputeLogReturns:
    def test_empty_bars(self):
        assert compute_log_returns([]) == []

    def test_single_bar(self):
        bars = _flat_bars(1, datetime(2024, 1, 1, tzinfo=timezone.utc))
        assert compute_log_returns(bars) == []

    def test_two_bars(self):
        bars = _flat_bars(
            2,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
            closes=[100.0, 110.0],
        )
        rets = compute_log_returns(bars)
        assert len(rets) == 1
        assert rets[0] == pytest.approx(math.log(1.10), abs=1e-12)

    def test_constant_closes_zero_returns(self):
        n = 20
        bars = _flat_bars(
            n,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
        )
        rets = compute_log_returns(bars)
        assert len(rets) == n - 1
        for r in rets:
            assert r == pytest.approx(0.0, abs=1e-12)

    def test_ascending_closes(self):
        """Monotonically increasing closes → positive log returns."""
        bars = _uptrend_bars(
            10,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
            pct_per_bar=0.01,
        )
        rets = compute_log_returns(bars)
        for r in rets:
            assert r > 0

    def test_descending_closes(self):
        """Monotonically decreasing closes → negative log returns."""
        bars = _uptrend_bars(
            10,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
            pct_per_bar=-0.01,
        )
        rets = compute_log_returns(bars)
        for r in rets:
            assert r < 0

    def test_length_matches_bars_minus_one(self):
        n = 50
        bars = _vol_bars(n, datetime(2024, 1, 1, tzinfo=timezone.utc))
        rets = compute_log_returns(bars)
        assert len(rets) == n - 1

    def test_rejects_zero_price(self):
        bars = _flat_bars(
            2,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
            closes=[100.0, 0.0],
        )
        with pytest.raises(VolTargetError, match="non-positive"):
            compute_log_returns(bars)

    def test_rejects_negative_price(self):
        bars = _flat_bars(
            2,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
            closes=[100.0, -50.0],
        )
        with pytest.raises(VolTargetError, match="non-positive"):
            compute_log_returns(bars)

    def test_rejects_nan_price(self):
        bars = _flat_bars(
            2,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
            closes=[100.0, math.nan],
        )
        with pytest.raises(VolTargetError, match="non-finite"):
            compute_log_returns(bars)


# ──────────────────────────────────────────────────────────────────────
# infer_annualization_factor
# ──────────────────────────────────────────────────────────────────────


class TestInferAnnualizationFactor:
    def test_empty_returns_default(self):
        assert infer_annualization_factor([]) == DEFAULT_ANNUALIZATION_FACTOR_1H

    def test_single_bar_returns_default(self):
        bars = _flat_bars(1, datetime(2024, 1, 1, tzinfo=timezone.utc))
        assert infer_annualization_factor(bars) == DEFAULT_ANNUALIZATION_FACTOR_1H

    def test_1h_bars(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc), step_minutes=60)
        assert infer_annualization_factor(bars) == DEFAULT_ANNUALIZATION_FACTOR_1H

    def test_4h_bars(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc), step_minutes=240)
        assert infer_annualization_factor(bars) == DEFAULT_ANNUALIZATION_FACTOR_4H

    def test_daily_bars(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc), step_minutes=1440)
        assert infer_annualization_factor(bars) == DEFAULT_ANNUALIZATION_FACTOR_DAILY

    def test_15m_bars(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc), step_minutes=15)
        # 15m: 4 × 24 × 365 = 35040
        assert infer_annualization_factor(bars) == 35040


# ──────────────────────────────────────────────────────────────────────
# compute_realized_vol_series
# ──────────────────────────────────────────────────────────────────────


class TestComputeRealizedVolSeries:
    def test_empty_bars(self):
        cfg = VolTargetConfig()
        assert compute_realized_vol_series([], cfg) == []

    def test_constant_prices_zero_vol(self):
        """No price movement → realized vol is 0 (after warmup)."""
        bars = _flat_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
        )
        cfg = VolTargetConfig(lookback_window=20, warmup_bars=20)
        vol = compute_realized_vol_series(bars, cfg)
        # Bars before warmup are 0.
        for v in vol[:20]:
            assert v == 0.0
        # Bars after warmup should still be ~0 (constant prices → stdev 0).
        for v in vol[20:]:
            assert v == pytest.approx(0.0, abs=1e-12)

    def test_window_too_short_returns_zero(self):
        """If ``bars < lookback_window + 1``, no window is realized."""
        bars = _flat_bars(
            5,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        cfg = VolTargetConfig(lookback_window=60)
        vol = compute_realized_vol_series(bars, cfg)
        assert all(v == 0.0 for v in vol)

    def test_realized_vol_is_annualized(self):
        """Random returns → realized vol roughly matches the input sigma × sqrt(AF)."""
        bars = _vol_bars(
            1000,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.02,
        )
        cfg = VolTargetConfig(
            lookback_window=60,
            annualization_factor=DEFAULT_ANNUALIZATION_FACTOR_1H,
            warmup_bars=60,
        )
        vol = compute_realized_vol_series(bars, cfg)
        # Mean of post-warmup realized vol should be near
        # sigma × sqrt(AF) = 0.02 × sqrt(8760) ≈ 1.87.
        post = vol[60:]
        mean = sum(post) / len(post)
        expected = 0.02 * math.sqrt(8760)
        # Allow 30% tolerance — finite-sample estimator.
        assert mean == pytest.approx(expected, rel=0.30)

    def test_length_matches_bars(self):
        bars = _vol_bars(50, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = VolTargetConfig()
        vol = compute_realized_vol_series(bars, cfg)
        assert len(vol) == len(bars)

    def test_warmup_bars_larger_than_lookback(self):
        """Warmup > lookback still computes realized vol for the
        extra bars; the vol-target scale clamp handles the gap."""
        bars = _vol_bars(50, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = VolTargetConfig(lookback_window=10, warmup_bars=30)
        vol = compute_realized_vol_series(bars, cfg)
        # Bars [10, 30) should have non-zero realized vol (the estimator
        # is warm by then).
        assert any(v > 0 for v in vol[10:30])

    def test_rejects_non_vol_target_config(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc))
        with pytest.raises(TypeError):
            compute_realized_vol_series(bars, "not-a-config")  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# compute_vol_scale_series
# ──────────────────────────────────────────────────────────────────────


class TestComputeVolScaleSeries:
    def test_null_config_returns_all_ones(self):
        """Legacy FX path: None config → all-1.0 scale."""
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        scale = compute_vol_scale_series(bars, None)
        assert len(scale) == len(bars)
        assert all(s == pytest.approx(1.0, abs=1e-12) for s in scale)

    def test_warmup_bars_get_min_scale(self):
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.02,
        )
        cfg = VolTargetConfig(
            lookback_window=20,
            warmup_bars=20,
            target_annualized_vol=0.10,
            min_scale=0.0,
            max_scale=4.0,
        )
        scale = compute_vol_scale_series(bars, cfg)
        for s in scale[:20]:
            assert s == 0.0

    def test_post_warmup_scales_around_target(self):
        """With sigma such that realized vol ≈ 0.5 (annualized),
        target_vol=0.10 → scale ≈ 0.20."""
        bars = _vol_bars(
            500,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.02,
        )
        cfg = VolTargetConfig(
            lookback_window=60,
            warmup_bars=60,
            target_annualized_vol=0.10,
            min_realized_vol=1e-6,
            min_scale=0.0,
            max_scale=4.0,
            annualization_factor=DEFAULT_ANNUALIZATION_FACTOR_1H,
        )
        scale = compute_vol_scale_series(bars, cfg)
        post = scale[60:]
        # Mean post-warmup scale should be near target_vol / realized_vol.
        mean_scale = sum(post) / len(post)
        # Expected scale = 0.10 / 1.87 ≈ 0.053.
        assert mean_scale == pytest.approx(0.053, rel=0.40)

    def test_max_scale_clamps(self):
        """When realized vol is far below target, scale caps at max_scale."""
        bars = _flat_bars(
            200,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
        )
        cfg = VolTargetConfig(
            lookback_window=20,
            warmup_bars=20,
            target_annualized_vol=0.50,
            min_realized_vol=1e-6,
            max_scale=4.0,
            min_scale=0.0,
        )
        scale = compute_vol_scale_series(bars, cfg)
        # Constant prices → realized vol ~0 → target/min_vol = huge → clamp at max_scale.
        for s in scale[20:]:
            assert s == pytest.approx(4.0, abs=1e-12)

    def test_min_scale_clamps(self):
        """When realized vol is far above target, scale clamps at min_scale."""
        bars = _vol_bars(
            200,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.20,  # huge vol
        )
        cfg = VolTargetConfig(
            lookback_window=20,
            warmup_bars=20,
            target_annualized_vol=0.01,  # very low target
            min_realized_vol=1e-6,
            min_scale=0.1,  # explicit floor
            max_scale=10.0,
        )
        scale = compute_vol_scale_series(bars, cfg)
        for s in scale[20:]:
            # Either clamped at min_scale=0.1 or computed scale is even smaller (below 0.1).
            assert s >= 0.1 - 1e-12

    def test_min_realized_vol_floor(self):
        """Zero-vol regime uses min_realized_vol as divisor floor."""
        bars = _flat_bars(
            50,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
        )
        cfg = VolTargetConfig(
            lookback_window=10,
            warmup_bars=10,
            target_annualized_vol=0.10,
            min_realized_vol=0.01,  # 1% floor
            max_scale=10.0,
        )
        scale = compute_vol_scale_series(bars, cfg)
        for s in scale[10:]:
            # scale = 0.10 / max(0, 0.01) = 10.0
            assert s == pytest.approx(10.0, abs=1e-12)

    def test_length_matches_bars(self):
        bars = _vol_bars(50, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = VolTargetConfig()
        scale = compute_vol_scale_series(bars, cfg)
        assert len(scale) == len(bars)

    def test_empty_bars(self):
        cfg = VolTargetConfig()
        assert compute_vol_scale_series([], cfg) == []

    def test_rejects_non_vol_target_config(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc))
        with pytest.raises(TypeError):
            compute_vol_scale_series(bars, "not-a-config")  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# compute_high_water_mark_series
# ──────────────────────────────────────────────────────────────────────


class TestComputeHighWaterMarkSeries:
    def test_empty(self):
        assert compute_high_water_mark_series([]) == []

    def test_monotonically_increasing(self):
        ec = [100.0, 110.0, 120.0, 130.0, 140.0]
        hwm = compute_high_water_mark_series(ec)
        assert hwm == [100.0, 110.0, 120.0, 130.0, 140.0]

    def test_monotonically_decreasing(self):
        ec = [140.0, 130.0, 120.0, 110.0, 100.0]
        hwm = compute_high_water_mark_series(ec)
        assert hwm == [140.0, 140.0, 140.0, 140.0, 140.0]

    def test_mixed_with_drawdown_recovery(self):
        ec = [100.0, 120.0, 80.0, 90.0, 110.0, 95.0, 130.0]
        hwm = compute_high_water_mark_series(ec)
        expected = [100.0, 120.0, 120.0, 120.0, 120.0, 120.0, 130.0]
        assert hwm == expected

    def test_starting_equity_anchor_higher_than_first(self):
        """starting_equity > equity_curve[0] lifts the initial HWM."""
        ec = [100.0, 110.0, 120.0]
        hwm = compute_high_water_mark_series(ec, starting_equity=150.0)
        assert hwm == [150.0, 150.0, 150.0]

    def test_starting_equity_anchor_lower_than_first(self):
        """starting_equity < equity_curve[0]: HWM starts at curve[0]."""
        ec = [100.0, 110.0, 120.0]
        hwm = compute_high_water_mark_series(ec, starting_equity=50.0)
        assert hwm == [100.0, 110.0, 120.0]

    def test_rejects_zero_starting_equity(self):
        with pytest.raises(VolTargetError, match="initial"):
            compute_high_water_mark_series([100.0], starting_equity=0.0)

    def test_rejects_negative_starting_equity(self):
        with pytest.raises(VolTargetError, match="initial"):
            compute_high_water_mark_series([100.0], starting_equity=-1.0)


# ──────────────────────────────────────────────────────────────────────
# compute_dd_series
# ──────────────────────────────────────────────────────────────────────


class TestComputeDdSeries:
    def test_empty(self):
        assert compute_dd_series([]) == []

    def test_at_hwm_zero_dd(self):
        ec = [100.0, 110.0, 120.0]
        dd = compute_dd_series(ec)
        assert all(d == 0.0 for d in dd)

    def test_monotonic_decline(self):
        ec = [100.0, 90.0, 80.0, 70.0]
        dd = compute_dd_series(ec)
        # HWM is 100 throughout. dd = (100 - ec) / 100.
        assert dd == pytest.approx([0.0, 0.10, 0.20, 0.30], abs=1e-12)

    def test_recovery_to_new_high_water(self):
        """A recovery to a new HWM resets the DD."""
        ec = [100.0, 80.0, 120.0]
        dd = compute_dd_series(ec)
        assert dd[0] == pytest.approx(0.0, abs=1e-12)
        assert dd[1] == pytest.approx(0.20, abs=1e-12)
        assert dd[2] == pytest.approx(0.0, abs=1e-12)

    def test_full_loss(self):
        ec = [100.0, 0.0]
        dd = compute_dd_series(ec)
        assert dd[1] == pytest.approx(1.0, abs=1e-12)

    def test_clamped_above_zero(self):
        """Equity above HWM (floating-point) is clamped to 0, not negative."""
        ec = [100.0, 100.00000000001]  # micro-tick above HWM
        dd = compute_dd_series(ec)
        for d in dd:
            assert d >= 0

    def test_rejects_nonfinite_value(self):
        with pytest.raises(VolTargetError, match="non-finite"):
            compute_dd_series([100.0, math.inf])


# ──────────────────────────────────────────────────────────────────────
# lookup_dd_risk_fraction
# ──────────────────────────────────────────────────────────────────────


class TestLookupDdRiskFraction:
    def test_below_first_tier_uses_first_tier(self):
        """dd == 0.0 → first tier (risk_fraction == 1.0 by default)."""
        cfg = DrawdownLadderConfig()
        assert lookup_dd_risk_fraction(0.0, cfg) == pytest.approx(1.0)

    def test_first_tier_range(self):
        cfg = DrawdownLadderConfig()
        # 0 <= dd < 5% → 1.0
        for dd in (0.0, 0.01, 0.04, 0.0499):
            assert lookup_dd_risk_fraction(dd, cfg) == pytest.approx(1.0)

    def test_second_tier_range(self):
        cfg = DrawdownLadderConfig()
        # 5% <= dd < 10% → 0.5
        for dd in (0.05, 0.06, 0.07, 0.099):
            assert lookup_dd_risk_fraction(dd, cfg) == pytest.approx(0.5)

    def test_third_tier_range(self):
        cfg = DrawdownLadderConfig()
        # dd >= 10% → 0.25
        for dd in (0.10, 0.15, 0.50, 1.0):
            assert lookup_dd_risk_fraction(dd, cfg) == pytest.approx(0.25)

    def test_min_risk_fraction_floor(self):
        cfg = DrawdownLadderConfig(min_risk_fraction=0.1)
        # Tier says 0.25, but floor is 0.1 → 0.25 (still above floor).
        assert lookup_dd_risk_fraction(0.5, cfg) == pytest.approx(0.25)
        # Custom tier with fraction 0.05, but floor 0.1 → clamps up.
        custom = DrawdownLadderConfig(
            tiers=(
                DrawdownTier(0.0, 0.05),
                DrawdownTier(0.5, 0.02),
            ),
            min_risk_fraction=0.1,
        )
        assert lookup_dd_risk_fraction(0.6, custom) == pytest.approx(0.1)

    def test_rejects_negative_dd(self):
        with pytest.raises(VolTargetError, match="dd"):
            lookup_dd_risk_fraction(-0.01, DrawdownLadderConfig())

    def test_rejects_nonfinite_dd(self):
        with pytest.raises(VolTargetError, match="dd"):
            lookup_dd_risk_fraction(math.inf, DrawdownLadderConfig())

    def test_rejects_non_drawdown_ladder_config(self):
        with pytest.raises(TypeError):
            lookup_dd_risk_fraction(0.05, "not-a-config")  # type: ignore[arg-type]

    def test_two_tier_ladder(self):
        custom = DrawdownLadderConfig(
            tiers=(
                DrawdownTier(0.0, 1.0),
                DrawdownTier(0.10, 0.0),
            )
        )
        assert lookup_dd_risk_fraction(0.05, custom) == pytest.approx(1.0)
        assert lookup_dd_risk_fraction(0.10, custom) == pytest.approx(0.0)
        assert lookup_dd_risk_fraction(1.0, custom) == pytest.approx(0.0)

    def test_single_tier_ladder(self):
        custom = DrawdownLadderConfig(tiers=(DrawdownTier(0.0, 0.5),))
        assert lookup_dd_risk_fraction(0.0, custom) == pytest.approx(0.5)
        assert lookup_dd_risk_fraction(0.99, custom) == pytest.approx(0.5)


# ──────────────────────────────────────────────────────────────────────
# compute_dd_risk_series
# ──────────────────────────────────────────────────────────────────────


class TestComputeDdRiskSeries:
    def test_null_config_returns_all_ones(self):
        ec = [100.0, 90.0, 80.0, 70.0]
        risk, hwm, dd, max_dd = compute_dd_risk_series(ec, None)
        assert risk == [1.0, 1.0, 1.0, 1.0]
        assert max_dd == pytest.approx(0.30, abs=1e-12)  # max dd still computed

    def test_tier_transitions_in_risk_series(self):
        """dd crosses 5% and 10% boundaries → risk fraction steps down."""
        ec = [100.0, 96.0, 91.0, 86.0]  # 0%, 4%, 9%, 14% DD
        risk, _hwm, _dd, _max = compute_dd_risk_series(
            ec,
            DrawdownLadderConfig(),
        )
        # Bar 0: dd=0% → 1.0
        # Bar 1: dd=4% → 1.0
        # Bar 2: dd=9% → 0.5
        # Bar 3: dd=14% → 0.25
        assert risk == pytest.approx([1.0, 1.0, 0.5, 0.25], abs=1e-12)

    def test_max_dd_observed(self):
        ec = [100.0, 95.0, 90.0, 85.0, 80.0, 90.0]
        _, _, _, max_dd = compute_dd_risk_series(ec, DrawdownLadderConfig())
        assert max_dd == pytest.approx(0.20, abs=1e-12)

    def test_starting_equity_override(self):
        """starting_equity shifts the HWM baseline."""
        ec = [100.0, 110.0, 120.0]
        # With starting_equity=200, the very first equity entry is in DD.
        risk, hwm, dd, max_dd = compute_dd_risk_series(
            ec,
            DrawdownLadderConfig(),
            starting_equity=200.0,
        )
        # HWM starts at 200, climbs to 200 then stays.
        # DD: bar 0 = 100/200 = 0.5 → tier 0.5 → 0.25 risk.
        assert hwm[0] == 200.0
        assert dd[0] == pytest.approx(0.5, abs=1e-12)
        assert risk[0] == pytest.approx(0.25, abs=1e-12)

    def test_empty_equity_curve(self):
        risk, hwm, dd, max_dd = compute_dd_risk_series([], None)
        assert risk == []
        assert hwm == []
        assert dd == []
        assert max_dd == 0.0

    def test_length_matches_input(self):
        ec = [100.0, 95.0, 90.0, 95.0, 100.0]
        risk, hwm, dd, _ = compute_dd_risk_series(ec, DrawdownLadderConfig())
        assert len(risk) == len(ec)
        assert len(hwm) == len(ec)
        assert len(dd) == len(ec)


# ──────────────────────────────────────────────────────────────────────
# compute_composite_risk_scale_series
# ──────────────────────────────────────────────────────────────────────


class TestComputeCompositeRiskScaleSeries:
    def test_length_mismatch_rejected(self):
        with pytest.raises(VolTargetError, match="length"):
            compute_composite_risk_scale_series([1.0, 2.0], [1.0])

    def test_both_ones_yield_ones(self):
        assert compute_composite_risk_scale_series(
            [1.0] * 5, [1.0] * 5
        ) == [1.0] * 5

    def test_multiplicative_composition(self):
        vol = [0.5, 1.0, 2.0, 0.0, 4.0]
        dd = [1.0, 0.5, 0.25, 1.0, 1.0]
        comp = compute_composite_risk_scale_series(vol, dd)
        assert comp == pytest.approx([0.5, 0.5, 0.5, 0.0, 4.0], abs=1e-12)

    def test_zero_input_propagates(self):
        """Either side zero → composite zero (no scaling)."""
        vol = [0.0, 1.0, 2.0]
        dd = [1.0, 1.0, 0.0]
        comp = compute_composite_risk_scale_series(vol, dd)
        assert comp == pytest.approx([0.0, 1.0, 0.0], abs=1e-12)

    def test_empty_inputs(self):
        assert compute_composite_risk_scale_series([], []) == []


# ──────────────────────────────────────────────────────────────────────
# load_vol_target_config
# ──────────────────────────────────────────────────────────────────────


class TestLoadVolTargetConfig:
    def test_minimum_load(self):
        cfg = load_vol_target_config({"target_annualized_vol": 0.15})
        assert cfg.target_annualized_vol == pytest.approx(0.15)
        assert cfg.lookback_window == DEFAULT_LOOKBACK_1H

    def test_full_load(self):
        cfg = load_vol_target_config(
            {
                "target_annualized_vol": 0.20,
                "lookback_window": 30,
                "annualization_factor": 2190,
                "min_realized_vol": 0.001,
                "max_scale": 2.0,
                "min_scale": 0.1,
                "warmup_bars": 20,
                "name": "test",
            }
        )
        assert cfg.target_annualized_vol == pytest.approx(0.20)
        assert cfg.lookback_window == 30
        assert cfg.annualization_factor == 2190
        assert cfg.min_realized_vol == pytest.approx(0.001)
        assert cfg.max_scale == pytest.approx(2.0)
        assert cfg.min_scale == pytest.approx(0.1)
        assert cfg.warmup_bars == 20
        assert cfg.name == "test"

    def test_rejects_missing_target(self):
        with pytest.raises(VolTargetError, match="missing required key"):
            load_vol_target_config({})

    def test_rejects_non_mapping(self):
        with pytest.raises(VolTargetError, match="mapping"):
            load_vol_target_config([])  # type: ignore[arg-type]

    def test_rejects_non_numeric_target(self):
        with pytest.raises(VolTargetError, match="numeric"):
            load_vol_target_config({"target_annualized_vol": "0.10"})

    def test_rejects_bool_target(self):
        with pytest.raises(VolTargetError, match="bool"):
            load_vol_target_config({"target_annualized_vol": True})

    def test_rejects_negative_target(self):
        with pytest.raises(VolTargetError, match="target_annualized_vol"):
            load_vol_target_config({"target_annualized_vol": -0.1})

    def test_rejects_lookback_lt_2(self):
        with pytest.raises(VolTargetError, match="lookback_window"):
            load_vol_target_config(
                {"target_annualized_vol": 0.1, "lookback_window": 1}
            )

    def test_rejects_max_scale_lt_1(self):
        with pytest.raises(VolTargetError, match="max_scale"):
            load_vol_target_config(
                {"target_annualized_vol": 0.1, "max_scale": 0.5}
            )

    def test_rejects_empty_name(self):
        with pytest.raises(VolTargetError, match="name"):
            load_vol_target_config(
                {"target_annualized_vol": 0.1, "name": ""}
            )


# ──────────────────────────────────────────────────────────────────────
# load_drawdown_ladder_config
# ──────────────────────────────────────────────────────────────────────


class TestLoadDrawdownLadderConfig:
    def test_minimum_load(self):
        cfg = load_drawdown_ladder_config(
            {
                "tiers": [
                    {"dd_threshold": 0.0, "risk_fraction": 1.0},
                    {"dd_threshold": 0.05, "risk_fraction": 0.5},
                ]
            }
        )
        assert len(cfg.tiers) == 2
        assert cfg.tiers[0].dd_threshold == 0.0

    def test_full_load(self):
        cfg = load_drawdown_ladder_config(
            {
                "tiers": [
                    {"dd_threshold": 0.0, "risk_fraction": 1.0},
                    {"dd_threshold": 0.05, "risk_fraction": 0.5},
                    {"dd_threshold": 0.10, "risk_fraction": 0.25},
                ],
                "starting_equity": 10_000.0,
                "min_risk_fraction": 0.1,
                "name": "test",
            }
        )
        assert cfg.starting_equity == 10_000.0
        assert cfg.min_risk_fraction == pytest.approx(0.1)
        assert cfg.name == "test"

    def test_rejects_non_mapping(self):
        with pytest.raises(VolTargetError, match="mapping"):
            load_drawdown_ladder_config([])  # type: ignore[arg-type]

    def test_rejects_missing_tiers(self):
        with pytest.raises(VolTargetError, match="missing required key"):
            load_drawdown_ladder_config({})

    def test_rejects_empty_tiers(self):
        with pytest.raises(VolTargetError, match="non-empty"):
            load_drawdown_ladder_config({"tiers": []})

    def test_rejects_non_mapping_tier(self):
        with pytest.raises(VolTargetError, match=r"tiers\[0\]"):
            load_drawdown_ladder_config({"tiers": ["not-a-mapping"]})

    def test_rejects_missing_tier_threshold(self):
        with pytest.raises(VolTargetError, match="dd_threshold"):
            load_drawdown_ladder_config(
                {"tiers": [{"risk_fraction": 0.5}]}
            )

    def test_rejects_missing_tier_fraction(self):
        with pytest.raises(VolTargetError, match="risk_fraction"):
            load_drawdown_ladder_config(
                {"tiers": [{"dd_threshold": 0.0}]}
            )

    def test_rejects_first_tier_nonzero(self):
        """First tier must start at 0.0 — the validator enforces this."""
        with pytest.raises(VolTargetError, match="first tier"):
            load_drawdown_ladder_config(
                {
                    "tiers": [
                        {"dd_threshold": 0.01, "risk_fraction": 1.0},
                    ]
                }
            )

    def test_rejects_non_numeric_threshold(self):
        with pytest.raises(VolTargetError, match="numeric"):
            load_drawdown_ladder_config(
                {
                    "tiers": [
                        {"dd_threshold": "0", "risk_fraction": 1.0},
                    ]
                }
            )


# ──────────────────────────────────────────────────────────────────────
# Engine integration — run_backtest_with_vol_target
# ──────────────────────────────────────────────────────────────────────


class TestRunBacktestWithVolTarget:
    def test_legacy_fx_path_no_scale(self):
        """vol_target_config=None → vol_scale_series all 1.0."""
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        cfg = _simple_config()
        metrics = run_backtest_with_vol_target(
            bars=bars,
            vol_target_config=None,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        assert isinstance(metrics, VolTargetBacktestMetrics)
        assert len(metrics.vol_scale_series) == len(bars)
        assert all(s == pytest.approx(1.0, abs=1e-12) for s in metrics.vol_scale_series)
        assert metrics.mean_realized_vol_post_warmup == 0.0

    def test_active_config_produces_scale_series(self):
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.02,
        )
        cfg = _simple_config()
        vt_cfg = VolTargetConfig(
            lookback_window=20,
            warmup_bars=20,
            target_annualized_vol=0.10,
            min_realized_vol=1e-6,
            max_scale=4.0,
            min_scale=0.0,
        )
        metrics = run_backtest_with_vol_target(
            bars=bars,
            vol_target_config=vt_cfg,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        assert len(metrics.vol_scale_series) == len(bars)
        # Warmup is min_scale (0.0).
        for s in metrics.vol_scale_series[:20]:
            assert s == pytest.approx(0.0, abs=1e-12)
        # Post-warmup scales are non-zero.
        assert any(s > 0 for s in metrics.vol_scale_series[20:])
        assert metrics.mean_realized_vol_post_warmup > 0

    def test_engine_metrics_byte_identical_to_no_overlay_run(self):
        """The engine metrics from the wrapper must match a plain
        BacktestEngine.run_single call with the same args — the
        overlay is purely additive on the scale series, not on the
        engine output."""
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        cfg = _simple_config()
        strategies = [_NoFireStrategy()]
        # Plain engine run.
        from engine.engine import BacktestEngine
        plain_engine = BacktestEngine(cfg, list(strategies))
        plain = plain_engine.run_single(strategies[0], list(bars))
        # Wrapper run with active vol-target.
        vt_cfg = VolTargetConfig()
        wrapped = run_backtest_with_vol_target(
            bars=bars,
            vol_target_config=vt_cfg,
            config=cfg,
            strategies=strategies,
        )
        assert wrapped.base.ending_balance == pytest.approx(
            plain.ending_balance, abs=1e-9
        )
        assert wrapped.base.total_pnl == pytest.approx(plain.total_pnl, abs=1e-9)

    def test_rejects_empty_bars(self):
        cfg = _simple_config()
        with pytest.raises(ValueError, match="bars"):
            run_backtest_with_vol_target(
                bars=[],
                vol_target_config=None,
                config=cfg,
                strategies=[_NoFireStrategy()],
            )

    def test_rejects_empty_strategies(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config()
        with pytest.raises(ValueError, match="strategies"):
            run_backtest_with_vol_target(
                bars=bars,
                vol_target_config=None,
                config=cfg,
                strategies=[],
            )

    def test_rejects_bad_vol_target_config_type(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config()
        with pytest.raises(TypeError):
            run_backtest_with_vol_target(
                bars=bars,
                vol_target_config="not-a-config",  # type: ignore[arg-type]
                config=cfg,
                strategies=[_NoFireStrategy()],
            )

    def test_strategy_name_selection(self):
        """strategy_name picks the right strategy from the list."""
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        cfg = _simple_config()
        strategies = [_NoFireStrategy(), _AlwaysLongStrategy("LongB")]
        metrics = run_backtest_with_vol_target(
            bars=bars,
            vol_target_config=None,
            config=cfg,
            strategies=strategies,
            strategy_name="LongB",
        )
        # The picked strategy's trades are in metrics.base.trades.
        assert isinstance(metrics.base.trades, list)

    def test_integrity_gate_opt_in(self):
        """integrity_config is plumbed through; clean bars pass."""
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        cfg = _simple_config()
        metrics = run_backtest_with_vol_target(
            bars=bars,
            vol_target_config=None,
            config=cfg,
            strategies=[_NoFireStrategy()],
            integrity_config=_integrity_config(),
        )
        assert isinstance(metrics, VolTargetBacktestMetrics)

    def test_integrity_gate_fails_loud_on_bad_bars(self):
        """integrity_config catches non-monotonic bars before engine runs."""
        # Inject a NaN price.
        bars = _vol_bars(
            50,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        bars[25] = Bar(
            time=bars[25].time,
            open=100.0,
            high=100.0,
            low=100.0,
            close=math.nan,
            volume=100.0,
        )
        cfg = _simple_config()
        with pytest.raises(ValueError):  # DataIntegrityError → ValueError subclass
            run_backtest_with_vol_target(
                bars=bars,
                vol_target_config=None,
                config=cfg,
                strategies=[_NoFireStrategy()],
                integrity_config=_integrity_config(),
            )


# ──────────────────────────────────────────────────────────────────────
# Engine integration — run_backtest_with_drawdown_ladder
# ──────────────────────────────────────────────────────────────────────


class TestRunBacktestWithDrawdownLadder:
    def test_legacy_fx_path_all_ones(self):
        """dd_config=None → dd_risk_series all 1.0."""
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        cfg = _simple_config()
        metrics = run_backtest_with_drawdown_ladder(
            bars=bars,
            dd_config=None,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        assert isinstance(metrics, DrawdownLadderBacktestMetrics)
        assert all(r == pytest.approx(1.0) for r in metrics.dd_risk_series)
        assert metrics.max_dd_observed == 0.0

    def test_active_config_produces_risk_series(self):
        """With active DD config, the risk series varies with DD."""
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.20,  # high vol → significant DD
        )
        cfg = _simple_config()
        metrics = run_backtest_with_drawdown_ladder(
            bars=bars,
            dd_config=DrawdownLadderConfig(),
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        # DD series is computed even with NoFire (no trades), because
        # the equity_curve is just the running balance.
        assert len(metrics.dd_risk_series) == len(bars)
        assert metrics.max_dd_observed >= 0.0

    def test_hwm_and_dd_series_match_equity_curve(self):
        """HWM and DD series align with the engine's equity curve."""
        bars = _vol_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        cfg = _simple_config()
        metrics = run_backtest_with_drawdown_ladder(
            bars=bars,
            dd_config=DrawdownLadderConfig(),
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        assert len(metrics.hwm_series) == len(bars)
        assert len(metrics.dd_series) == len(bars)

    def test_engine_metrics_byte_identical(self):
        bars = _vol_bars(50, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config()
        strategies = [_NoFireStrategy()]
        from engine.engine import BacktestEngine
        plain = BacktestEngine(cfg, list(strategies)).run_single(
            strategies[0], list(bars)
        )
        wrapped = run_backtest_with_drawdown_ladder(
            bars=bars,
            dd_config=DrawdownLadderConfig(),
            config=cfg,
            strategies=strategies,
        )
        assert wrapped.base.ending_balance == pytest.approx(
            plain.ending_balance, abs=1e-9
        )

    def test_rejects_empty_bars(self):
        cfg = _simple_config()
        with pytest.raises(ValueError, match="bars"):
            run_backtest_with_drawdown_ladder(
                bars=[],
                dd_config=DrawdownLadderConfig(),
                config=cfg,
                strategies=[_NoFireStrategy()],
            )

    def test_rejects_bad_dd_config_type(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config()
        with pytest.raises(TypeError):
            run_backtest_with_drawdown_ladder(
                bars=bars,
                dd_config="not-a-config",  # type: ignore[arg-type]
                config=cfg,
                strategies=[_NoFireStrategy()],
            )


# ──────────────────────────────────────────────────────────────────────
# Engine integration — run_backtest_with_vol_target_and_dd
# ──────────────────────────────────────────────────────────────────────


class TestRunBacktestWithVolTargetAndDd:
    def test_both_null_returns_all_one_composite(self):
        """vol_target_config=None and dd_config=None →
        composite_risk_scale_series all 1.0."""
        bars = _vol_bars(50, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config()
        metrics = run_backtest_with_vol_target_and_dd(
            bars=bars,
            vol_target_config=None,
            dd_config=None,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        assert isinstance(metrics, VolTargetDDBacktestMetrics)
        assert all(
            s == pytest.approx(1.0, abs=1e-12)
            for s in metrics.composite_risk_scale_series
        )
        assert all(
            s == pytest.approx(1.0, abs=1e-12)
            for s in metrics.vol_scale_series
        )
        assert all(
            r == pytest.approx(1.0, abs=1e-12)
            for r in metrics.dd_risk_series
        )

    def test_only_vol_target_active(self):
        bars = _vol_bars(50, datetime(2024, 1, 1, tzinfo=timezone.utc), sigma=0.02)
        cfg = _simple_config()
        vt_cfg = VolTargetConfig(lookback_window=20, warmup_bars=20)
        metrics = run_backtest_with_vol_target_and_dd(
            bars=bars,
            vol_target_config=vt_cfg,
            dd_config=None,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        # dd_risk is all 1.0 (no DD config).
        assert all(r == pytest.approx(1.0) for r in metrics.dd_risk_series)
        # composite == vol_scale when dd_risk is 1.0.
        assert metrics.composite_risk_scale_series == pytest.approx(
            metrics.vol_scale_series, abs=1e-12
        )

    def test_only_dd_active(self):
        bars = _vol_bars(50, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config()
        metrics = run_backtest_with_vol_target_and_dd(
            bars=bars,
            vol_target_config=None,
            dd_config=DrawdownLadderConfig(),
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        # vol_scale is all 1.0 (no vol-target config).
        assert all(s == pytest.approx(1.0) for s in metrics.vol_scale_series)
        # composite == dd_risk when vol_scale is 1.0.
        assert metrics.composite_risk_scale_series == pytest.approx(
            metrics.dd_risk_series, abs=1e-12
        )

    def test_composition_is_multiplicative(self):
        """When both configs are active, composite = vol * dd."""
        bars = _vol_bars(100, datetime(2024, 1, 1, tzinfo=timezone.utc), sigma=0.02)
        cfg = _simple_config()
        vt_cfg = VolTargetConfig(
            lookback_window=20,
            warmup_bars=20,
            target_annualized_vol=0.10,
        )
        dd_cfg = DrawdownLadderConfig()
        metrics = run_backtest_with_vol_target_and_dd(
            bars=bars,
            vol_target_config=vt_cfg,
            dd_config=dd_cfg,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        # Composite == product.
        for c, v, r in zip(
            metrics.composite_risk_scale_series,
            metrics.vol_scale_series,
            metrics.dd_risk_series,
            strict=True,
        ):
            assert c == pytest.approx(v * r, abs=1e-12)

    def test_engine_metrics_byte_identical(self):
        bars = _vol_bars(50, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config()
        strategies = [_NoFireStrategy()]
        from engine.engine import BacktestEngine
        plain = BacktestEngine(cfg, list(strategies)).run_single(
            strategies[0], list(bars)
        )
        wrapped = run_backtest_with_vol_target_and_dd(
            bars=bars,
            vol_target_config=VolTargetConfig(),
            dd_config=DrawdownLadderConfig(),
            config=cfg,
            strategies=strategies,
        )
        assert wrapped.base.ending_balance == pytest.approx(
            plain.ending_balance, abs=1e-9
        )

    def test_rejects_empty_bars(self):
        cfg = _simple_config()
        with pytest.raises(ValueError, match="bars"):
            run_backtest_with_vol_target_and_dd(
                bars=[],
                vol_target_config=None,
                dd_config=None,
                config=cfg,
                strategies=[_NoFireStrategy()],
            )

    def test_rejects_bad_vol_target_config_type(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config()
        with pytest.raises(TypeError):
            run_backtest_with_vol_target_and_dd(
                bars=bars,
                vol_target_config="bad",  # type: ignore[arg-type]
                dd_config=None,
                config=cfg,
                strategies=[_NoFireStrategy()],
            )

    def test_rejects_bad_dd_config_type(self):
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config()
        with pytest.raises(TypeError):
            run_backtest_with_vol_target_and_dd(
                bars=bars,
                vol_target_config=None,
                dd_config="bad",  # type: ignore[arg-type]
                config=cfg,
                strategies=[_NoFireStrategy()],
            )


# ──────────────────────────────────────────────────────────────────────
# Composition with full_crypto_overlay (byte-equality)
# ──────────────────────────────────────────────────────────────────────


def _build_crypto_overlay_args(
    bars: list[Bar],
    n: int,
) -> dict[str, Any]:
    """Build the args tuple for the full overlay / vol-target / DD wrapper."""
    start_dt = bars[0].time
    funding = [
        FundingEvent(
            time=start_dt + timedelta(hours=8 * i),
            rate=0.0001,
            interval_hours=8,
            cap_triggered=False,
        )
        for i in range(max(n // 8, 1))
    ]
    mark_prices = [bar.close for bar in bars]
    fills = [
        VenueOrderFill(
            time=bars[min(i * 12, n - 1)].time,
            fill_price=bars[min(i * 12, n - 1)].close,
            quantity=0.1,
            intent="taker",
        )
        for i in range(max(n // 12, 1))
    ]
    return dict(
        bars=bars,
        mark_prices=mark_prices,
        funding_events=funding,
        fills=fills,
        venue_config=DEFAULT_BINANCE_USDM_FEE_CONFIG,
        position=PositionSpec(notional_usd=10_000.0, direction="long"),
        liq_spec=LiquidationSpec(
            entry_price=bars[0].close,
            leverage=10.0,
            direction="long",
            maintenance_margin_rate=0.05,
        ),
    )


class TestCompositionWithFullCryptoOverlay:
    def test_null_vol_target_dd_does_not_change_full_overlay_metrics(self):
        """With vol_target_config=None and dd_config=None, the four
        overlays' (funded, liquidated, venue_metrics) byte-match the
        three-way composition of run_backtest_with_full_crypto_overlay."""
        bars = _vol_bars(
            200,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.02,
        )
        cfg = _simple_config()
        args = _build_crypto_overlay_args(bars, 200)

        # Run the three-way composition first.
        funded3, liquidated3, venue3 = run_backtest_with_full_crypto_overlay(
            **args,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )

        # Run the four-way composition with vol-target/DD disabled.
        funded4, liquidated4, venue4, vol_dd4 = (
            run_backtest_with_full_crypto_overlay_and_vol_target(
                **args,
                config=cfg,
                strategies=[_NoFireStrategy()],
                vol_target_config=None,
                dd_config=None,
            )
        )

        # funded, liquidated, venue_metrics must match.
        assert funded4.base.ending_balance == pytest.approx(
            funded3.base.ending_balance, abs=1e-9
        )
        assert liquidated4.base.ending_balance == pytest.approx(
            liquidated3.base.ending_balance, abs=1e-9
        )
        assert venue4.base.ending_balance == pytest.approx(
            venue3.base.ending_balance, abs=1e-9
        )
        # Composite scale is all 1.0 (no overlays active).
        assert all(
            s == pytest.approx(1.0, abs=1e-12)
            for s in vol_dd4.composite_risk_scale_series
        )
        # vol-target/DD overlay's base matches the others.
        assert vol_dd4.base.ending_balance == pytest.approx(
            funded3.base.ending_balance, abs=1e-9
        )

    def test_active_vol_target_does_not_affect_cost_overlay_metrics(self):
        """When vol-target is active, the cost overlay metrics
        (ending_balance, total_cost) are still byte-identical to the
        three-way run — vol-target is a *scale* overlay, not a cost."""
        bars = _vol_bars(
            200,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.02,
        )
        cfg = _simple_config()
        args = _build_crypto_overlay_args(bars, 200)

        # Three-way baseline.
        funded3, liquidated3, venue3 = run_backtest_with_full_crypto_overlay(
            **args,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )

        # Four-way with vol-target active.
        vt_cfg = VolTargetConfig(
            lookback_window=20,
            warmup_bars=20,
            target_annualized_vol=0.10,
        )
        funded4, liquidated4, venue4, vol_dd4 = (
            run_backtest_with_full_crypto_overlay_and_vol_target(
                **args,
                config=cfg,
                strategies=[_NoFireStrategy()],
                vol_target_config=vt_cfg,
                dd_config=None,
            )
        )

        # Cost overlay metrics must match exactly (vol-target is
        # additive-on-scale, not additive-on-cost).
        assert funded4.total_funding_cost == pytest.approx(
            funded3.total_funding_cost, abs=1e-9
        )
        assert liquidated4.total_liquidation_cost == pytest.approx(
            liquidated3.total_liquidation_cost, abs=1e-9
        )
        assert venue4.total_venue_cost == pytest.approx(
            venue3.total_venue_cost, abs=1e-9
        )
        assert funded4.base.ending_balance == pytest.approx(
            funded3.base.ending_balance, abs=1e-9
        )
        # vol-target overlay's base matches too (same engine run).
        assert vol_dd4.base.ending_balance == pytest.approx(
            funded3.base.ending_balance, abs=1e-9
        )

    def test_active_dd_does_not_affect_cost_overlay_metrics(self):
        """Same as the vol-target case, but with DD active."""
        bars = _vol_bars(
            200,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.02,
        )
        cfg = _simple_config()
        args = _build_crypto_overlay_args(bars, 200)

        # Three-way baseline.
        funded3, liquidated3, venue3 = run_backtest_with_full_crypto_overlay(
            **args,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )

        # Four-way with DD active.
        dd_cfg = DrawdownLadderConfig()
        funded4, liquidated4, venue4, vol_dd4 = (
            run_backtest_with_full_crypto_overlay_and_vol_target(
                **args,
                config=cfg,
                strategies=[_NoFireStrategy()],
                vol_target_config=None,
                dd_config=dd_cfg,
            )
        )

        # Cost overlay metrics byte-match.
        assert funded4.total_funding_cost == pytest.approx(
            funded3.total_funding_cost, abs=1e-9
        )
        assert liquidated4.total_liquidation_cost == pytest.approx(
            liquidated3.total_liquidation_cost, abs=1e-9
        )
        assert venue4.total_venue_cost == pytest.approx(
            venue3.total_venue_cost, abs=1e-9
        )
        # DD overlay's base matches.
        assert vol_dd4.base.ending_balance == pytest.approx(
            funded3.base.ending_balance, abs=1e-9
        )

    def test_active_both_vol_target_and_dd_byte_equality(self):
        """Both overlays active: cost overlays still byte-match."""
        bars = _vol_bars(
            200,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            sigma=0.02,
        )
        cfg = _simple_config()
        args = _build_crypto_overlay_args(bars, 200)

        funded3, liquidated3, venue3 = run_backtest_with_full_crypto_overlay(
            **args,
            config=cfg,
            strategies=[_NoFireStrategy()],
        )

        vt_cfg = VolTargetConfig(lookback_window=20, warmup_bars=20)
        dd_cfg = DrawdownLadderConfig()
        funded4, liquidated4, venue4, vol_dd4 = (
            run_backtest_with_full_crypto_overlay_and_vol_target(
                **args,
                config=cfg,
                strategies=[_NoFireStrategy()],
                vol_target_config=vt_cfg,
                dd_config=dd_cfg,
            )
        )

        # Cost overlay totals byte-match.
        assert funded4.total_funding_cost == pytest.approx(
            funded3.total_funding_cost, abs=1e-9
        )
        assert liquidated4.total_liquidation_cost == pytest.approx(
            liquidated3.total_liquidation_cost, abs=1e-9
        )
        assert venue4.total_venue_cost == pytest.approx(
            venue3.total_venue_cost, abs=1e-9
        )
        # Composite scale is product of vol and dd.
        for c, v, d in zip(
            vol_dd4.composite_risk_scale_series,
            vol_dd4.vol_scale_series,
            vol_dd4.dd_risk_series,
            strict=True,
        ):
            assert c == pytest.approx(v * d, abs=1e-12)


# ──────────────────────────────────────────────────────────────────────
# Degenerate inputs
# ──────────────────────────────────────────────────────────────────────


class TestDegenerateInputs:
    def test_vol_target_with_single_bar(self):
        """Single bar → realized vol series is 0 (no returns)."""
        bars = _flat_bars(1, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config(min_bars_before_signal=1)
        metrics = run_backtest_with_vol_target(
            bars=bars,
            vol_target_config=VolTargetConfig(lookback_window=2),
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        # Only one bar — all vol series entries are 0.
        assert all(v == 0.0 for v in metrics.realized_vol_series)

    def test_dd_with_monotonically_increasing_equity(self):
        """No drawdown → dd series all 0 → risk series all 1.0."""
        bars = _uptrend_bars(
            100,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            base_price=100.0,
            pct_per_bar=0.001,
        )
        cfg = _simple_config()
        metrics = run_backtest_with_drawdown_ladder(
            bars=bars,
            dd_config=DrawdownLadderConfig(),
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        assert all(d == 0.0 for d in metrics.dd_series)
        assert all(r == pytest.approx(1.0) for r in metrics.dd_risk_series)
        assert metrics.max_dd_observed == 0.0

    def test_vol_target_min_bars(self):
        """Engine needs min_bars_before_signal bars; n=30 is the floor."""
        bars = _flat_bars(30, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config(min_bars_before_signal=30)
        # Should not raise — engine accepts exactly the minimum.
        metrics = run_backtest_with_vol_target(
            bars=bars,
            vol_target_config=VolTargetConfig(lookback_window=2, warmup_bars=2),
            config=cfg,
            strategies=[_NoFireStrategy()],
        )
        assert len(metrics.vol_scale_series) == len(bars)

    def test_vol_target_below_min_bars(self):
        """Engine raises when bars < min_bars_before_signal."""
        bars = _flat_bars(20, datetime(2024, 1, 1, tzinfo=timezone.utc))
        cfg = _simple_config(min_bars_before_signal=30)
        with pytest.raises(ValueError, match="at least"):
            run_backtest_with_vol_target(
                bars=bars,
                vol_target_config=VolTargetConfig(lookback_window=2),
                config=cfg,
                strategies=[_NoFireStrategy()],
            )

    def test_composite_helper_length_mismatch(self):
        with pytest.raises(VolTargetError, match="length"):
            compute_composite_risk_scale_series([1.0, 2.0], [1.0, 2.0, 3.0])
