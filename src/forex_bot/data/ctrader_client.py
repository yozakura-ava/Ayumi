"""cTrader Open API v2 historical data client.

Uses the official ctrader-open-api Python package (protobuf over TCP)
to download OHLCV bars from cTrader's live servers.

Card 377b2bab auth-follow-up (2026-10-04, Craig-binding correction):
The historical client MUST consume the live-maintained credential store
(``adapters/ctrader/credential_store.py``) — the same path the live
forward test uses, kept fresh by ``token_lifecycle.manage()``. It must
NEVER mint its own token via the OAuth refresh grant. Wheel-reinvention
of the auth path is explicitly disallowed; reuse the proven in-repo
implementation.

Usage:
    # Preferred path — consume the live-maintained CredentialStore. The
    # store reads ``.env`` (the only credential source) and exposes a
    # ``Credentials`` dataclass. The token_lifecycle keeps it fresh.
    from adapters.ctrader.credential_store import CredentialStore
    client = CTraderHistoricalClient(credential_store=CredentialStore())
    df = client.get_historical_bars("EURUSD", "M15", "2026-01-01", "2026-04-10")

Legacy kwargs path — DEPRECATED. Kept only for unit tests that need to
inject placeholders without writing to .env. Production callers MUST use
the ``credential_store`` parameter.
    client = CTraderHistoricalClient(
        client_id="...",
        client_secret="...",
        access_token="...",
        refresh_token="...",
        account_id=CTID_ACCOUNT_ID,  # preferred, matches live adapter
    )
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from inspect import iscoroutinefunction
from typing import Optional

import pandas as pd
from ctrader_open_api import Client, Protobuf, TcpProtocol
from ctrader_open_api.endpoints import EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOAGetTrendbarsReq,
    ProtoOAGetTrendbarsRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod
from twisted.internet import reactor
from twisted.internet.defer import ensureDeferred

logger = logging.getLogger(__name__)

TIMEFRAME_MAP = {
    "M1": ProtoOATrendbarPeriod.M1,
    "M5": ProtoOATrendbarPeriod.M5,
    "M15": ProtoOATrendbarPeriod.M15,
    "M30": ProtoOATrendbarPeriod.M30,
    "H1": ProtoOATrendbarPeriod.H1,
    "H4": ProtoOATrendbarPeriod.H4,
    "D1": ProtoOATrendbarPeriod.D1,
    "W1": ProtoOATrendbarPeriod.W1,
    "MN": ProtoOATrendbarPeriod.MN1,
}

SYMBOL_NAME_MAP = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "GBPJPY": "GBP/JPY",
    "XAUUSD": "XAU/USD",
    "AUDUSD": "AUD/USD",
    "NZDUSD": "NZD/USD",
    "USDCHF": "USD/CHF",
    "USDCAD": "USD/CAD",
    "EURGBP": "EUR/GBP",
    "EURJPY": "EUR/JPY",
}

OAUTH_TOKEN_URL = "https://openapi.ctrader.com/apps/token"  # noqa: S105
# DEPRECATED (card 377b2bab auth-follow-up, Craig binding 2026-10-04):
# the historical client is forbidden from minting its own OAuth tokens.
# It must consume the access token that the live path maintains via
# ``token_lifecycle`` + ``credential_store``. The constant is kept here
# so external callers that already import it don't break, but no code
# path inside this module issues a refresh grant anymore.
# Card 377b2bab auth-follow-up (2026-10-04): the historical client used to
# hardcode ``_PROTOBUF_HOST = "live.ctraderapi.com"`` which is wrong on
# every demo / paper / practice account — the broker rejects the demo
# access token with ``UNSUPPORTED_MESSAGE / Trading account is not
# authorized``. The live working adapter (``adapters/ctrader/open_api_client.py``)
# defaults to ``EndPoints.PROTOBUF_DEMO_HOST`` and honours the
# ``CTRADER_HOST`` env var. We mirror that contract here so the two paths
# stay byte-equivalent on host selection.
_PROTOBUF_PORT = 5035
_MAX_BARS_PER_REQUEST = 1000


def _resolve_host(explicit: Optional[str] = None) -> str:
    """Return the protobuf host to connect to.

    Precedence: explicit argument > ``CTRADER_HOST`` env var >
    ``EndPoints.PROTOBUF_DEMO_HOST`` (matches the live adapter's pattern;
    demo is the safe default for forward tests / backfills).
    """
    return (
        explicit
        or os.environ.get("CTRADER_HOST")
        or EndPoints.PROTOBUF_DEMO_HOST
    )

# Canonical cTrader OpenAPI payloadType codes (the wire-format discriminator
# each ProtoMessage carries). Used for explicit dispatch when the broker
# returns a non-success frame (ProtoOAErrorRes / ProtoOAAccountAuthRes) on
# the shared TCP connection. Source: OpenApiCommonMessages / OpenApiMessages.
_PAYLOAD_TYPE_ERROR = 2102


def _extract_or_raise(res, expected_msg_name: str):
    """Extract the typed payload from a cTrader ProtoMessage and raise on
    error frames.

    The cTrader OpenAPI library ships ``Protobuf.extract(res)`` which routes
    a ``ProtoMessage`` to its concrete message class via ``payloadType``.
    That dispatch is what was missing on the async historical path — every
    protobuf response carries a ``payloadType`` discriminator and the
    caller MUST inspect it before calling ``ParseFromString`` against a
    hard-coded message type. Without that, an account-auth or symbols-list
    response frame gets misread as a trendbars/symbols payload and the
    protobuf parser raises ``DecodeError: Wire format was corrupt``.

    Returns the typed message on success. Raises ``RuntimeError`` with the
    broker-provided error code/description if the frame is a
    ``ProtoOAErrorRes`` (payloadType=2102) — never a try/except swallow.
    """
    typed = Protobuf.extract(res)
    if isinstance(typed, ProtoOAErrorRes):
        raise RuntimeError(
            f"cTrader {expected_msg_name} failed: errorCode={typed.errorCode} "
            f"description={typed.description!r}"
        )
    return typed


def _run_reactor(coro):
    """Run an async coroutine inside Twisted reactor, return result, then stop."""
    result_holder: list = [None]
    error_holder: list = [None]

    async def capture():
        try:
            result_holder[0] = await coro() if iscoroutinefunction(coro) else await coro
        except Exception as e:
            error_holder[0] = e
        finally:
            reactor.stop()

    reactor.callWhenRunning(lambda: reactor.callLater(0.01, lambda: ensureDeferred(capture())))
    reactor.run(installSignalHandlers=0)

    if error_holder[0]:
        raise error_holder[0]
    return result_holder[0]


class CTraderHistoricalClient:
    """Client for downloading historical OHLCV bars from cTrader Open API v2.

    Card 377b2bab auth-follow-up (2026-10-04, Craig binding): the
    historical client MUST consume the live-maintained
    :class:`~adapters.ctrader.credential_store.CredentialStore`. It must
    NEVER mint its own access token via the OAuth refresh grant. The
    ``token_lifecycle`` loop in ``adapters/ctrader/token_lifecycle.py``
    is the single owner of the refresh grant and writes fresh values
    back to ``.env``; this client just reads.

    Parameters
    ----------
    credential_store : CredentialStore, optional
        Preferred path. A :class:`CredentialStore` instance pointing at
        the same ``.env`` the live adapter uses. The historical client
        reads the access token (and account id) from this store at
        construction and re-reads the access token before each
        ``AccountAuth`` send so any refresh the live path performed is
        picked up.
    host : str, optional
        Protobuf host. Defaults to ``CTRADER_HOST`` env var or
        ``EndPoints.PROTOBUF_DEMO_HOST`` (matches the live adapter).
    port : int, optional
        Protobuf port. Defaults to 5035.
    client_id, client_secret, access_token, refresh_token : str, optional
        DEPRECATED. Only used by unit tests that need to inject
        placeholder credentials without touching ``.env``. Production
        callers MUST pass ``credential_store``.
    account_id : int, optional
        DEPRECATED. When using ``credential_store``, the
        ``ctidTraderAccountId`` is loaded from the store. When using
        legacy kwargs, supply ``account_id`` (ctid) to skip the failing
        ``GetAccountListByAccessToken`` round-trip.
    trader_login : int, optional
        DEPRECATED. Legacy human-readable account number; only used as
        a fallback for ``GetAccountListByAccessToken`` when
        ``account_id`` is absent. Production callers should always use
        ``account_id`` (or the credential_store, which provides it).
    """

    def __init__(
        self,
        credential_store=None,
        host: Optional[str] = None,
        port: int = 5035,
        # DEPRECATED legacy kwargs (tests-only). Production callers MUST
        # use ``credential_store``.
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        account_id: Optional[int] = None,
        trader_login: Optional[int] = None,
    ):
        # Lazy import — keeps the data module importable without pulling
        # in the full adapters stack at module load time.
        from adapters.ctrader.credential_store import CredentialStore

        self._credential_store = None
        if credential_store is not None:
            # PREFERRED path — consume the live-maintained credential
            # store. The store reads .env (the canonical credential
            # source) and exposes a Credentials snapshot. The
            # token_lifecycle loop writes refreshed tokens back to .env;
            # this client just reads.
            if not isinstance(credential_store, CredentialStore):
                raise TypeError(
                    "credential_store must be a "
                    "adapters.ctrader.credential_store.CredentialStore "
                    f"instance; got {type(credential_store).__name__}"
                )
            self._credential_store = credential_store
            creds = credential_store.load()
            self.client_id = creds.client_id
            self.client_secret = creds.client_secret
            self.access_token = creds.access_token
            self.refresh_token = creds.refresh_token
            self._ctid_account_id: Optional[int] = creds.account_id or None
            self.trader_login = creds.trader_login or None
        else:
            # DEPRECATED legacy path — only used by tests that inject
            # placeholder credentials. Production callers must use
            # ``credential_store`` per the Craig binding. No refresh
            # grant is issued from here regardless.
            missing = [
                name
                for name, val in (
                    ("client_id", client_id),
                    ("client_secret", client_secret),
                    ("access_token", access_token),
                    ("refresh_token", refresh_token),
                )
                if not val
            ]
            if missing:
                raise ValueError(
                    "CTraderHistoricalClient: legacy kwargs path requires "
                    f"client_id, client_secret, access_token, refresh_token. "
                    f"Missing: {missing}. Production callers should pass "
                    "credential_store=CredentialStore() instead."
                )
            if account_id is None and trader_login is None:
                raise ValueError(
                    "CTraderHistoricalClient: legacy kwargs path requires "
                    "either account_id (ctidTraderAccountId) or "
                    "trader_login (legacy human-readable account number)."
                )
            self.client_id = client_id
            self.client_secret = client_secret
            self.access_token = access_token
            self.refresh_token = refresh_token
            self._ctid_account_id = account_id
            self.trader_login = trader_login

        self.host = _resolve_host(host)
        self.port = port
        self._symbol_cache: dict[str, int] = {}

    def _new_client(self) -> Client:
        return Client(self.host, self.port, TcpProtocol)

    async def _resolve_ctid_account_id(self, client: Client) -> int:
        """Resolve trader_login to internal ctidTraderAccountId via protobuf.

        Card 377b2bab auth-follow-up (2026-10-04): this call is the
        round-trip the broker rejects with ``UNSUPPORTED_MESSAGE`` when
        the demo access token is presented at the live endpoint (or vice
        versa). When the constructor is given ``account_id`` (the
        canonical ctidTraderAccountId, matching the live working
        adapter) we short-circuit and never issue the failing call.
        The legacy ``trader_login``-only path remains as a fallback for
        callers without a pre-known ctid.
        """
        if self._ctid_account_id is not None:
            return self._ctid_account_id

        if self.trader_login is None:
            raise RuntimeError(
                "Cannot resolve ctidTraderAccountId: neither account_id "
                "nor trader_login was supplied."
            )

        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = self.access_token
        res = await client.send(req, responseTimeoutInSeconds=10)

        parsed = _extract_or_raise(res, "ProtoOAGetAccountListByAccessToken")
        if not isinstance(parsed, ProtoOAGetAccountListByAccessTokenRes):
            raise RuntimeError(
                f"Expected ProtoOAGetAccountListByAccessTokenRes, got "
                f"{type(parsed).__name__} (payloadType={res.payloadType})"
            )

        for acc in parsed.ctidTraderAccount:
            if acc.traderLogin == self.trader_login:
                self._ctid_account_id = acc.ctidTraderAccountId
                logger.info(
                    "Resolved trader_login=%d -> ctidTraderAccountId=%d (isLive=%s)",
                    self.trader_login,
                    self._ctid_account_id,
                    acc.isLive,
                )
                return self._ctid_account_id
        available = [a.traderLogin for a in parsed.ctidTraderAccount]
        raise ValueError(f"trader_login {self.trader_login} not found. Available: {available}")

    def _ensure_ctid(self) -> int:
        """Resolve trader_login to ctidTraderAccountId (outside reactor context)."""
        if self._ctid_account_id is not None:
            return self._ctid_account_id
        return self._resolve_ctid_account_id()

    async def _auth(self, client: Client) -> None:
        """Authenticate: app auth → account auth using OAuth2 access token.

        Card 377b2bab auth-follow-up (2026-10-04, Craig binding): the
        historical client MUST consume the access token the live path
        maintains via ``token_lifecycle`` + ``credential_store``. We
        never mint our own. Before sending ``AccountAuth`` we re-read
        from the credential_store so any refresh that happened since we
        were constructed is visible here. The ctid is stable so we only
        re-read the access token.

        When ``account_id`` (ctidTraderAccountId) is known up-front we
        skip the ``GetAccountListByAccessToken`` round-trip entirely
        and the auth sequence becomes ``App auth 2101 → Account auth
        2103`` — byte identical to the working live adapter. Only the
        legacy ``trader_login`` path falls back to the broker-side
        resolve.
        """
        # Re-read the access token from the live-maintained credential
        # store. The ctid is stable across token refreshes, so we only
        # refresh the access token here. Cross-process token refreshes
        # (when the live path in another process updates .env) are
        # picked up on the next construction of this client; for
        # within-process refreshes the cache is updated by
        # ``CredentialStore.update_tokens``.
        if self._credential_store is not None:
            creds = self._credential_store.load()
            self.access_token = creds.access_token
            # Defensive: ensure ctid is still known after a re-read.
            if self._ctid_account_id is None and creds.account_id:
                self._ctid_account_id = creds.account_id

        # Fast path: ctid known → no resolve round-trip; the call below
        # is a no-op short-circuit when self._ctid_account_id is set.
        if self._ctid_account_id is None:
            await self._resolve_ctid_account_id(client)

        auth = ProtoOAApplicationAuthReq()
        auth.clientId = self.client_id
        auth.clientSecret = self.client_secret
        app_res = await client.send(auth, responseTimeoutInSeconds=10)
        # App auth has no useful payload but the broker can return an error
        # frame; route on payloadType so a failure surfaces clearly.
        _extract_or_raise(app_res, "ProtoOAApplicationAuth")

        acct = ProtoOAAccountAuthReq()
        acct.ctidTraderAccountId = self._ctid_account_id
        acct.accessToken = self.access_token
        acc_res = await client.send(acct, responseTimeoutInSeconds=10)
        _extract_or_raise(acc_res, "ProtoOAAccountAuth")

    async def _a_ensure_symbols(self, client: Client) -> dict[str, int]:
        """Load and cache symbol list {name: symbolId} (async, reuses client)."""
        if self._symbol_cache:
            return self._symbol_cache

        # Defensive: ensure ctid is resolved before issuing a per-account
        # request. The async entry point (`fetch_all`) goes through
        # `_auth -> _a_ensure_symbols`; `_auth` now guarantees this, but
        # keep the resolve here too so it's idempotent if a caller hits
        # symbols before auth. Skipped entirely when ``account_id`` was
        # supplied at construction (matches the live adapter pattern).
        if self._ctid_account_id is None:
            await self._resolve_ctid_account_id(client)

        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self._ctid_account_id
        res = await client.send(req, responseTimeoutInSeconds=30)

        # Route on payloadType — DO NOT hard-parse the payload as
        # ProtoOASymbolsListRes. The earlier DecodeError ('Wire format was
        # corrupt') happened because the next frame on the shared TCP
        # connection was a non-symbols message that was misrouted onto
        # this parse slot.
        parsed = _extract_or_raise(res, "ProtoOASymbolsList")
        if not isinstance(parsed, ProtoOASymbolsListRes):
            raise RuntimeError(
                f"Expected ProtoOASymbolsListRes, got "
                f"{type(parsed).__name__} (payloadType={res.payloadType})"
            )

        syms = {}
        for s in parsed.symbol:
            syms[s.symbolName] = s.symbolId
        self._symbol_cache = syms
        return syms

    def _resolve_symbol(self, symbol: str) -> int:
        """Resolve symbol name to cTrader symbolId."""
        syms = self._symbol_cache
        ct_name = SYMBOL_NAME_MAP.get(symbol, symbol)
        if ct_name in syms:
            return syms[ct_name]
        if symbol in syms:
            return syms[symbol]
        for name, sid in syms.items():
            if name.replace("/", "").upper() == symbol.upper().replace("/", ""):
                return sid
        raise ValueError(f"Symbol '{symbol}' not found. Available: {list(syms.keys())[:20]}")

    async def _a_fetch_trendbars(
        self,
        client: Client,
        symbol_id: int,
        period: ProtoOATrendbarPeriod,
        from_ts: int,
        to_ts: int,
    ) -> list[dict]:
        """Fetch one page of trendbars (up to 1000 bars)."""
        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.symbolId = symbol_id
        req.period = period
        req.fromTimestamp = from_ts
        req.toTimestamp = to_ts
        req.count = _MAX_BARS_PER_REQUEST
        res = await client.send(req, responseTimeoutInSeconds=30)

        if res.payloadType == 2142:
            err = ProtoOAErrorRes()
            err.ParseFromString(res.payload)
            raise RuntimeError(f"Trendbars request failed: {err.errorCode} - {err.description}")

        parsed = ProtoOAGetTrendbarsRes()
        parsed.ParseFromString(res.payload)

        bars = []
        for tb in parsed.trendbar:
            low = tb.low / 100000.0
            bars.append(
                {
                    "utc_timestamp_ms": tb.utcTimestampInMinutes * 60000,
                    "open": low + tb.deltaOpen / 100000.0,
                    "high": low + tb.deltaHigh / 100000.0,
                    "low": low,
                    "close": low + tb.deltaClose / 100000.0,
                    "volume": tb.volume,
                }
            )
        return bars

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """Download historical OHLCV bars (handles pagination automatically).

        Returns DataFrame with columns: Date, Open, High, Low, Close, Volume
        matching the existing CSV format.
        """
        if timeframe not in TIMEFRAME_MAP:
            raise ValueError(f"Unsupported timeframe '{timeframe}'. Supported: {list(TIMEFRAME_MAP.keys())}")

        period = TIMEFRAME_MAP[timeframe]

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        from_ts = int(start_dt.timestamp() * 1000)
        to_ts = int(end_dt.timestamp() * 1000)

        async def fetch_all():
            client = self._new_client()
            client.startService()
            try:
                await self._auth(client)
                await self._a_ensure_symbols(client)
                symbol_id = self._resolve_symbol(symbol)

                all_bars = []
                current_from = from_ts
                while True:
                    bars_page = await self._a_fetch_trendbars(client, symbol_id, period, current_from, to_ts)
                    if not bars_page:
                        break
                    all_bars.extend(bars_page)
                    last_ts = bars_page[-1]["utc_timestamp_ms"]
                    current_from = last_ts + 1
                    if len(bars_page) < _MAX_BARS_PER_REQUEST:
                        break
                    if len(all_bars) >= 10000:
                        logger.warning(
                            "Reached 10000 bar limit for %s %s [%s → %s]",
                            symbol,
                            timeframe,
                            start_date,
                            end_date,
                        )
                        break
                return all_bars
            finally:
                client.stopService()

        all_bars = _run_reactor(fetch_all)

        if not all_bars:
            logger.warning("No bars for %s %s [%s → %s]", symbol, timeframe, start_date, end_date)
            return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])

        df = pd.DataFrame(all_bars)
        df["Date"] = pd.to_datetime(df["utc_timestamp_ms"], unit="ms", utc=True)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d %H:%M")
        df = df.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
        )
        df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
        df = df.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)
        return df

    def download_and_save(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
        output_path: str,
        append: bool = True,
    ):
        """Download bars and save/append to CSV matching existing format."""
        df = self.get_historical_bars(symbol, timeframe, start_date, end_date)

        if df.empty:
            logger.info("No data for %s %s, skipping", symbol, timeframe)
            return

        from pathlib import Path

        path = Path(output_path)
        if append and path.exists():
            existing = pd.read_csv(path)
            df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)

        df.to_csv(path, index=False)
        logger.info("Saved %d bars to %s", len(df), path)
