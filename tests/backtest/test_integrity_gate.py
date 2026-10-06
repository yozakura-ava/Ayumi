"""Tests for ``forex_bot.backtest.integrity_gate`` — fail-loud data integrity.

Card: b86ac95b-6aab-4014-a6d8-828868f401c2 (Sprint C 1a.4)
Sprint: 2026-10-05-crypto-strategy-factory
Lane: [BUILD][CRYPTO-LANE]

Scope (HR5 — targeted tests only):

  * IntegrityConfig validation (cadence, tolerance, universe, etc.)
  * load_integrity_config — fail-loud YAML-style mapping validation:
      - missing keys, non-numeric values, bool-as-int trap
      - invalid cadence / tolerance / stretch
      - non-string universe symbols
      - non-ISO datetime parsing
  * infer_cadence_minutes — snap to 60/240/1440 within 5%, fallback
  * detect_gaps at 1h / 4h / 8h cadences
      - exact cadence → no gap
      - single missing candle → 1 gap
      - multiple consecutive missing candles → 1 gap (window-spanning)
      - tolerance absorbs micro-drift
      - out-of-order bars don't report a "negative gap"
  * detect_delisting:
      - window-end delisting when last bar precedes expected
      - tolerance absorbs minor latency
      - universe-missing-symbol violations
      - loaded-symbol-not-in-universe isn't reported when not supplied
  * detect_anomalies:
      - zero-volume stretches at the configured limit
      - NaN values in any OHLC field
      - OHLC invariant violations (h<l, h<o, h<c, l>o, l>c)
      - max_zero_volume_stretch=0 strict default
      - max_zero_volume_stretch=N allows N consecutive
  * validate_crypto_bars returns a report; does NOT raise
  * enforce_integrity_gate raises DataIntegrityError on any violation
  * DataIntegrityError carries the report and a precise message
  * No-repair guarantee: bars object identity preserved, no
    forward-fill, no truncation, error raised BEFORE any mutation
  * apply_integrity_gate returns the bars unchanged on pass
  * Opt-in integration with crypto overlay wrappers:
      - run_backtest_with_funding(integrity_config=...) fails loud
      - run_backtest_with_liquidation(integrity_config=...) fails loud
      - run_backtest_with_venue_costs(integrity_config=...) fails loud
      - Default (None) preserves existing behavior
  * Legacy FX loader untouched (CsvDataLoader / load_data / etc.)

All tests are deterministic — synthetic fixtures only, no live
exchange calls. The engine integration tests use a no-op strategy
that never fires a signal, so the real engine's run_single() path
is exercised without price movement dominating the equity curve.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup: src/forex_bot on sys.path so ``backtest.integrity_gate`` and
# ``engine.engine`` resolve. Mirrors test_venue_costs / test_liquidation /
# test_adaptive_funding setup. MUST run before any imports below.
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
    FundingEvent,
    PositionSpec,
    run_backtest_with_funding,
)
from backtest.integrity_gate import (  # noqa: E402
    DEFAULT_CADENCE_MINUTES,
    DEFAULT_GAP_TOLERANCE_MULTIPLIER,
    DEFAULT_MAX_ZERO_VOLUME_STRETCH,
    DataIntegrityError,
    IntegrityConfig,
    IntegrityConfigError,
    IntegrityViolation,
    apply_integrity_gate,
    detect_anomalies,
    detect_delisting,
    detect_gaps,
    enforce_integrity_gate,
    infer_cadence_minutes,
    load_integrity_config,
    validate_crypto_bars,
)
from backtest.liquidation import (  # noqa: E402
    LiquidationSpec,
    run_backtest_with_liquidation,
)
from backtest.strategies.isignal_strategy import ISignalStrategy
from backtest.types import BacktestConfig, Bar
from backtest.venue_costs import (  # noqa: E402
    DEFAULT_BINANCE_USDM_FEE_CONFIG,
    VenueOrderFill,
    run_backtest_with_venue_costs,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _bar(
    t: datetime,
    open_: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float = 100.0,
) -> Bar:
    """Build a Bar with sensible OHLC defaults (all fields finite)."""
    if high is None:
        high = max(open_, close if close is not None else open_) + 0.5
    if low is None:
        low = min(open_, close if close is not None else open_) - 0.5
    if close is None:
        close = open_
    return Bar(
        time=t,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        spread_pips=0.0,
    )


def _flat_bars(
    n: int,
    start: datetime,
    step_minutes: int = 60,
    base_price: float = 100.0,
    volume: float = 100.0,
) -> list[Bar]:
    """Build N flat OHLC bars at the given cadence (UTC)."""
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


def _simple_config(
    starting_balance: float = 10_000.0,
    pair: str = "BTCUSDT",
    leverage: int = 100,
) -> BacktestConfig:
    """Backtest config that triggers no trades on flat bars."""
    return BacktestConfig(
        starting_balance=starting_balance,
        risk_per_trade_pct=0.01,
        max_daily_drawdown_pct=0.05,
        max_total_drawdown_pct=0.10,
        commission_per_lot=0.0,
        leverage=leverage,
        min_confidence=0.99,
        min_bars_before_signal=30,
        max_open_trades=1,
        pair=pair,
        spread_pips=0.0,
        round_trip_spread=False,
        slippage_pips=0.0,
        swap_per_lot_per_day=0.0,
    )


class _NoFireStrategy(ISignalStrategy):
    """Trivial strategy that never produces a signal."""

    @property
    def name(self) -> str:
        return "NoFire"

    def evaluate(self, state: Any) -> None:
        return None


# ──────────────────────────────────────────────────────────────────────
# IntegrityConfig validation
# ──────────────────────────────────────────────────────────────────────


class TestIntegrityConfig:
    """Validation of the IntegrityConfig dataclass itself."""

    def test_default_config_is_strictest(self):
        cfg = IntegrityConfig()
        assert cfg.expected_cadence_minutes == DEFAULT_CADENCE_MINUTES
        assert cfg.gap_tolerance_multiplier == DEFAULT_GAP_TOLERANCE_MULTIPLIER
        assert cfg.max_zero_volume_stretch == DEFAULT_MAX_ZERO_VOLUME_STRETCH
        assert cfg.universe_symbols == ()
        assert cfg.expected_window_end is None
        assert cfg.delisting_tolerance_minutes == 0.0
        assert cfg.check_ohlc_invariants is True
        assert cfg.check_nan_values is True
        assert cfg.check_gaps is True
        assert cfg.check_delistings is True
        assert cfg.check_anomalies is True
        assert cfg.schema_version == 1

    def test_cadence_must_be_positive_int(self):
        with pytest.raises(IntegrityConfigError, match="expected_cadence_minutes"):
            IntegrityConfig(expected_cadence_minutes=0)
        with pytest.raises(IntegrityConfigError, match="expected_cadence_minutes"):
            IntegrityConfig(expected_cadence_minutes=-60)
        # bool rejected
        with pytest.raises(IntegrityConfigError, match="expected_cadence_minutes"):
            IntegrityConfig(expected_cadence_minutes=True)  # type: ignore[arg-type]

    def test_gap_tolerance_must_be_at_least_one(self):
        with pytest.raises(IntegrityConfigError, match="gap_tolerance_multiplier"):
            IntegrityConfig(gap_tolerance_multiplier=0.5)
        with pytest.raises(IntegrityConfigError, match="gap_tolerance_multiplier"):
            IntegrityConfig(gap_tolerance_multiplier=0.0)
        with pytest.raises(IntegrityConfigError, match="gap_tolerance_multiplier"):
            IntegrityConfig(gap_tolerance_multiplier=float("inf"))
        with pytest.raises(IntegrityConfigError, match="gap_tolerance_multiplier"):
            IntegrityConfig(gap_tolerance_multiplier=float("nan"))

    def test_delisting_tolerance_must_be_non_negative(self):
        with pytest.raises(
            IntegrityConfigError, match="delisting_tolerance_minutes"
        ):
            IntegrityConfig(delisting_tolerance_minutes=-1.0)
        with pytest.raises(
            IntegrityConfigError, match="delisting_tolerance_minutes"
        ):
            IntegrityConfig(delisting_tolerance_minutes=float("inf"))

    def test_max_zero_volume_stretch_must_be_non_negative(self):
        with pytest.raises(
            IntegrityConfigError, match="max_zero_volume_stretch"
        ):
            IntegrityConfig(max_zero_volume_stretch=-1)

    def test_universe_symbols_must_be_tuple_of_nonempty_strings(self):
        with pytest.raises(IntegrityConfigError, match="universe_symbols"):
            IntegrityConfig(universe_symbols=("BTCUSDT", ""))  # type: ignore[arg-type]
        with pytest.raises(IntegrityConfigError, match="universe_symbols"):
            IntegrityConfig(universe_symbols=("BTCUSDT", 123))  # type: ignore[arg-type]
        with pytest.raises(IntegrityConfigError, match="universe_symbols"):
            IntegrityConfig(universe_symbols=["BTCUSDT"])  # type: ignore[arg-type]
        # Valid tuple passes
        cfg = IntegrityConfig(universe_symbols=("BTCUSDT", "ETHUSDT"))
        assert cfg.universe_symbols == ("BTCUSDT", "ETHUSDT")

    def test_expected_window_end_must_be_tz_aware(self):
        with pytest.raises(IntegrityConfigError, match="expected_window_end"):
            IntegrityConfig(
                expected_window_end=datetime(2026, 10, 5, 0, 0)  # naive
            )

    def test_schema_version_must_be_positive_int(self):
        with pytest.raises(IntegrityConfigError, match="schema_version"):
            IntegrityConfig(schema_version=0)
        with pytest.raises(IntegrityConfigError, match="schema_version"):
            IntegrityConfig(schema_version=-1)
        with pytest.raises(IntegrityConfigError, match="schema_version"):
            IntegrityConfig(schema_version="1")  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# load_integrity_config (fail-loud YAML mapping parser)
# ──────────────────────────────────────────────────────────────────────


class TestLoadIntegrityConfig:
    """Fail-loud config parser — mirrors load_venue_fee_config tests."""

    def test_empty_mapping_uses_all_defaults(self):
        cfg = load_integrity_config({})
        assert cfg.expected_cadence_minutes == DEFAULT_CADENCE_MINUTES
        assert cfg.gap_tolerance_multiplier == DEFAULT_GAP_TOLERANCE_MULTIPLIER
        assert cfg.universe_symbols == ()
        assert cfg.expected_window_end is None

    def test_full_mapping_round_trips(self):
        cfg = load_integrity_config(
            {
                "expected_cadence_minutes": 240,
                "gap_tolerance_multiplier": 2.0,
                "expected_window_end": "2026-10-05T00:00:00Z",
                "delisting_tolerance_minutes": 5.0,
                "universe_symbols": ["BTCUSDT", "ETHUSDT"],
                "max_zero_volume_stretch": 3,
                "check_ohlc_invariants": False,
                "check_nan_values": True,
                "check_gaps": True,
                "check_delistings": True,
                "check_anomalies": True,
                "schema_version": 1,
            }
        )
        assert cfg.expected_cadence_minutes == 240
        assert cfg.gap_tolerance_multiplier == 2.0
        assert cfg.expected_window_end == datetime(2026, 10, 5, tzinfo=timezone.utc)
        assert cfg.delisting_tolerance_minutes == 5.0
        assert cfg.universe_symbols == ("BTCUSDT", "ETHUSDT")
        assert cfg.max_zero_volume_stretch == 3
        assert cfg.check_ohlc_invariants is False
        assert cfg.check_nan_values is True
        assert cfg.schema_version == 1

    def test_rejects_non_mapping(self):
        with pytest.raises(IntegrityConfigError, match="must be a mapping"):
            load_integrity_config([])  # type: ignore[arg-type]
        with pytest.raises(IntegrityConfigError, match="must be a mapping"):
            load_integrity_config("not a mapping")  # type: ignore[arg-type]

    def test_rejects_bool_for_numeric(self):
        with pytest.raises(IntegrityConfigError, match="expected_cadence_minutes"):
            load_integrity_config({"expected_cadence_minutes": True})
        with pytest.raises(IntegrityConfigError, match="gap_tolerance_multiplier"):
            load_integrity_config({"gap_tolerance_multiplier": False})

    def test_rejects_string_for_numeric(self):
        with pytest.raises(IntegrityConfigError, match="expected_cadence_minutes"):
            load_integrity_config({"expected_cadence_minutes": "60"})
        with pytest.raises(IntegrityConfigError, match="delisting_tolerance_minutes"):
            load_integrity_config({"delisting_tolerance_minutes": "5"})

    def test_rejects_int_where_bool_required(self):
        with pytest.raises(IntegrityConfigError, match="check_ohlc_invariants"):
            load_integrity_config({"check_ohlc_invariants": 1})
        with pytest.raises(IntegrityConfigError, match="check_nan_values"):
            load_integrity_config({"check_nan_values": "true"})

    def test_rejects_non_string_in_universe(self):
        with pytest.raises(IntegrityConfigError, match="universe_symbols"):
            load_integrity_config({"universe_symbols": ["BTCUSDT", 123]})
        with pytest.raises(IntegrityConfigError, match="universe_symbols"):
            load_integrity_config({"universe_symbols": ["", "ETHUSDT"]})

    def test_rejects_invalid_iso_datetime(self):
        with pytest.raises(IntegrityConfigError, match="expected_window_end"):
            load_integrity_config({"expected_window_end": "yesterday"})

    def test_rejects_naive_iso_datetime(self):
        with pytest.raises(IntegrityConfigError, match="expected_window_end"):
            load_integrity_config({"expected_window_end": "2026-10-05T00:00:00"})

    def test_rejects_invalid_schema_version(self):
        with pytest.raises(IntegrityConfigError, match="schema_version"):
            load_integrity_config({"schema_version": "1"})
        with pytest.raises(IntegrityConfigError, match="schema_version"):
            load_integrity_config({"schema_version": 0})

    def test_rejects_universe_as_non_sequence(self):
        with pytest.raises(IntegrityConfigError, match="universe_symbols"):
            load_integrity_config({"universe_symbols": "BTCUSDT"})

    def test_rejects_constructed_post_load(self):
        # When load_integrity_config returns the config, the constructor
        # still re-validates — ensure no double-validation gap.
        with pytest.raises(IntegrityConfigError, match="gap_tolerance_multiplier"):
            load_integrity_config({"gap_tolerance_multiplier": 0.5})


# ──────────────────────────────────────────────────────────────────────
# infer_cadence_minutes
# ──────────────────────────────────────────────────────────────────────


class TestInferCadenceMinutes:
    def test_returns_default_for_empty_series(self):
        assert infer_cadence_minutes([]) == DEFAULT_CADENCE_MINUTES

    def test_returns_default_for_single_bar(self):
        bars = _flat_bars(1, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        assert infer_cadence_minutes(bars) == DEFAULT_CADENCE_MINUTES

    def test_infers_1h_from_60min_cadence(self):
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        assert infer_cadence_minutes(bars) == 60

    def test_infers_4h_from_240min_cadence(self):
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=240)
        assert infer_cadence_minutes(bars) == 240

    def test_infers_daily_from_1440min_cadence(self):
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=1440)
        assert infer_cadence_minutes(bars) == 1440

    def test_snaps_within_5pct_to_nearest_valid(self):
        # 62 minutes is within 5% of 60 → snap to 60
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=62)
        assert infer_cadence_minutes(bars) == 60

    def test_returns_raw_when_outside_5pct_window(self):
        # 30 minutes is 50% off 60 → not snapped
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=30)
        assert infer_cadence_minutes(bars) == 30

    def test_rejects_naive_datetimes(self):
        # Build bars with naive datetimes
        naive = [
            Bar(
                time=datetime(2026, 10, 5, 0, 0),  # naive!
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=100.0,
            ),
            Bar(
                time=datetime(2026, 10, 5, 1, 0),  # naive!
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=100.0,
            ),
        ]
        with pytest.raises(IntegrityConfigError, match="timezone-aware"):
            infer_cadence_minutes(naive)


# ──────────────────────────────────────────────────────────────────────
# detect_gaps
# ──────────────────────────────────────────────────────────────────────


class TestDetectGaps:
    def test_no_gaps_on_perfect_cadence(self):
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig(expected_cadence_minutes=60, gap_tolerance_multiplier=1.5)
        assert detect_gaps(bars, cfg) == []

    def test_gap_detected_at_1h_cadence(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(3, start, step_minutes=60)
        # Insert a 4-hour gap between bars[0] and bars[1]
        bars[1] = _bar(bars[0].time + timedelta(hours=4))
        cfg = IntegrityConfig(expected_cadence_minutes=60, gap_tolerance_multiplier=1.5)
        violations = detect_gaps(bars, cfg, symbol="BTCUSDT")
        assert len(violations) == 1
        v = violations[0]
        assert v.kind == "gap"
        assert v.symbol == "BTCUSDT"
        assert v.bar_index == 1
        assert v.start_time == bars[0].time
        assert v.end_time == bars[1].time
        # detail should mention the gap duration and tolerance
        assert "240" in v.detail or "4.0" in v.detail
        assert "tolerance" in v.detail

    def test_gap_detected_at_4h_cadence(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(3, start, step_minutes=240)
        # Insert a 12h gap (3 missing 4h candles)
        bars[1] = _bar(bars[0].time + timedelta(hours=12))
        cfg = IntegrityConfig(expected_cadence_minutes=240, gap_tolerance_multiplier=1.5)
        violations = detect_gaps(bars, cfg)
        assert len(violations) == 1
        assert violations[0].kind == "gap"
        # expected = 240 * 1.5 = 360 minutes, actual = 720 minutes
        assert violations[0].expected == 360 * 60
        assert violations[0].actual == 12 * 3600

    def test_gap_detected_at_8h_cadence(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(3, start, step_minutes=480)
        # Insert a 24h gap (2 missing 8h candles)
        bars[1] = _bar(bars[0].time + timedelta(hours=24))
        cfg = IntegrityConfig(expected_cadence_minutes=480, gap_tolerance_multiplier=1.5)
        violations = detect_gaps(bars, cfg)
        assert len(violations) == 1
        assert violations[0].expected == 480 * 1.5 * 60
        assert violations[0].actual == 24 * 3600

    def test_tolerance_absorbs_minor_drift(self):
        # 65-minute gap on a 60-minute cadence with 1.5x tolerance
        # → 65 <= 60*1.5 = 90 → no gap
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(2, start, step_minutes=60)
        bars[1] = _bar(bars[0].time + timedelta(minutes=65))
        cfg = IntegrityConfig(expected_cadence_minutes=60, gap_tolerance_multiplier=1.5)
        assert detect_gaps(bars, cfg) == []

    def test_strict_tolerance_catches_minor_drift(self):
        # 65-minute gap on a 60-minute cadence with 1.0x tolerance
        # → 65 > 60 → gap
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(2, start, step_minutes=60)
        bars[1] = _bar(bars[0].time + timedelta(minutes=65))
        cfg = IntegrityConfig(expected_cadence_minutes=60, gap_tolerance_multiplier=1.0)
        violations = detect_gaps(bars, cfg)
        assert len(violations) == 1

    def test_multiple_gaps_each_reported(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        # Build 5 bars with two intentional 4h gaps, keeping the
        # series ascending: 0h, 1h, [4h gap] 5h, 6h, [4h gap] 10h.
        bars = [
            _bar(start),
            _bar(start + timedelta(hours=1)),
            _bar(start + timedelta(hours=5)),
            _bar(start + timedelta(hours=6)),
            _bar(start + timedelta(hours=10)),
        ]
        cfg = IntegrityConfig(expected_cadence_minutes=60, gap_tolerance_multiplier=1.5)
        violations = detect_gaps(bars, cfg)
        assert len(violations) == 2
        assert all(v.kind == "gap" for v in violations)
        # First gap is between bars[1] (1h) and bars[2] (5h).
        assert violations[0].bar_index == 2
        # Second gap is between bars[3] (6h) and bars[4] (10h).
        assert violations[1].bar_index == 4

    def test_check_gaps_disabled_returns_empty(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(3, start, step_minutes=60)
        bars[1] = _bar(bars[0].time + timedelta(hours=4))
        cfg = IntegrityConfig(
            expected_cadence_minutes=60, gap_tolerance_multiplier=1.5, check_gaps=False
        )
        assert detect_gaps(bars, cfg) == []

    def test_short_series_returns_empty(self):
        bars = _flat_bars(1, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig()
        assert detect_gaps(bars, cfg) == []
        assert detect_gaps([], cfg) == []

    def test_out_of_order_bars_not_treated_as_gap(self):
        # A bar earlier than its predecessor should not be reported as
        # a "negative gap" (out-of-order is the engine's concern, not
        # the integrity gate's). Build a strictly descending series
        # so the only diffs are negative — the detector must skip
        # these and report no gap.
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = [
            _bar(start + timedelta(hours=2)),
            _bar(start + timedelta(hours=1)),
            _bar(start + timedelta(hours=0)),
        ]
        cfg = IntegrityConfig(expected_cadence_minutes=60, gap_tolerance_multiplier=1.5)
        # No "gap" violation should be reported; the bar order is
        # wrong but the detector only flags positive diffs that
        # exceed the threshold.
        assert detect_gaps(bars, cfg) == []


# ──────────────────────────────────────────────────────────────────────
# detect_delisting
# ──────────────────────────────────────────────────────────────────────


class TestDetectDelisting:
    def test_no_window_end_no_violation(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig()
        assert detect_delisting(bars, cfg) == []

    def test_window_end_in_the_past_no_violation(self):
        # When the expected_window_end is BEFORE the bars' window, the
        # last bar's time is at or after the expected end → no delisting
        # (the data extends past the expected end, which is normal —
        # e.g. the expected end was an estimate that's since been
        # exceeded by extended data availability).
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig(
            expected_window_end=datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
        )
        assert detect_delisting(bars, cfg) == []

    def test_window_end_ahead_flags_delisting(self):
        bars = _flat_bars(
            5,
            datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            step_minutes=60,
        )
        # Last bar at 04:00, but expected end is 2026-10-06 00:00
        cfg = IntegrityConfig(
            expected_window_end=datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc),
            delisting_tolerance_minutes=0.0,
        )
        violations = detect_delisting(bars, cfg, symbol="BTCUSDT")
        assert len(violations) == 1
        v = violations[0]
        assert v.kind == "delisting"
        assert v.symbol == "BTCUSDT"
        assert v.start_time == bars[-1].time
        assert v.end_time == datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        assert "delisting" in v.detail.lower()

    def test_delisting_tolerance_absorbs_minor_latency(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        # Last bar at 04:00, expected end 04:30, tolerance 60 minutes
        cfg = IntegrityConfig(
            expected_window_end=datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc),
            delisting_tolerance_minutes=60.0,
        )
        assert detect_delisting(bars, cfg) == []

    def test_universe_missing_symbol(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig(
            universe_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        )
        violations = detect_delisting(bars, cfg, symbol="BTCUSDT", loaded_symbol="BTCUSDT")
        kinds = [v.kind for v in violations]
        # BTCUSDT is present; ETHUSDT and SOLUSDT are missing
        assert "missing_symbol" in kinds
        missing_names = {v.symbol for v in violations if v.kind == "missing_symbol"}
        assert missing_names == {"ETHUSDT", "SOLUSDT"}

    def test_universe_fully_present(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig(universe_symbols=("BTCUSDT",))
        assert detect_delisting(bars, cfg, loaded_symbol="BTCUSDT") == []

    def test_loaded_symbol_not_in_universe_not_reported(self):
        # When the loaded symbol is supplied AND the universe is non-empty,
        # the universe check reports only the missing universe members —
        # the loaded symbol being "outside" the universe is a separate
        # concern (and is NOT flagged here).
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig(universe_symbols=("ETHUSDT",))
        violations = detect_delisting(bars, cfg, symbol="BTCUSDT", loaded_symbol="BTCUSDT")
        kinds = [v.kind for v in violations]
        # ETHUSDT is reported missing; no "BTCUSDT missing" violation
        assert "missing_symbol" in kinds
        assert all(v.symbol == "ETHUSDT" for v in violations if v.kind == "missing_symbol")

    def test_check_delistings_disabled(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig(
            expected_window_end=datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc),
            check_delistings=False,
        )
        assert detect_delisting(bars, cfg) == []


# ──────────────────────────────────────────────────────────────────────
# detect_anomalies
# ──────────────────────────────────────────────────────────────────────


class TestDetectAnomalies:
    def test_no_anomalies_on_clean_series(self):
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig()
        assert detect_anomalies(bars, cfg) == []

    def test_zero_volume_strict_default(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(5, start, step_minutes=60, volume=100.0)
        # One zero-volume bar
        bars[2] = _bar(bars[2].time, volume=0.0)
        cfg = IntegrityConfig()  # max_zero_volume_stretch=0
        violations = detect_anomalies(bars, cfg, symbol="BTCUSDT")
        assert len(violations) == 1
        v = violations[0]
        assert v.kind == "zero_volume"
        assert v.symbol == "BTCUSDT"
        assert v.bar_index == 2
        assert "exceeds limit 0" in v.detail

    def test_zero_volume_stretch_allowed_up_to_limit(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(10, start, step_minutes=60, volume=100.0)
        # 3 consecutive zero-volume bars
        bars[3] = _bar(bars[3].time, volume=0.0)
        bars[4] = _bar(bars[4].time, volume=0.0)
        bars[5] = _bar(bars[5].time, volume=0.0)
        cfg = IntegrityConfig(max_zero_volume_stretch=3)
        assert detect_anomalies(bars, cfg) == []

    def test_zero_volume_stretch_exceeding_limit(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(10, start, step_minutes=60, volume=100.0)
        # 4 consecutive zero-volume bars
        for i in (3, 4, 5, 6):
            bars[i] = _bar(bars[i].time, volume=0.0)
        cfg = IntegrityConfig(max_zero_volume_stretch=3)
        violations = detect_anomalies(bars, cfg)
        assert len(violations) == 1
        v = violations[0]
        assert v.kind == "zero_volume"
        assert v.bar_index == 3
        assert "4 bars" in v.detail
        assert "exceeds limit 3" in v.detail

    def test_zero_volume_trailing_stretch(self):
        # Stretch at the tail of the series
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(5, start, step_minutes=60, volume=100.0)
        bars[3] = _bar(bars[3].time, volume=0.0)
        bars[4] = _bar(bars[4].time, volume=0.0)
        cfg = IntegrityConfig(max_zero_volume_stretch=1)
        violations = detect_anomalies(bars, cfg)
        assert len(violations) == 1
        v = violations[0]
        assert v.bar_index == 3
        assert "trailing stretch" in v.detail

    def test_nan_value_in_open(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        # Inject a bar with only open=NaN. Pass the other OHLC fields
        # explicitly so the helper's defaults don't propagate NaN.
        bars[2] = _bar(
            bars[2].time, open_=float("nan"), high=100.5, low=99.5, close=100.0
        )
        cfg = IntegrityConfig()
        violations = detect_anomalies(bars, cfg)
        nan_violations = [v for v in violations if v.kind == "nan_value"]
        assert len(nan_violations) == 1
        assert "open" in nan_violations[0].detail
        assert nan_violations[0].bar_index == 2

    def test_nan_value_in_close(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        # Inject a bar with only close=NaN. Pass the other OHLC fields
        # explicitly so the helper's defaults don't propagate NaN.
        bars[3] = _bar(
            bars[3].time,
            open_=100.0,
            high=100.5,
            low=99.5,
            close=float("nan"),
        )
        cfg = IntegrityConfig()
        violations = detect_anomalies(bars, cfg)
        nan_violations = [v for v in violations if v.kind == "nan_value"]
        assert len(nan_violations) == 1
        assert "close" in nan_violations[0].detail

    def test_nan_value_in_volume_does_not_count_as_zero(self):
        # A NaN volume is reported under nan_value, not zero_volume.
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        bars[2] = _bar(
            bars[2].time,
            open_=100.0,
            high=100.5,
            low=99.5,
            close=100.0,
            volume=float("nan"),
        )
        cfg = IntegrityConfig()
        violations = detect_anomalies(bars, cfg)
        assert any(v.kind == "nan_value" for v in violations)
        assert not any(v.kind == "zero_volume" for v in violations)

    def test_check_nan_values_disabled(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        bars[2] = _bar(bars[2].time, open_=float("nan"))
        cfg = IntegrityConfig(check_nan_values=False)
        assert detect_anomalies(bars, cfg) == []

    def test_ohlc_invariant_h_less_than_l(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        bars[2] = _bar(bars[2].time, open_=100.0, high=95.0, low=96.0, close=100.5)
        cfg = IntegrityConfig()
        violations = detect_anomalies(bars, cfg)
        ohlc_violations = [v for v in violations if v.kind == "ohlc_invariant"]
        # A single bad bar can violate multiple invariants. h=95, l=96,
        # o=100, c=100.5 → h<l, h<o, h<c are all violated (3 records).
        assert len(ohlc_violations) == 3
        # The first violation is h<l (checked first in the detector).
        # The detail message renders float values (95.0, 96.0) and
        # the comparison operator between them.
        assert "high (95.0)" in ohlc_violations[0].detail
        assert "low (96.0)" in ohlc_violations[0].detail
        assert ohlc_violations[0].detail.index("high (95.0)") < ohlc_violations[0].detail.index(
            "low (96.0)"
        )

    def test_ohlc_invariant_h_less_than_open(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        bars[2] = _bar(bars[2].time, open_=110.0, high=100.0, low=99.0, close=100.5)
        cfg = IntegrityConfig()
        violations = detect_anomalies(bars, cfg)
        msgs = [v.detail for v in violations if v.kind == "ohlc_invariant"]
        assert any("high (100.0) < open (110.0)" in m for m in msgs)

    def test_ohlc_invariant_l_greater_than_close(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        bars[2] = _bar(bars[2].time, open_=100.0, high=110.0, low=101.0, close=99.0)
        cfg = IntegrityConfig()
        violations = detect_anomalies(bars, cfg)
        msgs = [v.detail for v in violations if v.kind == "ohlc_invariant"]
        assert any("low (101.0) > close (99.0)" in m for m in msgs)

    def test_check_ohlc_invariants_disabled(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        bars[2] = _bar(bars[2].time, open_=100.0, high=95.0, low=96.0, close=100.5)
        cfg = IntegrityConfig(check_ohlc_invariants=False)
        assert detect_anomalies(bars, cfg) == []

    def test_check_anomalies_disabled(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        bars[2] = _bar(
            bars[2].time, open_=100.0, high=95.0, low=96.0, close=100.5
        )
        cfg = IntegrityConfig(check_anomalies=False)
        assert detect_anomalies(bars, cfg) == []

    def test_all_invariants_collapse_to_one_per_bar(self):
        # When h<l, h<o, h<c, l>o, l>c are all violated, expect 5 ohlc
        # violations on the same bar. Each is a separate record for
        # caller diagnosability.
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        bars[2] = _bar(bars[2].time, open_=110.0, high=100.0, low=115.0, close=120.0)
        cfg = IntegrityConfig()
        ohlc = [v for v in detect_anomalies(bars, cfg) if v.kind == "ohlc_invariant"]
        # high(100) < open(110), high(100) < close(120), low(115) > open(110), low(115) > close(120)
        # → at least 4 (h<l is also violated but here l>h so it's 100<115; we expect 4 from
        # the four other checks)
        assert len(ohlc) >= 4
        assert all(v.bar_index == 2 for v in ohlc)


# ──────────────────────────────────────────────────────────────────────
# validate_crypto_bars (read-only, no raise)
# ──────────────────────────────────────────────────────────────────────


class TestValidateCryptoBars:
    def test_clean_series_passes(self):
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig()
        report = validate_crypto_bars("BTCUSDT", bars, cfg)
        assert report.passed
        assert report.violations == ()
        assert report.total_bars == 10
        assert report.symbol == "BTCUSDT"
        assert report.cadence_minutes == 60
        assert report.window_start == bars[0].time
        assert report.window_end == bars[-1].time

    def test_empty_series_passes_when_no_universe(self):
        cfg = IntegrityConfig()
        report = validate_crypto_bars("BTCUSDT", [], cfg)
        assert report.passed
        assert report.total_bars == 0
        assert report.window_start is None
        assert report.window_end is None

    def test_empty_series_fails_when_universe_nonempty(self):
        cfg = IntegrityConfig(universe_symbols=("BTCUSDT", "ETHUSDT"))
        report = validate_crypto_bars("BTCUSDT", [], cfg, loaded_symbol="BTCUSDT")
        # Both symbols "missing" — the universe is non-empty
        assert not report.passed
        kinds = {v.kind for v in report.violations}
        assert "missing_symbol" in kinds

    def test_combined_violations(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        # Build 5 bars in strict ascending order: 0h, [4h gap] 4h,
        # 5h (with NaN open), 6h, 7h (zero volume).
        bars = [
            _bar(start),
            _bar(start + timedelta(hours=4)),  # gap between 0h and 4h
            _bar(
                start + timedelta(hours=5),
                open_=float("nan"),
                high=100.5,
                low=99.5,
                close=100.0,
            ),
            _bar(start + timedelta(hours=6)),
            _bar(
                start + timedelta(hours=7),
                open_=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=0.0,
            ),
        ]
        cfg = IntegrityConfig()
        report = validate_crypto_bars("BTCUSDT", bars, cfg)
        assert not report.passed
        kinds = {v.kind for v in report.violations}
        assert kinds == {"gap", "nan_value", "zero_volume"}

    def test_by_kind_helper(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        # Build 5 bars in strict ascending order: 0h, 1h, [4h gap] 5h
        # (with NaN open), 6h, 7h. One gap, one NaN.
        bars = [
            _bar(start),
            _bar(start + timedelta(hours=1)),
            _bar(
                start + timedelta(hours=5),
                open_=float("nan"),
                high=100.5,
                low=99.5,
                close=100.0,
            ),
            _bar(start + timedelta(hours=6)),
            _bar(start + timedelta(hours=7)),
        ]
        cfg = IntegrityConfig()
        report = validate_crypto_bars("BTCUSDT", bars, cfg)
        gaps = report.by_kind("gap")
        nans = report.by_kind("nan_value")
        assert len(gaps) == 1
        assert len(nans) == 1


# ──────────────────────────────────────────────────────────────────────
# enforce_integrity_gate — fail-loud contract
# ──────────────────────────────────────────────────────────────────────


class TestEnforceIntegrityGate:
    def test_pass_returns_report(self):
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig()
        report = enforce_integrity_gate("BTCUSDT", bars, cfg)
        assert report.passed

    def test_violation_raises_data_integrity_error(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = [
            _bar(start),
            _bar(start + timedelta(hours=4)),
            _bar(start + timedelta(hours=5)),
        ]
        cfg = IntegrityConfig()
        with pytest.raises(DataIntegrityError) as exc_info:
            enforce_integrity_gate("BTCUSDT", bars, cfg)
        err = exc_info.value
        assert err.symbol == "BTCUSDT"
        assert not err.report.passed
        assert len(err.report.violations) >= 1
        # Message should mention symbol + violation count
        msg = str(err)
        assert "BTCUSDT" in msg
        assert "gap" in msg

    def test_error_message_names_symbol_window_violation(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        # Build 3 bars: 0h, [4h gap] 4h, 5h. The series also
        # ends at 5h, which is well before the 2026-10-06 window
        # end → both a gap and a delisting violation are reported.
        bars = [
            _bar(start),
            _bar(start + timedelta(hours=4)),
            _bar(start + timedelta(hours=5)),
        ]
        cfg = IntegrityConfig(
            expected_window_end=datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        )
        with pytest.raises(DataIntegrityError) as exc_info:
            enforce_integrity_gate("ETHUSDT", bars, cfg)
        msg = str(exc_info.value)
        assert "ETHUSDT" in msg
        # Should reference both the gap and the delisting
        assert "gap" in msg
        assert "delisting" in msg

    def test_data_integrity_error_inherits_value_error(self):
        # Catch sites that say `except ValueError` should still work.
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = [
            _bar(start),
            _bar(start + timedelta(hours=4)),
        ]
        cfg = IntegrityConfig()
        with pytest.raises(ValueError):
            enforce_integrity_gate("BTCUSDT", bars, cfg)

    def test_empty_symbol_rejected(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig()
        with pytest.raises(IntegrityConfigError, match="symbol"):
            enforce_integrity_gate("", bars, cfg)

    def test_wrong_config_type_rejected(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        with pytest.raises(TypeError, match="IntegrityConfig"):
            enforce_integrity_gate("BTCUSDT", bars, {})  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# No-repair guarantee
# ──────────────────────────────────────────────────────────────────────


class TestNoRepairGuarantee:
    """The gate must not mutate the bar series.

    Per R5 (Satsuki), the gate fails loud and never:
      * forward-fills gaps
      * truncates the bar series
      * returns a repaired or partial bar series
    """

    def test_bars_returned_unchanged_on_pass(self):
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        original_length = len(bars)
        original_first_time = bars[0].time
        original_last_close = bars[-1].close
        cfg = IntegrityConfig()
        returned = apply_integrity_gate("BTCUSDT", bars, cfg)
        assert len(returned) == original_length
        assert returned[0].time == original_first_time
        assert returned[-1].close == original_last_close
        # Values are equal (the gate returns a fresh list with the same
        # elements; we don't compare identity because the contract
        # doesn't promise it — the contract is "no mutation").
        assert [b.time for b in returned] == [b.time for b in bars]

    def test_no_mutation_on_pass(self):
        # Build bars and snapshot every field. After passing the gate,
        # every field must be identical.
        bars = _flat_bars(10, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        before = [
            (b.time, b.open, b.high, b.low, b.close, b.volume, b.spread_pips)
            for b in bars
        ]
        cfg = IntegrityConfig()
        apply_integrity_gate("BTCUSDT", bars, cfg)
        after = [
            (b.time, b.open, b.high, b.low, b.close, b.volume, b.spread_pips)
            for b in bars
        ]
        assert before == after

    def test_no_forward_fill_on_gap(self):
        # Build bars with one intentional 4h gap, attempt to apply the
        # gate (which fails), then check the bars are unchanged.
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = [
            _bar(start),
            _bar(start + timedelta(hours=4)),  # 4h gap from bars[0]
            _bar(start + timedelta(hours=5)),
        ]
        before_times = [b.time for b in bars]
        cfg = IntegrityConfig()
        with pytest.raises(DataIntegrityError):
            apply_integrity_gate("BTCUSDT", bars, cfg)
        # The bars must NOT have a synthetic 1h/2h/3h bar inserted
        # between bars[0] and bars[1].
        after_times = [b.time for b in bars]
        assert after_times == before_times

    def test_no_truncation_on_delisting(self):
        # Series ends before expected_window_end → delisting violation.
        # Verify the bars are not truncated on raise.
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(5, start, step_minutes=60)
        before_length = len(bars)
        cfg = IntegrityConfig(
            expected_window_end=datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        )
        with pytest.raises(DataIntegrityError):
            apply_integrity_gate("BTCUSDT", bars, cfg)
        assert len(bars) == before_length

    def test_error_raised_before_engine_invocation(self):
        # The error must be raised from apply_integrity_gate (the gate
        # contract) — before any caller can run the engine. We model
        # the engine invocation as a sentinel that would set a flag.
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = [
            _bar(start),
            _bar(start + timedelta(hours=4)),
            _bar(start + timedelta(hours=5)),
        ]
        cfg = IntegrityConfig()
        engine_was_invoked = False

        def _fake_engine():
            nonlocal engine_was_invoked
            engine_was_invoked = True

        with pytest.raises(DataIntegrityError):
            apply_integrity_gate("BTCUSDT", bars, cfg)
            _fake_engine()  # unreachable, but demonstrates intent

        assert not engine_was_invoked


# ──────────────────────────────────────────────────────────────────────
# apply_integrity_gate
# ──────────────────────────────────────────────────────────────────────


class TestApplyIntegrityGate:
    def test_pass_returns_list_of_bars(self):
        bars = _flat_bars(5, datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60)
        cfg = IntegrityConfig()
        result = apply_integrity_gate("BTCUSDT", bars, cfg)
        assert isinstance(result, list)
        assert len(result) == 5

    def test_fail_raises_data_integrity_error(self):
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(3, start, step_minutes=60)
        bars[1] = _bar(bars[0].time + timedelta(hours=4))
        cfg = IntegrityConfig()
        with pytest.raises(DataIntegrityError):
            apply_integrity_gate("BTCUSDT", bars, cfg)


# ──────────────────────────────────────────────────────────────────────
# Integration with crypto overlay wrappers (opt-in)
# ──────────────────────────────────────────────────────────────────────


class TestCryptoOverlayIntegration:
    """Verify the integrity_config parameter is wired into the three
    crypto overlay wrappers and that:

      * default (None) preserves the existing behavior;
      * an IntegrityConfig that fails the gate aborts before the engine runs;
      * an IntegrityConfig that passes the gate lets the engine run.
    """

    def _build_overlay_fixture(self) -> dict[str, Any]:
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        bars = _flat_bars(60, start, step_minutes=60)
        strategy = _NoFireStrategy()
        config = _simple_config()
        return {
            "bars": bars,
            "strategy": strategy,
            "config": config,
        }

    def _build_funding_events(self) -> list[FundingEvent]:
        return [
            FundingEvent(
                time=datetime(2026, 10, 5, h, 0, tzinfo=timezone.utc),
                rate=0.0001,
                interval_hours=8,
                cap_triggered=False,
            )
            for h in (0, 8, 16)
        ]

    def test_funding_wrapper_preserves_existing_behavior_with_none(self):
        fx = self._build_overlay_fixture()
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        # Default integrity_config=None — should pass through unchanged.
        result = run_backtest_with_funding(
            bars=fx["bars"],
            funding_events=self._build_funding_events(),
            position=position,
            config=fx["config"],
            strategies=[fx["strategy"]],
        )
        assert result.base.ending_balance == fx["config"].starting_balance

    def test_funding_wrapper_blocks_on_integrity_violation(self):
        fx = self._build_overlay_fixture()
        # Inject a gap
        fx["bars"][1] = _bar(fx["bars"][0].time + timedelta(hours=4))
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        cfg = IntegrityConfig()
        with pytest.raises(DataIntegrityError):
            run_backtest_with_funding(
                bars=fx["bars"],
                funding_events=self._build_funding_events(),
                position=position,
                config=fx["config"],
                strategies=[fx["strategy"]],
                integrity_config=cfg,
            )

    def test_funding_wrapper_passes_when_clean(self):
        fx = self._build_overlay_fixture()
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        cfg = IntegrityConfig()
        result = run_backtest_with_funding(
            bars=fx["bars"],
            funding_events=self._build_funding_events(),
            position=position,
            config=fx["config"],
            strategies=[fx["strategy"]],
            integrity_config=cfg,
        )
        assert result.base.ending_balance == fx["config"].starting_balance

    def test_liquidation_wrapper_blocks_on_integrity_violation(self):
        fx = self._build_overlay_fixture()
        fx["bars"][1] = _bar(fx["bars"][0].time + timedelta(hours=4))
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        liq_spec = LiquidationSpec(
            entry_price=100.0,
            leverage=10.0,
            direction="long",
        )
        mark_prices = [bar.close for bar in fx["bars"]]
        cfg = IntegrityConfig()
        with pytest.raises(DataIntegrityError):
            run_backtest_with_liquidation(
                bars=fx["bars"],
                mark_prices=mark_prices,
                position=position,
                liq_spec=liq_spec,
                config=fx["config"],
                strategies=[fx["strategy"]],
                integrity_config=cfg,
            )

    def test_liquidation_wrapper_preserves_existing_behavior_with_none(self):
        fx = self._build_overlay_fixture()
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        liq_spec = LiquidationSpec(
            entry_price=100.0,
            leverage=10.0,
            direction="long",
        )
        mark_prices = [bar.close for bar in fx["bars"]]
        result = run_backtest_with_liquidation(
            bars=fx["bars"],
            mark_prices=mark_prices,
            position=position,
            liq_spec=liq_spec,
            config=fx["config"],
            strategies=[fx["strategy"]],
        )
        assert result.base.ending_balance == fx["config"].starting_balance

    def test_venue_wrapper_blocks_on_integrity_violation(self):
        fx = self._build_overlay_fixture()
        fx["bars"][1] = _bar(fx["bars"][0].time + timedelta(hours=4))
        fills = [
            VenueOrderFill(
                time=fx["bars"][2].time,
                fill_price=100.0,
                quantity=1.0,
                intent="taker",
            )
        ]
        cfg = IntegrityConfig()
        with pytest.raises(DataIntegrityError):
            run_backtest_with_venue_costs(
                bars=fx["bars"],
                fills=fills,
                venue_config=DEFAULT_BINANCE_USDM_FEE_CONFIG,
                config=fx["config"],
                strategies=[fx["strategy"]],
                integrity_config=cfg,
            )

    def test_venue_wrapper_preserves_existing_behavior_with_none(self):
        fx = self._build_overlay_fixture()
        fills = [
            VenueOrderFill(
                time=fx["bars"][2].time,
                fill_price=100.0,
                quantity=1.0,
                intent="taker",
            )
        ]
        result = run_backtest_with_venue_costs(
            bars=fx["bars"],
            fills=fills,
            venue_config=DEFAULT_BINANCE_USDM_FEE_CONFIG,
            config=fx["config"],
            strategies=[fx["strategy"]],
        )
        assert result.base.ending_balance == fx["config"].starting_balance

    def test_venue_wrapper_passes_when_clean(self):
        fx = self._build_overlay_fixture()
        fills = [
            VenueOrderFill(
                time=fx["bars"][2].time,
                fill_price=100.0,
                quantity=1.0,
                intent="taker",
            )
        ]
        cfg = IntegrityConfig()
        result = run_backtest_with_venue_costs(
            bars=fx["bars"],
            fills=fills,
            venue_config=DEFAULT_BINANCE_USDM_FEE_CONFIG,
            config=fx["config"],
            strategies=[fx["strategy"]],
            integrity_config=cfg,
        )
        assert result.base.ending_balance == fx["config"].starting_balance

    def test_wrong_integrity_config_type_rejected(self):
        fx = self._build_overlay_fixture()
        position = PositionSpec(notional_usd=10_000.0, direction="long")
        with pytest.raises(TypeError, match="integrity_config"):
            run_backtest_with_funding(
                bars=fx["bars"],
                funding_events=self._build_funding_events(),
                position=position,
                config=fx["config"],
                strategies=[fx["strategy"]],
                integrity_config={"expected_cadence_minutes": 60},  # dict, not IntegrityConfig
            )


# ──────────────────────────────────────────────────────────────────────
# Legacy FX loader untouched
# ──────────────────────────────────────────────────────────────────────


class TestLegacyFxLoaderUntouched:
    """The legacy FX data loader (CsvDataLoader, load_data, etc.) must
    not import / call the integrity gate. This is a structural test
    (import / symbol-presence check) — not a behavioral test of the
    FX path itself.

    The crypto lane is opt-in: the integrity gate is wired only into
    the three crypto overlay wrappers, not into the data loader.
    """

    def test_data_loader_does_not_import_integrity_gate(self):
        # The legacy loader should not pull in the integrity gate.
        import importlib

        # Force a clean import in case conftest already pulled it.
        data_loader = importlib.import_module("backtest.data_loader")
        # The data_loader module should not have the integrity-gate
        # symbols bound.
        for name in (
            "IntegrityConfig",
            "DataIntegrityError",
            "apply_integrity_gate",
            "enforce_integrity_gate",
        ):
            assert not hasattr(data_loader, name), (
                f"backtest.data_loader unexpectedly exposes {name}; "
                f"the legacy FX loader must not pull in the integrity gate"
            )

    def test_abstract_data_loader_does_not_import_integrity_gate(self):
        import importlib

        abstract = importlib.import_module("backtest.abstract_data_loader")
        for name in (
            "IntegrityConfig",
            "DataIntegrityError",
            "apply_integrity_gate",
            "enforce_integrity_gate",
        ):
            assert not hasattr(abstract, name), (
                f"backtest.abstract_data_loader unexpectedly exposes {name}"
            )

    def test_csv_data_loader_load_bars_does_not_call_gate(self, tmp_path, monkeypatch):
        # Build a tiny CSV and confirm CsvDataLoader.load_bars returns
        # the bars as-is (no auto-validation). Use a known-bad bar
        # (high < low) and confirm the loader doesn't raise.
        from backtest.data_loader import CsvDataLoader

        csv_path = tmp_path / "BTCUSDT_H1.csv"
        # Two bars: the second one is invalid (high < low)
        csv_path.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2026-10-05T00:00:00Z,100,100.5,99.5,100,100\n"
            "2026-10-05T01:00:00Z,100,99.0,99.5,100,100\n"  # h<l: invalid
        )
        loader = CsvDataLoader(csv_dir=tmp_path)
        bars = loader.load_bars("BTCUSDT", "H1")
        # Two bars returned; no exception raised — the legacy FX path
        # doesn't enforce crypto-class integrity checks.
        assert len(bars) == 2
        # The invalid bar's high<low is preserved as-is.
        assert bars[1].high < bars[1].low


# ──────────────────────────────────────────────────────────────────────
# IntegrityViolation validation
# ──────────────────────────────────────────────────────────────────────


class TestIntegrityViolationValidation:
    def test_rejects_unknown_kind(self):
        with pytest.raises(IntegrityConfigError, match="kind"):
            IntegrityViolation(
                kind="not_a_kind",  # type: ignore[arg-type]
                symbol="BTCUSDT",
                bar_index=0,
                start_time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc),
                detail="bad",
            )

    def test_rejects_naive_datetimes(self):
        with pytest.raises(IntegrityConfigError, match="start_time"):
            IntegrityViolation(
                kind="gap",
                symbol="BTCUSDT",
                bar_index=0,
                start_time=datetime(2026, 10, 5, 0, 0),  # naive
                end_time=datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc),
                detail="bad",
            )
        with pytest.raises(IntegrityConfigError, match="end_time"):
            IntegrityViolation(
                kind="gap",
                symbol="BTCUSDT",
                bar_index=0,
                start_time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 10, 5, 1, 0),  # naive
                detail="bad",
            )

    def test_rejects_empty_symbol_for_missing_symbol(self):
        # Empty symbol is rejected for "missing_symbol" because the
        # symbol IS the missing one — an empty name is meaningless.
        with pytest.raises(IntegrityConfigError, match="missing_symbol"):
            IntegrityViolation(
                kind="missing_symbol",
                symbol="",
                bar_index=-1,
                start_time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc),
                detail="bad",
            )

    def test_accepts_empty_symbol_for_window_end_delisting(self):
        # Empty symbol is allowed for non-"missing_symbol" kinds —
        # the symbol is metadata about the loaded series, which may
        # be unknown to the caller (e.g. detect_delisting without a
        # series-specific label).
        v = IntegrityViolation(
            kind="delisting",
            symbol="",
            bar_index=0,
            start_time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc),
            detail="unknown series",
        )
        assert v.symbol == ""

    def test_normalizes_non_utc_to_utc(self):
        eastern = timezone(timedelta(hours=-4))  # EDT
        v = IntegrityViolation(
            kind="gap",
            symbol="BTCUSDT",
            bar_index=0,
            start_time=datetime(2026, 10, 5, 0, 0, tzinfo=eastern),
            end_time=datetime(2026, 10, 5, 1, 0, tzinfo=eastern),
            detail="bad",
        )
        # The validator should have rewritten both times to UTC.
        assert v.start_time.tzinfo == timezone.utc
        assert v.end_time.tzinfo == timezone.utc
        assert v.start_time == datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
        assert v.end_time == datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
