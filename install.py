"""Sets up kiro-signal, one step per command. Every step is safe to rerun and never prompts, so an
AI agent can run them all. Run `install.py doctor` for the next step, and see INSTALL.md."""

from __future__ import annotations

import argparse
import glob
import hashlib
import html
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import kiro_signal as ks

HERE = Path(__file__).resolve().parent
HOME = ks.HOME
LOGS = HOME / "logs"
PIDS = HOME / "run"
SIGNAL_DATA = HOME / "signal-data"
PIN_FILE = HOME / "registration-lock-pin.txt"
MCP_FILE = Path.home() / ".kiro" / "settings" / "mcp.json"
KIRO_SESSIONS = Path.home() / ".kiro" / "sessions"
LATEST_RELEASE = "https://api.github.com/repos/AsamK/signal-cli/releases/latest"
CAPTCHA_PAGE = "https://signalcaptchas.org/registration/generate.html"
EXTENSION_ID = "kiro-signal.kiro-signal-bridge"
JAVA_MIN = 25
# Signal stops accepting signal-cli releases older than about three months.
SIGNAL_CLI_MAX_AGE_DAYS = 75
WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"


class Fail(SystemExit):
    def __init__(self, what_to_do: str):
        super().__init__(f"error: {what_to_do}")


def read_cfg() -> dict:
    try:
        return json.loads(ks.CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def write_cfg(**changes) -> dict:
    cfg = {**read_cfg(), **changes}
    HOME.mkdir(parents=True, exist_ok=True)
    ks.CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


def with_defaults(cfg: dict) -> dict:
    return {**ks.DEFAULTS, **cfg}


def same_path(a, b) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


# ── signal-cli ──


def java_version(java: Path) -> int | None:
    try:
        out = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.search(r'version "(\d+)', out.stderr + out.stdout)
    return int(found.group(1)) if found else None


def find_java() -> tuple[Path, int] | None:
    """The newest Java home at JAVA_MIN or later. Also searches the usual install folders, because
    a Java installed a minute ago isn't on this terminal's PATH yet."""
    on_path = shutil.which("java")
    homes = [os.environ.get("JAVA_HOME"), on_path and Path(on_path).resolve().parent.parent]
    if WINDOWS:
        patterns = ["C:/Program Files/Eclipse Adoptium/*", "C:/Program Files/Microsoft/jdk-*", "C:/Program Files/Java/jdk-*", "C:/Program Files/Zulu/zulu-*"]
    elif MAC:
        patterns = ["/Library/Java/JavaVirtualMachines/*/Contents/Home", "/opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
                    "/usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home"]
    else:
        patterns = ["/usr/lib/jvm/*"]
    homes += [home for pattern in patterns for home in glob.glob(pattern)]
    best = None
    for home in {Path(h) for h in homes if h}:
        version = java_version(home / "bin" / ("java.exe" if WINDOWS else "java"))
        if version and version >= JAVA_MIN and (best is None or version > best[1]):
            best = (home, version)
    return best


def download(asset: dict, folder: Path) -> Path:
    """Downloads a release asset, refusing it unless it matches the SHA-256 GitHub publishes."""
    expected = str(asset.get("digest") or "")
    if not expected.startswith("sha256:"):
        raise Fail(f"GitHub published no SHA-256 for {asset['name']}, so it can't be verified")
    folder.mkdir(parents=True, exist_ok=True)
    path, sha = folder / asset["name"], hashlib.sha256()
    with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as resp, open(path, "wb") as out:
        while block := resp.read(1 << 20):
            sha.update(block)
            out.write(block)
    if sha.hexdigest() != expected.split(":", 1)[1]:
        path.unlink()
        raise Fail(f"{asset['name']} doesn't match its published SHA-256; nothing was installed")
    return path


def extract(archive: Path, folder: Path) -> None:
    """Refuses any entry that would land outside `folder`."""
    with tarfile.open(archive) as tf:
        if hasattr(tarfile, "data_filter"):
            try:
                tf.extractall(folder, filter="data")
            except tarfile.FilterError as exc:
                raise Fail(f"{archive.name} has an unsafe entry: {exc}")
            return
        root = folder.resolve()
        for member in tf.getmembers():
            target = (folder / member.name).resolve()
            if member.issym() or member.islnk() or not (target == root or root in target.parents):
                raise Fail(f"{archive.name} has an unsafe entry: {member.name}")
        tf.extractall(folder)


def java_command(launcher: Path, java_home: Path) -> list[str]:
    """Java with the options, classpath and main class from signal-cli's launcher script.

    On Windows the launcher puts all ~80 jars on one line, which passes cmd's 8,191-character limit
    once the install path is long, so the classpath goes in a Java argument file instead.
    ponytail: Java reads that file in the system code page, so a home folder with non-ASCII
    characters may need KIRO_SIGNAL_HOME set to an ASCII path."""
    app = launcher.parent.parent
    script = launcher.read_text(encoding="utf-8", errors="replace")
    classpath = re.search(r"^set CLASSPATH=(.+)$", script, re.M)
    main_class = re.search(r'-classpath "%CLASSPATH%" (\S+)', script)
    if not (classpath and main_class):
        raise Fail(f"{launcher.name} has an unexpected shape; please open an issue naming the signal-cli version")
    jvm_options = re.search(r"^set DEFAULT_JVM_OPTS=(.*)$", script, re.M)
    jars = [j.replace("%APP_HOME%", str(app)).replace("\\", "/") for j in classpath.group(1).strip().split(";") if j]
    argfile = app / "kiro-signal.args"
    lines = [*re.findall(r'"([^"]+)"', jvm_options.group(1) if jvm_options else ""), "-classpath", '"' + ";".join(jars) + '"', main_class.group(1)]
    argfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [str(java_home / "bin" / "java.exe"), f"@{argfile}"]


def signal_cli_command(cfg: dict) -> tuple[list[str], dict]:
    command = cfg.get("signal_cli_command") or []
    if not command or not Path(command[0]).exists():
        raise Fail("signal-cli isn't installed yet. Run: install.py signal-cli")
    return [*command, "--config", str(SIGNAL_DATA)], {**os.environ, "JAVA_HOME": cfg["java_home"]}


def run_signal_cli(cfg: dict, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    command, env = signal_cli_command(cfg)
    return subprocess.run([*command, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)


def cmd_signal_cli(args) -> None:
    java = find_java()
    if not java:
        install_java = {
            "nt": "winget install --id EclipseAdoptium.Temurin.25.JDK -e",
            "mac": "brew install openjdk",
            "linux": "your distribution's Java 25 package, for example: sudo apt install openjdk-25-jre-headless",
        }["nt" if WINDOWS else "mac" if MAC else "linux"]
        raise Fail(f"signal-cli needs Java {JAVA_MIN} or newer, and none was found. Install it with {install_java}, then rerun: install.py signal-cli")
    with urllib.request.urlopen(urllib.request.Request(LATEST_RELEASE, headers={"Accept": "application/vnd.github+json"}), timeout=30) as resp:
        release = json.loads(resp.read())
    version = release["tag_name"].lstrip("v")
    cfg = read_cfg()
    if cfg.get("signal_cli_version") == version and not args.force:
        print(f"signal-cli {version} is already installed, the latest release.")
        return
    name = f"signal-cli-{version}.tar.gz"
    asset = next((a for a in release.get("assets", []) if a["name"] == name), None)
    if not asset:
        raise Fail(f"signal-cli {version} has no {name}; see https://github.com/AsamK/signal-cli/releases")
    print(f"Downloading {name} ({asset['size'] >> 20} MB)...", flush=True)
    archive = download(asset, HOME / "downloads")
    folder = HOME / "signal-cli" / version
    shutil.rmtree(folder, ignore_errors=True)
    extract(archive, folder)
    archive.unlink()
    launcher = next(folder.glob("*/bin/" + ("signal-cli.bat" if WINDOWS else "signal-cli")), None)
    if not launcher:
        raise Fail(f"{name} has no launcher in bin/; please open an issue naming the signal-cli version")
    if WINDOWS:
        command = java_command(launcher, java[0])
    else:
        launcher.chmod(launcher.stat().st_mode | 0o111)
        command = [str(launcher)]
    cfg = write_cfg(signal_cli_command=command, signal_cli_version=version, signal_cli_released=release["published_at"][:10], java_home=str(java[0]))
    check = run_signal_cli(cfg, "--version", timeout=120)
    if check.returncode != 0:
        raise Fail(f"signal-cli {version} installed but doesn't run:\n{(check.stdout + check.stderr).strip()}")
    print(f"Installed signal-cli {version}, running on Java {java[1]} at {java[0]}.")
    if running("daemon"):
        print("The daemon still runs the old release: install.py stop, then install.py start.")
    else:
        for old in (HOME / "signal-cli").iterdir():
            if old.name != version:
                shutil.rmtree(old, ignore_errors=True)
        if not cfg.get("registered"):
            print("Next: install.py register --number <the bot's number>")


# ── the bot's Signal account ──


def cmd_register(args) -> None:
    number = args.number.strip().replace(" ", "")
    if not re.fullmatch(r"\+[1-9]\d{6,14}", number):
        raise Fail("--number must be in international form, for example +15551234567")
    cfg = read_cfg()
    if daemon_up(with_defaults(cfg)):
        raise Fail("the daemon is running. Run install.py stop first")
    result = run_signal_cli(cfg, "-a", number, "register", *(["--voice"] if args.voice else []), *(["--captcha", args.captcha.strip()] if args.captcha else []))
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        if re.search(r"captcha", output, re.I):
            raise Fail(
                f"Signal wants a captcha first. Ask the user to open {CAPTCHA_PAGE}, solve it, right-click 'Open Signal', "
                "copy the link (it starts with signalcaptcha://) and paste it to you. It works once and expires within "
                f'minutes, so run straight away: install.py register --number {number} --captcha "<link>"' + (" --voice" if args.voice else "")
            )
        if args.voice and re.search(r"sms", output, re.I):
            raise Fail("Signal calls with a code only after an SMS attempt. Run once without --voice, then again with it")
        raise Fail(f"signal-cli register failed:\n{output}")
    write_cfg(bot=number, registered=False)
    print(f"Signal is sending a code to {number} by {'voice call' if args.voice else 'SMS'}. Ask the user for it, then run: install.py verify --code <code>")


def cmd_verify(args) -> None:
    cfg = read_cfg()
    if not cfg.get("bot"):
        raise Fail("no bot number yet. Run: install.py register --number <number>")
    code = re.sub(r"[\s-]", "", args.code)
    if not code.isdigit():
        raise Fail("--code is the digits Signal sent")
    result = run_signal_cli(cfg, "-a", cfg["bot"], "verify", *(["--pin", args.pin] if args.pin else []), code)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        if re.search(r"registration lock|pin", output, re.I):
            raise Fail("the number still has a registration lock PIN from its last Signal account. If the user knows it, "
                       "run install.py verify --code <code> --pin <pin>; otherwise use another number")
        raise Fail(f"signal-cli verify failed:\n{output}")
    # A registration lock stops whoever gets this number next from taking over the bot.
    pin = args.pin or (PIN_FILE.read_text(encoding="utf-8").strip() if PIN_FILE.exists() else "".join(secrets.choice("0123456789") for _ in range(8)))
    locked = run_signal_cli(cfg, "-a", cfg["bot"], "setPin", pin).returncode == 0
    if locked:
        PIN_FILE.write_text(pin + "\n", encoding="utf-8")
        if not WINDOWS:
            PIN_FILE.chmod(0o600)
    run_signal_cli(cfg, "-a", cfg["bot"], "updateProfile", "--given-name", args.name)
    write_cfg(registered=True)
    print(f"Registered {cfg['bot']} as '{args.name}'.")
    print(f"Its registration lock PIN is in {PIN_FILE}. Ask the user to keep a copy in their password manager; don't paste it into the chat."
          if locked else "Setting the registration lock PIN failed; run install.py verify again later to retry it.")
    print("Next: install.py owner --phrase <a word the user will text the bot>")


def cmd_owner(args) -> None:
    cfg = read_cfg()
    if not cfg.get("registered"):
        raise Fail("register and verify the bot first (install.py doctor shows where you are)")
    phrase = args.phrase.strip().lower()
    if len(phrase) < 4:
        raise Fail("--phrase needs at least 4 characters: something nobody else would text the bot")
    if running("relay"):
        raise Fail("the relay would take the message. Run install.py stop first")
    start_daemon(cfg)

    def sender_of_phrase(envelope: dict):
        msg = envelope.get("dataMessage") or {}
        return None if msg.get("groupInfo") or phrase not in str(msg.get("message") or "").lower() else envelope.get("sourceUuid")

    print(f"Waiting up to {args.timeout} s for a Signal message to {cfg['bot']} containing '{phrase}'...", flush=True)
    owner = ks.wait_for_event(with_defaults(cfg), args.timeout, sender_of_phrase)
    if not owner:
        raise Fail(f"no message with '{phrase}' arrived. Ask the user to text it to {cfg['bot']} from their phone, then rerun this")
    cfg = write_cfg(owner=owner)
    ks.send_to_owner(with_defaults(cfg), "Paired with kiro-signal. Once setup finishes, what you send here goes to your Kiro chat.")
    print(f"Paired with the sender ({owner}), and sent their phone a confirmation. Next: install.py chat")


# ── Kiro ──


def kiro_chats() -> list[dict]:
    chats = []
    for f in KIRO_SESSIONS.glob("*/sess_*/session.json"):
        try:
            chats.append({**json.loads(f.read_text(encoding="utf-8")), "_mtime": f.stat().st_mtime})
        except (OSError, ValueError):
            pass
    return chats


def pick_chat(chats: list[dict], workspace: str, session: str | None) -> dict:
    """The chat named, or else the only chat in `workspace` mid-turn: the one running this installer."""
    if session:
        for chat in chats:
            if chat.get("id") == session:
                return chat
        raise Fail(f"no Kiro chat {session}")
    here = [c for c in chats if any(same_path(p, workspace) for p in c.get("workspacePaths") or [])]
    live = [c for c in here if c.get("status") == "in_progress"]
    if len(live) == 1:
        return live[0]
    recent = sorted(here, key=lambda c: c["_mtime"], reverse=True)[:10]
    listing = "\n".join(f"  {c.get('id')}  {c.get('title') or '(untitled)'}  [{c.get('status')}]" for c in recent) or "  (no chats)"
    raise Fail(f"{'More than one chat' if live else 'No chat'} in {workspace} is running a turn, so it isn't clear which is yours. "
               f"Rerun with --session <id>, from the most recent:\n{listing}")


def cmd_chat(args) -> None:
    workspace = str(Path(args.workspace or os.getcwd()).resolve())
    chat = pick_chat(kiro_chats(), workspace, args.session)
    folders = chat.get("workspacePaths") or [workspace]
    folder = next((p for p in folders if same_path(p, workspace)), folders[0])
    write_cfg(kiro_session=chat["id"], kiro_workspace=folder)
    print(f"Messages will go to the Kiro chat '{chat.get('title') or chat['id']}' ({chat['id']}) in {folder}. Next: install.py mcp")


def build_vsix(folder: Path) -> Path:
    """A VSIX is a zip of a manifest, a content-types file and the extension, so no vsce is needed."""
    pj = json.loads((HERE / "bridge" / "package.json").read_text(encoding="utf-8"))
    e = {k: html.escape(str(pj[k])) for k in ("name", "version", "publisher", "displayName", "description")}
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{e['name']}" Version="{e['version']}" Publisher="{e['publisher']}"/>
    <DisplayName>{e['displayName']}</DisplayName>
    <Description xml:space="preserve">{e['description']}</Description>
    <Categories>Other</Categories>
    <License>extension/LICENSE.txt</License>
    <Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="{html.escape(pj['engines']['vscode'])}"/></Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true"/>
  </Assets>
</PackageManifest>
"""
    content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".txt" ContentType="text/plain"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""
    folder.mkdir(parents=True, exist_ok=True)
    vsix = folder / f"{pj['name']}-{pj['version']}.vsix"
    with zipfile.ZipFile(vsix, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("extension.vsixmanifest", manifest)
        z.writestr("[Content_Types].xml", content_types)
        z.write(HERE / "bridge" / "package.json", "extension/package.json")
        z.write(HERE / "bridge" / "extension.js", "extension/extension.js")
        z.write(HERE / "LICENSE", "extension/LICENSE.txt")
    return vsix


def kiro_cli() -> str:
    candidates = [shutil.which("kiro")]
    if WINDOWS:
        candidates.append(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Kiro\bin\kiro.cmd"))
    elif MAC:
        candidates += ["/Applications/Kiro.app/Contents/Resources/app/bin/kiro", str(Path.home() / "Applications/Kiro.app/Contents/Resources/app/bin/kiro")]
    else:
        candidates += ["/usr/share/kiro/bin/kiro", "/opt/Kiro/bin/kiro"]
    found = next((c for c in candidates if c and Path(c).exists()), None)
    if not found:
        raise Fail("can't find Kiro's command line. In Kiro, run 'Shell Command: Install kiro command in PATH', then rerun")
    return found


def bridge_listening(cfg: dict) -> bool:
    if not ks.BRIDGE_TOKEN_FILE.exists():
        return False
    return ks.bridge(with_defaults(cfg), ks.BRIDGE_TOKEN_FILE.read_text(encoding="utf-8").strip(), "GET", "/health")[0] == 200


def cmd_bridge(args) -> None:
    cfg = read_cfg()
    if not cfg.get("kiro_workspace"):
        raise Fail("pick the chat first: install.py chat")
    vsix = build_vsix(HOME / "build")
    result = subprocess.run([kiro_cli(), "--install-extension", str(vsix), "--force"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if result.returncode != 0:
        raise Fail(f"Kiro didn't install {vsix.name}:\n{(result.stdout + result.stderr).strip()}")
    print(f"Installed {vsix.name}. Waiting for it to start in the Kiro window for {cfg['kiro_workspace']}...", flush=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if bridge_listening(cfg):
            print("The bridge is listening. Next: install.py start")
            return
        time.sleep(1)
    raise Fail("the bridge hasn't started. Ask the user to reload that Kiro window (Command Palette: 'Developer: Reload Window'), "
               "which ends this chat's turn, then continue with install.py start")


def mcp_entry() -> dict:
    entry = {"command": sys.executable, "args": [str(HERE / "kiro_signal.py"), "mcp"], "autoApprove": ["signal_reply"]}
    if os.environ.get("KIRO_SIGNAL_HOME"):
        entry["env"] = {"KIRO_SIGNAL_HOME": str(HOME)}
    return entry


def is_ours(entry) -> bool:
    return isinstance(entry, dict) and any(str(a).endswith("kiro_signal.py") for a in entry.get("args") or [])


def cmd_mcp(args) -> None:
    try:
        settings = json.loads(MCP_FILE.read_text(encoding="utf-8")) if MCP_FILE.exists() else {}
    except ValueError:
        raise Fail(f"{MCP_FILE} isn't plain JSON, so it wasn't touched. Add this under mcpServers by hand:\n\"{args.name}\": {json.dumps(mcp_entry(), indent=2)}")
    servers = settings.setdefault("mcpServers", {})
    current = servers.get(args.name)
    if current == mcp_entry():
        print(f"{MCP_FILE} already has this server as '{args.name}'.")
        return
    if current and not is_ours(current) and not args.force:
        raise Fail(f"{MCP_FILE} already has a different '{args.name}' server. Rerun with --name <another name>, or --force to replace it")
    if MCP_FILE.exists():
        shutil.copy2(MCP_FILE, MCP_FILE.with_name(MCP_FILE.name + ".bak-kiro-signal"))
    servers[args.name] = mcp_entry()
    MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MCP_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"Added '{args.name}' to {MCP_FILE}, with signal_reply approved to run without asking, so replies reach the user while "
          "they're away. Kiro loads it within a few seconds. Next: install.py bridge")


# ── processes ──


def pid_alive(pid: int) -> bool:
    if WINDOWS:  # os.kill(pid, 0) would end the process on Windows
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def running(name: str) -> int | None:
    """The PID recorded for `name`, if it still runs.
    ponytail: after a reboot a reused PID reads as alive, and `stop` would end that process. The
    autostart entry runs `start`, which rewrites the files, so in practice they're fresh."""
    try:
        pid = int((PIDS / f"{name}.pid").read_text())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def spawn(name: str, command: list[str], env: dict | None = None) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / f"{name}.log", "ab") as log_file:
        options: dict = {"stdin": subprocess.DEVNULL, "stdout": log_file, "stderr": subprocess.STDOUT, "env": env, "cwd": str(HOME)}
        if WINDOWS:  # its own hidden console, outside the terminal's job, so closing the terminal doesn't end it
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                process = subprocess.Popen(command, creationflags=flags | 0x01000000, **options)  # CREATE_BREAKAWAY_FROM_JOB
            except OSError:
                process = subprocess.Popen(command, creationflags=flags, **options)
        else:
            process = subprocess.Popen(command, start_new_session=True, **options)
    (PIDS / f"{name}.pid").write_text(str(process.pid))
    return process.pid


def stop(name: str) -> bool:
    pid = running(name)
    (PIDS / f"{name}.pid").unlink(missing_ok=True)
    if pid is None:
        return False
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return True


def daemon_up(cfg: dict) -> bool:
    try:
        with urllib.request.urlopen(cfg["signal_rpc"] + "/api/v1/check", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def start_daemon(cfg: dict) -> None:
    cfg = with_defaults(cfg)
    if daemon_up(cfg):
        return
    address = urllib.parse.urlsplit(cfg["signal_rpc"])
    if address.hostname not in ("127.0.0.1", "localhost"):
        raise Fail("signal_rpc must be on 127.0.0.1: the daemon's HTTP API has no authentication")
    if not running("daemon"):
        command, env = signal_cli_command(cfg)
        spawn("daemon", [*command, "-a", cfg["bot"], "--scrub-log", "daemon", f"--http={address.netloc}", "--receive-mode=on-connection"], env)
    deadline = time.monotonic() + 90
    while not daemon_up(cfg):
        if time.monotonic() > deadline:
            raise Fail(f"the signal-cli daemon didn't come up within 90 s; see {LOGS / 'daemon.log'}")
        time.sleep(1)


def cmd_start(args) -> None:
    cfg = read_cfg()
    for key, step in (("registered", "register"), ("owner", "owner"), ("kiro_session", "chat")):
        if not cfg.get(key):
            raise Fail(f"not set up yet: run install.py {step} (install.py doctor shows where you are)")
    if not ks.BRIDGE_TOKEN_FILE.exists():
        raise Fail("the bridge hasn't started yet. Run: install.py bridge")
    start_daemon(cfg)
    if running("relay"):
        print("The daemon and the relay are already running.")
        return
    spawn("relay", [sys.executable, str(HERE / "kiro_signal.py"), "relay"])
    print(f"The daemon and the relay are running. Logs: {LOGS}. Next: install.py autostart")


def cmd_stop(args) -> None:
    stopped = [name for name in ("relay", "daemon") if stop(name)]
    print("Stopped: " + ", ".join(stopped) if stopped else "Nothing was running.")


def autostart_file() -> Path:
    if WINDOWS:
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/kiro-signal.cmd"
    if MAC:
        return Path.home() / "Library/LaunchAgents/dev.kiro-signal.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart/kiro-signal.desktop"


def autostart_entry(python: str, script: str) -> str:
    if WINDOWS:
        pythonw = Path(python).with_name("pythonw.exe")
        return f'@echo off\r\nstart "" "{pythonw if pythonw.exists() else python}" "{script}" start\r\n'
    if MAC:
        arguments = "".join(f"<string>{html.escape(a)}</string>" for a in (python, script, "start"))
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                f'<plist version="1.0"><dict><key>Label</key><string>dev.kiro-signal</string><key>ProgramArguments</key><array>{arguments}</array>'
                "<key>RunAtLoad</key><true/></dict></plist>\n")
    return f'[Desktop Entry]\nType=Application\nName=kiro-signal\nExec="{python}" "{script}" start\nX-GNOME-Autostart-enabled=true\n'


def cmd_autostart(args) -> None:
    path = autostart_file()
    if args.off:
        path.unlink(missing_ok=True)
        print(f"Removed {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(autostart_entry(sys.executable, str(HERE / "install.py")), encoding="utf-8", newline="")
    print(f"kiro-signal starts at login: {path}")


def cmd_uninstall(args) -> None:
    cmd_stop(args)
    autostart_file().unlink(missing_ok=True)
    try:
        settings = json.loads(MCP_FILE.read_text(encoding="utf-8"))
        ours = [name for name, entry in (settings.get("mcpServers") or {}).items() if is_ours(entry)]
        for name in ours:
            del settings["mcpServers"][name]
        if ours:
            MCP_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except FileNotFoundError:
        pass
    except ValueError:
        print(f"{MCP_FILE} isn't plain JSON; remove the kiro-signal server from it by hand.")
    try:
        subprocess.run([kiro_cli(), "--uninstall-extension", EXTENSION_ID], capture_output=True, timeout=120)
    except Fail as exc:
        print(exc)
    print(f"Removed the autostart entry, the MCP server and the Kiro extension. The bot's Signal identity and settings "
          f"are still in {HOME}: delete that folder to remove them too.")


# ── doctor ──


def steps(cfg: dict) -> list[tuple[bool, str, str]]:
    """(done, what, the command that does it), in install order."""
    rows = [(sys.version_info >= (3, 9), f"Python {platform.python_version()}", "Python 3.9 or newer")]
    if cfg.get("signal_cli_version"):
        age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(cfg["signal_cli_released"]).date()).days
        fresh = age <= SIGNAL_CLI_MAX_AGE_DAYS and Path(cfg["java_home"]).exists()
        rows.append((fresh, f"signal-cli {cfg['signal_cli_version']}" + ("" if fresh else f", {age} days old or its Java is gone"), "install.py signal-cli --force"))
    else:
        rows.append((False, "signal-cli", "install.py signal-cli"))
    bot, owner, chat, workspace = (cfg.get(k) for k in ("bot", "owner", "kiro_session", "kiro_workspace"))
    rows += [
        (bool(cfg.get("registered")), f"bot {bot}" if cfg.get("registered") else f"bot {bot}, not verified yet" if bot else "the bot's number",
         "install.py verify --code <code>" if bot else "install.py register --number <number>"),
        (bool(owner), f"paired with {owner}" if owner else "pairing with your phone", "install.py owner --phrase <word>"),
        (any(c.get("id") == chat for c in kiro_chats()), f"chat {chat}" if chat else "the Kiro chat", "install.py chat"),
    ]
    try:
        servers = json.loads(MCP_FILE.read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, ValueError):
        servers = {}
    rows += [
        (any(is_ours(e) for e in servers.values()), "signal_reply in Kiro's MCP settings", "install.py mcp"),
        (bridge_listening(cfg), f"the bridge, in the Kiro window for {workspace}" if workspace else "the bridge",
         "open that Kiro window, or reload it" if ks.BRIDGE_TOKEN_FILE.exists() else "install.py bridge"),
        (daemon_up(with_defaults(cfg)) and bool(running("relay")), "the daemon and the relay, running", "install.py start"),
        (autostart_file().exists(), "starts at login", "install.py autostart"),
    ]
    return rows


def cmd_doctor(args) -> None:
    rows = steps(read_cfg())
    for done, what, command in rows:
        print(f"  {'ok  ' if done else 'todo'}  {what}" + ("" if done else f"  (run: {command})"))
    todo = next((r for r in rows if not r[0]), None)
    print(f"\nNext: {todo[2]}" if todo else "\nAll set. Text the bot from your phone; the message arrives in your Kiro chat.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="install.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    def command(name, fn, help_text, *arguments):
        sub = commands.add_parser(name, help=help_text, description=help_text)
        for flags, options in arguments:
            sub.add_argument(*flags, **options)
        sub.set_defaults(fn=fn)

    command("doctor", cmd_doctor, "show what's set up, and the next command to run")
    command("signal-cli", cmd_signal_cli, "download or update signal-cli, checked against GitHub's SHA-256",
            (["--force"], {"action": "store_true", "help": "reinstall the latest release"}))
    command("register", cmd_register, "register the bot's phone number with Signal",
            (["--number"], {"required": True, "help": "international form, like +15551234567"}),
            (["--captcha"], {"help": "the signalcaptcha:// link from " + CAPTCHA_PAGE}),
            (["--voice"], {"action": "store_true", "help": "get the code by voice call, after one SMS attempt"}))
    command("verify", cmd_verify, "finish registering with the code Signal sent, and set a registration lock PIN",
            (["--code"], {"required": True}), (["--pin"], {"help": "the number's existing registration lock PIN, if it has one"}),
            (["--name"], {"default": "Kiro", "help": "the bot's Signal profile name"}))
    command("owner", cmd_owner, "pair with the phone that texts PHRASE to the bot",
            (["--phrase"], {"required": True}), (["--timeout"], {"type": int, "default": 300, "help": "seconds to wait"}))
    command("chat", cmd_chat, "pick the Kiro chat your messages go to: the one running this, unless --session names another",
            (["--session"], {}), (["--workspace"], {"help": "the workspace folder of that chat; default: the current folder"}))
    command("mcp", cmd_mcp, "add signal_reply to Kiro's MCP settings",
            (["--name"], {"default": "signal"}), (["--force"], {"action": "store_true", "help": "replace a different server with that name"}))
    command("bridge", cmd_bridge, "build and install the Kiro extension that types messages into the chat")
    command("start", cmd_start, "start the signal-cli daemon and the relay")
    command("stop", cmd_stop, "stop them")
    command("autostart", cmd_autostart, "start kiro-signal at login", (["--off"], {"action": "store_true"}))
    command("uninstall", cmd_uninstall, "stop, and remove the autostart entry, the MCP server and the extension")
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
