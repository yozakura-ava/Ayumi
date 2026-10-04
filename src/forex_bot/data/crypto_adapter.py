"""Binance crypto data adapter — klines, open-interest, funding rate feed.

Card: 73efa3b9-beb5-4eb6-92a4-66b546bbeb25 (CRYPTO-A2)
Sprint: 2026-10-04-crypto-phase-a
Lane: [BUILD][CRYPTO-LANE]

What this module provides
=========================

A vendor-port of the Binance collectors from ``crypto_monitor``
(``/root/.openclaw/workspace/src/crypto_monitor/``) wired to Ayumi's
data layer. The adapter emits OHLCV bars in a shape compatible with
``forex_bot.backtest.types.Bar`` (time, open, high, low, close, volume)
plus typed snapshots for open-interest and funding rate — the input
format the crypto-extension signal factors (CRYPTO-A3) consume.

Hard egress constraint (Craig, 2026-10-04, see card comment)
============================================================

- ``api.binance.com`` and ``fapi.binance.com`` return **HTTP 451**
  ("Unavailable For Legal Reasons") from this server — verified live.
- ``api.binance.us`` returns 200 but exposes **no futures endpoints**.
- The adapter therefore routes per-endpoint:

    * **Spot klines** → Binance.US direct (``https://api.binance.us``)
    * **Futures OI** and **funding rate** → EI TunnelBear SOCKS5 egress
      (``data/ei/proxy-config.json``, 47 endpoints; see
      ``docs/plans/sprint-2026-08-13-ei-proxy-phase-a.md`` and the
      ``ei-proxy-egress-verification`` skill).

- On HTTP 451 the adapter raises :class:`EgressBlockedError`. There is
  **no silent fallback** to direct egress — that would mask the legal
  block and risk platform ban.

Scope guarantees
================

- **Symbols**: BTCUSDT, ETHUSDT, SOLUSDT only. Other symbols raise
  :class:`SymbolError` at construction or fetch time.
- **Interval**: 1h enforced as default; 4h configurable. Any sub-1h
  interval (notably 1m) is rejected with :class:`IntervalError`.
- **Prefill minimum**: 500 bars per symbol on startup via paginated
  backfill (Binance returns 1000-bar pages).
- **Evaluation gate**: 55-bar minimum — downstream consumers MUST
  pass :meth:`BinanceCryptoAdapter.assert_ready` before polling.
- **Rate limit**: shared token-bucket with 1200-weight-per-minute
  budget across klines, OI, and funding endpoints (Binance's spec).
- **Vendor port**: collector patterns (rate limiter, retry, JSONL
  pagination) come from ``crypto_monitor/collectors/base.py`` —
  we port them here rather than importing cross-repo. The token-bucket
  here is the Binance-style weight-aware variant (Binance returns
  ``X-MBX-USED-WEIGHT-1M`` headers; we track locally so the adapter
  works in tests where no headers are available).

Unit tests use synthetic fixtures and a mocked transport — no live
API calls. See ``tests/test_crypto_adapter.py``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — symbol policy, interval policy, sizing
# ---------------------------------------------------------------------------

#: Crypto symbols in scope for the Ayumi crypto lane Phase A. Adding
#: symbols requires updating the contract in the CRYPTO-A3 card and the
#: shared ``SymbolTypeGate`` in ``models/instrument.py``.
SUPPORTED_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

#: Allowed bar intervals. 1h is the enforced default for the Ayumi
#: signal-engine evaluation gate (4h is exposed for downstream consumers
#: that opt-in to the multi-timeframe pipeline; sub-1h intervals are
#: rejected because the 55-bar minimum on 1m produces too much whipsaw).
ALLOWED_INTERVALS: tuple[str, ...] = ("1h", "4h")
DEFAULT_INTERVAL: str = "1h"

#: Minimum number of bars required for the historical prefill on
#: startup. 500 1h bars = ~21 days of context, enough for the rolling
#: lookbacks the signal engine uses (longest is ~100 bars).
PREFILL_MIN_BARS: int = 500

#: Minimum bars required for downstream signal evaluation. Below this
#: the adapter raises :class:`InsufficientDataError` from
#: :meth:`BinanceCryptoAdapter.assert_ready`.
EVAL_GATE_BARS: int = 55

#: Shared weight budget across klines / OI / funding endpoints
#: (Binance spec: 1200 weight / minute for the endpoints we use).
WEIGHT_BUDGET_PER_MINUTE: int = 1200

#: Binance.US klines max page size.
KLINES_PAGE_SIZE: int = 1000

#: Binance per-endpoint weight cost (Binance spec, public market data).
WEIGHT_KLINES: int = 1
WEIGHT_OI: int = 1
WEIGHT_FUNDING: int = 1

#: HTTP status that means the legal/jurisdictional block. On 451 the
#: adapter raises :class:`EgressBlockedError` — never silently falls
#: back to direct egress from a proxy or vice versa.
HTTP_STATUS_LEGAL_BLOCK: int = 451


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CryptoAdapterError(Exception):
    """Base class for crypto adapter errors."""


class EgressBlockedError(CryptoAdapterError):
    """HTTP 451 (or comparable legal-block signal) was observed.

    The adapter refuses to silently fall back to direct egress when
    the proxy path is blocked. Callers must surface this to the user
    — there is no auto-recovery.
    """


class SymbolError(CryptoAdapterError):
    """A symbol outside the supported whitelist was requested."""


class IntervalError(CryptoAdapterError):
    """An interval not in ALLOWED_INTERVALS was requested."""


class InsufficientDataError(CryptoAdapterError):
    """Fewer than EVAL_GATE_BARS bars are available for a symbol."""


class RateLimitError(CryptoAdapterError):
    """The token bucket could not satisfy a request within the budget."""


# ---------------------------------------------------------------------------
# Egress policy
# ---------------------------------------------------------------------------


class EgressPolicy(str, Enum):
    """How a given endpoint is reached from Ayumi.

    - ``DIRECT_US``: hit Binance.US directly (works from this host).
    - ``EI_PROXY``: hit the upstream (api.binance.com / fapi.binance.com)
      via the EI TunnelBear SOCKS5 egress system.
    """

    DIRECT_US = "direct_us"
    EI_PROXY = "ei_proxy"


@dataclass(frozen=True)
class EgressEndpoint:
    """An HTTP endpoint with its egress policy and weight.

    Attributes
    ----------
    name : str
        Stable identifier used in logs and tests (e.g. ``"klines_spot"``).
    url : str
        Full URL of the endpoint (no query parameters).
    policy : EgressPolicy
        How to reach the endpoint from Ayumi.
    weight : int
        Binance weight cost per call (used by the rate limiter).
    """

    name: str
    url: str
    policy: EgressPolicy
    weight: int


# Endpoint registry — single place to audit the egress policy.
# Spot klines go through Binance.US direct (api.binance.us is reachable
# from this host and exposes the spot market data). Futures OI and
# funding must go through fapi.binance.com which is 451-blocked direct,
# so they route via the EI TunnelBear SOCKS5 egress.
ENDPOINT_KLINES_SPOT: EgressEndpoint = EgressEndpoint(
    name="klines_spot",
    url="https://api.binance.us/api/v3/klines",
    policy=EgressPolicy.DIRECT_US,
    weight=WEIGHT_KLINES,
)
ENDPOINT_OI_FUTURES: EgressEndpoint = EgressEndpoint(
    name="open_interest",
    url="https://fapi.binance.com/fapi/v1/openInterest",
    policy=EgressPolicy.EI_PROXY,
    weight=WEIGHT_OI,
)
ENDPOINT_FUNDING_FUTURES: EgressEndpoint = EgressEndpoint(
    name="funding_rate",
    url="https://fapi.binance.com/fapi/v1/fundingRate",
    policy=EgressPolicy.EI_PROXY,
    weight=WEIGHT_FUNDING,
)

ENDPOINTS: dict[str, EgressEndpoint] = {
    ENDPOINT_KLINES_SPOT.name: ENDPOINT_KLINES_SPOT,
    ENDPOINT_OI_FUTURES.name: ENDPOINT_OI_FUTURES,
    ENDPOINT_FUNDING_FUTURES.name: ENDPOINT_FUNDING_FUTURES,
}


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (Binance weight-style)
# ---------------------------------------------------------------------------


class TokenBucket:
    """Refill-based token bucket with a monotonic clock.

    Capacity is the burst budget; refill_rate_per_second is the steady-state
    budget. ``acquire(weight)`` blocks until ``weight`` tokens are
    available, sleeping on the supplied ``clock.sleep`` callable so unit
    tests can drive it deterministically with a fake clock.

    Vendored from ``crypto_monitor/collectors/base.py:RateLimiter`` (which is
    a sliding-window counter) — the token-bucket variant matches Binance's
    weight model better because it allows bursting up to capacity.
    """

    def __init__(
        self,
        capacity: int,
        refill_rate_per_second: float,
        clock: Optional["_Clock"] = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate_per_second <= 0:
            raise ValueError("refill_rate_per_second must be positive")
        self._capacity = float(capacity)
        self._refill_rate = float(refill_rate_per_second)
        self._clock = clock or _SystemClock()
        self._tokens = float(capacity)
        self._last_refill = self._clock.now()

    @property
    def capacity(self) -> float:
        return self._capacity

    def _refill(self) -> None:
        now = self._clock.now()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._refill_rate,
            )
            self._last_refill = now

    def tokens(self) -> float:
        """Return current token count without acquiring any."""
        self._refill()
        return self._tokens

    def acquire(self, weight: float = 1.0) -> None:
        """Block until ``weight`` tokens are available, then consume them.

        Raises :class:`RateLimitError` if ``weight`` exceeds capacity — a
        single request can never be larger than the burst budget.
        """
        if weight > self._capacity:
            raise RateLimitError(
                f"Requested weight {weight} exceeds bucket capacity {self._capacity}"
            )
        while True:
            self._refill()
            if self._tokens >= weight:
                self._tokens -= weight
                return
            # Compute sleep time to accumulate enough tokens.
            needed = weight - self._tokens
            sleep_seconds = needed / self._refill_rate
            self._clock.sleep(sleep_seconds)


# ---------------------------------------------------------------------------
# Clock abstraction — lets tests inject deterministic time.
# ---------------------------------------------------------------------------


class _Clock:
    """Abstract monotonic clock for deterministic tests."""

    def now(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _SystemClock(_Clock):
    """Real wall-clock implementation."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class _FakeClock(_Clock):
    """Manually-advanced clock for unit tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds

    def advance(self, seconds: float) -> None:
        """Advance the clock without recording a sleep (test helper)."""
        self._now += seconds


# ---------------------------------------------------------------------------
# Egress transport — wraps ``requests`` (or a callable in tests) with
# rate limiting and per-endpoint egress policy.
# ---------------------------------------------------------------------------


@dataclass
class _TransportResponse:
    """Minimal response wrapper — independent of requests.Response so
    unit tests don't need to import requests."""

    status_code: int
    json_body: Any = None
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class EgressTransport:
    """HTTP transport that enforces egress policy and weight budget.

    The default HTTP backend is ``requests`` (mirrors the
    ``crypto_monitor`` collectors). Tests can substitute a
    ``fake_get`` callable returning :class:`_TransportResponse` objects
    to avoid any real network I/O.
    """

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        us_base: str = "https://api.binance.us",
        fapi_base: str = "https://fapi.binance.com",
        bucket: Optional[TokenBucket] = None,
        clock: Optional[_Clock] = None,
        get_callable: Optional[Callable[..., _TransportResponse]] = None,
    ) -> None:
        """Initialize the transport.

        Parameters
        ----------
        proxy_url : str, optional
            SOCKS5 URL for EI egress (``socks5h://user:pass@host:port``).
            Used for endpoints with :attr:`EgressPolicy.EI_PROXY`. If
            ``None`` and an EI_PROXY endpoint is requested, the call
            raises :class:`EgressBlockedError` — we do NOT silently
            fall back to direct egress.
        us_base : str
            Override for the Binance.US base URL (tests).
        fapi_base : str
            Override for the fapi.binance.com base URL (tests).
        bucket : TokenBucket, optional
            Shared weight bucket. Defaults to a 1200/min bucket.
        clock : _Clock, optional
            Clock for the rate limiter.
        get_callable : callable, optional
            If supplied, replaces ``requests.get`` — must return a
            ``_TransportResponse``. Signature mirrors ``requests.get``
            with an extra ``proxies`` kwarg.
        """
        self._proxy_url = proxy_url
        self._us_base = us_base.rstrip("/")
        self._fapi_base = fapi_base.rstrip("/")
        self._clock = clock or _SystemClock()
        self._bucket = bucket or TokenBucket(
            capacity=WEIGHT_BUDGET_PER_MINUTE,
            refill_rate_per_second=WEIGHT_BUDGET_PER_MINUTE / 60.0,
            clock=self._clock,
        )
        self._get = get_callable  # injected for tests
        self._owns_session = get_callable is None
        self._session = None
        if self._owns_session:
            self._session = _make_default_session()

    # -- public --------------------------------------------------------------

    def bucket(self) -> TokenBucket:
        """Expose the shared weight bucket for inspection in tests."""
        return self._bucket

    def get(self, endpoint: EgressEndpoint, params: Optional[dict[str, Any]] = None) -> _TransportResponse:
        """Issue a GET against ``endpoint`` honoring policy + budget.

        Steps:
        1. Reserve ``endpoint.weight`` tokens from the shared bucket
           (blocks until available).
        3. Resolve the actual URL — direct US vs proxied fapi.
        4. Issue the GET (real or injected).
        5. Raise :class:`EgressBlockedError` on HTTP 451. On other
           non-2xx, raise :class:`CryptoAdapterError`.
        """
        self._bucket.acquire(float(endpoint.weight))
        url = self._resolve_url(endpoint)
        proxies = self._resolve_proxies(endpoint)
        response = self._do_get(url, params, proxies)
        if response.status_code == HTTP_STATUS_LEGAL_BLOCK:
            raise EgressBlockedError(
                f"Egress blocked for endpoint {endpoint.name!r} "
                f"(HTTP 451). Adapter refuses to fall back to direct egress."
            )
        if response.status_code >= 400:
            raise CryptoAdapterError(
                f"HTTP {response.status_code} for endpoint {endpoint.name!r}: "
                f"{response.text[:200]}"
            )
        return response

    # -- internals -----------------------------------------------------------

    def _resolve_url(self, endpoint: EgressEndpoint) -> str:
        if endpoint.policy == EgressPolicy.DIRECT_US:
            return endpoint.url
        if endpoint.policy == EgressPolicy.EI_PROXY:
            return endpoint.url
        raise CryptoAdapterError(f"Unknown egress policy: {endpoint.policy!r}")

    def _resolve_proxies(self, endpoint: EgressEndpoint) -> Optional[dict[str, str]]:
        if endpoint.policy == EgressPolicy.EI_PROXY:
            if not self._proxy_url:
                raise EgressBlockedError(
                    f"Endpoint {endpoint.name!r} requires EI proxy egress but "
                    f"no proxy_url was configured. Refusing silent fallback "
                    f"to direct (which would 451 on this host)."
                )
            return {"http": self._proxy_url, "https": self._proxy_url}
        return None

    def _do_get(
        self,
        url: str,
        params: Optional[dict[str, Any]],
        proxies: Optional[dict[str, str]],
    ) -> _TransportResponse:
        if self._get is not None:
            return self._get(url, params=params, proxies=proxies)
        # Production path: real ``requests`` call.
        # ``requests`` is imported by ``_make_default_session`` (which
        # constructs the shared session in __init__), so it is always
        # available here when owns_session is True.
        session = self._session
        if session is None:
            raise CryptoAdapterError(
                "EgressTransport has no session and no get_callable was injected"
            )
        resp = session.get(url, params=params, proxies=proxies, timeout=10.0)
        return _TransportResponse(
            status_code=resp.status_code,
            json_body=_safe_json(resp),
            text=resp.text,
            headers=dict(resp.headers),
        )


def _make_default_session() -> Any:
    """Create a requests.Session with conservative retry policy."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Domain types — bars, OI snapshot, funding snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CryptoBar:
    """OHLCV bar emitted by the adapter.

    Field names mirror ``forex_bot.backtest.types.Bar`` so consumers
    can convert with ``Bar(**asdict(crypto_bar))`` if needed. The
    adapter is independent of ``backtest.types`` to keep this module's
    import surface narrow (per card scope: only data/crypto_adapter.py
    + tests/test_crypto_adapter.py).

    Attributes
    ----------
    symbol : str
        Trading symbol (e.g. ``"BTCUSDT"``).
    time : datetime
        Bar open time (UTC).
    open, high, low, close : float
        OHLC prices.
    volume : float
        Quote-asset volume for the interval (USDT for USDT-margined).
    interval : str
        Bar interval (``"1h"`` or ``"4h"``).
    """

    symbol: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval: str

    @classmethod
    def from_binance_kline(cls, symbol: str, interval: str, kline: list[Any]) -> "CryptoBar":
        """Build a CryptoBar from a raw Binance kline array.

        Binance returns klines as
        ``[openTime, open, high, low, close, volume, closeTime, ...]``
        — we use the first six slots.
        """
        if len(kline) < 6:
            raise ValueError(f"kline array has {len(kline)} fields, need >=6")
        return cls(
            symbol=symbol,
            time=datetime.fromtimestamp(int(kline[0]) / 1000.0, tz=timezone.utc),
            open=float(kline[1]),
            high=float(kline[2]),
            low=float(kline[3]),
            close=float(kline[4]),
            volume=float(kline[5]),
            interval=interval,
        )


@dataclass(frozen=True)
class OpenInterestSnapshot:
    """A single open-interest snapshot for a symbol.

    Attributes
    ----------
    symbol : str
    time : datetime
        Snapshot timestamp (UTC).
    open_interest : float
        Open interest in contracts.
    open_interest_value : Optional[float]
        Open interest notional (USD), when reported by the venue.
    """

    symbol: str
    time: datetime
    open_interest: float
    open_interest_value: Optional[float] = None


@dataclass(frozen=True)
class FundingRateSnapshot:
    """A single funding-rate snapshot for a symbol.

    Attributes
    ----------
    symbol : str
    time : datetime
        Funding settlement time (UTC).
    funding_rate : float
        Funding rate as a decimal (e.g. ``0.0001`` = 1 bp).
    mark_price : Optional[float]
        Mark price at settlement, when reported.
    """

    symbol: str
    time: datetime
    funding_rate: float
    mark_price: Optional[float] = None


# ---------------------------------------------------------------------------
# Main public class: BinanceCryptoAdapter
# ---------------------------------------------------------------------------


class BinanceCryptoAdapter:
    """Binance crypto data adapter for the Ayumi signal engine.

    Lifecycle:

    >>> transport = BinanceCryptoAdapter(interval="1h")
    >>> transport.prefill()             # backfill >=500 bars per symbol
    >>> for sym in BinanceCryptoAdapter.symbols:
    ...     transport.assert_ready(sym)  # raises if <55 bars
    >>> transport.poll()                # refresh latest bar + OI + funding
    >>> bars = transport.bars("BTCUSDT")

    All public methods are deterministic when an :class:`EgressTransport`
    with a fake ``get_callable`` and a fake clock is supplied. Unit
    tests in ``tests/test_crypto_adapter.py`` use that pattern.
    """

    #: Supported symbols (mirrors :data:`SUPPORTED_SYMBOLS`).
    symbols: tuple[str, ...] = SUPPORTED_SYMBOLS
    #: Allowed intervals (mirrors :data:`ALLOWED_INTERVALS`).
    allowed_intervals: tuple[str, ...] = ALLOWED_INTERVALS
    #: Default interval (mirrors :data:`DEFAULT_INTERVAL`).
    default_interval: str = DEFAULT_INTERVAL
    #: Evaluation gate (mirrors :data:`EVAL_GATE_BARS`).
    eval_gate_bars: int = EVAL_GATE_BARS
    #: Prefill minimum (mirrors :data:`PREFILL_MIN_BARS`).
    prefill_min_bars: int = PREFILL_MIN_BARS

    def __init__(
        self,
        interval: str = DEFAULT_INTERVAL,
        symbols: Optional[Iterable[str]] = None,
        transport: Optional[EgressTransport] = None,
        clock: Optional[_Clock] = None,
    ) -> None:
        if interval not in ALLOWED_INTERVALS:
            raise IntervalError(
                f"interval {interval!r} not allowed; must be one of {ALLOWED_INTERVALS}"
            )
        self._interval = interval
        syms = tuple(symbols) if symbols is not None else SUPPORTED_SYMBOLS
        for s in syms:
            if s not in SUPPORTED_SYMBOLS:
                raise SymbolError(
                    f"symbol {s!r} not in supported whitelist {SUPPORTED_SYMBOLS}"
                )
        self._symbols = syms
        self._transport = transport or EgressTransport(clock=clock)
        self._clock = clock

        # Per-symbol storage: list of CryptoBar (oldest first),
        # latest OI snapshot, latest funding snapshot.
        self._bars: dict[str, list[CryptoBar]] = {s: [] for s in syms}
        self._oi: dict[str, Optional[OpenInterestSnapshot]] = {s: None for s in syms}
        self._funding: dict[str, Optional[FundingRateSnapshot]] = {s: None for s in syms}
        self._prefilled: set[str] = set()

    # -- properties ----------------------------------------------------------

    @property
    def interval(self) -> str:
        return self._interval

    @property
    def transport(self) -> EgressTransport:
        return self._transport

    def supported_symbols(self) -> tuple[str, ...]:
        return self._symbols

    # -- readiness -----------------------------------------------------------

    def bar_count(self, symbol: str) -> int:
        """Number of bars currently stored for ``symbol``."""
        self._require_symbol(symbol)
        return len(self._bars[symbol])

    def is_ready(self, symbol: str) -> bool:
        """True iff ``symbol`` has at least :data:`EVAL_GATE_BARS` bars."""
        return self.bar_count(symbol) >= self.eval_gate_bars

    def assert_ready(self, symbol: str) -> None:
        """Raise :class:`InsufficientDataError` if not ready."""
        if not self.is_ready(symbol):
            raise InsufficientDataError(
                f"symbol {symbol!r} has {self.bar_count(symbol)} bars; "
                f"need >= {self.eval_gate_bars} for evaluation gate"
            )

    def prefill_done(self, symbol: str) -> bool:
        return symbol in self._prefilled

    # -- prefill -------------------------------------------------------------

    def prefill(self) -> None:
        """Backfill :data:`PREFILL_MIN_BARS` bars per symbol.

        Paginates Binance klines backward (``endTime`` cursor) until the
        prefill minimum is met or the venue returns less than a full
        page. Bars are stored oldest-first.
        """
        for symbol in self._symbols:
            self._prefill_symbol(symbol)
            self._prefilled.add(symbol)

    def _prefill_symbol(self, symbol: str) -> None:
        bars: list[CryptoBar] = []
        end_time_ms: Optional[int] = None
        while len(bars) < self.prefill_min_bars:
            params: dict[str, Any] = {
                "symbol": symbol,
                "interval": self._interval,
                "limit": KLINES_PAGE_SIZE,
            }
            if end_time_ms is not None:
                params["endTime"] = end_time_ms
            response = self._transport.get(ENDPOINT_KLINES_SPOT, params=params)
            payload = response.json_body or []
            if not payload:
                break
            # Binance returns klines newest-first when paginated via endTime.
            page_bars = [
                CryptoBar.from_binance_kline(symbol, self._interval, k)
                for k in payload
            ]
            # Prepend to maintain oldest-first ordering.
            bars = page_bars + bars
            if len(payload) < KLINES_PAGE_SIZE:
                # Venue returned a partial page — we've exhausted history.
                break
            # Advance cursor to the open-time of the earliest bar minus 1ms.
            earliest_open_ms = int(page_bars[0].time.timestamp() * 1000)
            end_time_ms = earliest_open_ms - 1
        self._bars[symbol] = bars

    # -- poll ----------------------------------------------------------------

    def poll(self) -> None:
        """Refresh latest bar + OI + funding snapshot per symbol."""
        for symbol in self._symbols:
            self._poll_klines(symbol)
            self._poll_oi(symbol)
            self._poll_funding(symbol)

    def _poll_klines(self, symbol: str) -> None:
        response = self._transport.get(
            ENDPOINT_KLINES_SPOT,
            params={"symbol": symbol, "interval": self._interval, "limit": 2},
        )
        payload = response.json_body or []
        if not payload:
            return
        # The last entry is the still-open bar; the prior one is the most
        # recent closed bar. We append the closed bar if newer than what
        # we already have.
        latest_closed = payload[-2] if len(payload) >= 2 else payload[-1]
        bar = CryptoBar.from_binance_kline(symbol, self._interval, latest_closed)
        existing = self._bars[symbol]
        if not existing or bar.time > existing[-1].time:
            existing.append(bar)

    def _poll_oi(self, symbol: str) -> None:
        response = self._transport.get(
            ENDPOINT_OI_FUTURES,
            params={"symbol": symbol},
        )
        payload = response.json_body or {}
        try:
            oi_value = float(payload.get("openInterest", 0.0))
        except (TypeError, ValueError):
            oi_value = 0.0
        oi_notional_raw = payload.get("sumOpenInterestValue")
        try:
            oi_notional = float(oi_notional_raw) if oi_notional_raw is not None else None
        except (TypeError, ValueError):
            oi_notional = None
        ts_ms = int(payload.get("time", int(time.time() * 1000)))
        self._oi[symbol] = OpenInterestSnapshot(
            symbol=symbol,
            time=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
            open_interest=oi_value,
            open_interest_value=oi_notional,
        )

    def _poll_funding(self, symbol: str) -> None:
        response = self._transport.get(
            ENDPOINT_FUNDING_FUTURES,
            params={"symbol": symbol, "limit": 1},
        )
        payload = response.json_body or []
        if not payload:
            return
        entry = payload[0] if isinstance(payload, list) else payload
        try:
            rate = float(entry.get("fundingRate", 0.0))
        except (TypeError, ValueError):
            rate = 0.0
        ts_ms = int(entry.get("fundingTime", int(time.time() * 1000)))
        mark_raw = entry.get("markPrice")
        try:
            mark = float(mark_raw) if mark_raw is not None else None
        except (TypeError, ValueError):
            mark = None
        self._funding[symbol] = FundingRateSnapshot(
            symbol=symbol,
            time=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
            funding_rate=rate,
            mark_price=mark,
        )

    # -- accessors -----------------------------------------------------------

    def bars(self, symbol: str) -> list[CryptoBar]:
        """Return all bars for ``symbol`` (oldest first)."""
        self._require_symbol(symbol)
        return list(self._bars[symbol])

    def latest_bar(self, symbol: str) -> Optional[CryptoBar]:
        self._require_symbol(symbol)
        bars = self._bars[symbol]
        return bars[-1] if bars else None

    def latest_oi(self, symbol: str) -> Optional[OpenInterestSnapshot]:
        self._require_symbol(symbol)
        return self._oi[symbol]

    def latest_funding(self, symbol: str) -> Optional[FundingRateSnapshot]:
        self._require_symbol(symbol)
        return self._funding[symbol]

    # -- internals -----------------------------------------------------------

    def _require_symbol(self, symbol: str) -> None:
        if symbol not in self._symbols:
            raise SymbolError(
                f"symbol {symbol!r} not in adapter whitelist {self._symbols}"
            )


__all__ = [
    # constants
    "SUPPORTED_SYMBOLS",
    "ALLOWED_INTERVALS",
    "DEFAULT_INTERVAL",
    "PREFILL_MIN_BARS",
    "EVAL_GATE_BARS",
    "WEIGHT_BUDGET_PER_MINUTE",
    "KLINES_PAGE_SIZE",
    "HTTP_STATUS_LEGAL_BLOCK",
    "WEIGHT_KLINES",
    "WEIGHT_OI",
    "WEIGHT_FUNDING",
    # exceptions
    "CryptoAdapterError",
    "EgressBlockedError",
    "SymbolError",
    "IntervalError",
    "InsufficientDataError",
    "RateLimitError",
    # egress policy
    "EgressPolicy",
    "EgressEndpoint",
    "ENDPOINT_KLINES_SPOT",
    "ENDPOINT_OI_FUTURES",
    "ENDPOINT_FUNDING_FUTURES",
    "ENDPOINTS",
    # transport / rate
    "TokenBucket",
    "EgressTransport",
    "_Clock",
    "_SystemClock",
    "_FakeClock",
    "_TransportResponse",
    # domain types
    "CryptoBar",
    "OpenInterestSnapshot",
    "FundingRateSnapshot",
    # main class
    "BinanceCryptoAdapter",
]
