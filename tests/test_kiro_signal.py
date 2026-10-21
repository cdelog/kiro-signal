"""Run: python tests/test_kiro_signal.py. signal-cli and the bridge are stubbed out."""

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["KIRO_SIGNAL_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kiro_signal as ks  # noqa: E402

OWNER = "0f1e2d3c-aaaa-4bbb-8ccc-123456789abc"
STRANGER = "ffffffff-0000-4000-8000-000000000000"


def dm(text, **overrides):
    return {"sourceUuid": OWNER, "sourceNumber": None, "dataMessage": {"message": text}, **overrides}


# Only the owner's direct text messages get through.
assert ks.owners_text(dm(" hi "), OWNER) == "hi"
assert ks.owners_text(dm("hi"), OWNER.upper()) == "hi"
assert ks.owners_text({"sourceNumber": "+15550001111", "dataMessage": {"message": "hi"}}, "+15550001111") == "hi"
assert ks.owners_text(dm("hi", sourceUuid=STRANGER), OWNER) is None
assert ks.owners_text(dm("hi", dataMessage={"message": "hi", "groupInfo": {"groupId": "g"}}), OWNER) is None
assert ks.owners_text(dm("  "), OWNER) is None
assert ks.owners_text({"sourceUuid": OWNER, "syncMessage": {"sentMessage": {"message": "hi"}}}, OWNER) is None
assert ks.owners_text({}, OWNER) is None

# Every piece fits, and nothing but whitespace is lost.
text = ("word " * 900).strip() + "\n\n" + "x" * 2500
parts = ks.split_message(text)
assert len(parts) > 1 and all(len(p) <= ks.SIGNAL_MESSAGE_LIMIT for p in parts), [len(p) for p in parts]
assert "".join("".join(parts).split()) == "".join(text.split())
assert ks.split_message("short") == ["short"] and ks.split_message("  ") == []

# signal-cli writes no space after the colon, and ':' keep-alives.
stream = [b":\n", b"id:1-1\n", b"event:receive\n", b'data:{"a":1}\n', b"\n", b":\n", b"id: 1-2\n", b"data: x\n", b"\r\n"]
assert list(ks.parse_sse(stream)) == [("1-1", '{"a":1}'), ("1-2", "x")]

# No config, or no owner, and nothing runs.
for config in (None, {"owner": " "}):
    if config is not None:
        ks.CONFIG_FILE.write_text(json.dumps(config))
    try:
        ks.load_config()
        raise AssertionError(f"load_config ran with {config}")
    except SystemExit as exc:
        assert "install.py" in str(exc)
ks.CONFIG_FILE.unlink()

signal = []  # every signal-cli call: (method, params)
ks.rpc = lambda cfg, method, params: signal.append((method, params))
cfg = {"owner": OWNER, "kiro_session": "sess_x"}
init = ks.mcp_reply(cfg, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
assert init["result"]["capabilities"] == {"tools": {}} and init["result"]["protocolVersion"] == "2025-06-18"
assert ks.mcp_reply(cfg, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
assert [t["name"] for t in ks.mcp_reply(cfg, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]] == ["signal_reply"]


def call(arguments):
    return ks.mcp_reply(cfg, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "signal_reply", "arguments": arguments}})


# A recipient smuggled into the arguments changes nothing.
assert not call({"text": "hello", "recipient": "+19999999999"})["result"].get("isError")
assert signal == [("send", {"recipient": [OWNER], "message": "hello"})]
assert call({"text": "  "})["result"]["isError"]
assert ks.mcp_reply(cfg, {"jsonrpc": "2.0", "id": 5, "method": "resources/list"})["error"]["code"] == -32601


class Events(BaseHTTPRequestHandler):
    """A daemon that streams chunked server-sent events, as signal-cli does, then holds on."""

    protocol_version = "HTTP/1.1"
    script = []  # (seconds to wait, event text)

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
            pass

    def log_message(self, *args):
        pass


def sse(envelope):
    return "id:1\nevent:receive\ndata:" + json.dumps({"envelope": envelope}) + "\n\n"


server = ThreadingHTTPServer(("127.0.0.1", 0), Events)
threading.Thread(target=server.serve_forever, daemon=True).start()
daemon = {"signal_rpc": f"http://127.0.0.1:{server.server_port}"}
pairing = lambda env: env.get("sourceUuid") if "otter42" in str((env.get("dataMessage") or {}).get("message")).lower() else None  # noqa: E731

Events.script = [(0, ":\n"), (0.1, sse(dm("hello", sourceUuid=STRANGER))), (0.1, sse(dm("my word is Otter42")))]
start = time.monotonic()
assert ks.wait_for_event(daemon, 30, pairing) == OWNER
assert time.monotonic() - start < 3

Events.script = [(0, ":\n")]
start = time.monotonic()
assert ks.wait_for_event(daemon, 1, pairing) is None
assert 0.9 < time.monotonic() - start < 3
server.shutdown()

# ── the relay. The owner follows a message by Signal's marks: no notice while busy or closed, a
# read receipt once it's in the chat, typing while the turn runs, words only on a failure. ──
signal.clear()
answers, requests = [], []  # the bridge's answers to hand out, and the requests it got


def fake_bridge(cfg, token, method, path, body=None):
    requests.append((method, path, body))
    return answers.pop(0)


ks.bridge = fake_bridge


def event(text, ts, sender=OWNER):
    return json.dumps({"envelope": {"sourceUuid": sender, "timestamp": ts, "dataMessage": {"message": text, "timestamp": ts}}})


def tick(state):
    ks.deliver(cfg, "t", state)


def calls(method):
    return [p for m, p in signal if m == method]


for text, force, rest in [
    ("flush", True, ""), ("Force!", True, ""), ("force: stop and look at CI", True, "stop and look at CI"),
    ("FLUSH:\nnew plan", True, "new plan"), ("force push the branch", False, "force push the branch"),
    ("forceful", False, "forceful"), ("please flush", False, "please flush"),
]:
    q = ks.queued(text, 1)
    assert (q["force"], q["text"]) == (force, rest), (text, q)

ks.enqueue(cfg, event("first", 101))
ks.enqueue(cfg, event("hi", 102, sender=STRANGER))
ks.enqueue(cfg, event("second", 103))
ks.enqueue(cfg, "not json")
assert [(i["text"], i["ts"]) for i in ks.load_queue()] == [("first", 101), ("second", 103)]
assert signal == []

# Busy, then Kiro closed: they wait, silently.
state = {"typing": False, "typed_at": 0.0}
answers[:] = [(409, {}), (0, {})]
tick(state)
tick(state)
assert signal == [] and len(ks.load_queue()) == 2

# In: one prompt, both marked read, then typing while the turn runs, stopped when it ends.
answers[:] = [(202, {}), (200, {"busy": True})]
tick(state)
method, path, body = requests[-2]
assert (method, path, body["force"]) == ("POST", "/prompt", False)
assert body["text"].startswith("[") and "first\n\nsecond" in body["text"]
assert calls("sendReceipt") == [{"recipient": OWNER, "targetTimestamp": [101, 103], "type": "read"}]
assert calls("sendTyping") == [{"recipient": [OWNER]}] and ks.load_queue() == []
answers[:] = [(200, {"busy": True})]
tick(state)
assert len(calls("sendTyping")) == 1
state["typed_at"] -= ks.TYPING_REFRESH_SECONDS
answers[:] = [(200, {"busy": True})]
tick(state)
assert len(calls("sendTyping")) == 2
answers[:] = [(200, {"busy": False})]
tick(state)
assert calls("sendTyping")[-1] == {"recipient": [OWNER], "stop": True} and not state["typing"]
assert calls("send") == []

# A message mid-turn waits; "flush" puts it in, forced, and both are marked read.
signal.clear()
ks.enqueue(cfg, event("look at the failing test", 201))
answers[:] = [(409, {})]
tick(state)
ks.enqueue(cfg, event("flush", 202))
answers[:] = [(202, {}), (200, {"busy": True})]
tick(state)
body = requests[-2][2]
assert body["force"] is True and "look at the failing test" in body["text"] and "flush" not in body["text"]
assert calls("sendReceipt")[-1]["targetTimestamp"] == [201, 202]

# A turn that won't stop: one notice, and the message waits for the turn, no longer forced.
ks.save_queue([ks.queued("force: now", 301)])
answers[:] = [(504, {}), (200, {"busy": False})]
signal.clear()
tick(state)
assert ks.load_queue() == [{"text": "now", "ts": 301, "force": False}]
assert len(calls("send")) == 1 and "didn't stop" in calls("send")[0]["message"]

# "flush" with nothing queued puts nothing in, and is marked read.
ks.save_queue([ks.queued("flush", 401)])
requests.clear()
signal.clear()
tick(state)
assert requests == [] and ks.load_queue() == [] and calls("sendReceipt")[0]["targetTimestamp"] == [401]

# Refused: dropped, and said in words.
ks.save_queue([ks.queued("hello", 501)])
answers[:] = [(400, {"error": "text is empty or starts with /"})]
signal.clear()
tick(state)
assert ks.load_queue() == [] and "refused" in calls("send")[0]["message"]

print("kiro_signal: ok")
