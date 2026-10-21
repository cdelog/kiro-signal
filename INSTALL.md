# Installing kiro-signal

This is the runbook for the AI agent doing the install. The person in the chat is "the user". Follow the steps in order.

Each step is one `install.py` command that's safe to rerun and never prompts. `install.py doctor` shows what's done and the next command, any time.

## Rules

- **Run the commands yourself.** Ask the user only for the bot's number, the captcha link, the verification code, and one text from their phone. Ask for each when you reach it, and say why you need it.
- **Never use the user's own number for the bot.** If they offer it, explain that registering it signs their phone out of Signal, and ask for another.
- **Keep secrets out of the chat and the repo.** Don't print the registration lock PIN, and don't write the captcha link or the code anywhere.
- **When a command fails, its message says what to do.** Do that. If it fails again, show the user the error and the log in `~/.kiro-signal/logs/`.
- **Installing Java or Python may show a system prompt.** Tell the user to accept it.

`PY` below is the Python you use, 3.9 or newer, and `KS` is the clone's folder. Use the same Python for every step, because the MCP entry records it.

## 0. Clone, and find Python

Clone into the user's home folder, not their project. `$HOME` works in PowerShell, bash and zsh.

```
git clone https://github.com/cdelog/kiro-signal "$HOME/kiro-signal"
```

If it's already there, run `git -C "$HOME/kiro-signal" pull` instead.

Find Python 3.9 or newer with `python3 --version`, `python --version`, or on Windows `py -3 --version`. If there's none, install it:

- Windows: `winget install --id Python.Python.3.13 -e`
- macOS: `brew install python`
- Linux: the distribution's `python3` package

## 1. signal-cli

```
PY KS/install.py signal-cli
```

It downloads the latest signal-cli and checks it against the SHA-256 GitHub publishes. Without Java 25 or newer, it stops and prints the command that installs Java. Run that, then this step again. The installer looks in Java's usual install folders, so the new Java doesn't need to be on this terminal's PATH.

## 2. The bot's number

Ask the user: _"Which phone number should the bot use? It can't be the one your own Signal uses, and it needs to get one SMS or voice call."_ Write it in international form, like `+15551234567`.

```
PY KS/install.py register --number +15551234567
```

Signal usually wants a captcha first, and the command says so. Ask the user: _"Open https://signalcaptchas.org/registration/generate.html and solve the captcha. Then right-click 'Open Signal', copy the link, and paste it here. It expires in a few minutes."_ Run this as soon as you have it:

```
PY KS/install.py register --number +15551234567 --captcha "signalcaptcha://..."
```

If the number can't get an SMS, like a landline, run it once as above, wait a minute, then again with `--voice`.

## 3. The code

Ask the user for the code, then:

```
PY KS/install.py verify --code 123456
```

This also names the bot "Kiro" and sets a registration lock PIN, so whoever gets this number next can't take over the bot. Tell the user where the PIN file is, and ask them to save the PIN in their password manager.

## 4. Pair the user's phone

Make up a word nobody would send by accident, like `otter-lamp-42`. Ask the user: _"On your phone, open Signal, start a chat with +15551234567, and send: otter-lamp-42"_. Then run this, which waits up to 5 minutes:

```
PY KS/install.py owner --phrase otter-lamp-42
```

When it works, the bot sends the user's phone a confirmation.

## 5. This chat

```
PY KS/install.py chat --workspace "<this workspace's root folder>"
```

It picks the chat running a turn right now, which is this one. If it lists several instead, ask the user which one by its title, and rerun with `--session <id>`.

## 6. The reply tool

```
PY KS/install.py mcp
```

This adds a `signal` server to `~/.kiro/settings/mcp.json`, with `signal_reply` approved to run without asking, so replies reach the user while they're away. If a different `signal` server is already there, rerun with `--name kiro-signal`. Use `--force` only if the user says to replace theirs.

## 7. The bridge

```
PY KS/install.py bridge
```

It installs the Kiro extension and waits for it to listen. If it says the window needs a reload, warn the user that a reload ends this turn, and ask them to reload (Command Palette, "Developer: Reload Window") and then send "continue". Carry on at step 8.

## 8. Start

```
PY KS/install.py start
PY KS/install.py autostart
PY KS/install.py doctor
```

`autostart` brings kiro-signal back after a restart. Ask the user first; most want it. Every line of `doctor` should read `ok`, apart from autostart if they said no.

## 9. Hand over

Tell the user, in this chat:

- It's ready. Whatever they text the bot comes into this chat when your turn ends.
- They should turn on Read receipts and Typing indicators in Signal, under Settings, Privacy. Then delivered means queued, read means it's in the chat, and typing means the chat is mid-turn, whoever started it.
- **A message waits for the running turn to end, unless it starts with `force:`.** That stops the turn and puts their message in now, for example `force: stop and look at the CI logs`. `force` or `flush` alone does the same with whatever is queued.
- Photos work both ways: theirs arrive in this chat as images, and you can send them screenshots with `signal_reply`'s `attachments`.
- The computer and this Kiro window need to stay open.

Then end your turn. Their first message arrives as this chat's next prompt. Answer it with `signal_reply`, which proves the loop both ways.

## Troubleshooting

- **The daemon doesn't come up.** Read `~/.kiro-signal/logs/daemon.log`.
- **Messages stop arriving after a Kiro update.** Run `doctor`, then look at the "Kiro Signal Bridge" output channel in that window. The bridge relies on Kiro internals, listed at the top of `bridge/extension.js`. The messages wait in `~/.kiro-signal/pending.json` meanwhile.
- **`doctor` says signal-cli is old.** Run `install.py signal-cli`, then `install.py stop` and `install.py start`.
- **Another chat should get the messages.** Run `install.py chat --session <id>`, `install.py stop` and `install.py start`, then reload that Kiro window.
- **Nothing reaches the phone.** Check that `signal_reply` shows in Kiro's MCP panel, and look for errors in `~/.kiro-signal/logs/daemon.log`.
