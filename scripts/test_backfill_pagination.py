#!/usr/bin/env python3
"""Smoke test: verify backward pagination works across multiple chunks."""

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

symbol_id = 2  # GBPUSD
period = "M1"
max_bars = 5000
now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

# Fetch 3 consecutive chunks backward
cursor_end = now_ms
total_bars = 0
earliest_overall = None
latest_overall = None

for chunk_num in range(1, 4):
    bars = client.get_trendbars(
        symbol_id=symbol_id,
        period=period,
        from_ts=0,  # wide open
        to_ts=cursor_end,
        max_bars=max_bars,
    )
    if not bars:
        print(f"Chunk {chunk_num}: no bars — stopping")
        break

    # Sort by timestamp
    bars.sort(key=lambda b: b["timestamp"])

    # Convert to datetime for display
    first_ts = datetime.fromtimestamp(bars[0]["timestamp"] / 1000, tz=timezone.utc)
    last_ts = datetime.fromtimestamp(bars[-1]["timestamp"] / 1000, tz=timezone.utc)

    print(
        f"Chunk {chunk_num}: {len(bars)} bars, {first_ts.strftime('%Y-%m-%d %H:%M')} → {last_ts.strftime('%Y-%m-%d %H:%M')}"  # noqa: E501
    )

    total_bars += len(bars)
    if earliest_overall is None:
        earliest_overall = first_ts
    latest_overall = last_ts

    # Move cursor backward
    period_ms = 60 * 1000  # M1
    cursor_end = int(bars[0]["timestamp"]) - period_ms

    import time

    time.sleep(0.3)

chunk_num_final = chunk_num
print(f"\nTotal: {total_bars} bars across {min(chunk_num_final, 3)} chunks")
print(f"Range: {earliest_overall.strftime('%Y-%m-%d %H:%M')} → {latest_overall.strftime('%Y-%m-%d %H:%M')}")
print(f"Span: {(latest_overall - earliest_overall).total_seconds() / 3600:.1f} hours")

if total_bars > 10000:
    print("✅ PASS: Pagination works across multiple chunks")
else:
    print(f"❌ FAIL: Expected >10000 bars across 3 chunks, got {total_bars}")

client.disconnect()
