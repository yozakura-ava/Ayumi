import os, sys, time, logging, json, threading  # noqa: E401, F401, I001

sys.path.insert(0, "$AYUMI_ROOT")
sys.path.insert(0, "$AYUMI_ROOT/src/forex_bot")
os.chdir("$AYUMI_ROOT")

from dotenv import load_dotenv  # noqa: I001

load_dotenv("$AYUMI_ROOT/.env")

from ctrader_open_api.client import Client  # noqa: I001
from ctrader_open_api.endpoints import EndPoints
from ctrader_open_api.tcpProtocol import TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
    ProtoOAGetAccountListByAccessTokenReq,
)
from ctrader_open_api.protobuf import Protobuf
from twisted.internet import reactor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("diag")

client_id = os.getenv("CTRADER_OPENAPI_CLIENT_ID")
client_secret = os.getenv("CTRADER_OPENAPI_CLIENT_SECRET")
access_token = os.getenv("CTRADER_OPENAPI_ACCESS_TOKEN")
account_id = int(os.getenv("CTRADER_OPENAPI_ACCOUNT_ID"))

print("client_id", client_id[:20])
print("access_token", access_token[:20])
print("account_id", account_id)

from adapters.ctrader.reactor_manager import ReactorManager  # noqa: I001

ReactorManager().ensure_running()
time.sleep(0.5)

client = Client(EndPoints.PROTOBUF_DEMO_HOST, 5035, TcpProtocol)
connected = threading.Event()
client.setConnectedCallback(lambda _: connected.set())
client.startService()
if not connected.wait(timeout=15):
    print("TCP connect timeout")
    sys.exit(1)
print("TCP connected")


def send_and_wait(msg, timeout=15):
    event = threading.Event()
    res_holder = [None]

    def cb(c, m):
        res_holder[0] = m
        event.set()

    client.setMessageReceivedCallback(cb)
    d = client.send(msg, clientMsgId=str(id(msg)), responseTimeoutInSeconds=timeout)

    def ok(r):
        res_holder[0] = r
        event.set()

    def err(f):
        print("send err", f)
        event.set()

    from twisted.internet import threads  # noqa: F401

    reactor.callFromThread(lambda: d.addCallbacks(ok, err))
    if not event.wait(timeout=timeout + 5):
        return None
    return res_holder[0]


# app auth
res = send_and_wait(ProtoOAApplicationAuthReq(clientId=client_id, clientSecret=client_secret))
print("app auth response:", res)
try:
    p = Protobuf.extract(res)
    print("app auth payload type:", p.DESCRIPTOR.full_name)
except Exception as e:
    print("app auth extract error:", e)

# account list by token
res2 = send_and_wait(ProtoOAGetAccountListByAccessTokenReq(accessToken=access_token))
print("account list response:", res2)
try:
    p2 = Protobuf.extract(res2)
    print("account list payload type:", p2.DESCRIPTOR.full_name)
    print(
        "accounts:",
        [dict(id=a.ctidTraderAccountId, login=getattr(a, "traderLogin", None)) for a in p2.ctidTraderAccount],
    )  # noqa: E501
except Exception as e:
    print("account list extract error:", e)

# account auth
res3 = send_and_wait(ProtoOAAccountAuthReq(ctidTraderAccountId=account_id, accessToken=access_token))
print("account auth response:", res3)
try:
    p3 = Protobuf.extract(res3)
    print("account auth payload type:", p3.DESCRIPTOR.full_name)
except Exception as e:
    print("account auth extract error:", e)

client.stopService()
print("done")
