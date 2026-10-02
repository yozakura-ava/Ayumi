"""Tests for existing cTrader trade execution methods in OpenApiSpotFeed.

This module tests the real order execution surface that already lives in
``src/forex_bot/adapters/ctrader/open_api_spot_feed.py``:

- ``new_order()`` constructs and sends ``ProtoOANewOrderReq``.
- ``send_order()`` converts friendly arguments and delegates to ``new_order()``.
- ``close_position()`` constructs and sends ``ProtoOAClosePositionReq``.
- ``reconcile()`` constructs and sends ``ProtoOAReconcileReq`` and parses the
  response into ``Position`` objects.

All network calls are mocked; no real cTrader connection is established.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from adapters.ctrader.connection_state import ConnectionState
from adapters.ctrader.models import (
    OrderStatus,
    OrderType,
    PositionStatus,
    TradeDirection,
)
from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed, _normalize_symbol_name
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAClosePositionReq,
    ProtoOANewOrderReq,
    ProtoOAReconcileReq,
    ProtoOAReconcileRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOAPositionStatus,
    ProtoOATimeInForce,
    ProtoOATradeSide,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _run_reactor_calls_inline(monkeypatch):
    """Make reactor.callFromThread execute synchronously during tests.

    The spot feed queues outbound protobuf sends on the Twisted reactor thread.
    In a headless test there is no running reactor, so without this patch the
    sends would never execute and the tests would time out.
    """
    from twisted.internet import reactor

    def _inline_call_from_thread(callable, *args, **kwargs):
        callable(*args, **kwargs)

    monkeypatch.setattr(reactor, "callFromThread", _inline_call_from_thread)


def _make_feed() -> OpenApiSpotFeed:
    """Build an isolated OpenApiSpotFeed with a fake connection."""
    feed = OpenApiSpotFeed(
        ctid_account_id=5795523,
        client_id="test_client",
        client_secret="test_secret",  # noqa: S106
        access_token="test_token",  # noqa: S106
        host="demo.ctraderapi.com",
        port=5035,
    )
    # Walk the state machine through the normal connect sequence so it ends in AUTHENTICATED.
    feed._state_mgr.transition_to(ConnectionState.CONNECTING, reason="test")
    feed._state_mgr.transition_to(ConnectionState.CONNECTED, reason="test")
    feed._state_mgr.transition_to(ConnectionState.APP_AUTHENTICATING, reason="test")
    feed._state_mgr.transition_to(ConnectionState.ACCT_AUTHENTICATING, reason="test")
    feed._state_mgr.transition_to(ConnectionState.AUTHENTICATED, reason="test")
    # Pre-populate symbol map so resolve_symbol_id works.
    feed._id_to_name = {1: "EURUSD"}
    feed._name_to_id = {_normalize_symbol_name("EURUSD"): 1}
    # Mutate in-place: VolumeCalculator holds a reference to the original dict
    # from __init__, so re-assigning feed._symbols would orphan it.
    feed._symbols.clear()
    feed._symbols[1] = MagicMock(pip_size=1e-5, digits=5, lot_size=100_000)
    return feed


def _set_connected(feed: OpenApiSpotFeed, connected: bool = True) -> None:
    """Toggle operational state used by new_order / reconcile guards."""
    if connected:
        # Already at AUTHENTICATED from _make_feed; self-transition is allowed.
        feed._state_mgr.transition_to(ConnectionState.AUTHENTICATED, reason="test")
    else:
        feed._state_mgr.transition_to(ConnectionState.DISCONNECTED, reason="test")


def _capture_send(feed: OpenApiSpotFeed) -> list:
    """Replace the underlying ctrader_open_api Client.send and return captured calls."""
    captured = []

    def fake_send(req, *, clientMsgId=None, responseTimeoutInSeconds=None):
        captured.append((req, clientMsgId, responseTimeoutInSeconds))
        return MagicMock()

    feed._conn._client = MagicMock()
    feed._conn._client.send = fake_send
    return captured


# ── new_order ───────────────────────────────────────────────────────────────


class TestNewOrder:
    def test_new_order_builds_market_buy_request(self):
        feed = _make_feed()
        _set_connected(feed)
        captured = _capture_send(feed)

        order = feed.new_order(
            symbol_id=1,
            side=ProtoOATradeSide.BUY,
            volume=100_000,
            order_type=ProtoOAOrderType.MARKET,
            sl=1.09500,
            tp=1.11500,
            comment="market_buy_test",
            timeout=0.01,
        )

        assert len(captured) == 1
        req, client_msg_id, timeout = captured[0]
        assert isinstance(req, ProtoOANewOrderReq)
        assert req.ctidTraderAccountId == 5795523
        assert req.symbolId == 1
        assert req.orderType == ProtoOAOrderType.MARKET
        assert req.tradeSide == ProtoOATradeSide.BUY
        assert req.volume == 100_000
        assert req.timeInForce == ProtoOATimeInForce.GOOD_TILL_CANCEL
        assert req.stopLoss == 1.09500
        assert req.takeProfit == 1.11500
        assert req.comment == "market_buy_test"
        assert req.clientOrderId == order.order_id
        assert client_msg_id.startswith("order_")
        assert timeout == 0.01

    def test_new_order_builds_limit_sell_request(self):
        feed = _make_feed()
        _set_connected(feed)
        captured = _capture_send(feed)

        feed.new_order(
            symbol_id=1,
            side=ProtoOATradeSide.SELL,
            volume=50_000,
            order_type=ProtoOAOrderType.LIMIT,
            price=1.12000,
            sl=1.13000,
            tp=1.10000,
            time_in_force=ProtoOATimeInForce.GOOD_TILL_DATE,
            comment="limit_sell_test",
            timeout=0.01,
        )

        req = captured[0][0]
        assert req.orderType == ProtoOAOrderType.LIMIT
        assert req.tradeSide == ProtoOATradeSide.SELL
        assert req.limitPrice == 1.12000
        assert req.volume == 50_000
        assert req.timeInForce == ProtoOATimeInForce.GOOD_TILL_DATE

    def test_new_order_builds_stop_buy_request(self):
        feed = _make_feed()
        _set_connected(feed)
        captured = _capture_send(feed)

        feed.new_order(
            symbol_id=1,
            side=ProtoOATradeSide.BUY,
            volume=25_000,
            order_type=ProtoOAOrderType.STOP,
            price=1.12500,
            timeout=0.01,
        )

        req = captured[0][0]
        assert req.orderType == ProtoOAOrderType.STOP
        assert req.stopPrice == 1.12500

    def test_new_order_returns_order_model_with_long_direction(self):
        feed = _make_feed()
        _set_connected(feed)
        _capture_send(feed)

        order = feed.new_order(symbol_id=1, side=ProtoOATradeSide.BUY, volume=100_000, timeout=0.01)
        assert order.symbol == "EURUSD"
        assert order.direction == TradeDirection.LONG
        assert order.order_type == OrderType.MARKET
        assert order.volume == 1.0
        assert order.status == OrderStatus.PENDING

    def test_new_order_returns_order_model_with_short_direction(self):
        feed = _make_feed()
        _set_connected(feed)
        _capture_send(feed)

        order = feed.new_order(symbol_id=1, side=ProtoOATradeSide.SELL, volume=100_000, timeout=0.01)
        assert order.direction == TradeDirection.SHORT

    def test_new_order_not_connected_returns_order_with_reason(self):
        feed = _make_feed()
        _set_connected(feed, connected=False)
        _capture_send(feed)

        order = feed.new_order(symbol_id=1, side=ProtoOATradeSide.BUY, volume=100_000, timeout=0.01)

        assert len(_capture_send(feed)) == 0
        assert order.status == OrderStatus.PENDING
        assert getattr(order, "reason", None) == "not_connected"


# ── send_order ────────────────────────────────────────────────────────────────


class TestSendOrder:
    def test_send_order_converts_long_market_arguments(self):
        feed = _make_feed()
        _set_connected(feed)

        new_order_calls = []

        def fake_new_order(
            symbol_id,
            side,
            volume,
            *,
            order_type=ProtoOAOrderType.MARKET,
            price=None,
            sl=None,
            tp=None,
            time_in_force=ProtoOATimeInForce.GOOD_TILL_CANCEL,
            comment="",
            timeout=10.0,
        ):
            new_order_calls.append(
                {
                    "symbol_id": symbol_id,
                    "side": side,
                    "volume": volume,
                    "order_type": order_type,
                    "price": price,
                    "sl": sl,
                    "tp": tp,
                    "time_in_force": time_in_force,
                    "comment": comment,
                }
            )
            return MagicMock()

        feed.new_order = fake_new_order

        feed.send_order(
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            order_type=OrderType.MARKET,
            volume=0.5,
            stop_loss=1.09000,
            take_profit=1.12000,
            comment="high_conviction",
        )

        assert len(new_order_calls) == 1
        call = new_order_calls[0]
        assert call["symbol_id"] == 1
        assert call["side"] == ProtoOATradeSide.BUY
        assert call["order_type"] == ProtoOAOrderType.MARKET
        assert call["volume"] == 50_000
        assert call["sl"] == 1.09000
        assert call["tp"] == 1.12000
        assert call["comment"] == "high_conviction"

    def test_send_order_converts_short_limit_arguments(self):
        feed = _make_feed()
        _set_connected(feed)

        new_order_calls = []

        def fake_new_order(
            symbol_id,
            side,
            volume,
            *,
            order_type=ProtoOAOrderType.MARKET,
            price=None,
            sl=None,
            tp=None,
            time_in_force=ProtoOATimeInForce.GOOD_TILL_CANCEL,
            comment="",
            timeout=10.0,
        ):
            new_order_calls.append(
                {
                    "symbol_id": symbol_id,
                    "side": side,
                    "volume": volume,
                    "order_type": order_type,
                    "price": price,
                }
            )
            return MagicMock()

        feed.new_order = fake_new_order

        feed.send_order(
            symbol="EURUSD",
            direction=TradeDirection.SHORT,
            order_type=OrderType.LIMIT,
            volume=0.25,
            price=1.12500,
        )

        call = new_order_calls[0]
        assert call["side"] == ProtoOATradeSide.SELL
        assert call["order_type"] == ProtoOAOrderType.LIMIT
        assert call["price"] == 1.12500
        assert call["volume"] == 25_000

    def test_send_order_unknown_symbol_raises(self):
        feed = _make_feed()
        _set_connected(feed)
        with pytest.raises(ValueError, match="USDJPY"):
            feed.send_order(
                symbol="USDJPY",
                direction=TradeDirection.LONG,
                order_type=OrderType.MARKET,
                volume=0.1,
            )


# ── close_position ────────────────────────────────────────────────────────────


class TestClosePosition:
    def test_close_position_builds_close_request(self):
        feed = _make_feed()
        _set_connected(feed)

        responses = []

        def fake_send_and_wait(req, timeout=10.0, *, prefix="conn", client_msg_id=None):
            responses.append((req, timeout, prefix, client_msg_id))
            return MagicMock()

        feed._conn.send_and_wait = fake_send_and_wait
        result = feed.close_position(position_id=12345, volume=50_000, timeout=7.0)

        assert result is True
        assert len(responses) == 1
        req, timeout, prefix, _ = responses[0]
        assert isinstance(req, ProtoOAClosePositionReq)
        assert req.ctidTraderAccountId == 5795523
        assert req.positionId == 12345
        assert req.volume == 50_000
        assert timeout == 7.0
        assert prefix == "order"

    def test_close_position_send_failure_returns_false(self):
        feed = _make_feed()
        _set_connected(feed)
        feed._conn.send_and_wait = lambda *a, **kw: None
        assert feed.close_position(position_id=999, volume=10_000) is False


# ── reconcile ─────────────────────────────────────────────────────────────────


class TestReconcile:
    def test_reconcile_sends_reconcile_request(self):
        feed = _make_feed()
        _set_connected(feed)

        responses = []

        def fake_send_and_wait(req, timeout=10.0, *, prefix="conn", client_msg_id=None):
            responses.append((req, timeout, prefix))
            return None

        feed._conn.send_and_wait = fake_send_and_wait
        positions = feed.reconcile(timeout=8.0)

        assert len(responses) == 1
        req, timeout, prefix = responses[0]
        assert isinstance(req, ProtoOAReconcileReq)
        assert req.ctidTraderAccountId == 5795523
        assert timeout == 8.0
        assert prefix == "qry"
        assert positions == []

    def test_reconcile_parses_open_positions(self):
        feed = _make_feed()
        _set_connected(feed)

        payload = ProtoOAReconcileRes()
        payload.ctidTraderAccountId = 5795523
        pos = payload.position.add()
        pos.positionId = 101
        pos.positionStatus = ProtoOAPositionStatus.POSITION_STATUS_OPEN
        pos.swap = 0
        pos.tradeData.symbolId = 1
        pos.tradeData.volume = 100_000
        pos.tradeData.tradeSide = ProtoOATradeSide.BUY
        pos.price = 1.08500
        pos.stopLoss = 1.08000
        pos.takeProfit = 1.09000

        env = ProtoMessage()
        env.payloadType = payload.payloadType
        env.payload = payload.SerializeToString()

        feed._conn.send_and_wait = lambda *a, **kw: env

        positions = feed.reconcile()
        assert len(positions) == 1
        p = positions[0]
        assert p.position_id == "101"
        assert p.symbol == "EURUSD"
        assert p.direction == TradeDirection.LONG
        assert p.volume == 1.0
        assert p.entry_price == 1.08500
        assert p.stop_loss == 1.08000
        assert p.take_profit == 1.09000
        assert p.status == PositionStatus.OPEN

    def test_reconcile_not_operational_returns_empty(self):
        feed = _make_feed()
        _set_connected(feed, connected=False)

        def should_not_be_called(*a, **kw):
            raise AssertionError("send_and_wait should not be called when not operational")

        feed._conn.send_and_wait = should_not_be_called
        assert feed.reconcile() == []


# ── live-mode wiring sanity check ────────────────────────────────────────────


class TestLiveModeWiring:
    def test_spot_feed_reports_not_paper_mode(self):
        feed = _make_feed()
        _set_connected(feed)
        assert feed.is_paper_mode is False

    def test_spot_feed_reports_connected_when_authenticated(self):
        feed = _make_feed()
        _set_connected(feed)
        assert feed.is_connected is True
