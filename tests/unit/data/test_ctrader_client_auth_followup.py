"""Tests for the CTraderHistoricalClient host + account_id auth fix.

Card 377b2bab-1350-473c-b623-86cd86d4560b (auth follow-up).

Root cause: ``src/forex_bot/data/ctrader_client.py`` hardcoded
``_PROTOBUF_HOST = "live.ctraderapi.com"`` and required ``trader_login``
(human-readable account number) which forced a
``ProtoOAGetAccountListByAccessTokenReq`` round-trip on every run. The
demo access token (loaded from ``.env`` with ``CTRADER_HOST=demo``)
rejected that round-trip at the live endpoint with
``UNSUPPORTED_MESSAGE / Trading account is not authorized``.

The fix mirrors the working live adapter exactly:

1. Host defaults to ``EndPoints.PROTOBUF_DEMO_HOST`` and honours the
   ``CTRADER_HOST`` env var.
2. The constructor accepts an ``account_id`` (the internal
   ``ctidTraderAccountId``) and short-circuits the
   ``GetAccountListByAccessToken`` round-trip when it's known.
3. Auth sequence collapses to ``App auth 2101 → Account auth 2103``,
   byte-identical to the live adapter's pattern.

All tests use mocks; no live broker call is made.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Project root setup so ``data.ctrader_client`` resolves.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PROJECT_ROOT / "src" / "forex_bot"))

from ctrader_open_api.endpoints import EndPoints  # noqa: E402
from ctrader_open_api.messages.OpenApiMessages_pb2 import (  # noqa: E402
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthReq,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetAccountListByAccessTokenRes,
)
from data.ctrader_client import (  # noqa: E402
    CTraderHistoricalClient,
    _resolve_host,
)


def _make_proto_msg(payload_type: int, payload: bytes = b"") -> MagicMock:
    """Build a mock cTrader ProtoMessage with the given payloadType."""
    msg = MagicMock()
    msg.payloadType = payload_type
    msg.payload = payload
    return msg


def _make_account_auth_res_bytes(ctid: int = 46877902) -> bytes:
    """Serialize a real ProtoOAAccountAuthRes (acknowledgement frame)."""
    res = ProtoOAAccountAuthRes()
    res.ctidTraderAccountId = ctid
    return res.SerializeToString()


def _make_account_list_res_bytes(
    ctid: int = 46877902, login: int = 5795523, isLive: bool = False
) -> bytes:
    """Serialize a real ``ProtoOAGetAccountListByAccessTokenRes`` for
    the legacy ``trader_login`` resolve path."""
    res = ProtoOAGetAccountListByAccessTokenRes()
    res.accessToken = "test_access_token"  # required field  # noqa: S106
    acc = res.ctidTraderAccount.add()
    acc.ctidTraderAccountId = ctid
    acc.traderLogin = login
    acc.isLive = isLive
    return res.SerializeToString()


# ---------------------------------------------------------------------------
# 1. Host resolution — must NOT hardcode live.ctraderapi.com anymore
# ---------------------------------------------------------------------------


class TestHostResolution:
    """The historical path used to hardcode ``live.ctraderapi.com`` and
    therefore every demo / paper / practice account got
    ``UNSUPPORTED_MESSAGE`` from the broker. The fix defaults to
    ``EndPoints.PROTOBUF_DEMO_HOST`` and honours ``CTRADER_HOST``.
    """

    def test_default_host_is_demo_endpoints_constant(self, monkeypatch):
        """Without any env override, ``_resolve_host()`` must return the
        SDK's ``PROTOBUF_DEMO_HOST`` constant (NOT the old hardcoded
        ``live.ctraderapi.com`` string)."""
        monkeypatch.delenv("CTRADER_HOST", raising=False)
        assert _resolve_host() == EndPoints.PROTOBUF_DEMO_HOST
        # Sanity: it must not be the LIVE host that the old code used.
        assert _resolve_host() != "live.ctraderapi.com"

    def test_env_ctrader_host_is_honoured(self, monkeypatch):
        monkeypatch.setenv("CTRADER_HOST", "demo-uk-eqx-01.p.c-trader.com")
        assert _resolve_host() == "demo-uk-eqx-01.p.c-trader.com"

    def test_explicit_arg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CTRADER_HOST", "demo.ctraderapi.com")
        assert _resolve_host("live.ctraderapi.com") == "live.ctraderapi.com"

    def test_client_constructor_uses_demo_default(self, monkeypatch):
        """``CTraderHistoricalClient(... account_id=...)`` with no
        CTRADER_HOST env var must connect to the DEMO host, not the
        LIVE broker (this is the exact bug the prior fix masked by
        hardcoding live.ctraderapi.com)."""
        monkeypatch.delenv("CTRADER_HOST", raising=False)
        client = CTraderHistoricalClient(
            client_id="test_client_id",
            client_secret="test_secret",  # noqa: S106
            access_token="test_access_token",  # noqa: S106
            refresh_token="test_refresh_token",  # noqa: S106
            account_id=46877902,
        )
        assert client.host == EndPoints.PROTOBUF_DEMO_HOST
        assert client.host != "live.ctraderapi.com"

    def test_client_constructor_honours_ctrader_host_env(self, monkeypatch):
        monkeypatch.setenv("CTRADER_HOST", "demo.ctraderapi.com")
        client = CTraderHistoricalClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            access_token="x",  # noqa: S106
            refresh_token="x",  # noqa: S106
            account_id=123,
        )
        assert client.host == "demo.ctraderapi.com"

    def test_client_constructor_explicit_host_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CTRADER_HOST", "demo.ctraderapi.com")
        client = CTraderHistoricalClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            access_token="x",  # noqa: S106
            refresh_token="x",  # noqa: S106
            account_id=123,
            host="live.ctraderapi.com",
        )
        assert client.host == "live.ctraderapi.com"


# ---------------------------------------------------------------------------
# 2. Constructor contract — account_id preferred, trader_login legacy
# ---------------------------------------------------------------------------


class TestConstructorContract:
    def test_account_id_sets_ctid_immediately(self):
        """Supplying ``account_id`` at construction seeds the ctid so
        the failing ``GetAccountListByAccessToken`` round-trip is never
        issued — matches the live working adapter."""
        client = CTraderHistoricalClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            access_token="x",  # noqa: S106
            refresh_token="x",  # noqa: S106
            account_id=46877902,
        )
        assert client._ctid_account_id == 46877902
        # trader_login is None for the canonical path.
        assert client.trader_login is None

    def test_trader_login_only_keeps_ctid_unresolved(self):
        """Legacy callers that still pass only ``trader_login`` keep
        the broker-resolve path (back-compat)."""
        client = CTraderHistoricalClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            access_token="x",  # noqa: S106
            refresh_token="x",  # noqa: S106
            trader_login=5795523,
        )
        assert client._ctid_account_id is None
        assert client.trader_login == 5795523

    def test_missing_both_raises(self):
        """Both ``account_id`` and ``trader_login`` absent → ValueError
        with a clear message (no silent fallback)."""
        with pytest.raises(ValueError, match="account_id"):
            CTraderHistoricalClient(
                client_id="x",
                client_secret="x",  # noqa: S106
                access_token="x",  # noqa: S106
                refresh_token="x",  # noqa: S106
            )


# ---------------------------------------------------------------------------
# 3. Auth flow — account_id path skips GetAccountListByAccessToken
# ---------------------------------------------------------------------------


class TestAccountIdAuthFlow:
    """The cleanest proof that the historical path now mirrors the
    working live adapter: when ``account_id`` is supplied, ``_auth``
    sends exactly ``App auth 2101 → Account auth 2103`` and never
    issues ``GetAccountListByAccessToken`` (2150)."""

    @pytest.mark.asyncio
    async def test_account_id_skips_get_account_list(self):
        client = CTraderHistoricalClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            access_token="x",  # noqa: S106
            refresh_token="x",  # noqa: S106
            account_id=46877902,
        )
        fake_client = MagicMock()
        fake_client.send = AsyncMock(
            side_effect=[
                _make_proto_msg(2101, b""),                              # app auth ack
                _make_proto_msg(2103, _make_account_auth_res_bytes()),   # acct auth ack
            ]
        )

        await client._auth(fake_client)

        # Exactly two sends — no resolve round-trip.
        assert fake_client.send.await_count == 2
        # First send: App auth.
        first_call = fake_client.send.await_args_list[0]
        assert isinstance(first_call.args[0], ProtoOAApplicationAuthReq)
        # Second send: Account auth with the supplied ctid.
        second_call = fake_client.send.await_args_list[1]
        assert isinstance(second_call.args[0], ProtoOAAccountAuthReq)
        assert second_call.args[0].ctidTraderAccountId == 46877902
        # Critically: no ProtoOAGetAccountListByAccessTokenReq was ever sent.
        for call in fake_client.send.await_args_list:
            assert not isinstance(call.args[0], ProtoOAGetAccountListByAccessTokenReq)

    @pytest.mark.asyncio
    async def test_trader_login_path_still_uses_get_account_list(self):
        """Legacy path (trader_login only) keeps the broker-side
        resolve — backwards-compat for callers without a pre-known ctid.
        """
        client = CTraderHistoricalClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            access_token="x",  # noqa: S106
            refresh_token="x",  # noqa: S106
            trader_login=5795523,
        )
        bytes_obj = _make_account_list_res_bytes()

        fake_client = MagicMock()
        fake_client.send = AsyncMock(
            side_effect=[
                _make_proto_msg(2150, bytes_obj),                       # account list
                _make_proto_msg(2101, b""),                            # app auth ack
                _make_proto_msg(2103, _make_account_auth_res_bytes()), # acct auth ack
            ]
        )

        await client._auth(fake_client)

        # Three sends — resolve first, then app auth, then account auth.
        assert fake_client.send.await_count == 3
        first_call = fake_client.send.await_args_list[0]
        assert isinstance(first_call.args[0], ProtoOAGetAccountListByAccessTokenReq)
        assert isinstance(fake_client.send.await_args_list[1].args[0], ProtoOAApplicationAuthReq)
        assert isinstance(fake_client.send.await_args_list[2].args[0], ProtoOAAccountAuthReq)
        assert client._ctid_account_id == 46877902


# ---------------------------------------------------------------------------
# 4. download_ctrader_data.py passes account_id when available
# ---------------------------------------------------------------------------


class TestDownloadScriptWiring:
    def test_script_passes_account_id_from_env(self, monkeypatch, tmp_path):
        """``CTRADER_OPENAPI_ACCOUNT_ID`` must flow through to the
        client constructor as the preferred ``account_id`` arg."""
        from scripts import download_ctrader_data

        monkeypatch.setenv("CTRADER_OPENAPI_CLIENT_ID", "cid")
        monkeypatch.setenv("CTRADER_OPENAPI_CLIENT_SECRET", "csec")
        monkeypatch.setenv("CTRADER_OPENAPI_REFRESH_TOKEN", "rt")
        monkeypatch.setenv("CTRADER_OPENAPI_ACCESS_TOKEN", "at")
        monkeypatch.setenv("CTRADER_OPENAPI_ACCOUNT_ID", "46877902")
        # No trader_login env — the script should fall back gracefully
        # but account_id is canonical.

        captured: dict = {}

        class _FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def download_and_save(self, *a, **kw):
                # No-op so the script returns without touching the broker.
                return None

        monkeypatch.setattr(download_ctrader_data, "CTraderHistoricalClient", _FakeClient)

        with patch.object(download_ctrader_data, "validate_request", lambda args: None):
            rc = download_ctrader_data.main([
                "--symbols", "XAUUSD",
                "--timeframes", "H1",
                "--start", "2026-09-01",
                "--end", "2026-09-02",
                "--csv-dir", str(tmp_path),
            ])
        assert rc == 0
        # The script must have called the client with account_id=46877902.
        assert captured.get("account_id") == 46877902
        # And NOT with trader_login (since account_id was supplied).
        assert "trader_login" not in captured or captured.get("trader_login") is None

    def test_script_falls_back_to_trader_login(self, monkeypatch, tmp_path):
        """If ``CTRADER_OPENAPI_ACCOUNT_ID`` is absent, the script
        must still pass ``trader_login`` from the legacy
        ``CTRADER_OPENAPI_TRADER_LOGIN`` env."""
        from scripts import download_ctrader_data

        monkeypatch.setenv("CTRADER_OPENAPI_CLIENT_ID", "cid")
        monkeypatch.setenv("CTRADER_OPENAPI_CLIENT_SECRET", "csec")
        monkeypatch.setenv("CTRADER_OPENAPI_REFRESH_TOKEN", "rt")
        monkeypatch.setenv("CTRADER_OPENAPI_ACCESS_TOKEN", "at")
        monkeypatch.delenv("CTRADER_OPENAPI_ACCOUNT_ID", raising=False)
        monkeypatch.setenv("CTRADER_OPENAPI_TRADER_LOGIN", "5795523")

        captured: dict = {}

        class _FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def download_and_save(self, *a, **kw):
                return None

        monkeypatch.setattr(download_ctrader_data, "CTraderHistoricalClient", _FakeClient)

        with patch.object(download_ctrader_data, "validate_request", lambda args: None):
            rc = download_ctrader_data.main([
                "--symbols", "XAUUSD",
                "--timeframes", "H1",
                "--start", "2026-09-01",
                "--end", "2026-09-02",
                "--csv-dir", str(tmp_path),
            ])
        assert rc == 0
        assert captured.get("trader_login") == 5795523
        # And no account_id was supplied.
        assert "account_id" not in captured or captured.get("account_id") is None


# ---------------------------------------------------------------------------
# 5. _on_tcp_disconnected callback signature matches the SDK call shape
# ---------------------------------------------------------------------------


class TestProbeCallbackArity:
    """Card 377b2bab follow-up: ``CTraderOpenApiClient._on_tcp_disconnected``
    was registered directly as the SDK's ``setDisconnectedCallback``. The
    SDK invokes the callback as ``callback(client, reason)`` (two
    positional args), but the method declared ``(self, _)`` — leading
    to ``TypeError: takes 2 positional arguments but 3 were given`` and
    rendering the diagnostic probe unusable. The fix wraps the bound
    method with a lambda that drops the implicit client arg.
    """

    def test_disconnect_handler_accepts_sdk_call_shape(self):
        """The handler must accept the SDK's two-positional-arg call
        ``handler(reason)`` (the client arg is dropped by the lambda
        registered in ``_do_connect``)."""
        from src.forex_bot.adapters.ctrader.open_api_client import (
            CTraderOpenApiClient,
        )

        # Construct with placeholder creds — we never call connect().
        client = CTraderOpenApiClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            account_id=46877902,
            access_token="x",  # noqa: S106
        )
        # The registered handler must accept exactly one arg (the
        # reason) — i.e. _on_tcp_disconnected takes (self, reason) and
        # the lambda drops the SDK's first positional.
        # Simulate the SDK call pattern that used to crash.
        client._on_tcp_disconnected("connection lost")  # must not raise

    def test_disconnect_handler_handles_no_args(self):
        """Some SDK paths call the callback with no args at all (e.g.
        a graceful ``stopService``). The handler must default to
        ``reason=None`` so it remains callable."""
        from src.forex_bot.adapters.ctrader.open_api_client import (
            CTraderOpenApiClient,
        )
        client = CTraderOpenApiClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            account_id=46877902,
            access_token="x",  # noqa: S106
        )
        client._on_tcp_disconnected()  # must not raise
        client._on_tcp_disconnected(None)  # must not raise
