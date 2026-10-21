"""Self-check for kiro_signal.py. Run: python tests/test_kiro_signal.py

Covers the sender filter, message splitting, the webhook signature, event-stream parsing, the MCP
protocol and its fixed recipient, wait mode against a stub daemon, and the kiro relay's queue,
receipts and typing marks, with signal-cli and the bridge stubbed out."""

import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["KIRO_SIGNAL_HOME"] = tempfile.mkdtemp()  # never the real ~/.kiro-signal
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kiro_signal as ks  # noqa: E402

OWNER = "0f1e2d3c-aaaa-4bbb-8ccc-123456789abc"
STRANGER = "ffffffff-0000-4000-8000-000000000000"


def dm(text, **overrides):
    return {"sourceUuid": OWNER, "sourceNumber": None, "dataMessage": {"message": text}, **overrides}


# Inbound filter: only the owner's direct text messages get through.
assert ks.inbound_text(dm(" hi "), OWNER) == "hi"
assert ks.inbound_text(dm("hi"), OWNER.upper()) == "hi"
assert ks.inbound_text({"sourceNumber": "+15550001111", "dataMessage": {"message": "hi"}}, "+15550001111") == "hi"
assert ks.inbound_text(dm("hi", sourceUuid=STRANGER), OWNER) is None
assert ks.inbound_text(dm("hi", dataMessage={"message": "hi", "groupInfo": {"groupId": "g"}}), OWNER) is None
assert ks.inbound_text(dm("  "), OWNER) is None
assert ks.inbound_text({"sourceUuid": OWNER, "syncMessage": {"sentMessage": {"message": "hi"}}}, OWNER) is None
assert ks.inbound_text({}, OWNER) is None

# Chunking: every piece fits, nothing but whitespace is lost.
text = ("word " * 900).strip() + "\n\n" + "x" * 2500
parts = ks.chunks(text)
assert len(parts) > 1 and all(len(p) <= ks.CHUNK for p in parts), [len(p) for p in parts]
assert "".join("".join(parts).split()) == "".join(text.split())
assert ks.chunks("short") == ["short"] and ks.chunks("  ") == []

# Webhook signature format from KiroCrew's inbound-webhooks doc.
body = b'{"message":"x"}'
assert ks.sign("s3cret", "1785372000", body) == "sha256=" + hmac.new(b"s3cret", b"1785372000." + body, hashlib.sha256).hexdigest()

# SSE parsing as signal-cli writes it: no space after the colon, ':' keep-alives.
stream = [b":\n", b"id:1-1\n", b"event:receive\n", b'data:{"a":1}\n', b"\n", b":\n", b"id: 1-2\n", b"data: x\n", b"\r\n"]
assert list(ks.parse_sse(stream)) == [("1-1", '{"a":1}'), ("1-2", "x")]

# A missing config, or one without an owner, fails closed.
try:
    ks.load_config()
    raise AssertionError("load_config ran without a config")
except SystemExit as exc:
    assert "install.py" in str(exc)
ks.CONFIG_FILE.write_text(json.dumps({"owner": " "}))
try:
    ks.load_config()
    raise AssertionError("load_config accepted an empty owner")
except SystemExit as exc:
    assert "owner" in str(exc)
ks.CONFIG_FILE.unlink()

# MCP protocol, with the daemon stubbed out.
signal = []  # every signal-cli call: (method, params)
ks.rpc = lambda cfg, method, params: signal.append((method, params))
cfg = {"owner": OWNER, "kiro_session": "sess_x"}
init = ks.mcp_handle(cfg, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
assert init["result"]["capabilities"] == {"tools": {}} and init["result"]["protocolVersion"] == "2025-06-18"
assert ks.mcp_handle(cfg, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
tools = ks.mcp_handle(cfg, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
assert [t["name"] for t in tools] == ["signal_reply"]


def call(arguments, rid=3):
    req = {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": "signal_reply", "arguments": arguments}}
    return ks.mcp_handle(cfg, req)


# A recipient smuggled into the arguments is ignored: the owner is the only destination.
assert not call({"text": "hello", "recipient": "+19999999999"})["result"].get("isError")
assert signal == [("send", {"recipient": [OWNER], "message": "hello"})]
assert call({"text": "  "})["result"]["isError"]
assert ks.mcp_handle(cfg, {"jsonrpc": "2.0", "id": 5, "method": "resources/list"})["error"]["code"] == -32601

# crew relay: the owner's message is framed and forwarded, a stranger's is not.
posted = []
ks.post_hook = lambda cfg, token, secret, message: posted.append(message) or True
ks.crew_event(cfg, "t", "s", json.dumps({"envelope": dm("status?"), "account": "+15550002222"}))
ks.crew_event(cfg, "t", "s", json.dumps({"envelope": dm("hi", sourceUuid=STRANGER)}))
ks.crew_event(cfg, "t", "s", "not json")  # logged, never raised
assert len(posted) == 1 and "status?" in posted[0] and "signal_reply" in posted[0]


# wait mode against a stub daemon that streams chunked SSE, as signal-cli does, then holds the
# connection open: the owner's burst comes back together, a stranger and keep-alives are skipped.
class Events(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    script = []  # (seconds to sleep first, SSE text)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for pause, text in self.script:
                time.sleep(pause)
                raw = text.encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                self.wfile.flush()
            time.sleep(5)
        except OSError:
            pass  # the client hung up, which is what wait does

    def log_message(self, *args):
        pass


def sse(envelope):
    return "id:1\nevent:receive\ndata:" + json.dumps({"envelope": envelope}) + "\n\n"


server = ThreadingHTTPServer(("127.0.0.1", 0), Events)
threading.Thread(target=server.serve_forever, daemon=True).start()
wcfg = {"owner": OWNER, "signal_rpc": f"http://127.0.0.1:{server.server_port}"}

Events.script = [(0, ":\n"), (0.2, sse(dm("hi", sourceUuid=STRANGER))), (0.2, sse(dm("first"))), (0.5, sse(dm("second")))]
start = time.monotonic()
assert ks.wait_owner(wcfg, 30, settle=1) == ["first", "second"]
assert time.monotonic() - start < 4  # returned after the settle, not at the 30 s timeout

Events.script = [(0, ":\n")]
start = time.monotonic()
assert ks.wait_owner(wcfg, 1) == []
assert 0.9 < time.monotonic() - start < 3

# The pairing step's picker: the first sender whose text holds the phrase, with no settle.
Events.script = [(0, sse(dm("hello", sourceUuid=STRANGER))), (0.1, sse(dm("my phrase is Otter42"))), (0.1, sse(dm("Otter42", sourceUuid=STRANGER)))]
pick = lambda env: env.get("sourceUuid") if "otter42" in str((env.get("dataMessage") or {}).get("message")).lower() else None  # noqa: E731
assert ks.wait_events(wcfg, 10, pick, settle=0) == [OWNER]
server.shutdown()

# ── kiro mode: status shows only through Signal's marks. No notice while busy or closed, a read
# receipt for each message once it is in the chat, typing while the turn runs, words only on a
# failure. ──
signal.clear()
answers, requests = [], []  # bridge answers to hand out, requests it got


def fake_bridge(cfg, token, method, path, body=None):
    requests.append((method, path, body))
    return answers.pop(0)


ks.bridge = fake_bridge


def event(text, ts, sender=OWNER):
    env = {"sourceUuid": sender, "timestamp": ts, "dataMessage": {"message": text, "timestamp": ts}}
    return json.dumps({"envelope": env})


def tick(state):
    ks.deliver_to_kiro(cfg, "t", state)


def calls(method):
    return [p for m, p in signal if m == method]


# The keyword: alone, or followed by ":" or a new line. Not as an ordinary first word.
for text, force, rest in [
    ("flush", True, ""), ("Force!", True, ""), ("force: stop and look at CI", True, "stop and look at CI"),
    ("FLUSH:\nnew plan", True, "new plan"), ("force push the branch", False, "force push the branch"),
    ("forceful", False, "forceful"), ("please flush", False, "please flush"),
]:
    q = ks.queued(text, 1)
    assert (q["force"], q["text"]) == (force, rest), (text, q)

# Queued with their timestamps; a stranger's message never is.
ks.kiro_event(cfg, event("first", 101))
ks.kiro_event(cfg, event("hi", 102, sender=STRANGER))
ks.kiro_event(cfg, event("second", 103))
assert [(i["text"], i["ts"]) for i in ks.load_pending()] == [("first", 101), ("second", 103)]
assert signal == []  # no typing on arrival: queued is not working on it

# Busy, then closed: they wait, silently, with no read receipt.
state = {"typing": False, "typed_at": 0.0}
answers[:] = [(409, {}), (0, {})]
tick(state)
tick(state)
assert signal == [] and len(ks.load_pending()) == 2

# Then in: one prompt, read receipts for both, then typing while the turn runs, stop at its end.
answers[:] = [(202, {}), (200, {"busy": True})]
tick(state)
method, path, body = requests[-2]
assert (method, path, body["force"]) == ("POST", "/prompt", False)
assert body["text"].startswith("[") and "first\n\nsecond" in body["text"]  # never a "/" command
assert calls("sendReceipt") == [{"recipient": OWNER, "targetTimestamp": [101, 103], "type": "read"}]
assert calls("sendTyping") == [{"recipient": [OWNER]}] and ks.load_pending() == []
answers[:] = [(200, {"busy": True})]
tick(state)  # under 10 s since the last one: not sent again
assert len(calls("sendTyping")) == 1
state["typed_at"] -= ks.TYPING_EVERY
answers[:] = [(200, {"busy": True})]
tick(state)
assert len(calls("sendTyping")) == 2
answers[:] = [(200, {"busy": False})]
tick(state)
assert calls("sendTyping")[-1] == {"recipient": [OWNER], "stop": True} and not state["typing"]
assert calls("send") == []  # not one word sent in all of that

# A message mid-turn waits; "flush" then sends it, forced, and both get their receipts.
signal.clear()
ks.kiro_event(cfg, event("look at the failing test", 201))
answers[:] = [(409, {})]
tick(state)
ks.kiro_event(cfg, event("flush", 202))
answers[:] = [(202, {}), (200, {"busy": True})]
tick(state)
body = requests[-2][2]
assert body["force"] is True and "look at the failing test" in body["text"] and "flush" not in body["text"]
assert calls("sendReceipt")[-1]["targetTimestamp"] == [201, 202]

# A turn that won't stop: one notice, and the message goes in later without force.
ks.save_pending([ks.queued("force: now", 301)])
answers[:] = [(504, {}), (200, {"busy": False})]
signal.clear()
tick(state)
assert ks.load_pending() == [{"text": "now", "ts": 301, "force": False}]
assert len(calls("send")) == 1 and "didn't stop" in calls("send")[0]["message"]

# "flush" with nothing queued puts nothing in, and is marked read.
ks.save_pending([ks.queued("flush", 401)])
requests.clear()
signal.clear()
tick(state)
assert requests == [] and ks.load_pending() == [] and calls("sendReceipt")[0]["targetTimestamp"] == [401]

# Refused: dropped, and said in words.
ks.save_pending([ks.queued("hello", 501)])
answers[:] = [(400, {"error": "text is empty or starts with /"})]
signal.clear()
tick(state)
assert ks.load_pending() == [] and "refused" in calls("send")[0]["message"]

print("kiro_signal self-check: ok")
