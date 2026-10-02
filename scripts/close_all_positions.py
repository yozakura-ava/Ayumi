#!/usr/bin/env python3
"""Close all open positions on the cTrader demo account.

Use case: cleanup after a test run left naked positions with no SL/TP.
Connects to demo.ctraderapi.com, reconciles positions, and closes each.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path("$AYUMI_ROOT")
SRC = ROOT / "src" / "forex_bot"
sys.path.insert(0, str(SRC))
os.chdir(str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from adapters.ctrader.credential_store import CredentialStore
from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed


def main():
    creds = CredentialStore(env_path=ROOT / ".env").load()
    feed = OpenApiSpotFeed(
        ctid_account_id=creds.account_id,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        access_token=creds.access_token,
        refresh_token=creds.refresh_token,
        host="demo.ctraderapi.com",
        port=5035,
    )
    feed.start()
    print("Connected, subscribing to known symbols...")
    # Pre-subscribe to known symbols so reconcile can resolve names.
    for sym in ("EURUSD", "GBPUSD", "USDJPY"):
        try:
            sid = feed.resolve_symbol_id(sym)
            if sid:
                feed._fetch_symbol_details(sid)
                print(f"  {sym} -> id={sid} digits={feed._symbol_digits.get(sid)}")
        except Exception as e:
            print(f"  {sym} -> error: {e}")
    print("Reconciling...")
    positions = feed.reconcile(timeout=15.0)
    print(f"Found {len(positions)} open positions")
    if not positions:
        feed.stop()
        return
    for p in positions:
        try:
            symbol_id = feed.resolve_symbol_id(p.symbol)
            vol_raw = feed.lots_to_volume(symbol_id, p.volume)
            pos_id_int = int(p.position_id)
            print(f"  Closing {p.symbol} dir={p.direction.value} pos_id={pos_id_int} lots={p.volume} raw={vol_raw}")
            ok = feed.close_position(pos_id_int, vol_raw, timeout=15.0)
            print(f"    -> close {'OK' if ok else 'FAILED'}")
        except Exception as e:
            print(f"    -> error: {e}")
    feed.stop()
    print("Done")


if __name__ == "__main__":
    main()
