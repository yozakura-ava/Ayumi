#!/usr/bin/env python3
"""Close all open positions on the cTrader demo account.

Emergency close — used when positions are unprotected (no SL) and need
to be exited immediately.  Uses the same credential-loading pattern
as the forward test engine so token refresh works.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path("$AYUMI_ROOT")
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")


async def main():
    from adapters.ctrader.credential_store import CredentialStore
    from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed
    from adapters.ctrader.token_lifecycle import TokenLifecycle

    # Use the same loader the engine uses — handles refresh via lifecycle.
    store = CredentialStore(".env")
    lifecycle = TokenLifecycle(store)
    access_token = lifecycle.ensure_valid()
    creds = store.get()

    if not access_token:
        print("FATAL: empty access token after credential load")
        sys.exit(1)

    feed = OpenApiSpotFeed(
        ctid_account_id=creds.account_id,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        access_token=access_token,
        refresh_token=creds.refresh_token or None,
        host=os.environ.get("CTRADER_HOST", "demo.ctraderapi.com"),
        port=int(os.environ.get("CTRADER_SSL_PORT", "5035")),
        token_lifecycle=lifecycle,
    )

    ok = feed.start()
    if not ok:
        print("STARTUP FAILED — feed.start() returned False")
        sys.exit(1)
    # Allow async connect path to settle, then subscribe to symbols so
    # the symbol_id map is populated (needed for reconcile() to work).
    await asyncio.sleep(5)
    for sym in ("GBPUSD", "EURUSD", "USDJPY"):
        feed.subscribe(sym)
    await asyncio.sleep(3)

    print()
    print("=== Listing open positions (reconcile) ===")
    positions = feed.reconcile()
    print(f"Found {len(positions)} open position(s)")
    for p in positions:
        print(
            f"  id={p.position_id} {p.symbol} lots={p.volume} "
            f"entry={getattr(p, 'entry_price', '?')} "
            f"sl={getattr(p, 'sl_price', '?')} "
            f"tp={getattr(p, 'tp_price', '?')}"
        )

    if not positions:
        print("Nothing to close.")
        feed.stop()
        return

    print()
    print("=== Closing each position ===")
    closed = []
    failed = []
    for p in positions:
        print(
            f"  Closing position {p.position_id} ({p.symbol}, {p.volume} lots)...",
            flush=True,
        )
        # position_id is a decimal string; close_position expects int
        try:
            pid_int = int(p.position_id)
        except (TypeError, ValueError):
            print(f"    FAILED — non-integer position_id {p.position_id!r}")
            failed.append((p.position_id, "non-int id"))
            continue
        # Volume: lots × lot_size (100_000 for forex).  cTrader protocol
        # uses lots × lot_size as int.  feed.close_position doesn't convert,
        # so we do it here.
        try:
            volume_lots = float(p.volume)
        except (TypeError, ValueError):
            print(f"    FAILED — non-numeric volume {p.volume!r}")
            failed.append((p.position_id, "bad volume"))
            continue
        # Resolve symbol_id → lot_size via the feed's volume_calculator.
        try:
            sym_id = feed.resolve_symbol_id(p.symbol)
            lot_size = feed._volume_calc._symbols[sym_id].lot_size
        except Exception:
            lot_size = 100_000  # forex default
        volume_int = int(round(volume_lots * lot_size))
        print(
            f"    pid={pid_int}  volume(lots)={volume_lots}  volume(int)={volume_int}",
            flush=True,
        )
        try:
            ok = feed.close_position(pid_int, volume_int)
            if ok:
                print("    OK")
                closed.append(p.position_id)
            else:
                print("    FAILED — broker rejected")
                failed.append((p.position_id, "rejected"))
        except Exception as e:
            print(f"    FAILED — {type(e).__name__}: {e}")
            failed.append((p.position_id, str(e)))
        await asyncio.sleep(1.5)

    print()
    print("=== Verification (5s after last close) ===")
    await asyncio.sleep(5)
    remaining = feed.reconcile()
    print(f"Remaining positions: {len(remaining)}")
    for p in remaining:
        print(f"  STILL OPEN: id={p.position_id} {p.symbol} lots={p.volume}")

    print()
    print("=== Summary ===")
    print(f"  Closed: {len(closed)}")
    print(f"  Failed: {len(failed)}")
    if failed:
        for pid, reason in failed:
            print(f"    {pid}: {reason}")
    print(f"  Still open after attempt: {len(remaining)}")

    feed.stop()


if __name__ == "__main__":
    if os.geteuid() == 0:
        sys.exit("Refusing to run as root. Run as $USER.")
    asyncio.run(main())
