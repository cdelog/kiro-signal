"""Self-check for install.py's offline parts. Run: python tests/test_install.py

Release asset choice, checksum verification, safe extraction, picking the chat, merging Kiro's
mcp.json, the VSIX layout, autostart files and the doctor's order. Nothing here touches the
network, signal-cli, Kiro or your real settings: every path points into a temporary folder."""

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
os.environ["KIRO_SIGNAL_HOME"] = str(TMP / "home")  # never the real ~/.kiro-signal
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # noqa: E402

install.MCP_FILE = TMP / "kiro" / "settings" / "mcp.json"
install.SESSIONS = TMP / "kiro" / "sessions"


def fails(fn, *args, contains=""):
    try:
        fn(*args)
    except install.Fail as exc:
        assert contains.lower() in str(exc).lower(), str(exc)
        return str(exc)
    raise AssertionError(f"{fn.__name__} did not fail")


# Release assets: the native build on Linux x86_64, the JVM build everywhere else.
release = {"tag_name": "v0.14.9", "assets": [{"name": n} for n in (
    "signal-cli-0.14.9.tar.gz", "signal-cli-0.14.9-Linux-native.tar.gz", "signal-cli-0.14.9.tar.gz.asc")]}
assert install.pick_asset(release, "Linux", "x86_64")["name"] == "signal-cli-0.14.9-Linux-native.tar.gz"
assert install.pick_asset(release, "Linux", "aarch64")["name"] == "signal-cli-0.14.9.tar.gz"
assert install.pick_asset(release, "Windows", "AMD64")["name"] == "signal-cli-0.14.9.tar.gz"
assert install.pick_asset(release, "Darwin", "arm64")["name"] == "signal-cli-0.14.9.tar.gz"
assert not install.needs_java("Linux", "amd64") and install.needs_java("Darwin", "x86_64")
fails(install.pick_asset, {"tag_name": "v9", "assets": []}, "Windows", "AMD64", contains="has no")


# Downloads are checked against GitHub's published SHA-256, and a mismatch leaves nothing behind.
def tarball(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


exe_name = "signal-cli.bat" if install.WINDOWS else "signal-cli"
good = tarball([(f"signal-cli-0.14.9/bin/{exe_name}", b"echo hi"), ("signal-cli-0.14.9/lib/a.jar", b"jar")])


class Files(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(good)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Files)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}/signal-cli-0.14.9.tar.gz"
asset = {"name": "signal-cli-0.14.9.tar.gz", "browser_download_url": url, "digest": "sha256:" + hashlib.sha256(good).hexdigest()}
path = install.download(asset, TMP / "dl")
assert path.read_bytes() == good
fails(install.download, {**asset, "digest": "sha256:" + "0" * 64}, TMP / "bad", contains="doesn't match")
assert not (TMP / "bad" / asset["name"]).exists()
fails(install.download, {**asset, "digest": None}, TMP / "bad", contains="no SHA-256")
server.shutdown()

# Extraction refuses entries that climb out of the target folder.
install.safe_extract(path, TMP / "x")
assert install.find_executable(TMP / "x").name == exe_name
evil = TMP / "evil.tar.gz"
evil.write_bytes(tarball([("../escaped.txt", b"no")]))
fails(install.safe_extract, evil, TMP / "y", contains="unsafe")
assert not (TMP / "escaped.txt").exists()

# On Windows, Java runs directly with the launcher's options and classpath, in order, from an
# argument file, because the .bat's one-line classpath overflows cmd on a long install path.
app = TMP / "app dir" / "signal-cli-0.14.9"
(app / "bin").mkdir(parents=True)
bat = app / "bin" / "signal-cli.bat"
bat.write_text('set DEFAULT_JVM_OPTS="--enable-native-access=ALL-UNNAMED"\r\n'
               "set CLASSPATH=%APP_HOME%\\lib\\signal-cli-0.14.9.jar;%APP_HOME%\\lib\\b.jar\r\n"
               'endlocal & "%JAVA_EXE%" %DEFAULT_JVM_OPTS% -classpath "%CLASSPATH%" org.asamk.signal.Main %*\r\n')
cmd = install.windows_java_cmd(bat, TMP / "jdk")
assert cmd == [str(TMP / "jdk" / "bin" / "java.exe"), f"@{app / 'kiro-signal.args'}"], cmd
lines = (app / "kiro-signal.args").read_text().splitlines()
lib = str(app).replace("\\", "/") + "/lib/"
assert lines == ["--enable-native-access=ALL-UNNAMED", "-classpath", f'"{lib}signal-cli-0.14.9.jar;{lib}b.jar"', "org.asamk.signal.Main"], lines
bat.write_text("@echo something else\r\n")
fails(install.windows_java_cmd, bat, TMP / "jdk", contains="unexpected shape")

# Registration checks the number's form before running anything.
fails(install.cmd_register, type("A", (), {"number": "5551234567", "voice": False, "captcha": None})(), contains="+15551234567")


# The chat: the only one mid-turn in this workspace is the chat running the installer.
def session(sid, workspace, status="idle", title="t"):
    d = install.SESSIONS / "store" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(json.dumps({"id": sid, "title": title, "status": status, "workspacePaths": [str(workspace)]}))


ws, other = TMP / "proj", TMP / "elsewhere"
A, B, C = (f"sess_{c * 8}-0000-4000-8000-{c * 12}" for c in "abc")
session(A, ws, "in_progress", "installing")
session(B, ws, "idle")
session(C, other, "in_progress")
chats = list(install.kiro_sessions())
assert install.pick_chat(chats, str(ws), None)["id"] == A
assert install.pick_chat(chats, str(ws), B)["id"] == B
fails(install.pick_chat, chats, str(TMP / "none"), None, contains="--session")
session(B, ws, "in_progress")
listing = fails(install.pick_chat, list(install.kiro_sessions()), str(ws), None, contains="more than one")
assert A in listing and B in listing
fails(install.pick_chat, chats, str(ws), "sess_nope", contains="no kiro chat")


# mcp.json: other servers are kept, a foreign 'signal' isn't overwritten without --force, and a
# second run changes nothing.
def mcp(name="signal", force=False):
    install.cmd_mcp(type("A", (), {"name": name, "force": force})())


install.MCP_FILE.parent.mkdir(parents=True)
install.MCP_FILE.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
mcp()
servers = json.loads(install.MCP_FILE.read_text())["mcpServers"]
assert servers["other"] == {"command": "x"} and install.is_ours(servers["signal"])
assert servers["signal"]["autoApprove"] == ["signal_reply"] and servers["signal"]["args"][-1] == "mcp"
before = install.MCP_FILE.read_text()
mcp()
assert install.MCP_FILE.read_text() == before
install.MCP_FILE.write_text(json.dumps({"mcpServers": {"signal": {"command": "someone-elses"}}}))
fails(mcp, contains="--force")
mcp(force=True)
assert install.is_ours(json.loads(install.MCP_FILE.read_text())["mcpServers"]["signal"])
install.MCP_FILE.write_text("{ // comments\n}")
fails(mcp, contains="by hand")
assert install.MCP_FILE.read_text() == "{ // comments\n}"

# The VSIX: manifest, content types, package.json, the code and the licence, named for the build.
for test_build, name in ((False, "kiro-signal-bridge"), (True, "kiro-signal-bridge-test")):
    vsix = install.build_vsix(TMP / "build", test=test_build)
    with zipfile.ZipFile(vsix) as z:
        assert sorted(z.namelist()) == sorted(["extension.vsixmanifest", "[Content_Types].xml", "extension/package.json",
                                               "extension/extension.js", "extension/LICENSE.txt"])
        pj = json.loads(z.read("extension/package.json"))
        manifest = z.read("extension.vsixmanifest").decode()
    assert pj["name"] == name and f'Id="{name}"' in manifest and 'Publisher="kiro-signal"' in manifest
    assert (pj.get("bridgePort") == 39277) == test_build
assert install.EXTENSION_ID == "kiro-signal." + json.loads((install.HERE / "bridge" / "package.json").read_text())["name"]

# Autostart, on each system: the same command, `install.py start`, with paths quoted.
py, script = "/opt/my python/bin/python3", "/home/me/kiro signal/install.py"
for windows, mac, needle in ((True, False, '" start'), (False, True, "<string>start</string>"), (False, False, 'Exec="')):
    install.WINDOWS, install.MAC = windows, mac
    text = install.autostart_text(py, script)
    assert needle in text and script in text, text
install.WINDOWS, install.MAC = os.name == "nt", sys.platform == "darwin"

# Process liveness: this process is alive, a finished one isn't.
assert install.pid_alive(os.getpid())
done = subprocess.Popen([sys.executable, "-c", "pass"])
done.wait()
assert not install.pid_alive(done.pid)

# start and stop: a detached process gets a PID file and a log, and stop ends it and its children.
pid = install.spawn("probe", [sys.executable, "-c", "import subprocess, sys, time; "
                              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                              "print('up', flush=True); time.sleep(60)"], note="kiro")
assert install.read_pid("probe") == pid and (install.RUN / "probe.pid").read_text() == f"{pid} kiro"
assert install.stop_pid("probe") and install.read_pid("probe") is None and not (install.RUN / "probe.pid").exists()
assert not install.stop_pid("probe")  # nothing left to stop

# The doctor walks the steps in install order: with nothing set up, the next step is signal-cli.
rows = install.checks({})
assert rows[0][0] == "ok" and rows[1] == ("todo", "signal-cli", "install.py signal-cli"), rows[:2]
assert next(r for r in rows if r[0] == "todo")[2] == "install.py signal-cli"

print("install self-check: ok")
