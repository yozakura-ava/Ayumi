"""Crypto-native strategy templates — card fe773687.

Covers: signal emission on synthetic crypto-shaped bars, pair-config
binding on crypto symbols, registry round-trip via
``build_strategy_from_template`` + ``RegistryStrategyTemplate``, the
declared-grid guard, and (when the persisted sweep bars exist) nonzero
signal emission on real Binance.US H1 bars.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from core.types import Bar, MarketState, StrategySignal, TradeDirection

from forex_bot.factory.bridge import (
    BridgeError,
    RegistryStrategyTemplate,
    build_strategy_from_template,
)
from forex_bot.strategies.crypto_native import (
    CRYPTO_NATIVE_STRATEGY_IDS,
    CRYPTO_PARAM_GRIDS,
    CryptoDonchianBreakout,
    CryptoEMACrossTrend,
    CryptoZScoreMeanReversion,
    build_crypto_native_strategy,
)
from forex_bot.strategies.registry import default_registry

WORKTREE = Path(__file__).resolve().parents[2]
REAL_BARS_CSV = WORKTREE / "data" / "sweep_real_data_bars" / "real_crypto_bars_h1.csv"


# ---------------------------------------------------------------------------
# Synthetic crypto-shaped bar helpers
# ---------------------------------------------------------------------------


def _trend_bars(n: int = 400, start: float = 60_000.0, drift: float = 0.0012,
                vol: float = 0.004, seed: int = 7) -> list[Bar]:
    """GBM bars with drift — crypto-shaped (H1 σ ≈ 0.4% of price).

    Includes a leading flat/declining phase so the fast/slow EMA cross
    happens AFTER the 120-bar warmup (a pure uptrend from bar 0 crosses
    once, before any strategy is allowed to speak).
    """
    rng = random.Random(seed)  # noqa: S311 — synthetic fixture, not crypto/security
    bars: list[Bar] = []
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    price = start
    warmup = max(150, n // 3)
    for i in range(n):
        phase_drift = -0.0004 if i < warmup else drift * 1.4
        shock = rng.gauss(0.0, vol)
        o = price
        c = price * (1.0 + phase_drift + shock)
        hi = max(o, c) * (1.0 + abs(rng.gauss(0.0, vol / 2)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0.0, vol / 2)))
        bars.append(Bar(time=t0 + timedelta(hours=i), open=o, high=hi,
                        low=lo, close=c, volume=1.0))
        price = c
    return bars


def _range_bars(n: int = 400, start: float = 3_000.0, vol: float = 0.005,
                seed: int = 11) -> list[Bar]:
    """Mean-reverting ( Ornstein–Uhlenbeck-ish ) bars around a level."""
    rng = random.Random(seed)  # noqa: S311 — synthetic fixture, not crypto/security
    bars: list[Bar] = []
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    level = start
    price = start
    for i in range(n):
        price += (level - price) * 0.05 + rng.gauss(0.0, vol * start / 10.0)
        o = price * (1.0 + rng.gauss(0.0, vol / 4))
        c = price
        hi = max(o, c) * 1.002
        lo = min(o, c) * 0.998
        bars.append(Bar(time=t0 + timedelta(hours=i), open=o, high=hi,
                        low=lo, close=c, volume=1.0))
    return bars


def _run_evaluate(strategy, bars: list[Bar]) -> list[StrategySignal]:
    signals = []
    for i in range(len(bars)):
        state = MarketState(bars=bars[: i + 1])
        sig = strategy.evaluate(state)
        if sig is not None:
            signals.append(sig)
    return signals


# ---------------------------------------------------------------------------
# Signal emission on synthetic crypto-shaped bars
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pair_price", [(60_000.0, "BTC-scale"), (150.0, "SOL-scale")])
def test_ema_cross_emits_on_trending_crypto(pair_price) -> None:
    start, label = pair_price
    strat = CryptoEMACrossTrend()
    bars = _trend_bars(500, start=start)
    signals = _run_evaluate(strat, bars)
    assert signals, f"EMA-cross emitted 0 signals on trending {label} bars"
    longs = [s for s in signals if s.direction is TradeDirection.LONG]
    assert longs, f"no LONG signals on upward drift ({label})"
    for s in signals:
        assert 0.0 < s.confidence < 1.0
        assert s.stop_loss > 0.0
        assert s.take_profit_1 > 0.0
        assert s.rationale


def test_ema_cross_silent_on_flat_bars() -> None:
    """No drift + no slope-gate crossings ⇒ near-zero signals on flat bars."""
    strat = CryptoEMACrossTrend()
    bars = _range_bars(500, vol=0.0005, seed=3)  # tight OU range
    signals = _run_evaluate(strat, bars)
    assert len(signals) <= 2, f"churned {len(signals)} signals on flat bars"


def test_donchian_emits_on_breakout() -> None:
    strat = CryptoDonchianBreakout()
    bars = _range_bars(300, vol=0.002, seed=5)
    # Spike a decisive breakout close after a long consolidation.
    t_end = bars[-1].time + timedelta(hours=1)
    hi_ref = max(b.high for b in bars[-100:])
    spike_price = hi_ref * 1.06
    bars = bars + [
        Bar(time=t_end, open=spike_price * 0.999, high=spike_price * 1.001,
            low=spike_price * 0.998, close=spike_price, volume=1.0),
    ]
    signals = _run_evaluate(strat, bars)
    assert signals, "Donchian emitted 0 signals on a decisive breakout"
    last = signals[-1]
    assert last.direction is TradeDirection.LONG
    assert last.entry_price == pytest.approx(spike_price)


def test_zscore_emits_on_extension() -> None:
    strat = CryptoZScoreMeanReversion()
    bars = _range_bars(300, vol=0.002, seed=13)
    lo_ref = min(b.low for b in bars[-100:])
    dip = lo_ref * 0.96
    bars = bars + [
        Bar(time=bars[-1].time + timedelta(hours=1), open=dip * 1.001,
            high=dip * 1.002, low=dip * 0.999, close=dip, volume=1.0),
    ]
    signals = _run_evaluate(strat, bars)
    assert signals, "z-score emitted 0 signals on a −z extension"
    assert signals[-1].direction is TradeDirection.LONG


def test_all_strategies_need_min_bars() -> None:
    few = _trend_bars(60)
    for strat in (CryptoEMACrossTrend(), CryptoDonchianBreakout(), CryptoZScoreMeanReversion()):
        assert strat.evaluate(MarketState(bars=few)) is None


def test_no_pip_units_anywhere() -> None:
    """Guardrail: crypto-native module must not touch pip machinery."""
    import forex_bot.strategies.crypto_native as m
    src = Path(m.__file__).read_text()
    for banned in ("pip_value_for_symbol", "utils.pip_value", "_resolve_pip_size(",
                   "spread_pips=", "min_range_pips", "buffer_pips"):
        assert banned not in src, f"FX-pip anti-pattern {banned!r} found in crypto_native"


# ---------------------------------------------------------------------------
# Pair-config binding on crypto symbols
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
def test_registry_binds_on_crypto_symbols(symbol: str) -> None:
    reg = default_registry()
    ids = {c.strategy_id for c in reg.get_for_symbol(symbol)}
    assert set(CRYPTO_NATIVE_STRATEGY_IDS) <= ids


def test_registry_does_not_bind_on_fx_symbols() -> None:
    reg = default_registry()
    ids = {c.strategy_id for c in reg.get_for_symbol("EURUSD")}
    assert not (set(CRYPTO_NATIVE_STRATEGY_IDS) & ids)


@pytest.mark.parametrize("sid", CRYPTO_NATIVE_STRATEGY_IDS)
def test_registry_configs_are_crypto_paired(sid: str) -> None:
    cfg = default_registry().get(sid)
    assert cfg is not None
    assert cfg.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert "H1" in cfg.timeframes
    assert cfg.active


# ---------------------------------------------------------------------------
# Registry round-trip via build_strategy_from_template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sid", CRYPTO_NATIVE_STRATEGY_IDS)
def test_bridge_round_trip(sid: str) -> None:
    reg = default_registry()
    cfg = reg.get(sid)
    assert cfg is not None
    tmpl = RegistryStrategyTemplate(cfg)
    for pair in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        strat = build_strategy_from_template(tmpl, {}, pair)
        assert strat is not None
        assert isinstance(strat.name, str)
        assert callable(strat.evaluate)


def test_bridge_round_trip_produces_signals_on_crypto_bars() -> None:
    tmpl = RegistryStrategyTemplate(default_registry().get("crypto_ema_cross_trend"))  # type: ignore[arg-type]
    strat = build_strategy_from_template(tmpl, {}, "BTCUSDT")
    signals = _run_evaluate(strat, _trend_bars(500))
    assert signals


# ---------------------------------------------------------------------------
# Declared-grid guard (no post-hoc tuning)
# ---------------------------------------------------------------------------


def test_grid_guard_rejects_undeclared_params() -> None:
    with pytest.raises(ValueError, match="outside declared grid"):
        build_crypto_native_strategy("crypto_ema_cross_trend", {"rsi_period": 14})


def test_grid_guard_rejects_undeclared_values() -> None:
    """A declared key with a value outside the frozen grid is cherry-picking."""
    with pytest.raises(ValueError, match="outside declared grid"):
        build_crypto_native_strategy("crypto_ema_cross_trend", {"fast_period": 7})


def test_grid_values_are_within_declared_grid() -> None:
    for sid in CRYPTO_NATIVE_STRATEGY_IDS:
        strat = build_crypto_native_strategy(sid)
        for key, allowed in CRYPTO_PARAM_GRIDS[sid].items():
            val = getattr(strat.config, key)
            assert val in allowed, f"{sid}.{key}={val} not in declared grid {allowed}"


def test_unknown_strategy_id_raises() -> None:
    with pytest.raises(ValueError, match="unknown crypto-native"):
        build_crypto_native_strategy("crypto_laser_beam")


def test_bridge_error_for_unregistered_id() -> None:
    reg = default_registry()
    cfg = reg.get("crypto_ema_cross_trend")
    assert cfg is not None
    tmpl = RegistryStrategyTemplate(cfg)
    # Corrupt the id to simulate an unregistered entry.
    object.__setattr__(tmpl, "archetype_id", "trend")
    tmpl.strategy_id = "crypto_not_real"
    with pytest.raises(BridgeError):
        build_strategy_from_template(tmpl, {}, "BTCUSDT")


# ---------------------------------------------------------------------------
# Real persisted bars (feasibility check on live Binance.US H1 data)
# ---------------------------------------------------------------------------


def _load_real_bars(symbol: str) -> list[Bar]:
    import csv

    bars: list[Bar] = []
    with REAL_BARS_CSV.open() as fh:
        for row in csv.DictReader(fh):
            if row["symbol"] != symbol:
                continue
            bars.append(
                Bar(
                    time=datetime.fromisoformat(row["time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    spread_pips=float(row["spread_pips"]),
                )
            )
    return bars


@pytest.mark.skipif(not REAL_BARS_CSV.exists(), reason="persisted real sweep bars not present")
@pytest.mark.parametrize("sid", CRYPTO_NATIVE_STRATEGY_IDS)
def test_nonzero_signals_on_real_bars(sid: str) -> None:
    bars = _load_real_bars("BTCUSDT")
    if len(bars) < 150:
        pytest.skip(f"only {len(bars)} real BTCUSDT bars persisted")
    strat = build_crypto_native_strategy(sid)
    signals = _run_evaluate(strat, bars)
    assert signals, f"{sid} emitted 0 signals on {len(bars)} real BTCUSDT H1 bars"
    # Cooldown keeps emission sane (not a signal every bar).
    assert len(signals) < len(bars) / 10, f"{sid} churned {len(signals)} signals"


def test_grid_and_centers_consistent() -> None:
    import forex_bot.strategies.crypto_native as m
    assert callable(m.build_crypto_native_strategy)
    assert set(m.CRYPTO_PARAM_GRIDS) == set(m.DEFAULT_GRID_CENTERS)


def test_zscore_math_on_flat_series() -> None:
    """z-score of a perfectly flat series is undefined ⇒ no signal (std=0)."""
    bars = _flat_bars(200)
    strat = CryptoZScoreMeanReversion()
    assert strat.evaluate(MarketState(bars=bars)) is None


def _flat_bars(n: int) -> list[Bar]:
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return [
        Bar(time=t0 + timedelta(hours=i), open=100.0, high=100.0,
            low=100.0, close=100.0, volume=1.0)
        for i in range(n)
    ]


def test_signal_atr_ladder_monotonic() -> None:
    """TP ladder must be monotonic in R-multiples on both directions."""
    bars = _trend_bars(500)
    strat = CryptoEMACrossTrend()
    signals = _run_evaluate(strat, bars)
    assert signals
    for s in signals:
        if s.direction is TradeDirection.LONG:
            assert s.stop_loss < s.entry_price < s.take_profit_1 < s.take_profit_2 < s.take_profit_3
        else:
            assert s.stop_loss > s.entry_price > s.take_profit_1 > s.take_profit_2 > s.take_profit_3
