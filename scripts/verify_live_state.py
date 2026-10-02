#!/usr/bin/env python3
"""Live cTrader state verification — run after forward-test fixes.

Connects to cTrader, reads balance + open positions, and asserts the
forward-test engine's view of the world matches the broker's view.

Use this before trusting the forward test in live mode. Without it, the
system could have phantom positions (as it did pre-fix) and we'd never
know.

Usage:
    cd $AYUMI_ROOT && source .venv/bin/activate
    python scripts/verify_live_state.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "forex_bot"))
sys.path.insert(0, str(ROOT))

from adapters.ctrader.account_state import (  # type: ignore # noqa: E402
    AccountStateError,
    read_account_snapshot,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("verify_live_state")


def load_credentials() -> dict:
    """Load cTrader creds from .env or data/.credentials/."""
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    return {
        "client_id": env.get("CTRADER_OPENAPI_CLIENT_ID"),
        "client_secret": env.get("CTRADER_OPENAPI_CLIENT_SECRET"),
        "access_token": env.get("CTRADER_OPENAPI_ACCESS_TOKEN"),
        "account_id": env.get("CTRADER_OPENAPI_ACCOUNT_ID"),
        "host": env.get("CTRADER_OPENAPI_HOST", "demo.ctraderapi.com"),
        "port": int(env.get("CTRADER_OPENAPI_PORT", "5035")),
    }


def build_client(creds: dict):
    """Build a minimal SDK client + run app+account auth.

    Uses the existing OpenApiSpotFeed constructor + _conn.send_and_wait
    (the well-trodden path) instead of inventing our own client lifecycle.
    Returns the underlying connection (feed._conn) which exposes send_and_wait.
    """
    from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed  # type: ignore

    feed = OpenApiSpotFeed(
        ctid_account_id=int(creds["account_id"]),
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        access_token=creds["access_token"],
        host=creds["host"],
        port=creds["port"],
    )
    conn = feed._conn
    conn.connect()
    import time

    for _ in range(50):
        if conn.is_connected:
            break
        time.sleep(0.3)
    if not conn.is_connected:
        raise RuntimeError("cTrader connection timeout")

    from ctrader_open_api.messages.OpenApiMessages_pb2 import (  # noqa: I001
        ProtoOAAccountAuthReq,
        ProtoOAApplicationAuthReq,
    )

    app_req = ProtoOAApplicationAuthReq(
        clientId=creds["client_id"],
        clientSecret=creds["client_secret"],
    )
    _app_res = conn.send_and_wait(app_req, timeout=15.0, prefix="auth_app")
    log.info("✓ App auth OK")

    acct_req = ProtoOAAccountAuthReq(
        ctidTraderAccountId=int(creds["account_id"]),
        accessToken=creds["access_token"],
    )
    conn.send_and_wait(acct_req, timeout=15.0, prefix="auth_acct")
    log.info("✓ Account %s authed", creds["account_id"])

    return conn


def main() -> int:
    creds = load_credentials()
    missing = [k for k in ("client_id", "client_secret", "access_token", "account_id") if not creds.get(k)]
    if missing:
        log.error("Missing credentials: %s", missing)
        return 1

    log.info("Connecting to cTrader %s:%d...", creds["host"], creds["port"])
    client = build_client(creds)
    log.info("✓ Connected")

    # Read snapshot
    try:
        snapshot = read_account_snapshot(client, int(creds["account_id"]))
    except AccountStateError as exc:
        log.error("Snapshot failed: %s", exc)
        return 2

    balance = snapshot["balance"]
    positions = snapshot["positions"]
    log.info("✓ Demo balance: %s", balance)
    log.info("✓ Open positions: %d", len(positions))
    for pos in positions:
        log.info(
            "    - %s %s %s @ %s sl=%s tp=%s vol=%s",
            pos.position_id,
            pos.side,
            pos.symbol,
            pos.entry_price,
            pos.sl,
            pos.tp,
            pos.volume_lots,
        )

    # Compare against signal_stats.jsonl — phantom LIVE positions here = the bug.
    # POS_PAPER_ entries are paper-trader tests, not cTrader positions.
    stats_file = ROOT / "data" / "signal_stats.jsonl"
    if stats_file.exists():
        live_open_in_stats = []
        for line in stats_file.read_text().strip().split("\n"):
            try:
                import json

                entry = json.loads(line)
                if entry.get("outcome") == "open" and not entry.get("signal_id", "").startswith("POS_PAPER_"):
                    live_open_in_stats.append(entry)
            except json.JSONDecodeError:
                pass
        if live_open_in_stats and not positions:
            log.error(
                "⚠️  PHANTOM POSITIONS: signal_stats.jsonl shows %d LIVE open, "
                "cTrader shows 0. Same bug as before. Engine and broker disagree.",
                len(live_open_in_stats),
            )
            return 3
        log.info(
            "✓ signal_stats.jsonl live open count matches cTrader (%d in stats, %d on broker)",
            len(live_open_in_stats),
            len(positions),
        )

    log.info("✓ ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
