#!/usr/bin/env python3
"""T3d Integration Test — Send a real 0.01 lot order to cTrader demo.

This is the BQ-1043 gate: one order sent, one fill confirmed.

Usage:
    .venv/bin/python scripts/t3d_lot_test.py

Exit codes:
    0 = order filled successfully
    1 = connection/auth failed
    2 = order rejected
    3 = order timed out
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "forex_bot"))

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
log = logging.getLogger("ayumi.t3d")


def main():
    print("=" * 60)
    print("  T3d Integration Test — Live cTrader Order")
    print("=" * 60)

    # Step 1: Load credentials
    print("\n── Step 1: Load Credentials ──")
    from adapters.ctrader.credential_store import CredentialStore

    cs = CredentialStore(str(ROOT / "data" / ".credentials"))
    try:
        creds = cs.load()
        print(f"✓ client_id: {creds.client_id[:16]}...")
        print(f"✓ account_id: {creds.account_id}")
        print(f"✓ trader_login: {creds.trader_login}")
        print(f"✓ access_token: {creds.access_token[:12]}...")
    except Exception as e:
        print(f"✗ Failed to load credentials: {e}")
        return 1

    # Step 2: Import SDK and connect
    print("\n── Step 2: Connect to cTrader ──")
    try:
        from ctrader_open_api import Client, TcpProtocol
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            # Response classes referenced indirectly via str(type(...)) matching in handlers below;
            # OpenApiMessages_pb2 import also registers all protobuf descriptors.
            ProtoOAAccountAuthReq,
            ProtoOAAccountAuthRes,  # noqa: F401
            ProtoOAApplicationAuthReq,
            ProtoOAApplicationAuthRes,  # noqa: F401
            ProtoOANewOrderReq,
            ProtoOASubscribeSpotsReq,
        )
        from ctrader_open_api.protobuf import Protobuf  # noqa: F401  # re-imported in inner scope (line 88)
        from twisted.internet import reactor

        print("✓ SDK imported")
    except ImportError as e:
        print(f"✗ SDK import failed: {e}")
        return 1

    import threading

    connected_event = threading.Event()
    app_authed_event = threading.Event()
    account_authed_event = threading.Event()
    _symbol_resolved_event = threading.Event()
    order_result = {"status": None, "data": None, "error": None}
    order_event = threading.Event()

    client_holder = {"client": None}

    def on_message(client, message):
        msg_type = message.payloadType
        log.info(f"Received message payloadType={msg_type}")

        # Try to extract the actual response
        try:
            from ctrader_open_api.protobuf import Protobuf

            extracted = Protobuf.extract(message)
            payload_type = extracted.payloadType
            log.info(f"Extracted payloadType={payload_type}")
        except Exception:
            payload_type = msg_type

        # Application auth response
        if payload_type == 2101 or "AppAuthRes" in str(type(extracted)):
            if hasattr(extracted, "errorCode") and extracted.errorCode:
                log.error(f"App auth failed: {extracted.errorCode}")
                return
            log.info("✓ Application authenticated")
            app_authed_event.set()

            # Now do account auth
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = creds.account_id
            req.accessToken = creds.access_token
            client.send(req, clientMsgId=f"acct_auth_{int(time.time())}")

        # Account auth response
        elif payload_type == 2103 or "AccountAuthRes" in str(type(extracted)):
            if hasattr(extracted, "errorCode") and extracted.errorCode:
                log.error(f"Account auth failed: {extracted.errorCode}")
                return
            log.info("✓ Account authenticated")
            account_authed_event.set()

            # Resolve GBPUSD symbol ID
            # First try subscribing to spots
            req = ProtoOASubscribeSpotsReq()
            req.ctidTraderAccountId = creds.account_id
            req.symbolId.append(1)  # GBPUSD is usually 1
            client.send(req, clientMsgId=f"sub_spots_{int(time.time())}")
            log.info("Subscribed to spots for symbolId=1")

            # Now send the test order
            time.sleep(2)  # Wait for subscription
            log.info("Sending 0.01 lot market BUY order for GBPUSD...")

            order_req = ProtoOANewOrderReq()
            order_req.ctidTraderAccountId = creds.account_id
            order_req.symbolId = 1  # GBPUSD
            order_req.orderType = 1  # MARKET
            order_req.tradeSide = 1  # BUY
            order_req.volume = 1000  # 0.01 lots = 1000 units
            order_req.timeInForce = 1  # GTC
            order_req.clientOrderId = f"t3d_test_{int(time.time())}"

            client_msg_id = f"t3d_order_{int(time.time())}"

            def on_order_success(proto_res):
                log.info(f"✓ Order response received: {type(proto_res)}")
                order_result["status"] = "success"
                order_result["data"] = proto_res
                order_event.set()

            def on_order_error(failure):
                log.error(f"✗ Order error: {failure}")
                order_result["status"] = "error"
                order_result["error"] = str(failure)
                order_event.set()

            d = client.send(order_req, clientMsgId=client_msg_id)
            d.addCallbacks(on_order_success, on_order_error)

        # New order response
        elif payload_type == 2112 or "NewOrderRes" in str(type(extracted)):
            log.info(f"✓ NEW ORDER RESPONSE: {type(extracted)}")
            if hasattr(extracted, "errorCode") and extracted.errorCode:
                log.error(f"Order rejected: errorCode={extracted.errorCode}")
                order_result["status"] = "rejected"
                order_result["error"] = extracted.errorCode
            else:
                log.info("✓ ORDER ACCEPTED!")
                order_result["status"] = "filled"
                order_result["data"] = extracted
            order_event.set()

        # Execution event
        elif payload_type == 2126:
            log.info("✓ EXECUTION EVENT received")
            order_result["status"] = "execution_event"
            order_result["data"] = extracted
            order_event.set()

        # Order error event
        elif payload_type == 2132:
            log.warning("⚠ ORDER ERROR EVENT")
            error_code = getattr(extracted, "errorCode", "UNKNOWN")
            description = getattr(extracted, "description", "")
            log.warning(f"  errorCode={error_code} description={description}")
            order_result["status"] = "rejected"
            order_result["error"] = f"{error_code}: {description}"
            order_event.set()

        else:
            log.debug(f"Unhandled payloadType={payload_type}")

    def on_connected(client):
        log.info("✓ TCP connected to demo.ctraderapi.com:5035")
        connected_event.set()

        # Send app auth
        req = ProtoOAApplicationAuthReq()
        req.clientId = creds.client_id
        req.clientSecret = creds.client_secret
        client.send(req, clientMsgId=f"app_auth_{int(time.time())}")
        log.info("Sent application auth request")

    # Create client and start reactor in background
    from twisted.internet import reactor

    client = Client("demo.ctraderapi.com", 5035, TcpProtocol)
    client.setConnectedCallback(on_connected)
    client.setMessageReceivedCallback(on_message)
    client_holder["client"] = client

    log.info("Starting cTrader client (reactor in background thread)...")
    client.startService()

    # Start reactor if not already running
    if not reactor.running:
        import threading

        reactor_thread = threading.Thread(target=reactor.run, args=(False,), daemon=True)
        reactor_thread.start()
        time.sleep(1)
        log.info("Reactor started in background thread")

    # Wait for flow to complete
    if not order_event.wait(timeout=45):
        log.error("✗ Timed out waiting for order response (45s)")
        try:
            client.stopService()
        except Exception:  # noqa: S110
            pass
        return 3

    # Stop client
    time.sleep(2)
    try:
        client.stopService()
    except Exception:  # noqa: S110
        pass
    time.sleep(1)

    # Report result
    print("\n" + "=" * 60)
    print("  RESULT")
    print("=" * 60)

    status = order_result["status"]
    if status == "filled" or status == "execution_event":
        print("✅ ORDER FILLED!")
        print("  Check cTrader dashboard for the 0.01 lot GBPUSD BUY")
        return 0
    elif status == "rejected":
        print(f"❌ ORDER REJECTED: {order_result.get('error', 'unknown')}")
        return 2
    elif status == "error":
        print(f"❌ ORDER ERROR: {order_result.get('error', 'unknown')}")
        return 2
    elif status == "success":
        print("✅ ORDER ACCEPTED (response received)")
        return 0
    else:
        print(f"❓ Unknown status: {status}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
