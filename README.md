# kiro-signal

Text your Kiro chat from your phone, over Signal, and get its answers back there.

You message a Signal contact called Kiro. The message lands in the Kiro chat you picked, as if you'd typed it. The agent answers with a `signal_reply` tool, so its answer comes to your phone. Signal's own marks show where your message is: delivered means it's queued, read means it's in the chat, and typing means the agent is working.

> **AI agents:** to install this for a user, follow [INSTALL.md](INSTALL.md) step by step.

## Install: ask your AI

Open Kiro in the project you want to reach from your phone, start a chat, and paste:

```
Clone https://github.com/cdelog/kiro-signal to ~/kiro-signal and install it by following its INSTALL.md. Connect it to this chat.
```

The agent does the setup and checks each step. It stops four times, for things only you can do:

1. **Give it a phone number for the bot.** Not your own: registering a number signs out whatever Signal install uses it. A cheap prepaid SIM works, or any number that can take a voice call.
2. **Solve a captcha** in your browser and paste back the link it gives you.
3. **Read out the code** Signal sends to that number.
4. **Send the bot one text** from your phone, with a word the agent gives you, so it knows the messages are yours.

It takes about 15 minutes, mostly downloads and waiting for the code.

**Needs:** Kiro on Windows, macOS or Linux, Python 3.9 or newer, and Java 25 or newer (the agent installs Java if it's missing; Linux on x86_64 doesn't need it).

## Using it

- **Send anything.** If the agent is in the middle of a turn, your message waits and goes in when the turn ends. Several waiting messages go in together.
- **`force`** or **`flush`** on its own, or `force: <text>`, stops the running turn and puts everything in now. "Force push the branch" is an ordinary message.
- **Turn on Read receipts and Typing indicators** on your phone (Signal, Settings, Privacy) to see the marks. Words come only when something fails.
- **Your computer must be on, with that Kiro window open.** A message sent while Kiro is closed waits, and goes in when the window opens.

## How it works

```
your phone --Signal--> signal-cli daemon (127.0.0.1)
                         |-- events --> kiro_signal.py kiro --> bridge extension --> your Kiro chat
                         '-- send   <-- kiro_signal.py mcp  <-- signal_reply  <----- the agent
```

- **[signal-cli](https://github.com/AsamK/signal-cli)**, a third-party Signal client, runs as a local daemon and holds the bot's account.
- **`kiro_signal.py`** is the relay and the MCP server: about 450 lines of standard-library Python.
- **`bridge/`** is a Kiro extension, about 270 lines with no dependencies. It hands each message to the chat, and tells the relay when the chat is busy.
- **`install.py`** sets it all up. Every step is one command that never prompts, so an agent can run it.

## Security

Read this before installing.

- **A Signal message becomes a prompt on your computer.** If the agent runs with tools approved automatically, a message can do whatever the agent can.
- **Only you get through.** The relay accepts direct messages from one Signal account, yours, checked by its account ID, which Signal authenticates. Messages from anyone else, and group messages, are dropped.
- **Anyone with one of your Signal devices can drive your agent.** Check Signal, Settings, Linked devices, and turn on Registration Lock for your own account.
- **Text the agent reads is the bigger risk.** Web pages, issues and emails can carry instructions. That's true without kiro-signal, but you aren't watching when you're away, so keep auto-approval to tools you'd trust unattended.
- **Replies only reach you.** `signal_reply` sends to your account whatever arguments it's given.
- **Everything listens on 127.0.0.1 only.** The signal-cli daemon's HTTP API has no authentication, so any program on your computer can send as the bot. The bridge needs a random token and refuses other Host headers, so a web page can't reach it.
- **`~/.kiro-signal/signal-data` holds the bot's identity keys.** Anything that can read it can impersonate the bot.
- **signal-cli is checked before it's installed:** the download must match the SHA-256 that GitHub publishes for the release.

## Limits

- **Unofficial.** Signal has no bot API, and signal-cli isn't Signal's. A bot number can hit rate limits or captchas. signal-cli releases older than about three months stop working: `install.py doctor` warns you, and `install.py signal-cli` updates.
- **The bridge relies on Kiro internals that aren't documented:** `kiroAgent.sessions.sendPrompt`, Kiro's session records, and the agent server's cancel message. It was checked on Kiro 1.4.10. A Kiro update can break it; your messages stay queued until it's fixed.
- **One chat at a time.** Text only: no attachments, reactions or groups.
- **Updating the bridge needs a window reload,** which ends a running turn.

## Other ways to use it

- **Any MCP client:** `kiro_signal.py mcp` is a plain stdio MCP server with one tool, `signal_reply`.
- **[KiroCrew](https://github.com/kirodotdev/KiroCrew):** `install.py crew` relays to KiroCrew's inbound webhook instead of a Kiro chat. See INSTALL.md.
- **Polling:** `kiro_signal.py wait [seconds]` prints your next message and exits, for an agent that checks Signal itself. Start the daemon alone with `install.py start --no-relay`.

## Commands

| Command | What it does |
| --- | --- |
| `install.py doctor` | What's set up, what's running, and the next command |
| `install.py signal-cli` | Download or update signal-cli, and find Java |
| `install.py register --number +1...` | Register the bot's number (`--captcha`, `--voice`) |
| `install.py verify --code 123456` | Finish registering, set a registration lock PIN and the name "Kiro" |
| `install.py owner --phrase WORD` | Pair with the phone that texts WORD to the bot |
| `install.py chat` | Pick the Kiro chat your messages go to (`--session` to name one) |
| `install.py mcp` | Add `signal_reply` to `~/.kiro/settings/mcp.json` |
| `install.py bridge` | Build and install the Kiro extension |
| `install.py start` / `stop` | Start or stop the daemon and the relay |
| `install.py autostart` | Start at login (`--off` to undo) |
| `install.py uninstall` | Stop, and remove the autostart entry, the MCP server and the extension |

Everything kiro-signal writes lives in `~/.kiro-signal`, apart from one entry in Kiro's `mcp.json`, the extension, and the autostart entry. Set `KIRO_SIGNAL_HOME` to move it.

## Uninstall

```
python ~/kiro-signal/install.py uninstall
```

Then delete `~/kiro-signal` and `~/.kiro-signal`.

## Tests

```
python tests/test_kiro_signal.py
python tests/test_install.py
```

They stub out signal-cli, Kiro and the network, and never touch your real settings.

## License

MIT. signal-cli is GPL-3.0; it's downloaded from its own releases at install time, not bundled. Not affiliated with Signal, Kiro or AWS.
