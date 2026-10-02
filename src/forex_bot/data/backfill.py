"""Historical data backfill system for cTrader symbols.

Backfills bar data from cTrader Open API (protobuf) with
fallback to existing CSV files and gap detection.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from adapters.ctrader.models import cTraderCredentials
from adapters.ctrader.open_api_client import (
    CTraderOpenApiClient,
    calculate_chunks,
)
from adapters.ctrader.symbol_discovery import SymbolInfo

logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


class HistoricalDataBackfill:
    """Backfill historical bar data from cTrader for discovered symbols.

    Primary: cTrader Open API (protobuf) for historical trendbars.
    Fallback: existing CSV files for gap-filling when live data is unavailable.
    """

    def __init__(self, credentials: cTraderCredentials, data_dir: str = "data/forex/historical"):
        self._credentials = credentials
        self._data_dir = Path(data_dir)
        self._openapi_client: CTraderOpenApiClient | None = None

    def _get_openapi_client(self) -> CTraderOpenApiClient:
        """Lazily create and connect an Open API client."""
        if self._openapi_client is None:
            client_id = os.environ.get("CTRADER_OPENAPI_CLIENT_ID", "")
            client_secret = os.environ.get("CTRADER_OPENAPI_CLIENT_SECRET", "")
            account_id = int(os.environ.get("CTRADER_ACCOUNT", "0"))
            access_token = os.environ.get("CTRADER_OPENAPI_ACCESS_TOKEN", "")

            if not client_id or not client_secret:
                raise RuntimeError(
                    "CTRADER_OPENAPI_CLIENT_ID and CTRADER_OPENAPI_CLIENT_SECRET must be set in environment"
                )

            self._openapi_client = CTraderOpenApiClient(
                client_id=client_id,
                client_secret=client_secret,
                account_id=account_id,
                access_token=access_token,
            )
            if not self._openapi_client.connect():
                raise RuntimeError("Failed to connect to cTrader Open API")

        return self._openapi_client

    def close(self):
        """Disconnect the Open API client if connected."""
        if self._openapi_client:
            self._openapi_client.disconnect()
            self._openapi_client = None

    def backfill_symbol(
        self,
        symbol: str,
        timeframe: str = "H1",
        start_date: str | None = None,
        end_date: str | None = None,
        symbol_id: int | None = None,
    ) -> pd.DataFrame:
        """Backfill historical data for a symbol/timeframe.

        Args:
            symbol: e.g. "EUR/USD"
            timeframe: "M1", "M5", "M15", "M30", "H1", "H4", "D1"
            start_date: ISO date string (default: 6 months ago)
            end_date: ISO date string (default: now)
            symbol_id: cTrader numeric symbol ID (required for Open API)

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        if timeframe not in VALID_TIMEFRAMES:
            raise ValueError(f"Invalid timeframe '{timeframe}'. Must be one of {VALID_TIMEFRAMES}")

        now = datetime.now(timezone.utc)
        end_dt = datetime.fromisoformat(end_date) if end_date else now
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        start_dt = datetime.fromisoformat(start_date) if start_date else end_dt - timedelta(days=180)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)

        df = pd.DataFrame()

        # Try Open API backfill
        if symbol_id:
            df = self._backfill_via_open_api(symbol_id, symbol, timeframe, start_dt, end_dt)

        if df.empty:
            # Fallback: load existing CSV
            df = self._load_existing_csv(symbol, timeframe, start_dt, end_dt)

        if df.empty:
            logger.warning(f"No historical data available for {symbol} {timeframe}")
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        # Filter to requested range
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df[(df["timestamp"] >= start_dt) & (df["timestamp"] <= end_dt)]
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

        return df

    def backfill_multiple(
        self,
        symbols: list[str] | dict[str, int],
        timeframe: str = "H1",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, str]:
        """Backfill multiple symbols. Returns {symbol: output_path}.

        Args:
            symbols: list of symbol names (e.g. ["EUR/USD"]) or
                     dict of {name: symbol_id} for Open API lookups
            timeframe: Bar period
            start_date: ISO date string
            end_date: ISO date string

        CPU-metered: one symbol at a time, with pauses between.
        """
        results = {}

        # Normalize to dict
        if isinstance(symbols, list):
            symbol_map = {s: None for s in symbols}
        else:
            symbol_map = symbols

        try:
            for i, (name, sid) in enumerate(symbol_map.items()):
                logger.info(f"Backfilling {name} {timeframe} ({i + 1}/{len(symbol_map)})")
                try:
                    df = self.backfill_symbol(
                        name,
                        timeframe,
                        start_date=start_date,
                        end_date=end_date,
                        symbol_id=sid,
                    )
                except Exception as e:
                    logger.error(f"Failed to backfill {name}: {e}")
                    continue

                if not df.empty:
                    path = self._save_csv(df, name, timeframe)
                    results[name] = path
                    logger.info(f"  -> {path} ({len(df)} bars)")

                if i < len(symbol_map) - 1:
                    time.sleep(1.0)  # Pause between symbols
        finally:
            self.close()

        return results

    def get_missing_symbols(
        self,
        discovered: dict[int, SymbolInfo],
        timeframe: str = "H1",
    ) -> list[str]:
        """Check which discovered symbols don't have historical data yet."""
        missing = []
        for info in discovered.values():
            filename = self._csv_filename(info.name, timeframe)
            if not (self._data_dir / filename).exists():
                missing.append(info.name)
        return sorted(missing)

    def _backfill_via_open_api(
        self,
        symbol_id: int,
        symbol_name: str,
        timeframe: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> pd.DataFrame:
        """Fetch historical data from cTrader Open API."""
        try:
            client = self._get_openapi_client()
        except RuntimeError as e:
            logger.warning(f"Open API unavailable: {e}")
            return pd.DataFrame()

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        # Calculate chunks
        chunks = calculate_chunks(timeframe, start_ms, end_ms)
        if not chunks:
            return pd.DataFrame()

        all_bars = []
        for i, (chunk_start, chunk_end) in enumerate(chunks):
            logger.debug(f"  chunk {i + 1}/{len(chunks)}: {chunk_start} → {chunk_end}")
            try:
                bars = client.get_trendbars(symbol_id, timeframe, chunk_start, chunk_end)
                all_bars.extend(bars)
            except Exception as e:
                logger.error(f"  chunk {i + 1} failed: {e}")
            # Rate limit: 1 req/sec
            if i < len(chunks) - 1:
                time.sleep(1.0)

        if not all_bars:
            return pd.DataFrame()

        df = pd.DataFrame(all_bars)
        # Convert ms timestamps to datetime
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def _load_existing_csv(
        self,
        symbol: str,
        timeframe: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> pd.DataFrame:
        """Load data from existing CSV file."""
        filename = self._csv_filename(symbol, timeframe)
        path = self._data_dir / filename

        if not path.exists():
            return pd.DataFrame()

        try:
            df = pd.read_csv(path)
            # Normalize column names
            df.columns = [c.strip().lower() for c in df.columns]
            # Map 'date' to 'timestamp'
            if "date" in df.columns and "timestamp" not in df.columns:
                df = df.rename(columns={"date": "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            # Ensure required columns
            for col in ["open", "high", "low", "close", "volume"]:
                if col not in df.columns:
                    df[col] = 0.0
            return df[["timestamp", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.error(f"Failed to load CSV {path}: {e}")
            return pd.DataFrame()

    def _csv_filename(self, symbol: str, timeframe: str) -> str:
        """Generate standard CSV filename."""
        return f"{symbol.replace('/', '')}_{timeframe}.csv"

    def _save_csv(self, df: pd.DataFrame, symbol: str, timeframe: str) -> str:
        """Save DataFrame to CSV in standard format."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        filename = self._csv_filename(symbol, timeframe)
        path = self._data_dir / filename

        out = df.copy()
        out["timestamp"] = pd.to_datetime(out["timestamp"])
        out = out.rename(columns={"timestamp": "Date"})
        # Capitalize column names to match existing format
        for col in ["open", "high", "low", "close", "volume"]:
            if col in out.columns:
                out = out.rename(columns={col: col.capitalize()})
        out = out[["Date", "Open", "High", "Low", "Close", "Volume"]]
        out.to_csv(path, index=False)
        logger.info(f"Saved {len(out)} bars to {path}")
        return str(path)
