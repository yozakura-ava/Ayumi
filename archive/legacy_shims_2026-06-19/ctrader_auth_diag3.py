import os  # noqa: E401, I001
import sys
import threading
import time

sys.path.insert(0, "$AYUMI_ROOT")
sys.path.insert(0, "$AYUMI_ROOT/src/forex_bot")
os.chdir("$AYUMI_ROOT")

from dotenv import load_dotenv

load_dotenv("$AYUMI_ROOT/.env")

from adapters.ctrader.reactor_manager import ReactorManager
from ctrader_open_api.client import Client
from ctrader_open_api.endpoints import EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOAGetAccountListByAccessTokenReq,
)
from ctrader_open_api.protobuf import Protobuf
from ctrader_open_api.tcpProtocol import TcpProtocol
from twisted.internet import reactor

client_id = os.getenv("CTRADER_OPENAPI_CLIENT_ID")
client_secret = os.getenv("CTRADER_OPENAPI_CLIENT_SECRET")
access_token = os.getenv("CTRADER_OPENAPI_ACCESS_TOKEN")
account_id = int(os.getenv("CTRADER_OPENAPI_ACCOUNT_ID"))

print("access_token", access_token[:20])
print("account_id", account_id)

ReactorManager().ensure_running()
time.sleep(0.5)

client = Client(EndPoints.PROTOBUF_DEMO_HOST, 5035, TcpProtocol)
connected = threading.Event()
client.setConnectedCallback(lambda _: connected.set())
client.startService()
if not connected.wait(timeout=15):
    print("TCP timeout")
    sys.exit(1)
print("TCP OK")


def send_and_wait(msg, timeout=15):
    evt = threading.Event()
    res = [None]

    def cb(c, m):
        res[0] = m
        evt.set()

    client.setMessageReceivedCallback(cb)
    d = client.send(msg, clientMsgId=str(id(msg)), responseTimeoutInSeconds=timeout)

    def ok(r):
        res[0] = r
        evt.set()

    def err(f):
        print("err", f)
        evt.set()

    from twisted.internet import threads  # noqa: F401

    reactor.callFromThread(lambda: d.addCallbacks(ok, err))
    evt.wait(timeout=timeout + 5)
    return res[0]


# app auth
res = send_and_wait(ProtoOAApplicationAuthReq(clientId=client_id, clientSecret=client_secret))
try:
    p = Protobuf.extract(res)
    print("app auth ok:", p.DESCRIPTOR.full_name)
except Exception as e:
    print("app auth error:", e, res)

# accounts list
res2 = send_and_wait(ProtoOAGetAccountListByAccessTokenReq(accessToken=access_token))
try:
    p2 = Protobuf.extract(res2)
    print("accounts list ok:", p2.DESCRIPTOR.full_name)
    for a in p2.ctidTraderAccount:
        print("  account", a.ctidTraderAccountId, "login", getattr(a, "traderLogin", None))
except Exception as e:
    print("accounts list error:", e, res2)

# account auth
res3 = send_and_wait(ProtoOAAccountAuthReq(ctidTraderAccountId=account_id, accessToken=access_token))
try:
    p3 = Protobuf.extract(res3)
    print("account auth ok:", p3.DESCRIPTOR.full_name)
except Exception as e:
    print("account auth error:", e, res3)

client.stopService()
