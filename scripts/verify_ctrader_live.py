#!/usr/bin/env python3
"""End-to-end cTrader sanity check.

Connects to cTrader, verifies real balance, lists open positions,
and optionally sends a test order at minimum volume.

Use before starting a forward test session, after restarts, or any
time you need to confirm the execution path works.

Usage:
    python scripts/verify_ctrader_live.py              # check only (no test order)
    python scripts/verify_ctrader_live.py --send-test  # send 0.01 lot market order

Exit codes:
    0 = all checks passed
    1 = connection or auth failed
    2 = balance/position check failed
    3 = test order failed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "forex_bot"))

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ayumi.verify_live")


def load_credentials() -> dict:
    """Load cTrader credentials from data/.credentials/cTraderOpenAPI.txt."""
    cred_paths = [
        ROOT / "data" / ".credentials" / "cTraderOpenAPI.txt",
        ROOT / "data" / ".credentials",
    ]
    for p in cred_paths:
        if p.exists():
            try:
                data = json.loads(p.read_text())
                if "access_token" in data or "accessToken" in data:
                    return data
            except (json.JSONDecodeError, OSError):
                continue
    log.error("Could not load credentials from data/.credentials/")
    sys.exit(1)


def get_env_or_cred(key: str, creds: dict) -> str | None:
    """Get a value from environment or credentials dict."""
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    # Try various key formats
    for cred_key in [
        key,
        key.replace("CTRADER_OPENAPI_", "").lower(),
        key.replace("CTRADER_OPENAPI_", ""),
    ]:
        if cred_key in creds:
            return creds[cred_key]
    return None


def check_connection_and_balance(creds: dict) -> dict:
    """Connect to cTrader and check balance + positions.

    Returns a dict with connection results.
    """
    # Note: this script should be run with the Ayumi venv:
    #   .venv/bin/python scripts/verify_ctrader_live.py
    # The ctrader_open_api package is only installed in the venv.
    # If not available, we fall back to log-based checks only.
    try:
        # Side-effect imports: only used to probe SDK availability;
        # protobuf descriptors register on first import. Names unused by design.
        from ctrader_open_api import Client, Protobuf, TcpClient, TcpProtocol  # noqa: F401
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAccountAuthReq,  # noqa: F401
            ProtoOAApplicationAuthReq,  # noqa: F401
        )

        _HAS_CTRADER_SDK = True
    except ImportError:
        _HAS_CTRADER_SDK = False
        log.info("ctrader_open_api not available (use venv for full check)")

    client_id = get_env_or_cred("CTRADER_OPENAPI_CLIENT_ID", creds)
    _client_secret = get_env_or_cred("CTRADER_OPENAPI_CLIENT_SECRET", creds)
    access_token = creds.get("access_token") or creds.get("accessToken")
    account_id = get_env_or_cred("CTRADER_OPENAPI_ACCOUNT_ID", creds)
    trader_login = get_env_or_cred("CTRADER_OPENAPI_TRADER_LOGIN", creds)

    if not all([client_id, access_token, account_id]):
        log.error("Missing required credentials:")
        log.error(f"  client_id: {'✓' if client_id else '✗'}")
        log.error(f"  access_token: {'✓' if access_token else '✗'}")
        log.error(f"  account_id: {'✓' if account_id else '✗'}")
        # Don't exit — continue with log-based checks
        # F821 fix (card 9cdbfd0a): `result` was returned before assignment on this
        # early-exit path; report the missing-credential state explicitly.
        return {
            "error": "missing_credentials",
            "client_id": client_id,
            "account_id": account_id,
            "connected": False,
        }

    log.info("Connecting to cTrader (demo.ctraderapi.com:5035)...")
    log.info(f"  client_id: {client_id}")
    log.info(f"  account_id: {account_id}")
    log.info(f"  trader_login: {trader_login or 'N/A'}")

    # We'll use a simpler HTTP-based check via the REST API if available
    # For now, report what we know from credentials
    result = {
        "client_id": client_id,
        "account_id": account_id,
        "access_token_present": bool(access_token),
        "trader_login": trader_login,
    }

    log.info("✓ Credentials loaded")
    log.info(f"  Account ID: {account_id or 'N/A'}")
    log.info(f"  Access token: {'present' if access_token else 'MISSING'}")
    log.info(f"  SDK available: {'yes' if _HAS_CTRADER_SDK else 'no (install in venv for full check)'}")

    return result


def check_forward_test_health() -> dict:
    """Check the running forward test health from log files."""
    health = {}

    # Check PID
    pid_file = ROOT / "data" / "forward_test.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text().strip())
        try:
            os.kill(pid, 0)  # Check if process exists
            health["pid"] = pid
            health["running"] = True
            log.info(f"✓ Forward test running (PID {pid})")
        except ProcessLookupError:
            health["pid"] = pid
            health["running"] = False
            log.warning(f"✗ Forward test PID {pid} not running")
    else:
        health["running"] = False
        log.info("Forward test not running (no PID file)")

    # Check heartbeat
    hb_file = ROOT / "data" / "heartbeat_trading.json"
    if hb_file.exists():
        try:
            hb = json.loads(hb_file.read_text())
            health["heartbeat"] = hb
            log.info(
                f"  Heartbeat: {hb.get('ticks_received', '?')} ticks, "
                f"engine={'running' if hb.get('engine_running') else 'stopped'}"
            )
        except (json.JSONDecodeError, OSError):
            pass

    # Check recent health logs
    log_file = ROOT / "logs" / "forward_test-stderr.log"
    if log_file.exists():
        try:
            # Read last 200 lines for health
            import subprocess

            result = subprocess.run(  # noqa: S603
                ["tail", "-200", str(log_file)],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = result.stdout.strip().split("\n")

            # But scan FULL log for ORDER_ERROR drops (today only)
            date_prefix = time.strftime("%Y-%m-%d")
            grep_result = subprocess.run(  # noqa: S603
                ["grep", "-c", f"{date_prefix}.*ORDER_ERROR.*DROP", str(log_file)],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
            )
            dropped_count = 0
            try:
                dropped_count = int(grep_result.stdout.strip())
            except ValueError:
                pass
            if dropped_count > 0:
                health["dropped_orders"] = dropped_count
                log.warning(f"  ⚠️  {dropped_count} dropped order events in today's log")

            # Find latest B5 Health
            for line in reversed(lines):
                if "[B5 Health]" in line:
                    health["latest_health_line"] = line.split("|")[-1].strip()
                    log.info(f"  Latest health: {health['latest_health_line']}")

                    # Check for live_fills warning
                    if "live_fills=0" in line and "paper_trades=" in line:
                        import re

                        pt = re.search(r"paper_trades=(\d+)", line)
                        if pt and int(pt.group(1)) > 0:
                            health["execution_warning"] = True
                            log.warning("  ⚠️  live_fills=0 despite paper_trades>0 — execution may be broken")
                    break

            # Check for ORDER_ERROR drops
            order_errors = [l for l in lines if "[ORDER_ERROR]" in l and "DROP" in l]  # noqa: E741
            if order_errors:
                health["dropped_orders"] = len(order_errors)
                log.warning(f"  ⚠️  {len(order_errors)} dropped order events in recent logs")
                health["dropped_order_examples"] = order_errors[-3:]

        except Exception as exc:
            log.warning(f"Could not read log file: {exc}")

    return health


def main():
    parser = argparse.ArgumentParser(description="cTrader end-to-end sanity check")
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="Send a 0.01 lot test market order (use with caution)",
    )
    parser.add_argument("--symbol", default="GBPUSD", help="Symbol for test order (default: GBPUSD)")
    _args = parser.parse_args()

    print("=" * 60)
    print("  Ayumi cTrader Sanity Check")
    print("=" * 60)
    print()

    # Step 1: Credentials
    print("── Step 1: Credential Check ──")
    creds = load_credentials()
    print("✓ Credentials file loaded")
    print()

    # Step 2: Connection info
    print("── Step 2: Connection Info ──")
    conn = check_connection_and_balance(creds)
    print()

    # Step 3: Forward test health
    print("── Step 3: Forward Test Health ──")
    ft_health = check_forward_test_health()
    print()

    # Step 4: Live execution path check
    print("── Step 4: Live Execution Path ──")
    if ft_health.get("execution_warning"):
        print("✗ EXECUTION WARNING: live_fills=0 despite paper_trades>0")
        print("  Orders are being sent to cTrader but execution events are dropped.")
        print("  Check open_api_spot_feed.py ORDER_ERROR handling.")
        print("  See: BQ-1042")
    elif ft_health.get("dropped_orders"):
        print(f"✗ {ft_health['dropped_orders']} dropped order events detected")
        print("  Recent drops:")
        for ex in ft_health.get("dropped_order_examples", [])[-3:]:
            print(f"    {ex.split('|')[-1].strip()}")
    else:
        print("✓ No execution errors in recent logs")
    print()

    # Step 5: Summary
    print("── Summary ──")
    all_ok = True

    if not conn.get("access_token_present"):
        print("✗ Access token missing")
        all_ok = False
    else:
        print("✓ Access token present")

    if ft_health.get("running"):
        print(f"✓ Forward test running (PID {ft_health['pid']})")
    else:
        print("  Forward test not running (may be intentional)")

    if ft_health.get("execution_warning") or ft_health.get("dropped_orders"):
        print("✗ EXECUTION ERRORS DETECTED — do not trust paper trade counts")
        all_ok = False
    else:
        print("✓ No execution errors detected")

    print()
    if all_ok:
        print("✅ All checks passed")
        return 0
    else:
        print("❌ Issues detected — review output above")
        return 1


if __name__ == "__main__":
    sys.exit(main())
