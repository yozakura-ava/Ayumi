#!/usr/bin/env python3
"""Quick test: check bar ordering from cTrader API."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))
load_dotenv(PROJECT_ROOT / ".env")

from adapters.ctrader.open_api_client import CTraderOpenApiClient

client = CTraderOpenApiClient(
    client_id=os.getenv("CTRADER_OPENAPI_CLIENT_ID"),
    client_secret=os.getenv("CTRADER_OPENAPI_CLIENT_SECRET"),
    access_token=os.getenv("CTRADER_OPENAPI_ACCESS_TOKEN"),
    account_id=int(os.getenv("CTRADER_OPENAPI_ACCOUNT_ID")),
)
client.connect()

now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
from_ms = now_ms - 86400000  # 1 day ago

bars = client.get_trendbars(symbol_id=2, period="M1", from_ts=from_ms, to_ts=now_ms, max_bars=5)
print(f"Got {len(bars)} bars")
for b in bars:
    ts = b["timestamp"]
    print(
        f"  ts_ms={ts} -> {datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} close={b['close']}"  # noqa: E501
    )

if len(bars) >= 2:
    if bars[0]["timestamp"] < bars[-1]["timestamp"]:
        print("ORDER: ASCENDING (oldest first)")
    else:
        print("ORDER: DESCENDING (newest first)")

client.disconnect()
