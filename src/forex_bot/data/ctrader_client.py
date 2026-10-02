"""cTrader Open API v2 historical data client.

Uses the official ctrader-open-api Python package (protobuf over TCP)
to download OHLCV bars from cTrader's live servers.

Usage:
    client = CTraderHistoricalClient(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        access_token=OAUTH_ACCESS_TOKEN,
        refresh_token=OAUTH_REFRESH_TOKEN,
        trader_login=TRADER_LOGIN,  # e.g. 17087404
    )
    df = client.get_historical_bars("EURUSD", "M15", "2026-01-01", "2026-04-10")
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from inspect import iscoroutinefunction
from typing import Optional

import pandas as pd
import requests
from ctrader_open_api import Client, TcpProtocol
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
_PROTOBUF_HOST = "live.ctraderapi.com"
_PROTOBUF_PORT = 5035
_MAX_BARS_PER_REQUEST = 1000


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

    Parameters
    ----------
    client_id : str
        cTrader Open API client ID.
    client_secret : str
        cTrader Open API client secret.
    access_token : str
        OAuth2 access token (from token refresh flow).
    refresh_token : str
        OAuth2 refresh token (used to auto-refresh when token expires).
    trader_login : int
        The trader's login/account number (e.g. 17087404).
        This is the human-readable account ID, NOT the internal ctidTraderAccountId.
        The client resolves this to the internal ID automatically.
    host : str, optional
        Protobuf host. Defaults to live.ctraderapi.com.
    port : int, optional
        Protobuf port. Defaults to 5035.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        access_token: str,
        refresh_token: str,
        trader_login: int,
        host: Optional[str] = None,
        port: int = 5035,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.trader_login = trader_login
        self.host = host or _PROTOBUF_HOST
        self.port = port
        self._ctid_account_id: Optional[int] = None
        self._symbol_cache: dict[str, int] = {}

    def _refresh_oauth_token(self) -> str:
        """Refresh the OAuth2 access token. Returns the new access token."""
        resp = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("errorCode"):
            raise RuntimeError(f"OAuth2 token refresh failed: {data.get('description', data.get('errorCode'))}")
        self.access_token = data.get("accessToken") or data.get("access_token")
        self.refresh_token = data.get("refreshToken") or data.get("refresh_token")
        logger.info("OAuth2 token refreshed successfully")
        return self.access_token

    def _new_client(self) -> Client:
        return Client(self.host, self.port, TcpProtocol)

    async def _resolve_ctid_account_id(self, client: Client) -> int:
        """Resolve trader_login to internal ctidTraderAccountId via protobuf."""
        if self._ctid_account_id is not None:
            return self._ctid_account_id

        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = self.access_token
        res = await client.send(req, responseTimeoutInSeconds=10)

        parsed = ProtoOAGetAccountListByAccessTokenRes()
        parsed.ParseFromString(res.payload)

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
        """Authenticate: app auth → account auth using OAuth2 access token."""
        auth = ProtoOAApplicationAuthReq()
        auth.clientId = self.client_id
        auth.clientSecret = self.client_secret
        await client.send(auth, responseTimeoutInSeconds=10)

        acct = ProtoOAAccountAuthReq()
        acct.ctidTraderAccountId = self._ctid_account_id
        acct.accessToken = self.access_token
        await client.send(acct, responseTimeoutInSeconds=10)

    async def _a_ensure_symbols(self, client: Client) -> dict[str, int]:
        """Load and cache symbol list {name: symbolId} (async, reuses client)."""
        if self._symbol_cache:
            return self._symbol_cache

        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self._ctid_account_id
        res = await client.send(req, responseTimeoutInSeconds=30)

        parsed = ProtoOASymbolsListRes()
        parsed.ParseFromString(res.payload)

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
