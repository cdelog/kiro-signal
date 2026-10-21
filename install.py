"""kiro-signal installer. Each step is one command, safe to rerun, and never prompts, so an AI
agent can run them all and ask you only for what it can't know. INSTALL.md is the runbook.

    python install.py doctor                  what's done, and the next command to run
    python install.py signal-cli              download signal-cli (checksum verified), check Java
    python install.py register --number +15551234567 [--captcha LINK] [--voice]
    python install.py verify --code 123456 [--pin PIN]
    python install.py owner --phrase WORD     pair with the phone that texts WORD to the bot
    python install.py chat [--session ID] [--workspace DIR]   the Kiro chat your messages go to
    python install.py bridge [--test]         build and install the Kiro extension
    python install.py mcp [--name signal] [--force]           add signal_reply to Kiro
    python install.py start [--no-relay] | stop
    python install.py autostart [--off]       start at login
    python install.py crew                    relay to KiroCrew's webhook instead of a Kiro chat
    python install.py uninstall

Standard library only, Python 3.9+. Everything it writes lives in ~/.kiro-signal (or
$KIRO_SIGNAL_HOME), apart from one entry in ~/.kiro/settings/mcp.json, the Kiro extension and
the autostart entry.
"""

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
RUN = HOME / "run"
SIGNAL_DATA = HOME / "signal-data"  # signal-cli's own data: the bot's identity keys
PIN_FILE = HOME / "registration-lock-pin.txt"
MCP_FILE = Path.home() / ".kiro" / "settings" / "mcp.json"
SESSIONS = Path.home() / ".kiro" / "sessions"
RELEASES = "https://api.github.com/repos/AsamK/signal-cli/releases/latest"
CAPTCHA_URL = "https://signalcaptchas.org/registration/generate.html"
EXTENSION_ID = "kiro-signal.kiro-signal-bridge"
JAVA_MIN = 25  # signal-cli 0.14 needs Java 25
STALE_DAYS = 75  # Signal stops accepting signal-cli releases older than about three months
WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"

JAVA_HINT = {
    "windows": "winget install --id EclipseAdoptium.Temurin.25.JDK -e (Windows asks to allow it)",
    "mac": "brew install openjdk (or the Temurin 25 installer from adoptium.net)",
    "linux": "your distribution's Java 25 package, for example: sudo apt install openjdk-25-jre-headless",
}


def os_key() -> str:
    return "windows" if WINDOWS else "mac" if MAC else "linux"


class Fail(SystemExit):
    """A step that can't go on. The message says what to do next."""

    def __init__(self, msg: str):
        super().__init__(f"error: {msg}")


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


def runtime_cfg(cfg: dict) -> dict:
    return {**ks.DEFAULTS, **cfg}


def private_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if not WINDOWS:  # a Windows profile folder is already private to its user
        os.chmod(path, 0o600)


def same_path(a, b) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


# ── Java and signal-cli ──


def java_version(java: str) -> int | None:
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)', out.stderr + out.stdout)
    return int(m.group(1)) if m else None


def java_homes():
    """Likely Java homes, most specific first: JAVA_HOME, PATH, then the usual install folders."""
    if os.environ.get("JAVA_HOME"):
        yield Path(os.environ["JAVA_HOME"])
    on_path = shutil.which("java")
    if on_path:
        yield Path(on_path).resolve().parent.parent
    patterns = {
        "windows": [r"C:\Program Files\Eclipse Adoptium\jdk-*", r"C:\Program Files\Eclipse Adoptium\jre-*",
                    r"C:\Program Files\Microsoft\jdk-*", r"C:\Program Files\Java\jdk-*", r"C:\Program Files\Zulu\zulu-*"],
        "mac": ["/Library/Java/JavaVirtualMachines/*/Contents/Home",
                "/opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
                "/usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home"],
        "linux": ["/usr/lib/jvm/*"],
    }[os_key()]
    for pattern in patterns:
        yield from (Path(p) for p in sorted(glob.glob(pattern), reverse=True))


def find_java() -> tuple[Path, int] | None:
    """The newest Java home at JAVA_MIN or later, with its major version."""
    best, seen = None, set()
    for home in java_homes():
        java = home / "bin" / ("java.exe" if WINDOWS else "java")
        if str(java) in seen or not java.exists():
            continue
        seen.add(str(java))
        version = java_version(str(java))
        if version and version >= JAVA_MIN and (best is None or version > best[1]):
            best = (home, version)
    return best


def needs_java(system: str, machine: str) -> bool:
    # signal-cli ships a native build for Linux on x86_64 only; everything else runs on the JVM.
    return not (system == "Linux" and machine.lower() in ("x86_64", "amd64"))


def pick_asset(release: dict, system: str, machine: str) -> dict:
    version = release["tag_name"].lstrip("v")
    assets = {a["name"]: a for a in release.get("assets", [])}
    name = f"signal-cli-{version}.tar.gz" if needs_java(system, machine) else f"signal-cli-{version}-Linux-native.tar.gz"
    if name not in assets:
        raise Fail(f"signal-cli {version} has no {name}; see https://github.com/AsamK/signal-cli/releases")
    return assets[name]


def download(asset: dict, dest: Path) -> Path:
    """Download one release asset and check it against the SHA-256 GitHub publishes for it."""
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise Fail(f"GitHub published no SHA-256 for {asset['name']}, so it can't be verified")
    dest.mkdir(parents=True, exist_ok=True)
    path, sha = dest / asset["name"], hashlib.sha256()
    with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as resp, open(path, "wb") as out:
        while block := resp.read(1 << 20):
            sha.update(block)
            out.write(block)
    if sha.hexdigest() != digest.split(":", 1)[1]:
        path.unlink()
        raise Fail(f"{asset['name']} doesn't match its published SHA-256; nothing was installed")
    return path


def safe_extract(archive: Path, dest: Path) -> None:
    """Extract a tarball, refusing any entry that would land outside dest."""
    root = dest.resolve()
    with tarfile.open(archive) as tf:
        if hasattr(tarfile, "data_filter"):
            try:
                tf.extractall(dest, filter="data")
            except tarfile.FilterError as exc:
                raise Fail(f"{archive.name} has an unsafe entry: {exc}")
            return
        for member in tf.getmembers():  # Pythons without extraction filters
            target = (dest / member.name).resolve()
            if member.issym() or member.islnk() or not (target == root or root in target.parents):
                raise Fail(f"{archive.name} has an unsafe entry: {member.name}")
        tf.extractall(dest)


def windows_java_cmd(launcher: Path, java_home: Path) -> list[str]:
    """signal-cli.bat puts all ~80 jars on one command line, which overflows cmd's 8,191
    characters once the install folder's path is long. So run Java directly, with the launcher's
    own options and classpath, in order, in a Java argument file.
    ponytail: the argument file is read in the system code page, so a user folder with non-ASCII
    characters may need KIRO_SIGNAL_HOME set to an ASCII path."""
    app = launcher.parent.parent
    bat = launcher.read_text(encoding="utf-8", errors="replace")
    cp = re.search(r"^set CLASSPATH=(.+)$", bat, re.M)
    main = re.search(r'-classpath "%CLASSPATH%" (\S+)', bat)
    if not (cp and main):
        raise Fail(f"{launcher.name} has an unexpected shape; please open an issue with the signal-cli version")
    opts = re.search(r"^set DEFAULT_JVM_OPTS=(.*)$", bat, re.M)
    jars = [j.replace("%APP_HOME%", str(app)).replace("\\", "/") for j in cp.group(1).strip().split(";") if j]
    lines = re.findall(r'"([^"]+)"', opts.group(1)) if opts else []
    argfile = app / "kiro-signal.args"
    argfile.write_text("\n".join([*lines, "-classpath", '"' + ";".join(jars) + '"', main.group(1)]) + "\n", encoding="utf-8")
    return [str(java_home / "bin" / "java.exe"), f"@{argfile}"]


def find_executable(folder: Path) -> Path:
    names = ("signal-cli.bat",) if WINDOWS else ("signal-cli",)
    for name in names:
        hits = sorted(folder.rglob(name), key=lambda p: (p.parent.name != "bin", len(p.parts)))
        if hits:
            return hits[0]
    raise Fail(f"no signal-cli executable in {folder}")


def cmd_signal_cli(args) -> None:
    system, machine = platform.system(), platform.machine()
    java = None
    if needs_java(system, machine):
        java = find_java()
        if not java:
            raise Fail(f"signal-cli needs Java {JAVA_MIN} or newer, and none was found. Install it with "
                       f"{JAVA_HINT[os_key()]}, then rerun: install.py signal-cli")
    with urllib.request.urlopen(urllib.request.Request(RELEASES, headers={"Accept": "application/vnd.github+json"}), timeout=30) as resp:
        release = json.loads(resp.read())
    version = release["tag_name"].lstrip("v")
    cfg = read_cfg()
    if cfg.get("signal_cli_version") == version and cfg.get("signal_cli") and Path(cfg["signal_cli"]).exists() and not args.force:
        print(f"signal-cli {version} is already installed, the latest release.")
        return
    asset = pick_asset(release, system, machine)
    print(f"Downloading {asset['name']} ({asset['size'] // (1 << 20)} MB)...", flush=True)
    archive = download(asset, HOME / "downloads")
    target = HOME / "signal-cli" / version
    shutil.rmtree(target, ignore_errors=True)
    safe_extract(archive, target)
    archive.unlink()
    exe = find_executable(target)
    if WINDOWS:
        command = windows_java_cmd(exe, java[0])
    else:
        exe.chmod(exe.stat().st_mode | 0o111)
        command = [str(exe)]
    write_cfg(signal_cli=str(exe), signal_cli_cmd=command, signal_cli_version=version,
              signal_cli_released=release["published_at"][:10], java_home=str(java[0]) if java else None)
    check = signal_cli(read_cfg(), "--version", timeout=120)
    if check.returncode != 0:
        raise Fail(f"signal-cli {version} installed but doesn't run:\n{(check.stdout + check.stderr).strip()}")
    for old in (HOME / "signal-cli").iterdir():  # earlier versions, now unused
        if old.name != version and read_pid("daemon") is None:
            shutil.rmtree(old, ignore_errors=True)
    print(f"Installed signal-cli {version}" + (f", running on Java {java[1]} at {java[0]}." if java else " (native build)."))
    if read_pid("daemon"):
        print("The daemon still runs the old release: install.py stop, then install.py start.")
    elif not cfg.get("registered"):
        print("Next: install.py register --number <the bot's number>")


def signal_cli_cmd(cfg: dict) -> tuple[list[str], dict]:
    """The command that runs signal-cli with its data in SIGNAL_DATA, and its environment."""
    command = cfg.get("signal_cli_cmd") or ([cfg["signal_cli"]] if cfg.get("signal_cli") else [])
    if not command or not Path(command[0]).exists():
        raise Fail("signal-cli isn't installed yet. Run: install.py signal-cli")
    env = {**os.environ, **({"JAVA_HOME": cfg["java_home"]} if cfg.get("java_home") else {})}
    return [*command, "--config", str(SIGNAL_DATA)], env


def signal_cli(cfg: dict, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    command, env = signal_cli_cmd(cfg)
    return subprocess.run([*command, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


# ── the bot's Signal account ──


def cmd_register(args) -> None:
    number = args.number.strip().replace(" ", "")
    if not re.fullmatch(r"\+[1-9]\d{6,14}", number):
        raise Fail("--number must be in international form, for example +15551234567")
    cfg = read_cfg()
    if daemon_up(runtime_cfg(cfg)):
        raise Fail("the daemon is running. Run install.py stop first")
    cli = ["-a", number, "register"] + (["--voice"] if args.voice else []) + (["--captcha", args.captcha.strip()] if args.captcha else [])
    result = signal_cli(cfg, *cli)
    out = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        if re.search(r"captcha", out, re.I):
            raise Fail(
                f"Signal wants a captcha first. Ask the user to open {CAPTCHA_URL}, solve it, right-click "
                "'Open Signal', copy the link (it starts with signalcaptcha://) and paste it to you. It works "
                f'once and expires within minutes, so run straight away: install.py register --number {number} --captcha "<link>"'
                + (" --voice" if args.voice else ""))
        if args.voice and re.search(r"sms", out, re.I):
            raise Fail("Signal sends a voice code only after an SMS attempt. Run once without --voice, then again with it")
        raise Fail(f"signal-cli register failed:\n{out}")
    write_cfg(bot=number, registered=False)
    how = "a voice call" if args.voice else "SMS"
    print(f"Signal is sending a verification code to {number} by {how}. Ask the user for it, then run: install.py verify --code <code>")


def cmd_verify(args) -> None:
    cfg = read_cfg()
    bot = cfg.get("bot") or ""
    if not bot:
        raise Fail("no bot number yet. Run: install.py register --number <number>")
    code = re.sub(r"[\s-]", "", args.code)
    if not code.isdigit():
        raise Fail("--code is the digits Signal sent")
    result = signal_cli(cfg, "-a", bot, "verify", *(["--pin", args.pin] if args.pin else []), code)
    out = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        if re.search(r"registration lock|pin", out, re.I):
            raise Fail("this number still has a registration lock PIN from its previous Signal account. "
                       "If the user knows it, run install.py verify --code <code> --pin <pin>; otherwise use another number")
        raise Fail(f"signal-cli verify failed:\n{out}")
    # The registration lock stops whoever gets this number next from taking over the bot.
    pin = args.pin or (PIN_FILE.read_text(encoding="utf-8").strip() if PIN_FILE.exists() else
                       "".join(secrets.choice("0123456789") for _ in range(8)))
    locked = signal_cli(cfg, "-a", bot, "setPin", pin).returncode == 0
    if locked:
        private_file(PIN_FILE, pin + "\n")
    signal_cli(cfg, "-a", bot, "updateProfile", "--given-name", args.name)
    write_cfg(registered=True)
    print(f"Registered {bot} as '{args.name}'.")
    if locked:
        print(f"Its registration lock PIN is in {PIN_FILE}. Ask the user to keep a copy in their password manager; don't paste it into the chat.")
    else:
        print("Setting the registration lock PIN failed; run install.py verify again later to retry it.")
    print("Next: install.py owner --phrase <a word the user will text the bot>")


def cmd_owner(args) -> None:
    cfg = read_cfg()
    if not cfg.get("registered"):
        raise Fail("register and verify the bot first (install.py doctor shows where you are)")
    phrase = args.phrase.strip()
    if len(phrase) < 4:
        raise Fail("--phrase needs at least 4 characters: something nobody else would text the bot")
    if read_pid("relay"):
        raise Fail("a relay is running and would take the message. Run install.py stop first")
    start_daemon(cfg)
    rcfg = runtime_cfg(cfg)

    def pick(envelope: dict):
        msg = envelope.get("dataMessage") or {}
        if msg.get("groupInfo") or phrase.lower() not in str(msg.get("message") or "").lower():
            return None
        return envelope.get("sourceUuid") or None

    print(f"Waiting up to {args.timeout} s for a Signal message to {cfg['bot']} containing '{phrase}'...", flush=True)
    found = ks.wait_events(rcfg, args.timeout, pick, settle=0)
    if not found:
        raise Fail(f"no message with '{phrase}' arrived. Ask the user to text it to {cfg['bot']} from their phone, then rerun this")
    owner = found[0]
    write_cfg(owner=owner)
    ks.send_to_owner({**rcfg, "owner": owner}, "Paired with kiro-signal. Once setup finishes, what you send here goes to your Kiro chat.")
    print(f"Paired with the sender ({owner}); a confirmation went to their phone. Next: install.py chat")


# ── Kiro ──


def kiro_sessions():
    """Kiro's chat records, each with the time its file last changed."""
    for f in SESSIONS.glob("*/sess_*/session.json"):
        try:
            record = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        record["_mtime"] = f.stat().st_mtime
        yield record


def pick_chat(sessions: list[dict], workspace: str | None, session: str | None) -> dict:
    """The chat to bind: the one named, or the only chat in `workspace` that is mid-turn, which
    is the chat running this installer."""
    if session:
        for s in sessions:
            if s.get("id") == session:
                return s
        raise Fail(f"no Kiro chat {session}")
    mine = [s for s in sessions if any(same_path(p, workspace) for p in s.get("workspacePaths") or [])]
    live = [s for s in mine if s.get("status") == "in_progress"]
    if len(live) == 1:
        return live[0]
    recent = sorted(mine, key=lambda s: s["_mtime"], reverse=True)[:10]
    listing = "\n".join(f"  {s.get('id')}  {s.get('title') or '(untitled)'}  [{s.get('status')}]" for s in recent) or "  (no chats)"
    why = "More than one chat" if live else "No chat"
    raise Fail(f"{why} in {workspace} is running a turn, so it isn't clear which chat is yours. "
               f"Rerun with --session <id>, choosing from the most recent:\n{listing}")


def cmd_chat(args) -> None:
    workspace = str(Path(args.workspace or os.getcwd()).resolve())
    chat = pick_chat(list(kiro_sessions()), workspace, args.session)
    paths = chat.get("workspacePaths") or [workspace]
    bound = next((p for p in paths if same_path(p, workspace)), paths[0])
    write_cfg(kiro_session=chat["id"], kiro_workspace=bound, relay="kiro")
    print(f"Messages will go to the Kiro chat '{chat.get('title') or chat['id']}' ({chat['id']}) in {bound}.")
    print("Next: install.py bridge")


TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".txt" ContentType="text/plain"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""


def build_vsix(out_dir: Path, test: bool = False) -> Path:
    """Pack bridge/ into a VSIX. No vsce needed: a VSIX is a zip holding a manifest, a
    content-types file and the extension under extension/."""
    pj = json.loads((HERE / "bridge" / "package.json").read_text(encoding="utf-8"))
    if test:  # a side-by-side copy that starts at once, to try a change without a window reload
        pj["name"] += "-test"
        pj["displayName"] += " (test)"
        pj["bridgePort"] = 39277
    e = {k: html.escape(str(pj[k])) for k in ("name", "version", "publisher", "displayName", "description")}
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{e['name']}" Version="{e['version']}" Publisher="{e['publisher']}"/>
    <DisplayName>{e['displayName']}</DisplayName>
    <Description xml:space="preserve">{e['description']}</Description>
    <Categories>Other</Categories>
    <License>extension/LICENSE.txt</License>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{html.escape(pj['engines']['vscode'])}"/>
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true"/>
  </Assets>
</PackageManifest>
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    vsix = out_dir / f"{pj['name']}-{pj['version']}.vsix"
    with zipfile.ZipFile(vsix, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("extension.vsixmanifest", manifest)
        z.writestr("[Content_Types].xml", TYPES)
        z.writestr("extension/package.json", json.dumps(pj, indent=2))
        z.write(HERE / "bridge" / "extension.js", "extension/extension.js")
        z.write(HERE / "LICENSE", "extension/LICENSE.txt")
    return vsix


def kiro_cli() -> str:
    candidates = [shutil.which("kiro")]
    if WINDOWS:
        candidates.append(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Kiro\bin\kiro.cmd"))
    elif MAC:
        candidates += ["/Applications/Kiro.app/Contents/Resources/app/bin/kiro",
                       str(Path.home() / "Applications/Kiro.app/Contents/Resources/app/bin/kiro")]
    else:
        candidates += ["/usr/share/kiro/bin/kiro", "/opt/Kiro/bin/kiro", "/usr/bin/kiro"]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise Fail("can't find Kiro's command line (kiro). In Kiro, run 'Shell Command: Install kiro command in PATH', then rerun")


def bridge_health(cfg: dict) -> bool:
    if not ks.TOKEN_FILE.exists():
        return False
    token = ks.TOKEN_FILE.read_text(encoding="utf-8").strip()
    return ks.bridge(runtime_cfg(cfg), token, "GET", "/health")[0] == 200


def cmd_bridge(args) -> None:
    cfg = read_cfg()
    if not cfg.get("kiro_workspace"):
        raise Fail("pick the chat first: install.py chat")
    vsix = build_vsix(HOME / "build", args.test)
    result = subprocess.run([kiro_cli(), "--install-extension", str(vsix), "--force"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=300)
    if result.returncode != 0:
        raise Fail(f"Kiro didn't install {vsix.name}:\n{(result.stdout + result.stderr).strip()}")
    print(f"Installed {vsix.name}. Waiting for it to start in the Kiro window for {cfg['kiro_workspace']}...", flush=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if bridge_health(cfg):
            print("The bridge is listening. Next: install.py mcp")
            return
        time.sleep(1)
    raise Fail("the bridge hasn't started. Ask the user to reload that Kiro window (Command Palette: "
               "'Developer: Reload Window'), which ends this chat's turn, then continue with install.py mcp")


def mcp_entry() -> dict:
    entry = {"command": sys.executable, "args": [str(HERE / "kiro_signal.py"), "mcp"], "autoApprove": ["signal_reply"]}
    if os.environ.get("KIRO_SIGNAL_HOME"):
        entry["env"] = {"KIRO_SIGNAL_HOME": str(HOME)}
    return entry


def is_ours(entry: dict) -> bool:
    return any(str(a).endswith("kiro_signal.py") for a in entry.get("args") or [])


def cmd_mcp(args) -> None:
    try:
        data = json.loads(MCP_FILE.read_text(encoding="utf-8")) if MCP_FILE.exists() else {}
    except ValueError:
        raise Fail(f"{MCP_FILE} isn't plain JSON, so it wasn't touched. Add this under mcpServers by hand:\n"
                   f'"{args.name}": {json.dumps(mcp_entry(), indent=2)}')
    servers = data.setdefault("mcpServers", {})
    entry, current = mcp_entry(), servers.get(args.name)
    if current == entry:
        print(f"{MCP_FILE} already has this server as '{args.name}'.")
        return
    if current and not is_ours(current) and not args.force:
        raise Fail(f"{MCP_FILE} already has a different '{args.name}' server. Rerun with --name <another name>, or --force to replace it")
    if MCP_FILE.exists():
        shutil.copy2(MCP_FILE, MCP_FILE.with_name(MCP_FILE.name + ".bak-kiro-signal"))
    servers[args.name] = entry
    MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MCP_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Added '{args.name}' to {MCP_FILE}. Kiro loads it within a few seconds, and signal_reply is approved to run "
          "without asking, so replies reach the user while they're away. Next: install.py start")


# ── processes ──


def pid_alive(pid: int) -> bool:
    if WINDOWS:
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(name: str) -> int | None:
    """The PID this installer recorded for `name`, if that process still runs.
    ponytail: a PID reused after a reboot reads as alive; `stop` would then end that process.
    The autostart entry runs `start`, which rewrites the files, so in practice they are fresh."""
    try:
        pid = int((RUN / f"{name}.pid").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return pid if pid_alive(pid) else None


def spawn(name: str, cmd: list[str], env: dict | None = None, note: str = "") -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(parents=True, exist_ok=True)
    with open(LOGS / f"{name}.log", "ab") as logf:
        kw: dict = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": subprocess.STDOUT, "env": env, "cwd": str(HOME)}
        if WINDOWS:
            # A hidden console of its own, so closing the terminal that ran this doesn't end it.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
            except OSError:  # the terminal's job doesn't allow breaking away
                proc = subprocess.Popen(cmd, creationflags=flags, **kw)
        else:
            proc = subprocess.Popen(cmd, start_new_session=True, **kw)
    (RUN / f"{name}.pid").write_text(f"{proc.pid} {note}".strip())
    return proc.pid


def stop_pid(name: str) -> bool:
    pid = read_pid(name)
    (RUN / f"{name}.pid").unlink(missing_ok=True)
    if pid is None:
        return False
    if WINDOWS:  # the whole tree: signal-cli.bat runs Java as a child of cmd
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return True


def daemon_up(rcfg: dict) -> bool:
    try:
        with urllib.request.urlopen(rcfg["signal_rpc"] + "/api/v1/check", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def start_daemon(cfg: dict) -> None:
    rcfg = runtime_cfg(cfg)
    if daemon_up(rcfg):
        return
    listen = urllib.parse.urlsplit(rcfg["signal_rpc"])
    if listen.hostname not in ("127.0.0.1", "localhost"):
        raise Fail("signal_rpc must be on 127.0.0.1: the daemon's HTTP API has no authentication")
    if not read_pid("daemon"):
        command, env = signal_cli_cmd(cfg)
        spawn("daemon", [*command, "-a", cfg["bot"], "--scrub-log", "daemon",
                         f"--http={listen.netloc}", "--receive-mode=on-connection"], env)
    deadline = time.monotonic() + 90
    while not daemon_up(rcfg):
        if time.monotonic() > deadline:
            raise Fail(f"the signal-cli daemon didn't come up within 90 s; see {LOGS / 'daemon.log'}")
        time.sleep(1)


def user_env(name: str) -> str:
    """An environment variable, or on Windows the user-level value set with setx after this
    terminal started."""
    value = os.environ.get(name, "")
    if not value and WINDOWS:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            value = ""
    return value


def cmd_start(args) -> None:
    cfg = read_cfg()
    for key, step in (("registered", "register and verify"), ("owner", "owner")):
        if not cfg.get(key):
            raise Fail(f"not set up yet: run install.py {step} first (install.py doctor shows where you are)")
    start_daemon(cfg)
    print("The signal-cli daemon is up on", runtime_cfg(cfg)["signal_rpc"])
    if args.no_relay:
        stop_pid("relay")
        print("No relay: the daemon only, for kiro_signal.py wait.")
        return
    mode = cfg.get("relay") or "kiro"
    env = dict(os.environ)
    if mode == "kiro":
        if not cfg.get("kiro_session"):
            raise Fail("no chat picked yet. Run: install.py chat")
        if not ks.TOKEN_FILE.exists():
            raise Fail("the bridge hasn't started yet. Run: install.py bridge")
    else:
        for name in ("KIROCREW_WEBHOOK_TOKEN", "KIROCREW_WEBHOOK_SIGNING_SECRET"):
            env[name] = user_env(name)
        if not env["KIROCREW_WEBHOOK_TOKEN"]:
            raise Fail("KIROCREW_WEBHOOK_TOKEN isn't set. See INSTALL.md, 'KiroCrew instead of a Kiro chat'")
    running = read_pid("relay")
    if running and (RUN / "relay.pid").read_text().split()[1:] == [mode]:
        print(f"The {mode} relay is already running.")
        return
    stop_pid("relay")  # only one relay may run: each consumes the messages it reads
    spawn("relay", [sys.executable, str(HERE / "kiro_signal.py"), mode], env, note=mode)
    print(f"The {mode} relay is running. Logs: {LOGS}")


def cmd_stop(args) -> None:
    stopped = [name for name in ("relay", "daemon") if stop_pid(name)]
    print("Stopped: " + ", ".join(stopped) if stopped else "Nothing was running.")


def autostart_file() -> Path:
    if WINDOWS:
        return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "kiro-signal.cmd"
    if MAC:
        return Path.home() / "Library" / "LaunchAgents" / "dev.kiro-signal.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "kiro-signal.desktop"


def autostart_text(python: str, script: str) -> str:
    if WINDOWS:
        pythonw = Path(python).with_name("pythonw.exe")
        exe = pythonw if pythonw.exists() else Path(python)
        return f'@echo off\r\nstart "" "{exe}" "{script}" start\r\n'
    if MAC:
        args = "".join(f"<string>{html.escape(a)}</string>" for a in (python, script, "start"))
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict><key>Label</key><string>dev.kiro-signal</string>'
            f"<key>ProgramArguments</key><array>{args}</array><key>RunAtLoad</key><true/></dict></plist>\n"
        )
    return f'[Desktop Entry]\nType=Application\nName=kiro-signal\nExec="{python}" "{script}" start\nX-GNOME-Autostart-enabled=true\n'


def cmd_autostart(args) -> None:
    path = autostart_file()
    if args.off:
        path.unlink(missing_ok=True)
        print(f"Removed {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(autostart_text(sys.executable, str(HERE / "install.py")), encoding="utf-8", newline="")
    print(f"kiro-signal starts at login: {path}")


def cmd_crew(args) -> None:
    write_cfg(relay="crew")
    print("The relay now goes to KiroCrew's webhook. Set KIROCREW_WEBHOOK_TOKEN and KIROCREW_WEBHOOK_SIGNING_SECRET "
          "(INSTALL.md, 'KiroCrew instead of a Kiro chat'), then run install.py start. install.py chat switches back.")


def cmd_uninstall(args) -> None:
    cmd_stop(args)
    autostart_file().unlink(missing_ok=True)
    if MCP_FILE.exists():
        try:
            data = json.loads(MCP_FILE.read_text(encoding="utf-8"))
            servers = data.get("mcpServers") or {}
            ours = [k for k, v in servers.items() if isinstance(v, dict) and is_ours(v)]
            for k in ours:
                del servers[k]
            if ours:
                MCP_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except ValueError:
            print(f"{MCP_FILE} isn't plain JSON; remove the kiro-signal server from it by hand.")
    try:
        subprocess.run([kiro_cli(), "--uninstall-extension", EXTENSION_ID], capture_output=True, timeout=120)
    except Fail as exc:
        print(exc)
    print(f"Removed the autostart entry, the MCP server and the Kiro extension. The bot's Signal identity and "
          f"settings are still in {HOME}: delete that folder to remove them too.")


# ── doctor ──


def checks(cfg: dict) -> list[tuple[str, str, str]]:
    """(state, what, next command) per step, in install order. state is ok, todo, warn or optional."""
    rcfg, rows = runtime_cfg(cfg), []
    add = lambda state, what, nxt="": rows.append((state, what, nxt))  # noqa: E731

    add("ok" if sys.version_info >= (3, 9) else "todo", f"Python {platform.python_version()}", "Python 3.9 or newer")
    exe = cfg.get("signal_cli")
    if exe and Path(exe).exists():
        released = cfg.get("signal_cli_released") or ""
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(released).replace(tzinfo=timezone.utc)).days
        except ValueError:
            age = 0
        if age > STALE_DAYS:
            add("warn", f"signal-cli {cfg.get('signal_cli_version')} is {age} days old; Signal stops accepting old releases", "install.py signal-cli")
        else:
            add("ok", f"signal-cli {cfg.get('signal_cli_version')}" + (f" on Java at {cfg['java_home']}" if cfg.get("java_home") else " (native)"))
        if cfg.get("java_home") and not Path(cfg["java_home"]).exists():
            add("todo", f"Java at {cfg['java_home']} is gone", "install.py signal-cli --force")
    else:
        add("todo", "signal-cli", "install.py signal-cli")
    if not cfg.get("bot"):
        add("todo", "the bot's number", "install.py register --number <number>")
    elif not cfg.get("registered"):
        add("todo", f"{cfg['bot']} isn't verified", "install.py verify --code <code>")
    else:
        add("ok", f"bot {cfg['bot']}" + ("" if PIN_FILE.exists() else ", without a registration lock PIN"))
    add("ok", f"owner {cfg['owner']}") if cfg.get("owner") else add("todo", "the owner", "install.py owner --phrase <word>")
    if cfg.get("relay") == "crew":
        add("ok", "relay to KiroCrew's webhook")
    else:
        if cfg.get("kiro_session"):
            known = any(s.get("id") == cfg["kiro_session"] for s in kiro_sessions())
            add("ok" if known else "todo", f"chat {cfg['kiro_session']}" + ("" if known else " no longer exists"), "" if known else "install.py chat")
        else:
            add("todo", "the Kiro chat", "install.py chat")
        if bridge_health(cfg):
            add("ok", f"bridge listening in {cfg.get('kiro_workspace')}")
        elif ks.TOKEN_FILE.exists():
            add("todo", f"bridge not answering: is the Kiro window for {cfg.get('kiro_workspace')} open?", "open that window, or reload it")
        else:
            add("todo", "the bridge extension", "install.py bridge")
    try:
        servers = json.loads(MCP_FILE.read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, ValueError):
        servers = {}
    add("ok", "signal_reply in Kiro's MCP settings") if any(isinstance(v, dict) and is_ours(v) for v in servers.values()) \
        else add("todo", "signal_reply in Kiro's MCP settings", "install.py mcp")
    add("ok", "daemon running") if daemon_up(rcfg) else add("todo", "daemon not running", "install.py start")
    relay = read_pid("relay")
    add("ok", "relay running") if relay else add("todo", "relay not running", "install.py start")
    add("ok", "starts at login") if autostart_file().exists() else add("optional", "starts at login", "install.py autostart")
    return rows


def cmd_doctor(args) -> None:
    rows = checks(read_cfg())
    for state, what, nxt in rows:
        print(f"  {state:<8} {what}" + (f"  (run: {nxt})" if nxt and state != "ok" else ""))
    todo = next((r for r in rows if r[0] in ("todo", "warn")), None)
    print(f"\nNext: {todo[2]}" if todo else "\nAll set. Text the bot from your phone; the message arrives in your Kiro chat.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="install.py", description="Set up kiro-signal. See INSTALL.md.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("signal-cli")
    s.add_argument("--force", action="store_true", help="reinstall even if this release is installed")
    s.set_defaults(fn=cmd_signal_cli)
    s = sub.add_parser("register")
    s.add_argument("--number", required=True)
    s.add_argument("--captcha")
    s.add_argument("--voice", action="store_true")
    s.set_defaults(fn=cmd_register)
    s = sub.add_parser("verify")
    s.add_argument("--code", required=True)
    s.add_argument("--pin")
    s.add_argument("--name", default="Kiro", help="the bot's profile name")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("owner")
    s.add_argument("--phrase", required=True)
    s.add_argument("--timeout", type=int, default=300)
    s.set_defaults(fn=cmd_owner)
    s = sub.add_parser("chat")
    s.add_argument("--session")
    s.add_argument("--workspace")
    s.set_defaults(fn=cmd_chat)
    s = sub.add_parser("bridge")
    s.add_argument("--test", action="store_true")
    s.set_defaults(fn=cmd_bridge)
    s = sub.add_parser("mcp")
    s.add_argument("--name", default="signal")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_mcp)
    s = sub.add_parser("start")
    s.add_argument("--no-relay", action="store_true")
    s.set_defaults(fn=cmd_start)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    s = sub.add_parser("autostart")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_autostart)
    sub.add_parser("crew").set_defaults(fn=cmd_crew)
    sub.add_parser("uninstall").set_defaults(fn=cmd_uninstall)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
