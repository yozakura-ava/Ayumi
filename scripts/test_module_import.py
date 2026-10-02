#!/usr/bin/env python3
"""Test: import run_paper_mvp module, then create feed directly."""

import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src" / "forex_bot"))

# THIS is the key difference: importing the run_paper_mvp module
# which triggers module-level code including load_dotenv
print("About to import run_paper_mvp...", flush=True)
import importlib

run_paper_mvp = importlib.import_module("run_paper_mvp")
print("Imported run_paper_mvp successfully", flush=True)

# Now create feed directly (not via cTraderTickStreamer)
from adapters.ctrader.market_data_feed import LiveMarketDataFeed, SymbolInfo
from adapters.ctrader.models import cTraderCredentials

creds = cTraderCredentials(
    host=os.getenv("CTRADER_HOST"),
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
