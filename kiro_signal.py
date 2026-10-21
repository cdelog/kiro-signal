"""Relays your Signal messages into a Kiro chat, and gives the chat a signal_reply tool.

    python kiro_signal.py relay   the owner's messages go into the Kiro chat in config.json
    python kiro_signal.py mcp     stdio MCP server with one tool, signal_reply
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "1.0.0"
HOME = Path(os.environ.get("KIRO_SIGNAL_HOME") or Path.home() / ".kiro-signal")
CONFIG_FILE = HOME / "config.json"
STREAM_POSITION_FILE = HOME / "last_event_id.txt"
QUEUE_FILE = HOME / "pending.json"
BRIDGE_TOKEN_FILE = HOME / "bridge_token.txt"
DEFAULTS = {"signal_rpc": "http://127.0.0.1:39275", "kiro_bridge": "http://127.0.0.1:39276"}

# Signal's apps turn a longer message into a text attachment.
SIGNAL_MESSAGE_LIMIT = 2000
# Signal hides a typing indicator about 15 s after the last one.
TYPING_REFRESH_SECONDS = 10
# "force" or "flush" alone, or followed by ":" or a new line. "Force push the branch" isn't one.
FORCE = re.compile(r"(?i)^(force|flush)(?:\s*[:\n]|[.!]?\s*$)")
# Starts with "[": Kiro runs a prompt that starts with "/" as a slash command.
PROMPT = (
    "[Signal message from the owner, relayed by kiro-signal]\n"
    "{text}\n\n"
    "[Relay note] The owner may be away from their desk, so answer with the signal_reply tool, "
    "in plain text without markdown. What you only write in this chat they may not see."
)
TOOL = {
    "name": "signal_reply",
    "description": (
        "Send a Signal message to the owner, the only recipient this tool can reach. "
        "Plain text; long text is split into several messages."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The message to send"}},
        "required": ["text"],
    },
}


def log(msg: str) -> None:
    print(f"[kiro-signal] {msg}", file=sys.stderr, flush=True)  # stdout carries the MCP protocol


def load_config() -> dict:
    try:
        cfg = {**DEFAULTS, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
    except FileNotFoundError:
        raise SystemExit(f"{CONFIG_FILE} is missing: run install.py (see INSTALL.md)")
    cfg["owner"] = str(cfg.get("owner") or "").strip()
    if not cfg["owner"]:
        raise SystemExit(f"{CONFIG_FILE} has no owner yet: run install.py owner --phrase <word>")
    return cfg


# ── signal-cli ──


def rpc(cfg: dict, method: str, params: dict):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(cfg["signal_rpc"] + "/api/v1/rpc", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        reply = json.loads(resp.read() or b"{}")
    if reply.get("error"):
        raise RuntimeError(f"signal-cli {method}: {reply['error'].get('message')}")
    return reply.get("result")


def rpc_or_log(cfg: dict, method: str, params: dict) -> None:
    try:
        rpc(cfg, method, params)
    except Exception as exc:
        log(f"{method} failed: {exc}")


def split_message(text: str, limit: int = SIGNAL_MESSAGE_LIMIT) -> list[str]:
    """Pieces of at most `limit` characters, cut at a paragraph, then a line, then a word."""
    out, text = [], text.strip()
    while len(text) > limit:
        cut = next((i for sep in ("\n\n", "\n", " ") if (i := text.rfind(sep, 0, limit)) > limit // 2), limit)
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return out + [text] if text else out


def send_to_owner(cfg: dict, text: str) -> int:
    parts = split_message(text)
    for part in parts:
        rpc(cfg, "send", {"recipient": [cfg["owner"]], "message": part})
    return len(parts)


def owners_text(envelope: dict, owner: str) -> str | None:
    """The text of a direct message from `owner`. Groups are refused: a reply would reach everyone."""
    senders = {str(envelope.get(k) or "").lower() for k in ("sourceUuid", "sourceNumber")}
    msg = envelope.get("dataMessage") or {}
    text = str(msg.get("message") or "").strip()
    return text if owner.lower() in senders and not msg.get("groupInfo") and text else None


def envelope_of(event_data: str) -> dict:
    payload = json.loads(event_data)
    return payload.get("params", payload).get("envelope") or {}


def parse_sse(lines):
    """(id, data) for each server-sent event."""
    event_id, data = None, []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield event_id, "\n".join(data)
            event_id, data = None, []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            event_id = value
        elif field == "data":
            data.append(value)


def follow_events(cfg: dict, handle) -> None:
    """Calls handle(event_data) forever, resuming after the last event handled."""
    while True:
        last = STREAM_POSITION_FILE.read_text().strip() if STREAM_POSITION_FILE.exists() else ""
        headers = {"Accept": "text/event-stream", **({"Last-Event-ID": last} if last else {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(cfg["signal_rpc"] + "/api/v1/events", headers=headers), timeout=300) as resp:
                for event_id, data in parse_sse(resp):
                    handle(data)
                    if event_id:
                        STREAM_POSITION_FILE.write_text(event_id)
        except Exception as exc:
            log(f"event stream lost ({exc}); reconnecting in 5 s")
        time.sleep(5)


def wait_for_event(cfg: dict, timeout: float, pick):
    """The first non-None pick(envelope) from the event stream, or None after `timeout` seconds."""
    url = urllib.parse.urlsplit(cfg["signal_rpc"])
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=10)
    deadline = time.monotonic() + timeout
    try:
        conn.request("GET", "/api/v1/events", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"signal-cli events: HTTP {resp.status}")

        def lines():
            while (left := deadline - time.monotonic()) > 0:
                conn.sock.settimeout(left)
                try:
                    line = resp.readline()
                except OSError:
                    return
                if not line:
                    return
                yield line

        for _, data in parse_sse(lines()):
            try:
                hit = pick(envelope_of(data))
            except ValueError:
                continue
            if hit is not None:
                return hit
    finally:
        conn.close()
    return None


# ── mcp ──


def mcp_reply(cfg: dict, req: dict) -> dict | None:
    method, rid = req.get("method"), req.get("id")
    if rid is None:  # a notification
        return None
    params = req.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "kiro-signal", "version": VERSION},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call" and params.get("name") == TOOL["name"]:
        # Only `text` is read, so no argument can change the recipient.
        text = str((params.get("arguments") or {}).get("text") or "").strip()
        try:
            if not text:
                raise ValueError("text is empty")
            result = {"content": [{"type": "text", "text": f"Sent {send_to_owner(cfg, text)} Signal message(s) to the owner."}]}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": f"Signal send failed: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"not supported: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve_mcp(cfg: dict) -> None:
    for line in sys.stdin.buffer:  # bytes, so Windows doesn't turn \n into \r\n
        if not line.strip():
            continue
        try:
            reply = mcp_reply(cfg, json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.buffer.write(json.dumps(reply).encode() + b"\n")
            sys.stdout.buffer.flush()


# ── relay ──
#
# The owner follows a message by Signal's own marks: delivered means queued, read means it's in
# the chat, typing means the turn is running. Words come only when something fails.

_queue_lock = threading.Lock()


def load_queue() -> list[dict]:
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_queue(items: list[dict]) -> None:
    QUEUE_FILE.write_text(json.dumps(items), encoding="utf-8")


def queued(text: str, timestamp: int | None) -> dict:
    force = FORCE.match(text)
    return {"text": text[force.end():].strip() if force else text, "ts": timestamp, "force": bool(force)}


def enqueue(cfg: dict, event_data: str) -> None:
    try:
        envelope = envelope_of(event_data)
        text = owners_text(envelope, cfg["owner"])
        if text is None:
            if (envelope.get("dataMessage") or {}).get("message"):
                log("ignored a message from someone else, or a group")
            return
        with _queue_lock:
            save_queue(load_queue() + [queued(text, envelope.get("timestamp"))])
    except Exception as exc:  # one bad event mustn't stop the stream
        log(f"could not queue an event: {exc}")


def bridge(cfg: dict, token: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """(status, reply), or (0, {}) when the Kiro window isn't open."""
    req = urllib.request.Request(
        cfg["kiro_bridge"] + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:  # a forced prompt waits up to 60 s for the stop
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except OSError:
        return 0, {}


def deliver(cfg: dict, token: str, state: dict) -> None:
    """Hands everything queued to the chat as one prompt, when it will take it, and keeps the
    typing indicator in step with the turn."""
    with _queue_lock:
        items = load_queue()
    if items:
        texts = [i["text"] for i in items if i["text"]]
        code, reply = 202, {}  # "flush" alone has nothing to put in, and is done
        if texts:
            body = {"session": cfg["kiro_session"], "text": PROMPT.format(text="\n\n".join(texts)), "force": any(i["force"] for i in items)}
            code, reply = bridge(cfg, token, "POST", "/prompt", body)
        if code not in (409, 0):  # busy, or Kiro closed: they wait
            with _queue_lock:
                rest = load_queue()[len(items):]
                if code == 504:  # the turn didn't stop: they go in when it ends
                    rest = [{**i, "force": False} for i in items] + rest
                save_queue(rest)
            if code == 202:
                stamps = [i["ts"] for i in items if i.get("ts")]
                if stamps:
                    rpc_or_log(cfg, "sendReceipt", {"recipient": cfg["owner"], "targetTimestamp": stamps, "type": "read"})
                state.update(typing=bool(texts), typed_at=0.0)
            elif code == 504:
                send_to_owner(cfg, "Kiro didn't stop the running turn, so your message goes in when it ends.")
            else:
                log(f"bridge refused the message ({code}): {reply}")
                send_to_owner(cfg, f"The Kiro bridge refused that message ({code}: {reply.get('error')}), so it was dropped.")

    if state.get("typing"):
        code, status = bridge(cfg, token, "GET", f"/status?session={cfg['kiro_session']}")
        if code == 200 and status.get("busy"):
            if time.monotonic() - state["typed_at"] >= TYPING_REFRESH_SECONDS:
                rpc_or_log(cfg, "sendTyping", {"recipient": [cfg["owner"]]})
                state["typed_at"] = time.monotonic()
        else:
            rpc_or_log(cfg, "sendTyping", {"recipient": [cfg["owner"]], "stop": True})
            state["typing"] = False


def run_relay(cfg: dict) -> None:
    if not cfg.get("kiro_session"):
        raise SystemExit(f"{CONFIG_FILE} has no chat yet: run install.py chat")
    if not BRIDGE_TOKEN_FILE.exists():
        raise SystemExit(f"{BRIDGE_TOKEN_FILE} is missing: run install.py bridge, and keep that Kiro window open")
    token = BRIDGE_TOKEN_FILE.read_text(encoding="utf-8").strip()

    def deliver_every_5_seconds():
        state = {"typing": False, "typed_at": 0.0}
        while True:
            try:
                deliver(cfg, token, state)
            except Exception as exc:
                log(f"delivery failed: {exc}")
            time.sleep(5)

    threading.Thread(target=deliver_every_5_seconds, daemon=True).start()
    log(f"relaying the owner's messages to Kiro chat {cfg['kiro_session']}")
    follow_events(cfg, lambda data: enqueue(cfg, data))


def main() -> None:
    modes = {"relay": run_relay, "mcp": serve_mcp}
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in modes:
        raise SystemExit("usage: kiro_signal.py relay|mcp")
    modes[mode](load_config())


if __name__ == "__main__":
    main()
