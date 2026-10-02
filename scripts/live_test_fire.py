#!/usr/bin/env python3
"""Live test fire: send a real micro-order to cTrader to verify the signal->order chain."""

import logging
import os
import sys
import time

sys.path.insert(0, "src/forex_bot")
os.chdir("$AYUMI_ROOT")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

from dotenv import load_dotenv

load_dotenv(".env")

account_id = int(os.environ["CTRADER_OPENAPI_ACCOUNT_ID"])
client_id = os.environ["CTRADER_OPENAPI_CLIENT_ID"]
client_secret = os.environ["CTRADER_OPENAPI_CLIENT_SECRET"]
access_token = os.environ["CTRADER_OPENAPI_ACCESS_TOKEN"]
refresh_token = os.environ["CTRADER_OPENAPI_REFRESH_TOKEN"]

print(f"Token: {access_token[:12]}...")
print(f"Account: {account_id}")

from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed

# Use the HOST from .env or default — check if it's demo
host = os.environ.get("CTRADER_HOST", "demo.ctraderapi.com")
print(f"Host: {host}")

feed = OpenApiSpotFeed(
    ctid_account_id=account_id,
    client_id=client_id,
    client_secret=client_secret,
    access_token=access_token,
    refresh_token=refresh_token,
    host=host,
    port=5035,
)

print("Connecting to cTrader...")
started = feed.start(auto_subscribe=["GBPUSD"])
if not started:
    print("FAILED — retrying after 10s pause")
    time.sleep(10)
    started = feed.start(auto_subscribe=["GBPUSD"])

if not started:
    print("FAILED twice. Aborting.")
    sys.exit(1)

print("Connected! Waiting for ticks...")
time.sleep(8)

gbpusd_id = feed.resolve_symbol_id("GBPUSD")
print(f"GBPUSD symbol_id={gbpusd_id}")

from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATradeSide

print("\n>>> FIRING: BUY GBPUSD 1000 units (0.01 lots) <<<")
order = feed.new_order(
    symbol_id=gbpusd_id,
    side=ProtoOATradeSide.BUY,
    volume=100000,
    comment="AYUMI_TEST_FIRE",
    timeout=20,
)

s = str(order.status)
print(f"\n{'=' * 50}")
print(f"STATUS:    {s}")
print(f"ORDER_ID:  {getattr(order, 'order_id', 'N/A')}")
print(f"REASON:    {getattr(order, 'reason', 'N/A')}")
print(f"DIRECTION: {getattr(order, 'direction', 'N/A')}")
print(f"{'=' * 50}")

if "FILLED" in s:
    print("LIVE TEST FIRE PASSED!")
elif "PENDING" in s:
    print("SENT - waiting for late fill...")
    time.sleep(10)
    print(f"   Final: {order.status}")
elif "REJECTED" in s:
    print(f"REJECTED: {getattr(order, 'reason', '?')}")
elif "TIMEOUT" in s:
    print("TIMEOUT")
    time.sleep(10)
    print(f"   Late: {order.status}")

time.sleep(2)
feed.stop()
print("Done. Check cTrader terminal.")
