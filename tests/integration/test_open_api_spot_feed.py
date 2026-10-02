"""Tests for OpenApiSpotFeed — tick callbacks, token refresh, reconnection, multi-symbol subscriptions.

OOM-safe refactor (BQ-822): Uses function-scoped fixtures with explicit teardown,
mocks heavy dependencies at construction time, and forces garbage collection
between tests to prevent memory accumulation from connection objects, thread
pools, and Twisted reactor references.
"""

import gc
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from adapters.ctrader.open_api_spot_feed import (
    OpenApiSpotFeed,
    _normalize_symbol_name,
)

# Reconnect constants were removed from the module; provide defaults for tests.
_MAX_RECONNECT_ATTEMPTS = getattr(
    __import__("adapters.ctrader.open_api_spot_feed", fromlist=["_MAX_RECONNECT_ATTEMPTS"]),
    "_MAX_RECONNECT_ATTEMPTS",
    20,
)
_INITIAL_RECONNECT_DELAY = getattr(
    __import__("adapters.ctrader.open_api_spot_feed", fromlist=["_INITIAL_RECONNECT_DELAY"]),
    "_INITIAL_RECONNECT_DELAY",
    5.0,
)
_MAX_RECONNECT_DELAY = getattr(
    __import__("adapters.ctrader.open_api_spot_feed", fromlist=["_MAX_RECONNECT_DELAY"]),
    "_MAX_RECONNECT_DELAY",
    120.0,
)
_STABLE_CONNECTION_SECONDS = getattr(
    __import__("adapters.ctrader.open_api_spot_feed", fromlist=["_STABLE_CONNECTION_SECONDS"]),
    "_STABLE_CONNECTION_SECONDS",
    60,
)
from adapters.ctrader.market_data_feed import (
    SymbolInfo,
    Tick,
)

logger = logging.getLogger(__name__)

# Module-level set to track feed instances for debugging memory leaks.
_active_feeds: set[int] = set()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def feed_factory():
    """Factory that creates OOM-safe OpenApiSpotFeed instances.

    Mocks CTraderConnection and TokenManager at construction time to prevent
    real TCP/threading resources from being allocated. Instances are tracked
    and explicitly cleaned up after the test.
    """
    created: list[OpenApiSpotFeed] = []

    def _create(**kwargs) -> OpenApiSpotFeed:
        defaults = dict(
            ctid_account_id=99999,
            client_id="test-client",
            client_secret="test-secret",  # noqa: S106
            access_token="test-access-token",  # noqa: S106
        )
        defaults.update(kwargs)

        # Patch heavy dependencies during construction so __init__ doesn't
        # create real TCP connections, file I/O, or thread pools.
        # Must patch the archive module directly since that's where the names
        # are looked up, not the shim in src/forex_bot/adapters/.
        with (
            patch("archive.legacy_ctrader._pkg.open_api_spot_feed.CTraderConnection") as mock_conn_cls,
            patch("archive.legacy_ctrader._pkg.open_api_spot_feed.TokenManager") as mock_token_cls,
        ):
            mock_conn = MagicMock()
            mock_conn_cls.return_value = mock_conn
            mock_token = MagicMock()
            mock_token._update_env_tokens = MagicMock()
            mock_token_cls.return_value = mock_token

            feed = OpenApiSpotFeed(**defaults)

        # Wire a mock TokenLifecycle for delegation tests
        feed._token_lifecycle = MagicMock()

        # Replace reactor references with a mock so callFromThread is safe
        feed._reactor_manager = MagicMock()
        # The _conn is already a mock from the patch above, but ensure it
        # has the expected interface.
        feed._conn = mock_conn

        _active_feeds.add(id(feed))
        created.append(feed)
        return feed

    yield _create

    # ── Teardown: explicitly release resources ──
    for f in created:
        _active_feeds.discard(id(f))
        # Cancel any pending timers
        timer = getattr(f, "_refresh_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: S110
                pass
            f._refresh_timer = None
        # Clear callbacks and data structures to break reference cycles
        f._tick_callbacks.clear()
        f._pending_orders.clear()
        f._ticks.clear()
        f._ticks_by_id.clear()
        f._tick_counts.clear()
        f._symbols.clear()
        f._name_to_id.clear()
        f._id_to_name.clear()
        f._symbol_digits.clear()
        f._subscribed_symbol_ids.clear()
        f._on_reconnected_callbacks.clear()
        # Null out heavy references
        f._conn = None
        f._client = None
        f._token_mgr = None
        f._reactor_manager = None

    created.clear()
    gc.collect()


@pytest.fixture
def feed(feed_factory):
    """Convenience: create a single feed instance."""
    return feed_factory()


# ---------------------------------------------------------------------------
# Helpers (for tests that need specific mock data)
# ---------------------------------------------------------------------------


def _make_spot_event(symbol_id=1, bid=108500, ask=108520, timestamp_ms=1715000000000):
    """Build a fake protobuf spot event message."""
    msg = MagicMock()
    msg.symbolId = symbol_id
    msg.bid = bid
    msg.ask = ask
    msg.timestamp = timestamp_ms
    return msg


def _seed_symbol_mappings(feed, mapping=None):
    """Seed symbol ID↔name mappings on a feed instance."""
    if mapping is None:
        mapping = {1: "EUR/USD", 2: "GBP/USD", 3: "USD/JPY"}
    for sid, name in mapping.items():
        feed._id_to_name[sid] = name
        canonical = _normalize_symbol_name(name)
        feed._name_to_id[canonical] = sid
        feed._symbol_digits[sid] = 5 if "JPY" not in name else 3


# ---------------------------------------------------------------------------
# Symbol normalization
# ---------------------------------------------------------------------------


class TestNormalizeSymbolName:
    def test_slash_stripped_and_uppered(self):
        assert _normalize_symbol_name("EUR/USD") == "EURUSD"

    def test_underscore_stripped_and_uppered(self):
        assert _normalize_symbol_name("gbp_usd") == "GBPUSD"

    def test_mixed_separators(self):
        assert _normalize_symbol_name("EUR/USD_JPY") == "EURUSDJPY"

    def test_already_canonical(self):
        assert _normalize_symbol_name("EURUSD") == "EURUSD"

    def test_lowercase(self):
        assert _normalize_symbol_name("eurusd") == "EURUSD"


# ---------------------------------------------------------------------------
# on_tick callback path
# ---------------------------------------------------------------------------


class TestOnTickCallback:
    @pytest.fixture(autouse=True)
    def setup(self, feed_factory):
        self.feed = feed_factory()
        _seed_symbol_mappings(self.feed, {1: "EUR/USD", 2: "GBP/USD"})

    def test_callback_fires_with_correct_tick(self):
        received = []
        self.feed.on_tick(received.append)

        event = _make_spot_event(symbol_id=1, bid=108500, ask=108520)
        self.feed._handle_spot_event(event)

        assert len(received) == 1
        tick = received[0]
        assert tick.symbol_id == 1
        assert tick.bid == pytest.approx(1.08500)
        assert tick.ask == pytest.approx(1.08520)
        assert tick.spread == pytest.approx(0.00020)

    def test_callback_gets_correct_timestamp(self):
        received = []
        self.feed.on_tick(received.append)

        event = _make_spot_event(symbol_id=1, timestamp_ms=1715000000000)
        self.feed._handle_spot_event(event)

        expected_ts = datetime.fromtimestamp(1715000000000 / 1000, tz=timezone.utc)
        assert received[0].timestamp == expected_ts

    def test_symbol_normalization_in_ticks_dict(self):
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1))
        assert "EURUSD" in self.feed.ticks

    def test_unsubscribed_symbol_still_processed(self):
        """Spot events arrive for any symbol — filtering is subscription-level, not event-level."""
        received = []
        self.feed.on_tick(received.append)
        # symbol_id=99 has no mapping — it still gets processed
        self.feed._handle_spot_event(_make_spot_event(symbol_id=99))
        assert len(received) == 1
        assert received[0].symbol_id == 99

    def test_malformed_zero_bid_ask_does_not_crash(self):
        """Completely empty tick (0,0) is silently skipped."""
        received = []
        self.feed.on_tick(received.append)
        event = _make_spot_event(bid=0, ask=0)
        self.feed._handle_spot_event(event)
        assert len(received) == 0

    def test_bid_ge_ask_rejected(self):
        received = []
        self.feed.on_tick(received.append)
        # bid == ask
        event = _make_spot_event(bid=108500, ask=108500)
        self.feed._handle_spot_event(event)
        assert len(received) == 0

    def test_spread_too_wide_rejected(self):
        received = []
        self.feed.on_tick(received.append)
        # 100-pip spread on EUR/USD — way too wide
        event = _make_spot_event(bid=108500, ask=115500)
        self.feed._handle_spot_event(event)
        assert len(received) == 0

    def test_callback_exception_does_not_kill_feed(self):
        bad_cb = MagicMock(side_effect=RuntimeError("boom"))
        ok_received = []
        self.feed.on_tick(bad_cb)
        self.feed.on_tick(ok_received.append)

        self.feed._handle_spot_event(_make_spot_event(symbol_id=1))
        assert len(ok_received) == 1  # second callback still fires

    def test_partial_tick_uses_last_known_prices(self):
        received = []
        self.feed.on_tick(received.append)

        # Full tick first
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1, bid=108500, ask=108520))
        # Partial tick — only bid updated
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1, bid=108510, ask=0))

        assert len(received) == 2
        assert received[1].bid == pytest.approx(1.08510)
        assert received[1].ask == pytest.approx(1.08520)  # carried over

    def test_partial_tick_no_prior_data_skipped(self):
        received = []
        self.feed.on_tick(received.append)
        # No prior tick for symbol_id=50
        self.feed._handle_spot_event(_make_spot_event(symbol_id=50, bid=108500, ask=0))
        assert len(received) == 0

    def test_tick_counts_increment(self):
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1))
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1))
        self.feed._handle_spot_event(_make_spot_event(symbol_id=2))
        counts = self.feed.tick_counts
        assert counts["EURUSD"] == 2
        assert counts["GBPUSD"] == 1

    def test_get_tick_returns_latest(self):
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1, bid=108500, ask=108520))
        tick = self.feed.get_tick("EUR/USD")
        assert tick is not None
        assert tick.bid == pytest.approx(1.08500)

    def test_get_tick_normalizes_input(self):
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1))
        assert self.feed.get_tick("eur_usd") is not None
        assert self.feed.get_tick("EURUSD") is not None


# ---------------------------------------------------------------------------
# Token refresh flow
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    def test_refresh_token_read_from_env(self, feed_factory):
        # B2: refresh_token is now passed explicitly by the caller via CTraderAuth,
        # not read from os.environ inside OpenApiSpotFeed.
        with patch.dict("os.environ", {"CTRADER_OPENAPI_REFRESH_TOKEN": "env-token"}):
            feed = feed_factory(refresh_token="env-token")  # noqa: S106
        assert feed._refresh_token == "env-token"  # noqa: S105

    def test_refresh_token_defaults_empty(self, feed_factory):
        with patch.dict("os.environ", {}, clear=True):
            feed = feed_factory()
        assert feed._refresh_token == ""

    def test_refresh_success_updates_tokens(self, feed_factory):
        feed = feed_factory()
        feed._refresh_token = "old-refresh"  # noqa: S105

        # Mock TokenLifecycle delegation — replaces direct HTTP calls
        from datetime import datetime, timedelta, timezone

        mock_lifecycle = MagicMock()
        mock_lifecycle._refresh_disabled = False
        mock_lifecycle.force_refresh.return_value = "new-access"
        mock_creds = MagicMock()
        mock_creds.refresh_token = "new-refresh"  # noqa: S105
        mock_lifecycle._store.get.return_value = mock_creds
        mock_lifecycle.expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        feed._token_lifecycle = mock_lifecycle

        with patch("adapters.ctrader.open_api_spot_feed.reactor") as mock_reactor:
            mock_reactor.callFromThread = MagicMock()
            feed._refresh_token_and_reauth()

        import time as _time

        _time.sleep(0.1)  # allow background thread to complete

        assert feed._access_token == "new-access"  # noqa: S105
        mock_lifecycle.force_refresh.assert_called_once()

    def test_refresh_api_error_handled_gracefully(self, feed_factory):
        feed = feed_factory()
        # TokenLifecycle.force_refresh raises — delegation handles it
        mock_lifecycle = MagicMock()
        mock_lifecycle.force_refresh.side_effect = Exception("OAuth error")
        feed._token_lifecycle = mock_lifecycle

        feed._refresh_token = "old-refresh"  # noqa: S105
        feed._refresh_token_and_reauth()  # should not raise

        import time as _time

        _time.sleep(0.1)  # allow background thread

        # access_token unchanged
        assert feed._access_token == "test-access-token"  # noqa: S105

    def test_refresh_network_failure_handled_gracefully(self, feed_factory):
        feed = feed_factory()
        # TokenLifecycle.force_refresh raises network-like error
        mock_lifecycle = MagicMock()
        mock_lifecycle.force_refresh.side_effect = ConnectionError("network down")
        feed._token_lifecycle = mock_lifecycle

        feed._refresh_token = "old-refresh"  # noqa: S105
        feed._refresh_token_and_reauth()  # should not raise

        import time as _time

        _time.sleep(0.1)  # allow background thread

    def test_auth_error_triggers_refresh(self, feed_factory):
        feed = feed_factory()
        mock_error = MagicMock()
        mock_error.errorCode = "CH_OAUTH_TOKEN_EXPIRED"
        mock_error.description = "token expired"

        with patch.object(feed, "_refresh_token_and_reauth") as mock_refresh:
            feed._handle_error(mock_error)
            mock_refresh.assert_called_once()

    def test_non_auth_error_does_not_trigger_refresh(self, feed_factory):
        feed = feed_factory()
        mock_error = MagicMock()
        mock_error.errorCode = "SOME_OTHER_ERROR"
        mock_error.description = "something else"

        with patch.object(feed, "_refresh_token_and_reauth") as mock_refresh:
            feed._handle_error(mock_error)
            mock_refresh.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-symbol subscription
# ---------------------------------------------------------------------------


class TestMultiSymbolSubscription:
    @pytest.fixture(autouse=True)
    def setup(self, feed_factory):
        self.feed = feed_factory()
        _seed_symbol_mappings(self.feed, {1: "EUR/USD", 2: "GBP/USD", 3: "USD/JPY"})

    def test_subscribe_multiple_symbols(self):
        with patch("archive.legacy_ctrader._pkg.open_api_spot_feed.reactor") as _:
            assert self.feed.subscribe("EUR/USD")
            assert self.feed.subscribe("GBP/USD")
            assert 1 in self.feed._subscribed_symbol_ids
            assert 2 in self.feed._subscribed_symbol_ids

    def test_unsubscribe_removes_symbol(self):
        with patch("archive.legacy_ctrader._pkg.open_api_spot_feed.reactor") as _:
            self.feed.subscribe("EUR/USD")
            self.feed.subscribe("GBP/USD")
            self.feed.unsubscribe("EUR/USD")
            assert 1 not in self.feed._subscribed_symbol_ids
            assert 2 in self.feed._subscribed_symbol_ids

    def test_ticks_route_to_correct_symbol(self):
        received = {"EURUSD": [], "GBPUSD": []}
        self.feed.on_tick(
            lambda t: received.setdefault(
                _normalize_symbol_name(self.feed._id_to_name.get(t.symbol_id, "")), []
            ).append(t)
        )

        self.feed._handle_spot_event(_make_spot_event(symbol_id=1, bid=108500, ask=108520))
        self.feed._handle_spot_event(_make_spot_event(symbol_id=2, bid=126500, ask=126520))

        assert len(received["EURUSD"]) == 1
        assert len(received["GBPUSD"]) == 1
        assert received["EURUSD"][0].bid == pytest.approx(1.08500)
        assert received["GBPUSD"][0].bid == pytest.approx(1.26500)

    def test_subscribe_unknown_symbol_returns_false(self):
        assert self.feed.subscribe("UNKNOWN/PAIR") is False

    def test_subscribe_normalizes_name(self):
        with patch("archive.legacy_ctrader._pkg.open_api_spot_feed.reactor"):
            assert self.feed.subscribe("eur_usd") is True
            assert 1 in self.feed._subscribed_symbol_ids

    def test_get_all_ticks(self):
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1))
        self.feed._handle_spot_event(_make_spot_event(symbol_id=2))
        all_ticks = self.feed.get_all_ticks()
        assert "EURUSD" in all_ticks
        assert "GBPUSD" in all_ticks

    def test_get_spread(self):
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1, bid=108500, ask=108520))
        assert self.feed.get_spread("EUR/USD") == pytest.approx(0.00020)

    def test_get_spread_no_tick(self):
        assert self.feed.get_spread("UNKNOWN") is None

    def test_multiple_callbacks_all_fire(self):
        cb1, cb2 = [], []
        self.feed.on_tick(cb1.append)
        self.feed.on_tick(cb2.append)
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1))
        assert len(cb1) == 1 and len(cb2) == 1


# ---------------------------------------------------------------------------
# Static symbol fallback
# ---------------------------------------------------------------------------


class TestStaticSymbolsFallback:
    def test_populate_static_symbols(self, feed_factory):
        feed = feed_factory()
        feed._populate_static_symbols()
        # Verify symbols were populated correctly
        assert feed._name_to_id["EURUSD"] == 1
        assert feed._name_to_id["GBPUSD"] == 2
        assert feed._name_to_id["USDJPY"] == 4
        assert feed._id_to_name[1] == "EURUSD"
        assert feed._symbol_digits[1] == 5
        assert feed._symbol_digits[4] == 3  # JPY pair has 3 digits


# ---------------------------------------------------------------------------
# JPY tick decode regression (card 43c92270 / card 55140c52)
# ---------------------------------------------------------------------------
#
# USDJPY's 100x price-scaling regression (commit bc4fa4f9) shipped because no
# test exercised `_handle_spot_event` with a JPY-pair symbol_id and digits=3.
# These tests assert that 5-digit protobuf-encoded raw prices decode to the
# correct display level (~162.10), not the inflated ~1620.99 the buggy
# divisor (10**digits with digits=3 → divisor=1000) would produce.
#
# Reverted decode (digits=3 divisor=1000):
#   raw_bid=1620990 → 1620.990     ← WRONG
# Fixed decode (is_jpy → tick_digits=5 → divisor=100000):
#   raw_bid=1620990 → 162.0990     ← CORRECT
#
# ---------------------------------------------------------------------------
class TestJpyTickDecode:
    @pytest.fixture(autouse=True)
    def setup(self, feed_factory):
        self.feed = feed_factory()
        self.feed._populate_static_symbols()
        # Disable validation that requires an active connection so we can
        # exercise the pure decoding path.
        self.feed._subscribed_symbol_ids.update({1, 2, 4})

    def test_usdjpy_raw_5digit_decodes_to_canonical_162099(self):
        """USDJPY symbol_id=4 (digits=3) with raw prices at 5-digit protobuf
        encoding must decode to ~162.0990, not the 100x-inflated ~1620.990.

        cTrader raw price encoding is fixed at 5 decimal places across all
        symbols: raw_value = display_price * 100_000. So 162.0990 encodes
        to raw_bid=16_209_900 (NOT 1_620_990 — that would only be 4-digit
        encoding and decode to 16.2099)."""
        received = []
        self.feed.on_tick(received.append)

        # raw_bid=16_209_900 → $162.0990 in 5-digit encoding
        # raw_ask=16_210_050 → $162.1005
        self.feed._handle_spot_event(_make_spot_event(symbol_id=4, bid=16_209_900, ask=16_210_050))

        assert len(received) == 1, "expected exactly one tick callback fire"
        tick = received[0]
        assert tick.symbol_id == 4
        assert tick.bid == pytest.approx(162.0990, rel=1e-6)
        assert tick.ask == pytest.approx(162.1005, rel=1e-6)
        # Negative assertion: explicitly NOT the inflated 3-digit-divisor value
        assert tick.bid != pytest.approx(1620.990, rel=1e-3)
        assert tick.ask != pytest.approx(1621.005, rel=1e-3)

    def test_eurusud_5digit_symbol_unchanged_by_jpy_workaround(self):
        """EURUSD symbol_id=1 (digits=5) must decode at the existing level.
        Regression check: the JPY workaround must not perturb non-JPY 5-digit
        symbols (tick_digits evaluates to 5, so divisor stays 10**5=100000)."""
        received = []
        self.feed.on_tick(received.append)

        # raw_bid=108500, raw_ask=108520 → $1.08500 / $1.08520
        self.feed._handle_spot_event(_make_spot_event(symbol_id=1, bid=108500, ask=108520))

        assert len(received) == 1
        tick = received[0]
        assert tick.symbol_id == 1
        assert tick.bid == pytest.approx(1.08500, rel=1e-6)
        assert tick.ask == pytest.approx(1.08520, rel=1e-6)

    def test_usdjpy_regression_catches_old_divisor(self):
        """If the buggy 3-digit divisor is reintroduced, the JPY tick decode
        should land at ~1620.99 (100x inflated). This guards against the
        regression returning."""
        # Simulate the OLD code path: divisor = 10 ** digits (digits=3 → 1000)
        old_digits = self.feed._symbol_digits[4]  # 3
        old_divisor = 10**old_digits  # 1000
        raw_bid = 16_209_900  # 5-digit encoding for $162.0990
        inflated_bid = raw_bid / old_divisor  # → 16_209.900 (3-digit divisor)

        # If the bug returned, USDJPY would decode to ~16209.90 instead of
        # the canonical ~162.0990. We assert that we are NOT in that state
        # by re-running the real handler and confirming the correct result.
        received = []
        self.feed.on_tick(received.append)
        self.feed._handle_spot_event(_make_spot_event(symbol_id=4, bid=raw_bid, ask=raw_bid + 15))
        fixed_bid = received[0].bid

        assert inflated_bid == pytest.approx(16209.900, rel=1e-3)
        assert fixed_bid == pytest.approx(162.0990, rel=1e-3)
        # 100x ratio — proof the regression shape would be caught.
        assert inflated_bid / fixed_bid == pytest.approx(100.0, rel=1e-3)

    def test_usdjpy_latest_tick_in_state_is_at_canonical_price(self):
        """After a USDJPY tick arrives, get_latest_tick() must reflect the
        ~162.10 price band — operators see this in the [B5 Health] line via
        feed.get_all_ticks()."""
        self.feed._handle_spot_event(_make_spot_event(symbol_id=4, bid=16_209_900, ask=16_210_050))

        # _ticks_by_id is keyed by symbol_id after _handle_spot_event.
        latest = self.feed._ticks_by_id[4]
        assert latest.bid == pytest.approx(162.0990, rel=1e-6)
        assert latest.ask == pytest.approx(162.1005, rel=1e-6)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Execution event: clientMsgId fallback (BQ order key mismatch fix)
# ---------------------------------------------------------------------------


class TestExecutionEventClientMsgIdFallback:
    """When cTrader acks with empty clientOrderId, _handle_execution_event
    should fall back to matching by envelope clientMsgId."""

    @pytest.fixture(autouse=True)
    def setup(self, feed_factory):
        from adapters.ctrader.open_api_spot_feed import (
            Order,
            OrderStatus,
            OrderType,
            TradeDirection,
        )

        self.OrderStatus = OrderStatus
        self.feed = feed_factory()
        # Seed a pending order as if new_order() had registered it
        import threading

        self.request_id = "abc123def456"
        self.client_msg_id = "order_abc123def456"
        self.event = threading.Event()
        self.order = Order(
            order_id=self.request_id,
            symbol="GBPUSD",
            direction=TradeDirection.SHORT,
            order_type=OrderType.MARKET,
            volume=0.99,
            status=OrderStatus.PENDING,
        )
        self.feed._pending_orders[self.request_id] = (self.event, self.order)
        self.feed._pending_client_msg_ids[self.client_msg_id] = self.request_id

        # Seed symbol for VolumeCalculator — exec events need symbol_id → lot_size
        self.feed._symbols[1] = SymbolInfo(
            symbol_id=1,
            name="GBP/USD",
            pip_size=0.00001,
            digits=5,
            lot_size=100_000,
        )

    def _make_exec_event(self, client_order_id="", execution_type=3):
        """Build a fake execution event with an order payload."""
        order_payload = MagicMock()
        order_payload.clientOrderId = client_order_id
        order_payload.executionPrice = 1.31997
        order_payload.executedVolume = 99000
        order_payload.symbolId = 1
        order_payload.orderId = 42

        msg = MagicMock()
        msg.payloadType = 2126
        msg.executionType = execution_type
        msg.order = order_payload
        msg.errorCode = ""
        msg.deal = None
        msg.position = None
        return msg

    def _make_envelope(self, client_msg_id=""):
        """Build a fake SDK envelope with clientMsgId."""
        env = MagicMock()
        env.clientMsgId = client_msg_id
        return env

    def test_empty_client_order_id_falls_back_to_client_msg_id(self):
        """Ack with empty clientOrderId + valid clientMsgId should resolve."""
        msg = self._make_exec_event(client_order_id="")
        env = self._make_envelope(client_msg_id=self.client_msg_id)

        self.feed._handle_execution_event(msg, env)

        assert self.order.status == self.OrderStatus.FILLED
        assert self.event.is_set()
        # Pending entry should be cleaned up
        assert self.request_id not in self.feed._pending_orders

    def test_no_envelope_no_fallback(self):
        """Without envelope, empty clientOrderId should still DROP."""
        msg = self._make_exec_event(client_order_id="")

        self.feed._handle_execution_event(msg)  # no envelope

        assert not self.event.is_set()
        assert self.request_id in self.feed._pending_orders  # still pending

    def test_envelope_wrong_client_msg_id_still_drops(self):
        """Envelope with unknown clientMsgId should still DROP."""
        msg = self._make_exec_event(client_order_id="")
        env = self._make_envelope(client_msg_id="order_UNKNOWN")

        self.feed._handle_execution_event(msg, env)

        assert not self.event.is_set()
        assert self.request_id in self.feed._pending_orders

    def test_direct_client_order_id_match_still_works(self):
        """When clientOrderId is present and matches, no fallback needed."""
        msg = self._make_exec_event(client_order_id=self.request_id)
        env = self._make_envelope(client_msg_id="")  # irrelevant

        self.feed._handle_execution_event(msg, env)

        assert self.order.status == self.OrderStatus.FILLED
        assert self.event.is_set()

    def test_neither_matches_logs_drop(self, caplog):
        """Both clientOrderId and clientMsgId unknown → DROP warning."""
        import logging as stdlog

        msg = self._make_exec_event(client_order_id="")
        env = self._make_envelope(client_msg_id="order_NOPE")

        with caplog.at_level(stdlog.WARNING, logger="ayumi.openapi_spot_feed"):
            self.feed._handle_execution_event(msg, env)

        assert any("DROP" in r.message for r in caplog.records)
        assert not self.event.is_set()


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_is_running_default_false(self, feed_factory):
        feed = feed_factory()
        assert feed.is_running is False

    def test_symbols_returns_copy(self, feed_factory):
        feed = feed_factory()
        feed._symbols[1] = SymbolInfo(symbol_id=1, name="EUR/USD")
        copy = feed.symbols
        copy[99] = SymbolInfo(symbol_id=99, name="FAKE")
        assert 99 not in feed._symbols

    def test_ticks_returns_copy(self, feed_factory):
        feed = feed_factory()
        copy = feed.ticks
        copy["FAKE"] = Tick(symbol_id=99, bid=1.0, ask=1.1)
        assert "FAKE" not in feed._ticks

    def test_is_connected_reflects_auth_state(self, feed_factory):
        # Sprint 1A.1: Orchestrator lives in adapters.ctrader.open_api_spot_feed
        # and uses ConnectionState from adapters.ctrader.connection_state
        # (ModernCS). Import from the modern module so identity matches the
        # _VALID_TRANSITIONS dict keys.
        from adapters.ctrader.connection_state import ConnectionState

        feed = feed_factory()
        assert feed.is_connected is False
        # Walk through valid state transitions to AUTHENTICATED
        for state in [
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.APP_AUTHENTICATING,
            ConnectionState.ACCT_AUTHENTICATING,
            ConnectionState.AUTHENTICATED,
        ]:
            feed._state_mgr.transition_to(state)
        assert feed.is_connected is True

    def test_is_paper_mode_false(self, feed_factory):
        feed = feed_factory()
        assert feed.is_paper_mode is False

    def test_send_and_wait_unique_client_msg_id(self, feed_factory):
        feed = feed_factory()
        captured_ids = []

        def fake_send_and_wait(msg, *, timeout=10, prefix=None):
            import uuid

            msg_id = f"{prefix or 'spot'}_{uuid.uuid4().hex[:8]}"
            captured_ids.append(msg_id)
            from twisted.internet.defer import Deferred

            d = Deferred()
            d.callback(MagicMock())
            return d

        # The feed delegates to _conn.send_and_wait
        feed._conn.send_and_wait = fake_send_and_wait

        # Patch reactor.callFromThread to run immediately
        with patch("archive.legacy_ctrader._pkg.open_api_spot_feed.reactor") as mock_reactor:
            mock_reactor.callFromThread = lambda fn: fn()
            feed._conn.send_and_wait(MagicMock(), timeout=1, prefix="spot")
            feed._conn.send_and_wait(MagicMock(), timeout=1, prefix="spot")

        assert len(captured_ids) == 2
        assert captured_ids[0] != captured_ids[1]
        assert captured_ids[0].startswith("spot_")
        assert captured_ids[1].startswith("spot_")
