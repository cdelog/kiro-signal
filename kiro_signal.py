"""kiro-signal: talk to a Kiro chat from your phone, over Signal.

    signal-cli daemon --http   (one process; it alone holds the bot's Signal account)
      |-- GET  /api/v1/events --> relay --> your Kiro chat (kiro) or KiroCrew's webhook (crew)
      '-- POST /api/v1/rpc    <-- mcp: signal_reply(text), recipient fixed to the owner

Modes (standard library only, Python 3.9+):
    python kiro_signal.py mcp          stdio MCP server with one tool, signal_reply; Kiro starts it
    python kiro_signal.py kiro         relay the owner's messages into one Kiro IDE chat, through
                                       the bridge extension (bridge/)
    python kiro_signal.py crew         relay them to KiroCrew's inbound webhook instead
    python kiro_signal.py wait [secs]  print the owner's next message(s) and exit, for an agent
                                       that polls. Run only one of kiro, crew and wait at a time:
                                       each consumes what it reads.

State lives in ~/.kiro-signal (or $KIRO_SIGNAL_HOME): config.json, the bridge token, the queue
and the stream position. `owner` in config.json is both the ONLY sender the relays accept and
the ONLY recipient signal_reply can reach. install.py sets everything up; see INSTALL.md.
"""

from __future__ import annotations

import hashlib
import hmac
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
STATE_FILE = HOME / "last_event_id.txt"  # resume point, so a relay restart neither replays nor drops
PENDING_FILE = HOME / "pending.json"  # owner messages the Kiro chat hasn't taken yet
TOKEN_FILE = HOME / "bridge_token.txt"  # written by the bridge extension on its first start
CHUNK = 2000  # official clients turn longer bodies into a text attachment; stay under it
RETRY_DELAYS = (0, 5, 10, 20, 40) + (60,) * 9  # ~10 min of waiting out busy/capacity answers

DEFAULTS = {
    "signal_rpc": "http://127.0.0.1:39275",  # off the common dev ports; the bridge is 39276
    "hook_url": "http://127.0.0.1:5476/api/hooks/agent",
    "kiro_bridge": "http://127.0.0.1:39276",
}

FRAME_CREW = (
    "[Signal message from the owner, relayed by kiro-signal]\n"
    "{text}\n\n"
    "[Relay note] Answer by calling the signal_reply tool; nothing else reaches the owner. "
    "Signal shows plain text, so skip markdown. Before you finish, call register_hook with "
    'hook_id "signal" and a summary of this conversation so far (under 4000 characters), '
    "so the next Signal message has context."
)

# Starts with "[" on purpose: a prompt starting with "/" runs a slash command and aborts a turn.
FRAME_KIRO = (
    "[Signal message from the owner, relayed by kiro-signal]\n"
    "{text}\n\n"
    "[Relay note] The owner may be away from their desk, so answer with the signal_reply tool, "
    "in plain text without markdown. What you only write in this chat they may not see."
)
# "force" or "flush" alone, or followed by ":" or a new line, stops the running turn and puts
# everything queued in now. "Force push the branch" is an ordinary message.
FORCE = re.compile(r"(?i)^(force|flush)(?:\s*[:\n]|[.!]?\s*$)")
TYPING_EVERY = 10  # Signal hides a typing indicator about 15 s after the last one

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
    # stderr only: in mcp mode stdout carries the protocol.
    print(f"[kiro-signal] {msg}", file=sys.stderr, flush=True)


def load_config(require_owner: bool = True) -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        if require_owner:
            raise SystemExit(f"{CONFIG_FILE} is missing: run install.py (see INSTALL.md)")
    cfg["owner"] = str(cfg.get("owner") or "").strip()
    if require_owner and not cfg["owner"]:
        # Fail closed: an empty owner would leave the relay with no sender to accept.
        raise SystemExit(f"{CONFIG_FILE}: no 'owner' yet. Run: install.py owner --phrase <word>")
    return cfg


# ── signal-cli ──


def rpc(cfg: dict, method: str, params: dict):
    """One JSON-RPC call to the signal-cli daemon."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        cfg["signal_rpc"] + "/api/v1/rpc", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        reply = json.loads(resp.read() or b"{}")
    if reply.get("error"):
        raise RuntimeError(f"signal-cli {method}: {reply['error'].get('message')}")
    return reply.get("result")


def chunks(text: str, limit: int = CHUNK) -> list[str]:
    """Split text into Signal-sized pieces, preferring paragraph, then line, then word breaks."""
    out, text = [], text.strip()
    while len(text) > limit:
        cut = next(
            (i for sep in ("\n\n", "\n", " ") if (i := text.rfind(sep, 0, limit)) > limit // 2),
            limit,
        )
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return out + [text] if text else out


def send_to_owner(cfg: dict, text: str) -> int:
    parts = chunks(text)
    for part in parts:
        rpc(cfg, "send", {"recipient": [cfg["owner"]], "message": part})
    return len(parts)


# ── MCP server (stdio, newline-delimited JSON-RPC) ──


def mcp_handle(cfg: dict, req: dict) -> dict | None:
    """Answer one MCP request; None for notifications, which take no reply."""
    method, rid = req.get("method"), req.get("id")
    if rid is None:
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
        # Only `text` is read: whatever else the caller passes, the recipient stays the owner.
        text = str((params.get("arguments") or {}).get("text") or "").strip()
        try:
            if not text:
                raise ValueError("text is empty")
            sent = send_to_owner(cfg, text)
            result = {"content": [{"type": "text", "text": f"Sent {sent} Signal message(s) to the owner."}]}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": f"Signal send failed: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"not supported: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve_mcp(cfg: dict) -> None:
    # Binary streams: Windows text mode would re-encode and turn \n into \r\n.
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            reply = mcp_handle(cfg, json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.buffer.write(json.dumps(reply).encode() + b"\n")
            sys.stdout.buffer.flush()


# ── the event stream ──


def inbound_text(envelope: dict, owner: str) -> str | None:
    """The text of a direct message from the owner, or None for anything else."""
    sender = {str(envelope.get(k) or "").lower() for k in ("sourceUuid", "sourceNumber")}
    msg = envelope.get("dataMessage") or {}
    text = str(msg.get("message") or "").strip()
    # Groups are refused: a reply there would reach everyone in the group.
    if owner.lower() not in sender or msg.get("groupInfo") or not text:
        return None
    return text


def envelope_of(data: str) -> dict:
    """The envelope in one event's data. Raises on data that isn't JSON."""
    payload = json.loads(data)
    payload = payload.get("params", payload)  # tolerate a JSON-RPC notification wrapper
    return payload.get("envelope") or {}


def owner_message(cfg: dict, data: str) -> tuple[str, int | None] | None:
    """(text, Signal timestamp) of the owner's direct message in one event's data, or None."""
    envelope = envelope_of(data)
    text = inbound_text(envelope, cfg["owner"])
    if text is None:
        if (envelope.get("dataMessage") or {}).get("message"):
            log("ignored a message from someone else, or a group")
        return None
    return text, envelope.get("timestamp")


def parse_sse(lines):
    """Yield (id, data) for each server-sent event in an iterable of raw lines."""
    event_id, data = None, []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield event_id, "\n".join(data)
            event_id, data = None, []
            continue
        field, _, value = line.partition(":")  # a ':' comment line has an empty field
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            event_id = value
        elif field == "data":
            data.append(value)


def stream_events(cfg: dict, handle) -> None:
    """Feed every event's data to `handle`, forever, resuming after the last one handled."""
    while True:
        last_id = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""
        headers = {"Accept": "text/event-stream", **({"Last-Event-ID": last_id} if last_id else {})}
        req = urllib.request.Request(cfg["signal_rpc"] + "/api/v1/events", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for event_id, data in parse_sse(resp):
                    handle(data)
                    if event_id:
                        STATE_FILE.write_text(event_id)
        except Exception as exc:  # stream dropped, daemon restarting, read timeout
            log(f"event stream lost ({exc}); reconnecting in 5s")
        time.sleep(5)


# ── crew: signal-cli events -> KiroCrew inbound webhook ──


def sign(secret: str, ts: str, body: bytes) -> str:
    """KiroCrew webhook signature: HMAC-SHA256 over '{timestamp}.{raw body}'."""
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def post_hook(cfg: dict, token: str, secret: str, message: str) -> bool:
    body = json.dumps(
        {"message": message, "sessionKey": "hook:signal", "name": "Signal", "deliver": False}
    ).encode()
    url = cfg["hook_url"]
    parts = urllib.parse.urlsplit(url)
    for delay in RETRY_DELAYS:
        time.sleep(delay)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": f"{parts.scheme}://{parts.netloc}",
        }
        if secret:
            # Re-signed per attempt: the gateway refuses a signature it has already seen.
            ts = str(int(time.time()))
            headers.update({"X-KiroCrew-Timestamp": ts, "X-KiroCrew-Signature": sign(secret, ts, body)})
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=30).close()
            return True
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            if exc.code not in (409, 429, 503):  # only busy / capacity / switched-off are worth waiting out
                log(f"webhook refused the message ({exc.code}): {detail}")
                return False
            log(f"webhook busy ({exc.code}), retrying")
        except OSError as exc:
            log(f"webhook unreachable ({exc}), retrying")
    return False


def crew_event(cfg: dict, token: str, secret: str, data: str) -> None:
    # Never raises: an event that cannot be handled must not stall the stream on a retry loop.
    try:
        got = owner_message(cfg, data)
        if got is None:
            return
        try:
            rpc(cfg, "sendTyping", {"recipient": [cfg["owner"]]})
        except Exception:
            pass  # cosmetic
        if not post_hook(cfg, token, secret, FRAME_CREW.format(text=got[0])):
            send_to_owner(cfg, "KiroCrew did not accept that message, so it was dropped. Check the relay log.")
    except Exception as exc:
        log(f"could not relay an event: {exc}")


def run_crew(cfg: dict) -> None:
    token = os.environ.get("KIROCREW_WEBHOOK_TOKEN", "")
    secret = os.environ.get("KIROCREW_WEBHOOK_SIGNING_SECRET", "")
    if not token:
        raise SystemExit("KIROCREW_WEBHOOK_TOKEN is not set")
    log(f"relaying the owner's direct messages to {cfg['hook_url']}")
    stream_events(cfg, lambda data: crew_event(cfg, token, secret, data))


# ── kiro: signal-cli events -> a Kiro IDE chat, through the bridge extension ──
#
# Messages are queued in pending.json, so neither a busy chat nor a relay restart loses one, and
# a thread hands everything queued to the bridge as one prompt once the chat is between turns.
# The owner sees where a message is from Signal's own marks, never from status messages:
# delivered = queued, read = in the chat, typing = Kiro's turn is running.

_pending_lock = threading.Lock()


def load_pending() -> list[dict]:
    """Queued messages: {"text", "ts" (Signal timestamp, for the read receipt), "force"}."""
    try:
        items = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i if isinstance(i, dict) else {"text": i, "ts": None, "force": False} for i in items]


def save_pending(items: list[dict]) -> None:
    PENDING_FILE.write_text(json.dumps(items), encoding="utf-8")


def queued(text: str, ts: int | None) -> dict:
    """A message as queued: the force keyword comes off and sets `force`."""
    m = FORCE.match(text)
    return {"text": text[m.end():].strip() if m else text, "ts": ts, "force": bool(m)}


def kiro_event(cfg: dict, data: str) -> None:
    # Never raises, like crew_event.
    try:
        got = owner_message(cfg, data)
        if got is not None:
            with _pending_lock:
                save_pending(load_pending() + [queued(*got)])
    except Exception as exc:
        log(f"could not queue an event: {exc}")


def bridge(cfg: dict, token: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """One bridge request: (status, reply), or (0, {}) when nothing answers."""
    req = urllib.request.Request(
        cfg["kiro_bridge"] + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:  # a forced prompt waits for the stop
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except OSError:
        return 0, {}


def signal_quietly(cfg: dict, method: str, params: dict) -> None:
    try:
        rpc(cfg, method, params)
    except Exception as exc:  # a missed receipt or typing mark is cosmetic
        log(f"{method} failed: {exc}")


def settle_delivery(cfg: dict, items: list[dict], sent_text: bool, code: int, reply: dict, state: dict) -> None:
    """After the bridge's answer to `items`: update the queue and tell the owner, by receipt
    when they went in, in words only when something failed."""
    with _pending_lock:
        rest = load_pending()
        if code == 504:  # couldn't stop the turn: these go in when it ends, without force
            save_pending([{**i, "force": False} for i in rest[: len(items)]] + rest[len(items):])
        else:  # in, or refused: done either way; keep what arrived meanwhile
            save_pending(rest[len(items):])
    if code == 202:
        stamps = [i["ts"] for i in items if i.get("ts")]
        if stamps:
            signal_quietly(cfg, "sendReceipt", {"recipient": cfg["owner"], "targetTimestamp": stamps, "type": "read"})
        state["typing"], state["typed_at"] = sent_text, 0.0
    elif code == 504:
        send_to_owner(cfg, "Kiro didn't stop the running turn, so your message goes in when it ends.")
    else:
        log(f"bridge refused the message ({code}): {reply}")
        send_to_owner(cfg, f"The Kiro bridge refused that message ({code}: {reply.get('error')}), so it was dropped.")


def deliver_to_kiro(cfg: dict, token: str, state: dict) -> None:
    """One tick: hand everything queued to the chat if it will take it, and keep the typing
    indicator in step with the turn. `state` carries "typing" and "typed_at" between ticks."""
    with _pending_lock:
        items = load_pending()
    if items:
        texts = [i["text"] for i in items if i["text"]]
        force = any(i.get("force") for i in items)
        code, reply = 202, {}  # a keyword alone, with nothing queued, has nothing to put in
        if texts:
            body = {"session": cfg["kiro_session"], "text": FRAME_KIRO.format(text="\n\n".join(texts)), "force": force}
            code, reply = bridge(cfg, token, "POST", "/prompt", body)
        # 409 busy or 0 Kiro closed: they stay queued, shown as delivered but not read.
        if code not in (409, 0):
            settle_delivery(cfg, items, bool(texts), code, reply, state)

    if state.get("typing"):
        code, st = bridge(cfg, token, "GET", f"/status?session={cfg['kiro_session']}")
        if code == 200 and st.get("busy"):
            if time.monotonic() - state["typed_at"] >= TYPING_EVERY:
                signal_quietly(cfg, "sendTyping", {"recipient": [cfg["owner"]]})
                state["typed_at"] = time.monotonic()
        else:  # the turn ended, or Kiro went away
            signal_quietly(cfg, "sendTyping", {"recipient": [cfg["owner"]], "stop": True})
            state["typing"] = False


def run_kiro(cfg: dict) -> None:
    for key in ("kiro_bridge", "kiro_session"):
        if not cfg.get(key):
            raise SystemExit(f"{CONFIG_FILE}: no '{key}' yet. Run: install.py chat")
    if not TOKEN_FILE.exists():
        raise SystemExit(f"{TOKEN_FILE} is missing: open the Kiro window so the bridge writes it")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    def deliver_forever():
        state: dict = {"typing": False, "typed_at": 0.0}
        while True:
            try:
                deliver_to_kiro(cfg, token, state)
            except Exception as exc:
                log(f"delivery failed: {exc}")
            time.sleep(5)

    threading.Thread(target=deliver_forever, daemon=True).start()
    log(f"relaying the owner's direct messages to Kiro chat {cfg['kiro_session']}")
    stream_events(cfg, lambda data: kiro_event(cfg, data))


# ── wait: the owner's next message, for an agent that polls instead of being pushed to ──


def wait_events(cfg: dict, timeout: float, pick, settle: float = 3.0) -> list:
    """Whatever `pick(envelope)` returns (not None) from the next delivery, or [] once `timeout`
    seconds pass. After each pick it keeps reading for `settle` seconds, so a burst comes back
    together.
    ponytail: in on-connection mode the daemon acks a message to Signal before handing it over,
    so one delivered in the instant this closes the stream is lost. The relays have no such gap.
    """
    url = urllib.parse.urlsplit(cfg["signal_rpc"])
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=10)
    deadline, got = time.monotonic() + timeout, []

    def lines():
        while (left := deadline - time.monotonic()) > 0:
            conn.sock.settimeout(left)
            try:
                line = resp.readline()
            except OSError:  # read timeout, or the daemon went away
                return
            if not line:
                return
            yield line

    try:
        conn.request("GET", "/api/v1/events", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"signal-cli events: HTTP {resp.status}")
        for _, data in parse_sse(lines()):
            try:
                hit = pick(envelope_of(data))
            except ValueError:
                continue
            if hit is not None:
                got.append(hit)
                deadline = time.monotonic() + settle
    finally:
        conn.close()
    return got


def wait_owner(cfg: dict, timeout: float, settle: float = 3.0) -> list[str]:
    return wait_events(cfg, timeout, lambda env: inbound_text(env, cfg["owner"]), settle)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("mcp", "kiro", "crew", "wait"):
        raise SystemExit("usage: kiro_signal.py mcp|kiro|crew|wait [seconds]")
    cfg = load_config()
    if mode == "wait":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 600
        got = wait_owner(cfg, secs)
        # UTF-8 bytes: the Windows console code page would mangle emoji and accents.
        out = "\n\n".join(f"[Signal from the owner] {t}" for t in got) or f"No Signal message in {secs:g} s."
        sys.stdout.buffer.write(out.encode() + b"\n")
        return
    {"mcp": serve_mcp, "kiro": run_kiro, "crew": run_crew}[mode](cfg)


if __name__ == "__main__":
    main()
