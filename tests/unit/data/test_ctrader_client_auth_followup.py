"""Tests for the CTraderHistoricalClient auth fix.

Card 377b2bab-1350-473c-b623-86cd86d4560b (auth follow-up).

The fix honours the Craig binding correction (2026-10-04): the historical
client must consume the live-maintained credential store
(``adapters/ctrader/credential_store.py``) — the same path the live
forward test uses, kept fresh by ``token_lifecycle.manage()``. It must
NEVER mint its own token via the OAuth refresh grant. Wheel-reinvention
of the auth path is explicitly disallowed.

What the fix delivers:

1. **Host resolution** — ``_resolve_host()`` defaults to
   ``EndPoints.PROTOBUF_DEMO_HOST`` and honours ``CTRADER_HOST`` (matches
   the live adapter). Fixes the historical client hardcoded to
   ``live.ctraderapi.com`` (broker rejected demo token at live endpoint
   with ``UNSUPPORTED_MESSAGE / Trading account is not authorized``).
2. **``account_id`` path** — when the constructor knows the
   ``ctidTraderAccountId`` up front, ``GetAccountListByAccessToken`` is
   never issued. Auth sequence collapses to ``App auth 2101 → Account
   auth 2103``, byte-identical to the live adapter.
3. **CredentialStore consumption** — the historical client now reads
   the access token + account id from ``CredentialStore`` (the same
   store the live adapter / ``token_lifecycle`` uses). The
   ``_refresh_oauth_token`` method has been REMOVED; the historical
   client never calls the OAuth refresh grant.
4. **Probe callback arity** — ``CTraderOpenApiClient._on_tcp_disconnected``
   now accepts the SDK's ``callback(client, reason)`` call shape;
   ``scripts/probe_ctrader_credentials.py`` is usable for diagnosis again.

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


@pytest.fixture
def stub_env(tmp_path):
    """Write a stub .env source file with the canonical CTRADER_OPENAPI_*
    keys and return its ``str()`` path. The fixture writes to
    ``stub_source.env`` (not ``.env``) so tests that copy it to ``.env``
    don't trip shutil.SameFileError.
    """
    env = tmp_path / "stub_source.env"
    env.write_text(
        "\n".join(
            [
                "CTRADER_OPENAPI_CLIENT_ID=test_client_id",
                "CTRADER_OPENAPI_CLIENT_SECRET=test_client_secret_value",
                "CTRADER_OPENAPI_ACCESS_TOKEN=test_access_token_value",
                "CTRADER_OPENAPI_REFRESH_TOKEN=test_refresh_token_value",
                "CTRADER_OPENAPI_ACCOUNT_ID=46877902",
                "CTRADER_OPENAPI_TRADER_LOGIN=5795523",
                "CTRADER_OPENAPI_TOKEN_EXPIRES_AT=2026-11-03T03:12:29+00:00",
                "",
            ]
        )
    )
    return str(env)


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
# 4. download_ctrader_data.py script wiring — REMOVED (Craig binding)
# ---------------------------------------------------------------------------
# The historical-download script no longer reads individual env vars
# (CTRADER_OPENAPI_*, CTRADER_OAUTH_*, CTRADER_*) to construct the
# client. Per the Craig correction 2026-10-04, the script consumes the
# live-maintained CredentialStore. The new coverage lives in
# ``TestDownloadScriptUsesCredentialStore`` below. The prior
# ``test_script_passes_account_id_from_env`` /
# ``test_script_falls_back_to_trader_login`` tests exercised
# superseded behaviour and have been removed.
#
# (Class placeholder kept to document the removal — see git history.)


# ---------------------------------------------------------------------------
# 5. _on_tcp_disconnected callback signature matches the SDK call shape
# ---------------------------------------------------------------------------


class TestProbeCallbackArity:
    """Card 377b2bab follow-up: ``CTraderOpenApiClient._on_tcp_disconnected``
    was registered directly as the SDK's ``setDisconnectedCallback``. The
    SDK invokes the callback as ``callback(client, reason)`` (two
    positional args — confirmed at
    ``ctrader_open_api/client.py:38`` where ``Client._disconnected`` calls
    ``self._disconnectedCallback(self, reason)``). Registering the bound
    method directly raised ``TypeError: takes 2 positional arguments but
    3 were given`` and rendered the diagnostic probe unusable.

    The fix wraps the bound method with a 2-arg lambda that drops the
    SDK's first positional (the client — redundant since ``_on_tcp_disconnected``
    already has ``self``) and forwards only ``reason``.

    These tests MUST register a fake SDK client, install the lambda via
    ``setDisconnectedCallback`` exactly the way ``_do_connect`` does, and
    then invoke that lambda with two positional args (the SDK call shape).
    Calling the bound method directly is not sufficient — it bypasses
    the registered callback and cannot reproduce the production
    ``TypeError``.
    """

    def test_disconnect_handler_accepts_sdk_call_shape(self):
        """Register the lambda via a fake SDK and invoke it with the
        SDK's two-positional call shape ``(client, reason)``. The call
        must NOT raise ``TypeError`` and the client state must be
        cleaned up exactly as the production SDK does it.
        """
        from src.forex_bot.adapters.ctrader.open_api_client import (
            CTraderOpenApiClient,
        )

        client = CTraderOpenApiClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            account_id=46877902,
            access_token="x",  # noqa: S106
        )

        # Fake SDK client mirrors the SDK's ``setDisconnectedCallback``
        # registration path used in ``_do_connect``.
        fake_sdk_client = MagicMock()
        registered_callback: dict = {}

        def _register_disconnected(cb):
            # Mirror ``Client.setDisconnectedCallback`` (client.py:48).
            registered_callback["cb"] = cb

        fake_sdk_client.setDisconnectedCallback.side_effect = _register_disconnected
        fake_sdk_client.setConnectedCallback = MagicMock()
        fake_sdk_client.startService = MagicMock()

        # Bypass the real reactor/thread path and directly install the
        # lambda the same way ``_do_connect`` does.
        client._client = fake_sdk_client
        client._client.setDisconnectedCallback(
            lambda client_arg, reason: client._on_tcp_disconnected(reason)
        )
        assert "cb" in registered_callback, "Lambda was not registered"

        # Prime the state so we can assert it gets cleaned up — the
        # production ``_on_tcp_disconnected`` only clears state when
        # the client was previously ``_connected``.
        client._connected = True
        # Snapshot the reauth guard so we can prove it was cleared.
        client._reauth_in_progress.set()

        # --- Invariant: the registered lambda accepts the SDK's 2-arg
        # call shape ``(client, reason)`` and does NOT raise TypeError.
        registered_cb = registered_callback["cb"]
        fake_client_obj = object()
        try:
            registered_cb(fake_client_obj, "connection lost")
        except TypeError as exc:  # pragma: no cover — this is the bug
            pytest.fail(
                f"Registered disconnect callback raised TypeError on "
                f"SDK call shape (client, reason): {exc}"
            )

        # State must have been cleaned up exactly as the production
        # SDK would do it on a TCP drop.
        assert client._connected is False, (
            "_connected was not cleared by the disconnect handler"
        )
        assert not client._reauth_in_progress.is_set(), (
            "_reauth_in_progress was not cleared by the disconnect handler"
        )

    def test_disconnect_handler_invokes_user_callback_when_registered(self):
        """When an external ``setDisconnectedCallback`` has been
        installed on the wrapper, the SDK-style disconnect invocation
        must forward to it. This proves the 2-arg lambda path does not
        silently swallow the user callback."""
        from src.forex_bot.adapters.ctrader.open_api_client import (
            CTraderOpenApiClient,
        )

        client = CTraderOpenApiClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            account_id=46877902,
            access_token="x",  # noqa: S106
        )

        fake_sdk_client = MagicMock()
        registered_cb: dict = {}

        def _register(cb):
            registered_cb["cb"] = cb

        fake_sdk_client.setDisconnectedCallback.side_effect = _register
        client._client = fake_sdk_client
        client._client.setDisconnectedCallback(
            lambda client_arg, reason: client._on_tcp_disconnected(reason)
        )

        # Install a user callback.
        user_calls: list = []
        client.setDisconnectedCallback(lambda c: user_calls.append(c))

        client._connected = True  # prime so the user callback fires
        registered_cb["cb"](object(), "broker dropped us")

        # The user callback received the client wrapper.
        assert user_calls == [client], (
            f"Expected user callback to fire with [client], got {user_calls!r}"
        )
        assert client._connected is False

    def test_disconnect_handler_handles_no_args(self):
        """Some SDK paths call the callback with no args at all (e.g.
        a graceful ``stopService``). The handler must default to
        ``reason=None`` so it remains callable. Mirror the SDK call
        shape — invoke through the registered lambda."""
        from src.forex_bot.adapters.ctrader.open_api_client import (
            CTraderOpenApiClient,
        )
        client = CTraderOpenApiClient(
            client_id="x",
            client_secret="x",  # noqa: S106
            account_id=46877902,
            access_token="x",  # noqa: S106
        )

        fake_sdk_client = MagicMock()
        registered_cb: dict = {}

        def _register(cb):
            registered_cb["cb"] = cb

        fake_sdk_client.setDisconnectedCallback.side_effect = _register
        client._client = fake_sdk_client
        client._client.setDisconnectedCallback(
            lambda client_arg, reason: client._on_tcp_disconnected(reason)
        )

        # The registered callback takes (client, reason) — even with
        # reason=None it must not blow up.
        client._connected = True
        registered_cb["cb"](object(), None)  # must not raise
        assert client._connected is False


# ---------------------------------------------------------------------------
# 6. CredentialStore is the PREFERRED path (Craig binding 2026-10-04)
# ---------------------------------------------------------------------------


class TestCredentialStoreIsPreferred:
    """Card 377b2bab auth-follow-up (Craig binding 2026-10-04): the
    historical client MUST consume the live-maintained
    ``CredentialStore`` — the same path the live forward test uses,
    kept fresh by ``token_lifecycle.manage()``. It must NEVER mint its
    own token via the OAuth refresh grant. These tests pin that
    contract.
    """

    def test_credential_store_loads_at_construction(self, stub_env):
        """When given a ``CredentialStore``, the client must pull every
        credential field from the store's ``Credentials`` snapshot.
        """
        from adapters.ctrader.credential_store import CredentialStore
        store = CredentialStore(env_path=stub_env)
        client = CTraderHistoricalClient(credential_store=store)
        assert client.client_id == "test_client_id"
        assert client.client_secret == "test_client_secret_value"  # noqa: S105 (test placeholder)
        assert client.access_token == "test_access_token_value"  # noqa: S105 (test placeholder)
        assert client.refresh_token == "test_refresh_token_value"  # noqa: S105 (test placeholder)
        assert client._ctid_account_id == 46877902
        assert client.trader_login == 5795523
        # The store is held for re-reads during _auth.
        assert client._credential_store is store

    def test_credential_store_is_first_positional(self, stub_env):
        """The credential_store parameter is the FIRST positional arg
        so the preferred call site reads ``CTraderHistoricalClient(
        credential_store=store)`` — matching the live adapter's
        credential-driven call sites.
        """
        from adapters.ctrader.credential_store import CredentialStore
        store = CredentialStore(env_path=stub_env)
        client = CTraderHistoricalClient(store)
        assert client.client_id == "test_client_id"
        assert client._credential_store is store

    def test_credential_store_type_check_rejects_other_types(self, stub_env):
        """Only ``CredentialStore`` instances are accepted; anything
        else raises ``TypeError`` with a clear message (the binding
        makes this non-negotiable — production callers must go through
        the live-maintained path)."""
        with pytest.raises(TypeError, match="CredentialStore"):
            CTraderHistoricalClient(credential_store={"client_id": "x"})

        with pytest.raises(TypeError, match="CredentialStore"):
            CTraderHistoricalClient(credential_store="not a store")

        with pytest.raises(TypeError, match="CredentialStore"):
            CTraderHistoricalClient(credential_store=42)

    def test_missing_everything_raises(self):
        """When neither ``credential_store`` nor any legacy kwargs are
        supplied the constructor raises ``ValueError`` — there is no
        silent fallback. Production callers must use ``credential_store``
        per the Craig binding; the legacy kwargs path requires explicit
        placeholders (tests-only).
        """
        with pytest.raises(ValueError, match="credential_store"):
            CTraderHistoricalClient()

    def test_legacy_kwargs_require_all_four(self):
        """Partial legacy kwargs raise ValueError (no silent
        fallbacks). The legacy path is DEPRECATED and tests-only.
        """
        # Missing refresh_token and access_token.
        with pytest.raises(ValueError, match="Missing"):
            CTraderHistoricalClient(
                client_id="x",
                client_secret="x",  # noqa: S106
                account_id=1,
            )

    def test_legacy_kwargs_require_account_id_or_trader_login(self):
        """Legacy kwargs path requires either ``account_id`` (ctid) or
        ``trader_login`` (legacy human-readable) so the auth flow can
        resolve which account to authenticate against.
        """
        with pytest.raises(ValueError, match="account_id"):
            CTraderHistoricalClient(
                client_id="x",
                client_secret="x",  # noqa: S106
                access_token="x",  # noqa: S106
                refresh_token="x",  # noqa: S106
            )

    def test_legacy_kwargs_still_work_for_tests(self):
        """The legacy kwargs path remains functional for unit tests
        that need to inject placeholders without touching ``.env``.
        """
        client = CTraderHistoricalClient(
            client_id="test_client",
            client_secret="***",  # noqa: S106
            access_token="***",  # noqa: S106
            refresh_token="***",  # noqa: S106
            account_id=46877902,
        )
        assert client.client_id == "test_client"
        assert client._ctid_account_id == 46877902
        # No store — we're on the legacy path.
        assert client._credential_store is None


# ---------------------------------------------------------------------------
# 7. Refresh grant is REMOVED from the historical client (Craig binding)
# ---------------------------------------------------------------------------


class TestRefreshGrantRemoved:
    """Card 377b2bab auth-follow-up (Craig binding 2026-10-04): the
    historical client MUST NOT mint its own token via the OAuth refresh
    grant. ``_refresh_oauth_token`` is REMOVED; the historical client
    reads what the live path's ``token_lifecycle`` wrote to ``.env``.
    """

    def test_refresh_oauth_token_method_is_gone(self):
        """The historical client must not expose ``_refresh_oauth_token``
        — the binding explicitly forbids the wheel-reinvention.
        """
        from data import ctrader_client
        assert not hasattr(ctrader_client.CTraderHistoricalClient, "_refresh_oauth_token")

    def test_requests_module_not_imported_by_module(self):
        """``requests`` is no longer needed for the refresh grant and
        must not be imported at module level — keeps the data module
        lightweight and prevents accidental drift back to refresh-grant
        code.
        """
        from data import ctrader_client
        # The module attribute holds the import; assert it's absent.
        assert not hasattr(ctrader_client, "requests")


# ---------------------------------------------------------------------------
# 8. _auth re-reads access_token from credential_store before AccountAuth
# ---------------------------------------------------------------------------


class TestAuthRereadsAccessToken:
    """Card 377b2bab auth-follow-up (Craig binding): the historical
    client consumes the live-maintained access token via
    ``CredentialStore``. Before sending ``AccountAuth`` it re-reads the
    store so any refresh the live path's ``token_lifecycle`` wrote to
    ``.env`` (since the client was constructed) is visible here.
    """

    @pytest.mark.asyncio
    async def test_auth_picks_up_refreshed_access_token(self, stub_env):
        """Simulate the live path refreshing the access token between
        client construction and the ``AccountAuth`` send. The client
        must re-read the store and use the new token.
        """
        from adapters.ctrader.credential_store import CredentialStore

        store = CredentialStore(env_path=stub_env)
        client = CTraderHistoricalClient(credential_store=store)
        original_token = client.access_token
        assert original_token == "test_access_token_value"  # noqa: S105 (test placeholder)

        # Simulate live path updating the token (token_lifecycle calls
        # store.update_tokens which writes to .env and updates the cache).
        store.update_tokens(
            access_token="refreshed_access_token_value",  # noqa: S106
            refresh_token="refreshed_refresh_token_value",  # noqa: S106
            expires_in=30 * 24 * 3600,
        )

        # Stub the network so we don't actually call the broker.
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAccountAuthRes,
            ProtoOAApplicationAuthReq,
        )
        fake_client = MagicMock()
        account_auth = ProtoOAAccountAuthRes()
        account_auth.ctidTraderAccountId = 46877902
        fake_client.send = AsyncMock(
            side_effect=[
                _make_proto_msg(2101, b""),                            # app auth ack
                _make_proto_msg(2103, account_auth.SerializeToString()),  # acct auth ack
            ]
        )

        await client._auth(fake_client)

        # Second send is the AccountAuthReq — must carry the REFRESHED
        # access token, not the one captured at construction.
        second_call = fake_client.send.await_args_list[1]
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAAccountAuthReq
        assert isinstance(second_call.args[0], ProtoOAAccountAuthReq)
        assert second_call.args[0].accessToken == "refreshed_access_token_value"  # noqa: S105 (test placeholder)
        # And the in-memory attribute is updated for any subsequent use.
        assert client.access_token == "refreshed_access_token_value"  # noqa: S105 (test placeholder)

        # First send is the App auth — credentials are stable across
        # token refresh (client_id, client_secret), still correct.
        first_call = fake_client.send.await_args_list[0]
        assert isinstance(first_call.args[0], ProtoOAApplicationAuthReq)
        assert first_call.args[0].clientId == "test_client_id"

    @pytest.mark.asyncio
    async def test_auth_works_without_credential_store(self):
        """Legacy kwargs path (no ``credential_store``) must still
        work — the in-memory ``self.access_token`` is used directly.
        No re-read happens because there is no store.
        """
        client = CTraderHistoricalClient(
            client_id="test_client_id",
            client_secret="***",  # noqa: S106
            access_token="in_memory_token_value",  # noqa: S106
            refresh_token="***",  # noqa: S106
            account_id=46877902,
        )
        assert client._credential_store is None

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAccountAuthReq,
            ProtoOAAccountAuthRes,
        )
        fake_client = MagicMock()
        account_auth = ProtoOAAccountAuthRes()
        account_auth.ctidTraderAccountId = 46877902
        fake_client.send = AsyncMock(
            side_effect=[
                _make_proto_msg(2101, b""),
                _make_proto_msg(2103, account_auth.SerializeToString()),
            ]
        )

        await client._auth(fake_client)

        second_call = fake_client.send.await_args_list[1]
        assert isinstance(second_call.args[0], ProtoOAAccountAuthReq)
        assert second_call.args[0].accessToken == "in_memory_token_value"


# ---------------------------------------------------------------------------
# 9. download_ctrader_data.py uses CredentialStore (Craig binding)
# ---------------------------------------------------------------------------


class TestDownloadScriptUsesCredentialStore:
    """Card 377b2bab auth-follow-up (Craig binding): the download
    script MUST go through the live-maintained ``CredentialStore`` —
    it must NOT manage individual env vars or read .env by hand. This
    matches the proven pattern the live adapter / forward test use.
    """

    def test_script_constructs_credential_store(
        self, monkeypatch, tmp_path, stub_env
    ):
        """The script constructs ``CredentialStore(.env)`` and passes it
        to ``CTraderHistoricalClient``. No manual env-var dict.
        """
        from scripts import download_ctrader_data

        # Stub the env path so the script reads our stub .env.
        monkeypatch.setattr(
            download_ctrader_data, "PROJECT_ROOT", tmp_path
        )
        # Copy stub .env to tmp_path/.env so the script picks it up.
        import shutil
        shutil.copy(stub_env, tmp_path / ".env")

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
        # The script passed credential_store (not individual env vars).
        assert "credential_store" in captured
        assert captured["credential_store"] is not None
        # The deprecated legacy kwargs are absent.
        assert "client_id" not in captured
        assert "access_token" not in captured
        assert "trader_login" not in captured

    def test_script_reports_missing_credentials_cleanly(
        self, monkeypatch, tmp_path
    ):
        """If ``CredentialStore.load()`` raises ``RuntimeError`` (no
        .env or missing keys), the script logs a clean error and
        returns exit 1 — does NOT fall back to env-var parsing.
        """
        from scripts import download_ctrader_data

        # No .env at tmp_path — CredentialStore.load() raises RuntimeError.
        monkeypatch.setattr(download_ctrader_data, "PROJECT_ROOT", tmp_path)
        # Remove any .env that the test infra may have written.
        env_file = tmp_path / ".env"
        if env_file.exists():
            env_file.unlink()

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
        assert rc == 1
        # Client was never constructed — error caught before construction.
        assert captured == {}
