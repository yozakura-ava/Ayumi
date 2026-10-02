#!/usr/bin/env python3
"""Test if the feed works when imported via the MVP module's bootstrap."""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src" / "forex_bot"))
sys.path.insert(0, str(_PROJECT_ROOT))

# Simulate run_paper_mvp.py's module-level bootstrap
try:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass

import logging
import os
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

print(f"HOST: {os.getenv('CTRADER_HOST')}", flush=True)
print(f"PORT: {os.getenv('CTRADER_READONLY_SSL_PORT')}", flush=True)
print(f"ACCOUNT: {os.getenv('CTRADER_ACCOUNT')}", flush=True)

from adapters.ctrader.market_data_feed import LiveMarketDataFeed, SymbolInfo
from adapters.ctrader.models import cTraderCredentials

creds = cTraderCredentials(
    host=os.getenv("CTRADER_HOST", "live-uk-eqx-01.p.c-trader.com"),
    port=int(os.getenv("CTRADER_READONLY_SSL_PORT", "5211")),
    use_ssl=True,
    sender_comp_id=os.getenv("CTRADER_SENDER_COMP_ID", ""),
    target_comp_id=os.getenv("CTRADER_TARGET_COMP_ID", "cServer"),
    sender_sub_id="QUOTE",
    target_sub_id="QUOTE",
    username=os.getenv("CTRADER_ACCOUNT", ""),
    password=os.getenv("CTRADER_PASSWORD", ""),
)

feed = LiveMarketDataFeed(creds)
feed._symbols[31] = SymbolInfo(symbol_id=31, name="XAU/USD")
feed._name_to_id["XAU/USD"] = 31

count = 0


def on_tick(tick):
    global count
    count += 1
    if count <= 3:
        print(f"TICK #{count}: bid={tick.bid}", flush=True)


feed.on_tick(on_tick)
result = feed.start(auto_subscribe=["XAU/USD"])
print(f"start() returned: {result}", flush=True)
time.sleep(15)
print(f"Total in 15s: {count}", flush=True)
feed.stop()
