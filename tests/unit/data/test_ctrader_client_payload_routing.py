"""Tests for the CTraderHistoricalClient async-path root-cause fix.

Card 377b2bab-1350-473c-b623-86cd86d4560b. The fix has three parts:

1. The async ``fetch_all`` path now resolves ``trader_login → ctid``
   *before* sending ``ProtoOAAccountAuthReq``. Without this, the
   broker rejected the auth with a None ctid and the next response
   frame on the shared TCP connection was misrouted onto the
   symbols-list parse slot, raising
   ``google.protobuf.message.DecodeError: Wire format was corrupt``.

2. ``_auth`` and ``_a_ensure_symbols`` now route on ``payloadType``
   via ``ctrader_open_api.Protobuf.extract`` instead of hard-parsing
   every frame as the expected concrete message type. The library
   already ships this dispatcher — it was simply not used here.

3. ``download_ctrader_data.py`` now reads ``CTRADER_OPENAPI_*`` env
   aliases (the canonical names from ``credential_store._ENV_KEYS``)
   before falling back to the legacy ``CTRADER_OAUTH_*`` / ``CTRADER_*``
   variants.

All tests use mocks so no live broker call is made.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Project root setup: src/forex_bot/ on sys.path so ``data.ctrader_client``
# resolves. Mirrors what conftest.py does at suite level.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PROJECT_ROOT / "src" / "forex_bot"))

from ctrader_open_api.messages.OpenApiMessages_pb2 import (  # noqa: E402
    ProtoOAAccountAuthRes,
    ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOASymbolsListRes,
)
from data.ctrader_client import (  # noqa: E402
    CTraderHistoricalClient,
    _extract_or_raise,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """Bare CTraderHistoricalClient with dummy credentials."""
    return CTraderHistoricalClient(
        client_id="test_client_id",
        client_secret="test_secret",  # noqa: S106
        access_token="test_access_token",  # noqa: S106
        refresh_token="test_refresh_token",  # noqa: S106
        trader_login=17087404,
    )


def _make_proto_msg(payload_type: int, payload: bytes) -> MagicMock:
    """Build a mock cTrader ProtoMessage with the given payloadType."""
    msg = MagicMock()
    msg.payloadType = payload_type
    msg.payload = payload
    return msg


def _make_account_list_res_bytes(ctid: int = 98765, login: int = 17087404) -> bytes:
    """Serialize a real ProtoOAGetAccountListByAccessTokenRes for use as
    a fake payload. Returns wire bytes — what the broker would send."""
    res = ProtoOAGetAccountListByAccessTokenRes()
    res.accessToken = "test_access_token"  # noqa: S106 (required field)
    acc = res.ctidTraderAccount.add()
    acc.ctidTraderAccountId = ctid
    acc.traderLogin = login
    acc.isLive = True
    return res.SerializeToString()


def _make_symbols_list_res_bytes(symbol_name: str = "XAU/USD", symbol_id: int = 42) -> bytes:
    """Serialize a real ProtoOASymbolsListRes with one symbol."""
    res = ProtoOASymbolsListRes()
    res.ctidTraderAccountId = 12345  # required field
    s = res.symbol.add()
    s.symbolName = symbol_name
    s.symbolId = symbol_id
    return res.SerializeToString()


def _make_error_res_bytes(error_code: str = "AUTH_FAILURE", description: str = "bad token") -> bytes:
    """Serialize a real ProtoOAErrorRes for use as a fake payload."""
    res = ProtoOAErrorRes()
    res.errorCode = error_code  # required field
    res.description = description
    return res.SerializeToString()


# ---------------------------------------------------------------------------
# 1. _auth resolves ctid BEFORE account-auth
# ---------------------------------------------------------------------------


class TestAuthResolvesCtidFirst:
    @pytest.mark.asyncio
    async def test_auth_resolves_ctid_before_account_auth(self, client):
        """The async path used to send ProtoOAAccountAuthReq with a None
        ctidTraderAccountId. After the fix, _resolve_ctid_account_id
        must run first and the cached ctid is what account-auth carries.
        """
        # Build a fake client where send() returns the account-list res on
        # the first call (resolve, payloadType=2150) and a stubbed success
        # frame on every subsequent call. _extract_or_raise dispatches
        # via Protobuf.extract, which uses the payloadType discriminator.
        account_list_bytes = _make_account_list_res_bytes()
        # App-auth (2104) and account-auth (2103) responses carry an empty
        # payload — they're just acknowledgement frames.
        account_auth_bytes = ProtoOAAccountAuthRes()
        account_auth_bytes.ctidTraderAccountId = 11111
        account_auth_bytes_bytes = account_auth_bytes.SerializeToString()
        send_responses = [
            _make_proto_msg(2150, account_list_bytes),  # resolve
            _make_proto_msg(2101, b""),                # app auth ack (empty payload is fine)
            _make_proto_msg(2103, account_auth_bytes_bytes),  # acct auth ack
        ]
        fake_client = MagicMock()
        fake_client.send = AsyncMock(side_effect=send_responses)

        await client._auth(fake_client)

        # Verify the resolve was the first send call and that the
        # account-auth sent a non-None ctid.
        first_call = fake_client.send.await_args_list[0]
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAGetAccountListByAccessTokenReq,
        )
        assert isinstance(first_call.args[0], ProtoOAGetAccountListByAccessTokenReq)

        # The third send (account-auth) must carry the resolved ctid,
        # not None.
        acct_call = fake_client.send.await_args_list[2]
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAAccountAuthReq
        assert isinstance(acct_call.args[0], ProtoOAAccountAuthReq)
        assert acct_call.args[0].ctidTraderAccountId == 98765
        assert client._ctid_account_id == 98765

    @pytest.mark.asyncio
    async def test_resolve_is_idempotent_when_ctid_already_set(self, client):
        """If a caller pre-populates _ctid_account_id, _auth must not
        issue a redundant resolve round-trip."""
        client._ctid_account_id = 11111  # pre-populated
        account_auth_bytes = ProtoOAAccountAuthRes()
        account_auth_bytes.ctidTraderAccountId = 11111
        account_auth_bytes_bytes = account_auth_bytes.SerializeToString()
        fake_client = MagicMock()
        fake_client.send = AsyncMock(
            side_effect=[
                _make_proto_msg(2101, b""),                       # app auth ack
                _make_proto_msg(2103, account_auth_bytes_bytes),  # acct auth ack
            ]
        )

        await client._auth(fake_client)

        # Two sends only (app-auth + account-auth), no resolve round-trip.
        assert fake_client.send.await_count == 2


# ---------------------------------------------------------------------------
# 2. _a_ensure_symbols routes on payloadType
# ---------------------------------------------------------------------------


class TestSymbolsPayloadRouting:
    @pytest.mark.asyncio
    async def test_symbols_list_success(self, client):
        """Happy path: a real ProtoOASymbolsListRes payload is parsed and
        the symbolId is exposed by the cache."""
        client._ctid_account_id = 12345
        sym_bytes = _make_symbols_list_res_bytes("XAU/USD", 42)
        # payloadType=2115 is ProtoOASymbolsListRes.
        res = _make_proto_msg(2115, sym_bytes)
        fake_client = MagicMock()
        fake_client.send = AsyncMock(return_value=res)

        syms = await client._a_ensure_symbols(fake_client)

        assert syms == {"XAU/USD": 42}
        assert client._symbol_cache == {"XAU/USD": 42}

    @pytest.mark.asyncio
    async def test_error_frame_raises_runtime_error_with_broker_detail(self, client):
        """If the broker returns ProtoOAErrorRes where we expected a
        symbols list, the error code + description must surface as a
        readable RuntimeError — never a swallowed DecodeError."""
        client._ctid_account_id = 12345
        err_bytes = _make_error_res_bytes("UNAUTHORIZED", "access token expired")
        # payloadType=2142 is ProtoOAErrorRes.
        res = _make_proto_msg(2142, err_bytes)
        fake_client = MagicMock()
        fake_client.send = AsyncMock(return_value=res)

        with pytest.raises(RuntimeError) as excinfo:
            await client._a_ensure_symbols(fake_client)

        msg = str(excinfo.value)
        assert "ProtoOASymbolsList failed" in msg
        assert "UNAUTHORIZED" in msg
        assert "access token expired" in msg
        # Crucially: no 'Wire format was corrupt' leak.
        assert "Wire format" not in msg

    @pytest.mark.asyncio
    async def test_wrong_typed_frame_raises_runtime_error(self, client):
        """If the broker returns the wrong concrete message type for
        the symbols slot (the original repro scenario — an account-auth
        response occupying the symbols parse slot), the fix must raise
        a clear 'wrong type' error, not a DecodeError.

        The cTrader broker puts ProtoOAAccountAuthRes (payloadType=2103)
        on the stream *before* the symbols-list response. With the old
        code we hard-parsed that frame as ProtoOASymbolsListRes and the
        protobuf parser raised DecodeError. The fix uses
        Protobuf.extract(res) which dispatches on payloadType, returns
        the correct concrete type (ProtoOAAccountAuthRes here), and the
        caller then raises a readable 'wrong type' RuntimeError.
        """
        client._ctid_account_id = 12345
        # A real ProtoOAAccountAuthRes occupying the symbols-list
        # response slot.
        wrong_res = ProtoOAAccountAuthRes()
        wrong_res.ctidTraderAccountId = 12345
        payload_bytes = wrong_res.SerializeToString()
        res = _make_proto_msg(2103, payload_bytes)  # payloadType=2103 = account-auth
        fake_client = MagicMock()
        fake_client.send = AsyncMock(return_value=res)

        with pytest.raises(RuntimeError) as excinfo:
            await client._a_ensure_symbols(fake_client)

        msg = str(excinfo.value)
        assert "Expected ProtoOASymbolsListRes" in msg
        # Crucially: no protobuf 'Wire format was corrupt' leak.
        assert "Wire format" not in msg


# ---------------------------------------------------------------------------
# 3. Env alias ladder for tokens — REMOVED (Craig binding 2026-10-04)
# ---------------------------------------------------------------------------
# The historical client no longer reads env vars directly. It consumes
# the live-maintained CredentialStore (see
# ``tests/unit/data/test_ctrader_client_auth_followup.py`` for the new
# coverage). The prior ``read_env_with_aliases`` helper and its tests
# have been superseded; the binding explicitly forbids wheel-reinvention
# of credential/env handling on the historical path.
#
# (Class placeholder kept to document the removal — see git history.)


# ---------------------------------------------------------------------------
# 4. _extract_or_raise helper direct unit tests
# ---------------------------------------------------------------------------


class TestExtractOrRaise:
    def test_extracts_calls_protobuf_extract(self):
        """_extract_or_raise must delegate to Protobuf.extract (not
        hard-parse) so payloadType dispatch happens."""
        sym_bytes = _make_symbols_list_res_bytes("EUR/USD", 7)
        res = _make_proto_msg(0, sym_bytes)

        with patch("data.ctrader_client.Protobuf.extract") as extract:
            typed = ProtoOASymbolsListRes()
            extract.return_value = typed
            out = _extract_or_raise(res, "ProtoOASymbolsList")
            extract.assert_called_once_with(res)
            assert out is typed

    def test_extracts_raises_runtime_error_on_error_res(self):
        """If Protobuf.extract returns a ProtoOAErrorRes, _extract_or_raise
        must raise RuntimeError with errorCode + description."""
        err = ProtoOAErrorRes()
        err.errorCode = "BAD_TOKEN"
        err.description = "token revoked"
        res = MagicMock()

        with patch("data.ctrader_client.Protobuf.extract", return_value=err):
            with pytest.raises(RuntimeError) as excinfo:
                _extract_or_raise(res, "ProtoOASymbolsList")

        msg = str(excinfo.value)
        assert "ProtoOASymbolsList failed" in msg
        assert "BAD_TOKEN" in msg
        assert "token revoked" in msg
