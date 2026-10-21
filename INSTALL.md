# Installing kiro-signal

This is the runbook for the AI agent doing the install. The person in the chat is "the user". Follow the steps in order.

Every step is one `install.py` command. Each is safe to rerun and never prompts. After any step, `install.py doctor` lists what's done and the next command to run.

## Rules

- **Run the commands yourself.** Ask the user only for: the bot's phone number, the captcha link, the verification code, and sending one text. Ask for each when you reach it, one at a time, and say why you need it.
- **Never use the user's own number for the bot.** If they offer it, explain that registering it signs their phone out of Signal, and ask for another number.
- **Keep secrets out of the chat and the repo.** The registration lock PIN stays in `~/.kiro-signal/registration-lock-pin.txt`; don't print it. Don't write the captcha link or the code anywhere.
- **When a command fails, its message says what to do.** Do that. If it fails again, show the user the error and the log in `~/.kiro-signal/logs/`.
- **Installing Java or Python may show an admin prompt.** Tell the user to accept it.

In the commands, `PY` is the Python you use (3.9 or newer) and `KS` is the clone's folder. Use the same Python for every step: the MCP entry records it.

## 0. Clone and check Python

Clone into the user's home folder, not inside their project. `$HOME` works in PowerShell, bash and zsh:

```
git clone https://github.com/cdelog/kiro-signal "$HOME/kiro-signal"
```

If it's already there, `git -C "$HOME/kiro-signal" pull` instead.

Find Python 3.9 or newer: `python3 --version`, `python --version`, or on Windows `py -3 --version`. If there's none:

- Windows: `winget install --id Python.Python.3.13 -e`
- macOS: `brew install python`, or `xcode-select --install`
- Linux: the distribution's `python3` package

## 1. signal-cli

```
PY KS/install.py signal-cli
```

It downloads the latest signal-cli and checks it against GitHub's published SHA-256. If Java 25 or newer is missing, it stops and prints the command that installs it. Run that, then rerun this step. The installer finds a new Java in its usual folder, even if this terminal's PATH doesn't have it yet.

## 2. The bot's number

Ask the user: _"Which phone number should the bot use? It can't be the one your own Signal uses, and it needs to receive one SMS or voice call."_ Write it in international form, like `+15551234567`.

```
PY KS/install.py register --number +15551234567
```

Signal usually wants a captcha first, and the command says so. Ask the user: _"Open https://signalcaptchas.org/registration/generate.html and solve the captcha. Then right-click 'Open Signal', copy the link, and paste it here. It expires within a few minutes."_ Run this as soon as you have the link:

```
PY KS/install.py register --number +15551234567 --captcha "signalcaptcha://..."
```

If the number can't receive SMS (a landline, some VoIP numbers), run it once without `--voice`, wait a minute, then run it again with `--voice`.

## 3. The code

Ask the user for the code that arrived, then:

```
PY KS/install.py verify --code 123456
```

This also sets a registration lock PIN, so nobody who gets this number later can take over the bot, and names the bot "Kiro". Tell the user where the PIN file is and ask them to save the PIN in their password manager.

## 4. Pair the user's phone

Make up a pairing word nobody would send by accident, like `otter-lamp-42`. Ask the user: _"On your phone, open Signal, start a new chat with +15551234567, and send: otter-lamp-42"_. Then run this; it waits up to 5 minutes:

```
PY KS/install.py owner --phrase otter-lamp-42
```

When it succeeds, the bot sends the user's phone a confirmation.

## 5. This chat

```
PY KS/install.py chat --workspace "<this workspace's root folder>"
```

It picks the chat that's running a turn right now, which is this one. If it lists several chats instead, ask the user which one by its title, and rerun with `--session <id>`.

## 6. The reply tool

```
PY KS/install.py mcp
```

This adds a `signal` server to `~/.kiro/settings/mcp.json`, with `signal_reply` approved to run without asking, so replies reach the user while they're away. If it reports that a different `signal` server already exists, rerun with `--name kiro-signal`. Use `--force` only if the user says to replace theirs.

## 7. The bridge

```
PY KS/install.py bridge
```

It installs the Kiro extension and waits for it to listen. If it says the window needs a reload, warn the user that reloading ends this turn. Ask them to reload (Command Palette: "Developer: Reload Window") and then send "continue". Pick up at step 8.

## 8. Start

```
PY KS/install.py start
PY KS/install.py autostart
PY KS/install.py doctor
```

`autostart` brings kiro-signal back after a restart. Ask the user first; most will want it. `doctor` should show every line as `ok`, apart from `optional` ones.

## 9. Hand over

Tell the user, in this chat:

- It's ready. Anything they text the bot comes into this chat once your turn ends.
- To turn on Read receipts and Typing indicators on their phone (Signal, Settings, Privacy), so they can see when a message is queued, read and being worked on.
- `force` or `flush` stops a running turn and puts their message in now.
- The computer and this Kiro window need to stay open.

Then end your turn. Their first message arrives as the next prompt in this chat. Answer it with `signal_reply`, which confirms the loop works both ways.

## KiroCrew instead of a Kiro chat

To answer from a [KiroCrew](https://github.com/kirodotdev/KiroCrew) agent instead, do steps 0 to 4 and 6, then:

1. In KiroCrew, turn on Settings, Developer, Feature Previews, Webhooks. Create a webhook source for the agent that should answer, with signing on.
2. The user stores its token and signing secret as user environment variables, in their own terminal, so the secrets never pass through a chat:
   - Windows: `setx KIROCREW_WEBHOOK_TOKEN "<token>"` and `setx KIROCREW_WEBHOOK_SIGNING_SECRET "<secret>"`
   - macOS and Linux: export both in their shell profile
3. Run `PY KS/install.py crew`, then `PY KS/install.py start`.
4. KiroCrew copies MCP servers into each agent's config when MCP settings are saved in its dashboard, or when its gateway restarts. Switch the `signal` server off and on again there.

Each webhook turn runs in a fresh KiroCrew session. The relay asks every turn to save a summary with `register_hook` (hook id `signal`), which KiroCrew adds to the next message.

## Troubleshooting

- **The daemon doesn't come up:** read `~/.kiro-signal/logs/daemon.log`.
- **Messages stop arriving after a Kiro update:** run `doctor`, then open the "Kiro Signal Bridge" output channel in that window. The bridge depends on Kiro internals (see README, Limits). Messages stay queued in `~/.kiro-signal/pending.json` meanwhile.
- **`doctor` warns that signal-cli is old:** `install.py signal-cli`, then `install.py stop` and `install.py start`.
- **A different chat should get the messages:** `install.py chat --session <id>`, then `install.py stop`, `install.py start`, and reload that Kiro window.
- **Nothing reaches the phone:** check that `signal_reply` appears in Kiro's MCP panel, and look for errors in `~/.kiro-signal/logs/daemon.log`.
