"""Tests for ``forex_bot.data.crypto_adapter`` — Binance CRYPTO-A2 adapter.

Card: 73efa3b9-beb5-4eb6-92a4-66b546bbeb25 (CRYPTO-A2)
Sprint: 2026-10-04-crypto-phase-a

Scope rules (HR5): this file contains the full scoped test set for the
adapter. Run only via ``scripts/run_test_scope.sh
tests/test_crypto_adapter.py --project ayumi``. Do not include in
``pytest tests/`` blanket runs — they would touch production source paths
out of scope for this card.

All tests use synthetic fixtures and a mocked transport
(``EgressTransport(get_callable=fake_get, ...)``). No live network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup: src/forex_bot on sys.path so ``data.crypto_adapter`` resolves.
# Mirrors the conftest.py setup at suite level for these scoped runs.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_FX = _PROJECT_ROOT / "src" / "forex_bot"
_SRC_ROOT = _PROJECT_ROOT / "src"
for p in (_SRC_FX, _SRC_ROOT):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from data.crypto_adapter import (  # noqa: E402
    ALLOWED_INTERVALS,
    DEFAULT_INTERVAL,
    ENDPOINT_FUNDING_FUTURES,
    ENDPOINT_KLINES_SPOT,
    ENDPOINT_OI_FUTURES,
    ENDPOINTS,
    EVAL_GATE_BARS,
    HTTP_STATUS_LEGAL_BLOCK,
    PREFILL_MIN_BARS,
    SUPPORTED_SYMBOLS,
    WEIGHT_BUDGET_PER_MINUTE,
    BinanceCryptoAdapter,
    CryptoAdapterError,
    CryptoBar,
    EgressBlockedError,
    EgressPolicy,
    EgressTransport,
    FundingRateSnapshot,
    InsufficientDataError,
    IntervalError,
    OpenInterestSnapshot,
    RateLimitError,
    SymbolError,
    TokenBucket,
    _FakeClock,
    _TransportResponse,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_clock() -> _FakeClock:
    return _FakeClock(start=1000.0)


@pytest.fixture
def transport(fake_clock: _FakeClock):
    """An EgressTransport with a fake clock and a recording fake GET.

    Tests mutate ``transport._get`` to install their own response
    sequence (or wrap with ``fake_get_recorder`` to capture calls).
    """
    calls: list[dict] = []
    fake_get = _make_recorder(calls)
    t = EgressTransport(
        proxy_url="socks5h://user:pass@127.0.0.1:11001",
        bucket=TokenBucket(
            capacity=WEIGHT_BUDGET_PER_MINUTE,
            refill_rate_per_second=WEIGHT_BUDGET_PER_MINUTE / 60.0,
            clock=fake_clock,
        ),
        clock=fake_clock,
        get_callable=fake_get,
    )
    t.calls = calls  # type: ignore[attr-defined]
    return t


def _make_recorder(calls: list[dict]):
    """Build a fake ``get`` callable that records (url, params, proxies)
    and returns a programmable response.

    Tests set ``recorder.next`` to a list of :class:`_TransportResponse`
    objects; the recorder pops one per call.
    """

    class _Recorder:
        def __init__(self) -> None:
            self.next: list[_TransportResponse] = []
            self.default = _TransportResponse(
                status_code=200, json_body=[], text=""
            )

        def __call__(self, url, params=None, proxies=None, **_):
            calls.append({"url": url, "params": params, "proxies": proxies})
            if self.next:
                return self.next.pop(0)
            return self.default

    recorder = _Recorder()
    # Bind the recorder to the calls list so tests can reach it via ``recorder``
    return recorder


def _kline(open_time_ms: int, o: float, h: float, lo: float, c: float, v: float) -> list:
    """Build a Binance-style kline array (first 6 slots used)."""
    return [open_time_ms, str(o), str(h), str(lo), str(c), str(v)]


def _klines_payload(count: int, start_ms: int, base: float = 30000.0, step: float = 50.0) -> list[list]:
    """Build ``count`` synthetic 1h klines starting at ``start_ms``.

    Returns oldest-first ordering (matches Binance first-page response).
    Each bar advances ``base`` by ``step`` so prices are distinct.
    """
    out = []
    for i in range(count):
        ts = start_ms + i * 3600 * 1000
        o = base + i * step
        h = o + step / 2
        lo = o - step / 2
        c = o + step / 4
        v = 100.0 + i
        out.append(_kline(ts, o, h, lo, c, v))
    return out


def _oi_payload(symbol: str, oi_value: float = 50000.0, oi_notional: float | None = 12345678.0,
                ts_ms: int | None = None) -> dict:
    """Build a Binance-style open-interest response."""
    return {
        "symbol": symbol,
        "openInterest": str(oi_value),
        "sumOpenInterestValue": str(oi_notional) if oi_notional is not None else "",
        "time": ts_ms if ts_ms is not None else 1_700_000_000_000,
    }


def _funding_payload(symbol: str, rate: float = 0.0001, mark: float | None = 30000.0,
                     ts_ms: int | None = None) -> dict:
    """Build a Binance-style funding-rate response (single entry)."""
    return {
        "symbol": symbol,
        "fundingRate": str(rate),
        "fundingTime": ts_ms if ts_ms is not None else 1_700_000_000_000,
        "markPrice": str(mark) if mark is not None else "",
    }


# ---------------------------------------------------------------------------
# 1. Endpoint registry + egress policy
# ---------------------------------------------------------------------------


class TestEndpointRegistry:
    def test_endpoint_registry_has_three_endpoints(self) -> None:
        assert set(ENDPOINTS.keys()) == {"klines_spot", "open_interest", "funding_rate"}

    def test_spot_klines_use_direct_us(self) -> None:
        ep = ENDPOINTS["klines_spot"]
        assert ep.policy == EgressPolicy.DIRECT_US
        assert ep.url.startswith("https://api.binance.us")

    def test_futures_oi_uses_ei_proxy(self) -> None:
        ep = ENDPOINTS["open_interest"]
        assert ep.policy == EgressPolicy.EI_PROXY
        assert ep.url.startswith("https://fapi.binance.com")

    def test_futures_funding_uses_ei_proxy(self) -> None:
        ep = ENDPOINTS["funding_rate"]
        assert ep.policy == EgressPolicy.EI_PROXY
        assert ep.url.startswith("https://fapi.binance.com")

    def test_all_endpoints_have_positive_weight(self) -> None:
        for ep in ENDPOINTS.values():
            assert ep.weight >= 1

    def test_endpoint_is_frozen(self) -> None:
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            ENDPOINT_KLINES_SPOT.weight = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. EgressTransport — policy resolution + 451 fail-loud
# ---------------------------------------------------------------------------


class TestEgressTransport:
    def test_get_uses_no_proxy_for_direct_us(self, transport: EgressTransport) -> None:
        # Program the recorder to return a successful empty payload
        transport._get.next = [_TransportResponse(status_code=200, json_body=[])]
        transport.get(ENDPOINT_KLINES_SPOT, params={"symbol": "BTCUSDT"})
        assert transport.calls[0]["proxies"] is None

    def test_get_routes_ei_proxy_through_proxy_url(self, transport: EgressTransport) -> None:
        transport._get.next = [_TransportResponse(status_code=200, json_body={})]
        transport.get(ENDPOINT_OI_FUTURES, params={"symbol": "BTCUSDT"})
        assert transport.calls[0]["proxies"] == {
            "http": "socks5h://user:pass@127.0.0.1:11001",
            "https": "socks5h://user:pass@127.0.0.1:11001",
        }

    def test_451_raises_egress_blocked(self, transport: EgressTransport) -> None:
        transport._get.next = [_TransportResponse(
            status_code=HTTP_STATUS_LEGAL_BLOCK,
            text="Unavailable For Legal Reasons",
        )]
        with pytest.raises(EgressBlockedError) as exc:
            transport.get(ENDPOINT_OI_FUTURES, params={"symbol": "BTCUSDT"})
        assert "451" in str(exc.value)
        assert "fall back" in str(exc.value).lower()

    def test_451_on_direct_us_endpoint_also_raises(self, transport: EgressTransport) -> None:
        """Defensive: if the venue starts 451'ing api.binance.us (e.g.
        legal block expands), the adapter still fails loud rather than
        silently falling back to fapi.binance.com direct (which would
        451 anyway and burn the proxy budget)."""
        transport._get.next = [_TransportResponse(status_code=HTTP_STATUS_LEGAL_BLOCK, text="")]
        with pytest.raises(EgressBlockedError):
            transport.get(ENDPOINT_KLINES_SPOT, params={"symbol": "BTCUSDT"})

    def test_other_4xx_raises_crypto_adapter_error(self, transport: EgressTransport) -> None:
        transport._get.next = [_TransportResponse(status_code=400, text="bad symbol")]
        with pytest.raises(CryptoAdapterError):
            transport.get(ENDPOINT_KLINES_SPOT, params={"symbol": "BTCUSDT"})

    def test_ei_proxy_without_proxy_url_raises(self, fake_clock: _FakeClock) -> None:
        """Without a configured proxy_url, EI_PROXY endpoints must
        raise — never default to direct egress."""
        t = EgressTransport(
            proxy_url=None,
            bucket=TokenBucket(
                capacity=WEIGHT_BUDGET_PER_MINUTE,
                refill_rate_per_second=WEIGHT_BUDGET_PER_MINUTE / 60.0,
                clock=fake_clock,
            ),
            clock=fake_clock,
            get_callable=lambda *a, **kw: _TransportResponse(status_code=200, json_body={}),
        )
        with pytest.raises(EgressBlockedError) as exc:
            t.get(ENDPOINT_OI_FUTURES, params={"symbol": "BTCUSDT"})
        assert "no proxy_url" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. TokenBucket — refill-based rate limiter
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_initial_tokens_equal_capacity(self, fake_clock: _FakeClock) -> None:
        b = TokenBucket(capacity=10, refill_rate_per_second=1.0, clock=fake_clock)
        assert b.tokens() == pytest.approx(10.0)

    def test_acquire_drains_tokens(self, fake_clock: _FakeClock) -> None:
        b = TokenBucket(capacity=10, refill_rate_per_second=1.0, clock=fake_clock)
        b.acquire(weight=4)
        assert b.tokens() == pytest.approx(6.0)

    def test_acquire_blocks_until_refill(self, fake_clock: _FakeClock) -> None:
        b = TokenBucket(capacity=10, refill_rate_per_second=2.0, clock=fake_clock)
        b.acquire(weight=10)  # empty the bucket
        assert b.tokens() == pytest.approx(0.0)
        b.acquire(weight=4)  # needs 4 tokens; rate = 2/sec → 2s sleep
        assert fake_clock.sleeps[-1] == pytest.approx(2.0)
        # After sleeping 2s, bucket holds exactly 4 tokens, all consumed.
        assert b.tokens() == pytest.approx(0.0)

    def test_acquire_weight_above_capacity_raises(self, fake_clock: _FakeClock) -> None:
        b = TokenBucket(capacity=10, refill_rate_per_second=1.0, clock=fake_clock)
        with pytest.raises(RateLimitError):
            b.acquire(weight=20)

    def test_capacity_validation(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=0, refill_rate_per_second=1.0, clock=_FakeClock())
        with pytest.raises(ValueError):
            TokenBucket(capacity=10, refill_rate_per_second=0.0, clock=_FakeClock())

    def test_refill_caps_at_capacity(self, fake_clock: _FakeClock) -> None:
        b = TokenBucket(capacity=10, refill_rate_per_second=5.0, clock=fake_clock)
        b.acquire(weight=10)
        fake_clock.advance(100.0)  # enough time to massively over-fill
        assert b.tokens() == pytest.approx(10.0)

    def test_shared_bucket_drains_across_klines_oi_funding(self, fake_clock: _FakeClock) -> None:
        """The transport's bucket is shared across all 3 endpoint types."""
        b = TokenBucket(capacity=12, refill_rate_per_second=1.0, clock=fake_clock)
        b.acquire(weight=5)
        b.acquire(weight=5)
        assert b.tokens() == pytest.approx(2.0)
        # Next call would need 3 more tokens (5 total) → 3s sleep at 1/sec refill.
        b.acquire(weight=5)
        assert fake_clock.sleeps[-1] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# 4. Interval + symbol policy
# ---------------------------------------------------------------------------


class TestIntervalPolicy:
    def test_default_interval_is_1h(self) -> None:
        assert DEFAULT_INTERVAL == "1h"
        assert "1h" in ALLOWED_INTERVALS

    def test_4h_is_allowed(self) -> None:
        a = BinanceCryptoAdapter(interval="4h", transport=_empty_transport())
        assert a.interval == "4h"

    def test_1m_is_rejected(self) -> None:
        with pytest.raises(IntervalError) as exc:
            BinanceCryptoAdapter(interval="1m", transport=_empty_transport())
        assert "1m" in str(exc.value)

    def test_5m_is_rejected(self) -> None:
        with pytest.raises(IntervalError):
            BinanceCryptoAdapter(interval="5m", transport=_empty_transport())

    def test_daily_is_rejected(self) -> None:
        """Daily not in scope for Phase A — daily bars + 55-bar gate = too sparse."""
        with pytest.raises(IntervalError):
            BinanceCryptoAdapter(interval="1d", transport=_empty_transport())


class TestSymbolPolicy:
    def test_supported_symbols(self) -> None:
        assert SUPPORTED_SYMBOLS == ("BTCUSDT", "ETHUSDT", "SOLUSDT")

    def test_construction_accepts_whitelisted_symbols(self) -> None:
        a = BinanceCryptoAdapter(
            symbols=("BTCUSDT", "ETHUSDT"),
            transport=_empty_transport(),
        )
        assert a.supported_symbols() == ("BTCUSDT", "ETHUSDT")

    def test_construction_rejects_unknown_symbol(self) -> None:
        with pytest.raises(SymbolError) as exc:
            BinanceCryptoAdapter(symbols=("DOGEUSDT",), transport=_empty_transport())
        assert "DOGEUSDT" in str(exc.value)

    def test_method_access_rejects_unknown_symbol(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(transport=transport)
        with pytest.raises(SymbolError):
            a.bars("XRPUSDT")

    def test_default_symbols_are_all_three(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(transport=transport)
        assert a.supported_symbols() == ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def _empty_transport():
    """Tiny helper: a transport that returns an empty 200 for any call."""
    return EgressTransport(
        proxy_url="socks5h://u:p@127.0.0.1:11001",
        get_callable=lambda *a, **kw: _TransportResponse(status_code=200, json_body=[]),
    )


# ---------------------------------------------------------------------------
# 5. CryptoBar shape — backtest.types.Bar compatibility
# ---------------------------------------------------------------------------


class TestCryptoBarShape:
    def test_bar_has_backtest_bar_fields(self) -> None:
        bar = CryptoBar(
            symbol="BTCUSDT",
            time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            open=30000.0,
            high=30100.0,
            low=29900.0,
            close=30050.0,
            volume=123.4,
            interval="1h",
        )
        # Same field set as backtest.types.Bar — minus spread_pips which
        # is forex-specific. The crypto path doesn't carry spread_pips
        # because Binance.US is the venue and spread is venue-internal.
        assert hasattr(bar, "time")
        assert hasattr(bar, "open")
        assert hasattr(bar, "high")
        assert hasattr(bar, "low")
        assert hasattr(bar, "close")
        assert hasattr(bar, "volume")
        assert hasattr(bar, "symbol")
        assert hasattr(bar, "interval")

    def test_from_binance_kline_parses_array(self) -> None:
        kline = [1_700_000_000_000, "30000.0", "30100.0", "29900.0", "30050.0", "123.4"]
        bar = CryptoBar.from_binance_kline("BTCUSDT", "1h", kline)
        assert bar.symbol == "BTCUSDT"
        assert bar.open == 30000.0
        assert bar.high == 30100.0
        assert bar.low == 29900.0
        assert bar.close == 30050.0
        assert bar.volume == 123.4
        assert bar.time == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
        assert bar.interval == "1h"

    def test_from_binance_kline_rejects_short_array(self) -> None:
        with pytest.raises(ValueError):
            CryptoBar.from_binance_kline("BTCUSDT", "1h", [1, 2, 3])

    def test_backtest_bar_construction_compat(self) -> None:
        """A CryptoBar can be unpacked into a backtest.types.Bar if a
        consumer imports that module — verifying the field names
        match. We attempt the import; if backtest.types is unavailable
        in the test env (different sys.path) we skip the assertion."""
        try:
            from backtest.types import Bar as BacktestBar  # type: ignore
        except Exception:
            pytest.skip("backtest.types not importable in this env")
        from dataclasses import asdict as _asdict

        cb = CryptoBar(
            symbol="BTCUSDT",
            time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0, interval="1h",
        )
        # Bar(time=..., open=..., high=..., low=..., close=..., volume=...)
        bb = BacktestBar(
            time=cb.time,
            open=cb.open,
            high=cb.high,
            low=cb.low,
            close=cb.close,
            volume=cb.volume,
        )
        assert _asdict(bb)["time"] == cb.time


# ---------------------------------------------------------------------------
# 6. Prefill pagination + 55-bar evaluation gate
# ---------------------------------------------------------------------------


class TestPrefill:
    def test_prefill_reaches_minimum_bars(self, transport: EgressTransport) -> None:
        # Program 2 pages: 1000 + 500 = 1500 (>= 500 minimum)
        page1 = _klines_payload(1000, start_ms=1_700_000_000_000)
        page2 = _klines_payload(500, start_ms=1_700_000_000_000 - 1000 * 3600 * 1000)
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=page1),
            _TransportResponse(status_code=200, json_body=page2),
            _TransportResponse(status_code=200, json_body=[]),  # OI
            _TransportResponse(status_code=200, json_body=[]),  # OI
            _TransportResponse(status_code=200, json_body=[]),  # OI
            _TransportResponse(status_code=200, json_body=[]),  # funding
            _TransportResponse(status_code=200, json_body=[]),  # funding
            _TransportResponse(status_code=200, json_body=[]),  # funding
        ]
        a = BinanceCryptoAdapter(transport=transport)
        a.prefill()
        assert a.bar_count("BTCUSDT") >= PREFILL_MIN_BARS

    def test_prefill_stops_on_partial_page(self, transport: EgressTransport) -> None:
        # Single partial page of 30 bars (< KLINES_PAGE_SIZE) — venue exhausted.
        # Use 30 (below the 55-bar eval gate) so we can also verify the
        # downstream ready flag.
        transport._get.next = [
            _TransportResponse(
                status_code=200,
                json_body=_klines_payload(30, start_ms=1_700_000_000_000),
            ),
        ]
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        a.prefill()
        # Did NOT reach 500-bar minimum — adapter stores what it got.
        assert a.bar_count("BTCUSDT") == 30
        # 30 < 55 → not ready for downstream evaluation.
        assert not a.is_ready("BTCUSDT")
        with pytest.raises(InsufficientDataError):
            a.assert_ready("BTCUSDT")
        # Prefill still marked done so callers know the (partial) attempt finished.
        assert a.prefill_done("BTCUSDT")

    def test_prefill_raises_egress_blocked_on_451(self, transport: EgressTransport) -> None:
        transport._get.next = [
            _TransportResponse(status_code=HTTP_STATUS_LEGAL_BLOCK, text=""),
        ]
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        with pytest.raises(EgressBlockedError):
            a.prefill()

    def test_prefill_stores_oldest_first(self, transport: EgressTransport) -> None:
        page = _klines_payload(600, start_ms=1_700_000_000_000)
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=page),
        ]
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        a.prefill()
        bars = a.bars("BTCUSDT")
        # Strictly non-decreasing time order.
        for i in range(1, len(bars)):
            assert bars[i].time >= bars[i - 1].time
        # First bar is exactly start_ms of the page.
        assert bars[0].time == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


class TestEvaluationGate:
    def _adapter_with_bars(self, transport: EgressTransport, n_bars: int) -> BinanceCryptoAdapter:
        """Build an adapter with ``n_bars`` synthetic bars already loaded."""
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        # Skip prefill HTTP — inject bars directly.
        page = _klines_payload(n_bars, start_ms=1_700_000_000_000)
        a._bars["BTCUSDT"] = [
            CryptoBar.from_binance_kline("BTCUSDT", "1h", k) for k in page
        ]
        return a

    def test_is_ready_true_above_gate(self, transport: EgressTransport) -> None:
        a = self._adapter_with_bars(transport, EVAL_GATE_BARS + 10)
        assert a.is_ready("BTCUSDT") is True

    def test_is_ready_false_below_gate(self, transport: EgressTransport) -> None:
        a = self._adapter_with_bars(transport, EVAL_GATE_BARS - 1)
        assert a.is_ready("BTCUSDT") is False

    def test_assert_ready_raises_below_gate(self, transport: EgressTransport) -> None:
        a = self._adapter_with_bars(transport, 10)
        with pytest.raises(InsufficientDataError) as exc:
            a.assert_ready("BTCUSDT")
        assert "55" in str(exc.value) or str(EVAL_GATE_BARS) in str(exc.value)

    def test_assert_ready_passes_at_gate(self, transport: EgressTransport) -> None:
        a = self._adapter_with_bars(transport, EVAL_GATE_BARS)
        a.assert_ready("BTCUSDT")  # must not raise

    def test_eval_gate_constant(self) -> None:
        assert EVAL_GATE_BARS == 55
        assert PREFILL_MIN_BARS == 500


# ---------------------------------------------------------------------------
# 7. Poll — klines + OI + funding refresh
# ---------------------------------------------------------------------------


class TestPoll:
    def test_poll_appends_new_closed_bar(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        # Seed with one closed bar at T-3600 (the bar BEFORE the next poll).
        existing_open_ms = 1_700_000_000_000 - 3600 * 1000
        a._bars["BTCUSDT"] = [
            CryptoBar.from_binance_kline(
                "BTCUSDT", "1h",
                _kline(existing_open_ms, 30000, 30100, 29900, 30050, 100.0),
            )
        ]
        # Binance returns klines oldest-first by default (no endTime).
        # For limit=2: response = [just-closed-bar, current-open-bar].
        just_closed_ms = 1_700_000_000_000  # 1h after existing
        current_open_ms = just_closed_ms + 3600 * 1000
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=[
                _kline(just_closed_ms, 30050, 30150, 29950, 30100, 110.0),  # just-closed
                _kline(current_open_ms, 30100, 30200, 30000, 30150, 120.0),  # current open
            ]),
            _TransportResponse(status_code=200, json_body=_oi_payload("BTCUSDT")),
            _TransportResponse(status_code=200, json_body=[_funding_payload("BTCUSDT")]),
        ]
        a.poll()
        assert len(a.bars("BTCUSDT")) == 2
        assert a.bars("BTCUSDT")[-1].time == datetime.fromtimestamp(just_closed_ms / 1000, tz=timezone.utc)
        assert a.bars("BTCUSDT")[-1].close == 30100.0

    def test_poll_does_not_reappend_stale_bar(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        a._bars["BTCUSDT"] = [
            CryptoBar.from_binance_kline("BTCUSDT", "1h", _kline(1_700_003_600_000, 30050, 30150, 29950, 30100, 110.0))
        ]
        # Poll returns a stale bar (older than what we have).
        stale = _kline(1_700_000_000_000, 30000, 30100, 29900, 30050, 100.0)
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=[stale, stale]),
            _TransportResponse(status_code=200, json_body=_oi_payload("BTCUSDT")),
            _TransportResponse(status_code=200, json_body=[_funding_payload("BTCUSDT")]),
        ]
        a.poll()
        assert len(a.bars("BTCUSDT")) == 1

    def test_poll_records_oi_snapshot(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=[]),
            _TransportResponse(status_code=200, json_body=_oi_payload("BTCUSDT", oi_value=42000.0)),
            _TransportResponse(status_code=200, json_body=[_funding_payload("BTCUSDT")]),
        ]
        a.poll()
        oi = a.latest_oi("BTCUSDT")
        assert isinstance(oi, OpenInterestSnapshot)
        assert oi.symbol == "BTCUSDT"
        assert oi.open_interest == 42000.0
        assert oi.open_interest_value == 12345678.0

    def test_poll_records_funding_snapshot(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=[]),
            _TransportResponse(status_code=200, json_body=_oi_payload("BTCUSDT")),
            _TransportResponse(status_code=200, json_body=[_funding_payload("BTCUSDT", rate=0.00025)]),
        ]
        a.poll()
        f = a.latest_funding("BTCUSDT")
        assert isinstance(f, FundingRateSnapshot)
        assert f.symbol == "BTCUSDT"
        assert f.funding_rate == 0.00025
        assert f.mark_price == 30000.0

    def test_poll_uses_correct_egress_policy_per_endpoint(
        self, transport: EgressTransport
    ) -> None:
        """Spot klines must NOT go via proxy; OI and funding MUST go via proxy."""
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=[]),  # klines
            _TransportResponse(status_code=200, json_body=_oi_payload("BTCUSDT")),
            _TransportResponse(status_code=200, json_body=[_funding_payload("BTCUSDT")]),
        ]
        a.poll()
        proxies_seq = [c["proxies"] for c in transport.calls]
        assert proxies_seq[0] is None  # klines via direct US
        assert proxies_seq[1] is not None  # OI via EI proxy
        assert proxies_seq[2] is not None  # funding via EI proxy

    def test_poll_weight_budget_shared(self, transport: EgressTransport) -> None:
        """klines + OI + funding all draw from the same token bucket."""
        before = transport.bucket().tokens()
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=[]),
            _TransportResponse(status_code=200, json_body=_oi_payload("BTCUSDT")),
            _TransportResponse(status_code=200, json_body=[_funding_payload("BTCUSDT")]),
        ]
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        a.poll()
        # 3 calls × weight 1 each = 3 tokens consumed from a 1200 bucket.
        assert before - transport.bucket().tokens() == pytest.approx(3.0)

    def test_poll_raises_on_451_oi(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=[]),  # klines OK
            _TransportResponse(status_code=HTTP_STATUS_LEGAL_BLOCK, text=""),  # OI 451
        ]
        with pytest.raises(EgressBlockedError):
            a.poll()


# ---------------------------------------------------------------------------
# 8. Default weight budget + per-call weight consumption
# ---------------------------------------------------------------------------


class TestWeightBudget:
    def test_default_weight_budget(self, fake_clock: _FakeClock) -> None:
        t = EgressTransport(clock=fake_clock)
        assert t.bucket().capacity == WEIGHT_BUDGET_PER_MINUTE
        # 1200/60 = 20 tokens/sec refill
        assert t.bucket().tokens() == pytest.approx(WEIGHT_BUDGET_PER_MINUTE)

    def test_per_endpoint_weight_costs(self) -> None:
        assert ENDPOINT_KLINES_SPOT.weight == 1
        assert ENDPOINT_OI_FUTURES.weight == 1
        assert ENDPOINT_FUNDING_FUTURES.weight == 1

    def test_klines_call_consumes_one_token(self, transport: EgressTransport) -> None:
        before = transport.bucket().tokens()
        transport._get.next = [_TransportResponse(status_code=200, json_body=[])]
        transport.get(ENDPOINT_KLINES_SPOT, params={"symbol": "BTCUSDT"})
        assert before - transport.bucket().tokens() == pytest.approx(1.0)

    def test_oi_call_consumes_one_token(self, transport: EgressTransport) -> None:
        before = transport.bucket().tokens()
        transport._get.next = [_TransportResponse(status_code=200, json_body={})]
        transport.get(ENDPOINT_OI_FUTURES, params={"symbol": "BTCUSDT"})
        assert before - transport.bucket().tokens() == pytest.approx(1.0)

    def test_funding_call_consumes_one_token(self, transport: EgressTransport) -> None:
        before = transport.bucket().tokens()
        transport._get.next = [_TransportResponse(status_code=200, json_body=[])]
        transport.get(ENDPOINT_FUNDING_FUTURES, params={"symbol": "BTCUSDT"})
        assert before - transport.bucket().tokens() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 9. Adapter end-to-end smoke (no live calls)
# ---------------------------------------------------------------------------


class TestAdapterSmoke:
    def test_construct_default_adapter(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(transport=transport)
        assert a.interval == DEFAULT_INTERVAL
        assert a.supported_symbols() == SUPPORTED_SYMBOLS

    def test_construct_4h_adapter(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(interval="4h", transport=transport)
        assert a.interval == "4h"

    def test_accessors_return_empty_before_poll(self, transport: EgressTransport) -> None:
        a = BinanceCryptoAdapter(transport=transport)
        assert a.latest_bar("BTCUSDT") is None
        assert a.latest_oi("BTCUSDT") is None
        assert a.latest_funding("BTCUSDT") is None
        assert a.bars("BTCUSDT") == []
        assert not a.prefill_done("BTCUSDT")

    def test_prefill_done_flag_set(self, transport: EgressTransport) -> None:
        transport._get.next = [
            _TransportResponse(status_code=200, json_body=_klines_payload(600, start_ms=1_700_000_000_000)),
        ]
        a = BinanceCryptoAdapter(symbols=("BTCUSDT",), transport=transport)
        a.prefill()
        assert a.prefill_done("BTCUSDT")
