"""Run: python tests/test_install.py. No network, signal-cli or Kiro, and every path is temporary."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["KIRO_SIGNAL_HOME"] = str(TMP / "home")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # noqa: E402

install.MCP_FILE = TMP / "kiro" / "settings" / "mcp.json"
install.KIRO_SESSIONS = TMP / "kiro" / "sessions"


def fails(fn, *args, says=""):
    try:
        fn(*args)
    except install.Fail as exc:
        assert says.lower() in str(exc).lower(), str(exc)
        return str(exc)
    raise AssertionError(f"{fn.__name__} didn't fail")


def tarball(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# A download must match the SHA-256 GitHub publishes, and a mismatch leaves nothing behind.
good = tarball([("signal-cli-0.14.9/bin/signal-cli", b"echo hi")])


class Files(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(good)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Files)
threading.Thread(target=server.serve_forever, daemon=True).start()
asset = {"name": "signal-cli-0.14.9.tar.gz", "browser_download_url": f"http://127.0.0.1:{server.server_port}/a", "digest": "sha256:" + hashlib.sha256(good).hexdigest()}
assert install.download(asset, TMP / "dl").read_bytes() == good
fails(install.download, {**asset, "digest": "sha256:" + "0" * 64}, TMP / "bad", says="doesn't match")
assert not (TMP / "bad" / asset["name"]).exists()
fails(install.download, {**asset, "digest": None}, TMP / "bad", says="no SHA-256")
server.shutdown()

# Extraction refuses entries that climb out of the folder.
install.extract(TMP / "dl" / asset["name"], TMP / "x")
assert (TMP / "x" / "signal-cli-0.14.9" / "bin" / "signal-cli").exists()
(TMP / "evil.tar.gz").write_bytes(tarball([("../escaped.txt", b"no")]))
fails(install.extract, TMP / "evil.tar.gz", TMP / "y", says="unsafe")
assert not (TMP / "escaped.txt").exists()

# On Windows, Java runs from an argument file holding the launcher's options, classpath and main
# class, in order.
app = TMP / "app dir" / "signal-cli-0.14.9"
(app / "bin").mkdir(parents=True)
bat = app / "bin" / "signal-cli.bat"
bat.write_text('set DEFAULT_JVM_OPTS="--enable-native-access=ALL-UNNAMED"\r\n'
               "set CLASSPATH=%APP_HOME%\\lib\\signal-cli-0.14.9.jar;%APP_HOME%\\lib\\b.jar\r\n"
               'endlocal & "%JAVA_EXE%" %DEFAULT_JVM_OPTS% -classpath "%CLASSPATH%" org.asamk.signal.Main %*\r\n')
assert install.java_command(bat, TMP / "jdk") == [str(TMP / "jdk" / "bin" / "java.exe"), f"@{app / 'kiro-signal.args'}"]
lib = str(app).replace("\\", "/") + "/lib/"
assert (app / "kiro-signal.args").read_text().splitlines() == [
    "--enable-native-access=ALL-UNNAMED", "-classpath", f'"{lib}signal-cli-0.14.9.jar;{lib}b.jar"', "org.asamk.signal.Main"]
bat.write_text("@echo something else\r\n")
fails(install.java_command, bat, TMP / "jdk", says="unexpected shape")

fails(install.cmd_register, type("Args", (), {"number": "5551234567", "voice": False, "captcha": None})(), says="+15551234567")


# The chat is the one named, or else the only chat in the workspace running a turn.
def chat(sid, workspace, status="idle"):
    folder = install.KIRO_SESSIONS / "store" / sid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "session.json").write_text(json.dumps({"id": sid, "title": sid[:6], "status": status, "workspacePaths": [str(workspace)]}))


ws = TMP / "project"
A, B, C = (f"sess_{c * 8}-0000-4000-8000-{c * 12}" for c in "abc")
chat(A, ws, "in_progress")
chat(B, ws)
chat(C, TMP / "elsewhere", "in_progress")
assert install.pick_chat(install.kiro_chats(), str(ws), None)["id"] == A
assert install.pick_chat(install.kiro_chats(), str(ws), B)["id"] == B
fails(install.pick_chat, install.kiro_chats(), str(TMP / "none"), None, says="--session")
fails(install.pick_chat, install.kiro_chats(), str(ws), "sess_nope", says="no kiro chat")
chat(B, ws, "in_progress")
listing = fails(install.pick_chat, install.kiro_chats(), str(ws), None, says="more than one")
assert A in listing and B in listing


# mcp.json keeps other servers, won't replace a different 'signal' without --force, and leaves a
# file it can't parse alone.
def mcp(name="signal", force=False):
    install.cmd_mcp(type("Args", (), {"name": name, "force": force})())


install.MCP_FILE.parent.mkdir(parents=True)
install.MCP_FILE.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
mcp()
servers = json.loads(install.MCP_FILE.read_text())["mcpServers"]
assert servers["other"] == {"command": "x"} and install.is_ours(servers["signal"]) and servers["signal"]["autoApprove"] == ["signal_reply"]
before = install.MCP_FILE.read_text()
mcp()
assert install.MCP_FILE.read_text() == before
install.MCP_FILE.write_text(json.dumps({"mcpServers": {"signal": {"command": "someone-elses"}}}))
fails(mcp, says="--force")
mcp(force=True)
assert install.is_ours(json.loads(install.MCP_FILE.read_text())["mcpServers"]["signal"])
install.MCP_FILE.write_text("{ // comments\n}")
fails(mcp, says="by hand")
assert install.MCP_FILE.read_text() == "{ // comments\n}"

# The VSIX holds what Kiro needs, under the id uninstall removes.
with zipfile.ZipFile(install.build_vsix(TMP / "build")) as z:
    assert sorted(z.namelist()) == sorted(["extension.vsixmanifest", "[Content_Types].xml", "extension/package.json", "extension/extension.js", "extension/LICENSE.txt"])
    package = json.loads(z.read("extension/package.json"))
assert install.EXTENSION_ID == f"{package['publisher']}.{package['name']}"

# Each system's autostart entry runs `install.py start`, with paths quoted.
python, script = "/opt/my python/bin/python3", "/home/me/kiro signal/install.py"
for windows, mac, expected in ((True, False, '" start'), (False, True, "<string>start</string>"), (False, False, 'Exec="')):
    install.WINDOWS, install.MAC = windows, mac
    entry = install.autostart_entry(python, script)
    assert expected in entry and script in entry, entry
install.WINDOWS, install.MAC = os.name == "nt", sys.platform == "darwin"

# A detached process gets a PID file and a log, and stop ends it and its children.
assert install.pid_alive(os.getpid())
done = subprocess.Popen([sys.executable, "-c", "pass"])
done.wait()
assert not install.pid_alive(done.pid)
child = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
pid = install.spawn("probe", [sys.executable, "-c", child])
assert install.running("probe") == pid
assert install.stop("probe") and install.running("probe") is None and not install.stop("probe")

# With nothing set up, the doctor's next step is signal-cli.
rows = install.steps({})
assert rows[0][0] and rows[1] == (False, "signal-cli", "install.py signal-cli")

print("install: ok")
