// kiro-signal bridge: lets kiro_signal.py's `kiro` relay type the owner's Signal messages into
// a Kiro chat session, through Kiro's own (internal) kiroAgent.sessions.sendPrompt.
//
// Only the window whose workspace folder is `kiro_workspace` in ~/.kiro-signal/config.json
// listens, on 127.0.0.1 at the port in `kiro_bridge`. Every request needs the token in
// bridge_token.txt, which this writes on first start, and a 127.0.0.1 Host header, so a web
// page can't reach it through DNS rebinding.
//
// Kiro refuses a second prompt while a turn runs, so /prompt answers 409 while the chat is
// busy and the relay keeps the message. With `force`, it first stops the running turn the way
// the chat's Stop button does: a session/cancel over the window's agent server websocket.
// Busy means our own prompt's turn hasn't ended (sendPrompt resolves when it does), Kiro's
// session.json says "in_progress", or Kiro answers that a prompt is already in flight.
// ponytail: session.json, the agent registry command and the websocket are Kiro internals
// (checked on Kiro 1.4.10), used because Kiro exposes no busy flag or cancel command.
// Re-check them after a Kiro update.

const vscode = require("vscode");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const HOME =
  process.env.KIRO_SIGNAL_HOME || path.join(os.homedir(), ".kiro-signal");
const SESSIONS = path.join(os.homedir(), ".kiro", "sessions");
const TOKEN_FILE = path.join(HOME, "bridge_token.txt");
const SESSION_ID = /^sess_[0-9a-f-]{36}$/;
const MAX_BODY = 64 * 1024;
const IN_FLIGHT = /already in-flight/i;
const ownTurns = new Map(); // session id -> promise of our prompt's turn

const samePath = (a, b) =>
  path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function token() {
  if (!fs.existsSync(TOKEN_FILE)) {
    fs.mkdirSync(HOME, { recursive: true });
    fs.writeFileSync(TOKEN_FILE, crypto.randomBytes(32).toString("hex"), {
      mode: 0o600,
    });
  }
  return fs.readFileSync(TOKEN_FILE, "utf8").trim();
}

// Kiro's own record of a session, or undefined when no store holds it.
function sessionRecord(id) {
  for (const dir of fs.readdirSync(SESSIONS)) {
    const file = path.join(SESSIONS, dir, id, "session.json");
    if (fs.existsSync(file)) return JSON.parse(fs.readFileSync(file, "utf8"));
  }
  return undefined;
}

function busy(id) {
  return ownTurns.has(id) || sessionRecord(id)?.status === "in_progress";
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const parts = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY) {
        reject(new Error("body too large"));
        req.destroy();
      } else parts.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(parts).toString("utf8")));
    req.on("error", reject);
  });
}

// The error sendPrompt fails with within 3 s, or null once it hasn't; it resolves only when
// the turn ends.
async function sendPrompt(id, text, log) {
  const turn = Promise.resolve(
    vscode.commands.executeCommand("kiroAgent.sessions.sendPrompt", id, text),
  );
  ownTurns.set(id, turn);
  turn
    .then(
      () => log.info(`turn ended in ${id}`),
      (err) => log.warn(`turn in ${id} failed: ${err?.message ?? err}`),
    )
    .finally(() => ownTurns.get(id) === turn && ownTurns.delete(id));
  return Promise.race([
    turn.then(
      () => null,
      (err) => err,
    ),
    sleep(3000).then(() => null),
  ]);
}

// Stops the session's running turn: session/cancel to this window's agent server, as an observer
// client, which is what the chat's Stop button sends.
async function cancelTurn(workspace, id) {
  const endpoints =
    (await vscode.commands.executeCommand(
      "kiro.agentRegistry.getAgentEndpoints",
    )) ?? [];
  const ep = endpoints.find((e) =>
    (e.folders ?? []).some((f) => samePath(f.path, workspace)),
  );
  if (!ep) throw new Error("no Kiro agent server for this workspace");
  const ws = new WebSocket(
    `ws://127.0.0.1:${ep.port}?token=${encodeURIComponent(ep.token)}`,
  );
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error("agent server did not answer")),
        10000,
      );
      ws.onerror = () => reject(new Error("cannot reach the agent server"));
      ws.onopen = () =>
        ws.send(
          JSON.stringify({
            jsonrpc: "2.0",
            id: 1,
            method: "initialize",
            params: {
              protocolVersion: 1,
              clientCapabilities: {},
              clientInfo: { name: "kiro-signal-bridge", version: "1.0.0" },
            },
          }),
        );
      ws.onmessage = (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.id !== 1 || "method" in msg) return;
        clearTimeout(timer);
        msg.error ? reject(new Error(msg.error.message)) : resolve();
      };
    });
    ws.send(
      JSON.stringify({
        jsonrpc: "2.0",
        method: "session/cancel",
        params: { sessionId: id },
      }),
    );
    await sleep(300); // let the frame go out before closing
  } finally {
    ws.close();
  }
}

// Sends `text`, first stopping a running turn when `force` is set. Returns [status, body].
async function prompt(cfg, id, text, force, log) {
  if (busy(id)) {
    if (!force) return [409, { error: "session is mid-turn" }];
    await cancelTurn(cfg.kiro_workspace, id);
    log.info(`cancelled the running turn in ${id}`);
  }
  // A cancelled turn takes a moment to wind down; Kiro refuses the prompt until it has.
  const deadline = Date.now() + (force ? 60000 : 0);
  for (;;) {
    if (!busy(id)) {
      const err = await sendPrompt(id, text, log);
      if (!err) return [202, { session: id, accepted: true }];
      if (!IN_FLIGHT.test(String(err?.message ?? err)))
        return [502, { error: String(err?.message ?? err) }];
    }
    if (Date.now() >= deadline)
      return force
        ? [504, { error: "the running turn did not stop" }]
        : [409, { error: "session is mid-turn" }];
    await sleep(500);
  }
}

function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(path.join(HOME, "config.json"), "utf8"));
  } catch {
    return undefined;
  }
}

function activate(context) {
  const cfg = readConfig();
  const folders = (vscode.workspace.workspaceFolders ?? []).map(
    (f) => f.uri.fsPath,
  );
  // Silent in every other window, and until install.py has picked a chat.
  if (
    !cfg?.kiro_workspace ||
    !folders.some((f) => samePath(f, cfg.kiro_workspace))
  )
    return;

  const log = vscode.window.createOutputChannel("Kiro Signal Bridge", {
    log: true,
  });
  context.subscriptions.push(log);
  // bridgePort is set only in a side-by-side test build (install.py bridge --test).
  const port =
    context.extension.packageJSON.bridgePort ??
    Number(new URL(cfg.kiro_bridge || "http://127.0.0.1:39276").port);
  const secret = Buffer.from(token());

  const server = http.createServer(async (req, res) => {
    const reply = (code, body) => {
      res.writeHead(code, { "Content-Type": "application/json" });
      res.end(JSON.stringify(body));
    };
    try {
      if (req.headers.host !== `127.0.0.1:${port}`)
        return reply(403, { error: "host not allowed" });
      const given = Buffer.from(
        String(req.headers.authorization || "").replace(/^Bearer /, ""),
      );
      if (
        given.length !== secret.length ||
        !crypto.timingSafeEqual(given, secret)
      ) {
        return reply(401, { error: "bad token" });
      }
      const url = new URL(req.url, `http://127.0.0.1:${port}`);

      if (req.method === "GET" && url.pathname === "/health") {
        return reply(200, { ok: true, workspace: cfg.kiro_workspace });
      }
      if (req.method === "POST" && url.pathname === "/session") {
        // A new chat in this window, for tests or a dedicated Signal chat.
        const created = await vscode.commands.executeCommand(
          "kiroAgent.sessions.create",
        );
        log.info(`created ${created?.sessionId}`);
        return reply(200, { session: created?.sessionId });
      }

      const body =
        req.method === "POST" ? JSON.parse((await readBody(req)) || "{}") : {};
      const id =
        req.method === "POST" ? body.session : url.searchParams.get("session");
      if (!SESSION_ID.test(String(id)))
        return reply(400, { error: "session must be a sess_<uuid> id" });
      const record = sessionRecord(id);
      if (!record) return reply(404, { error: `no session ${id}` });

      if (req.method === "GET" && url.pathname === "/status") {
        return reply(200, {
          session: id,
          title: record.title,
          status: record.status,
          busy: busy(id),
        });
      }
      if (req.method === "POST" && url.pathname === "/prompt") {
        const text = String(body.text ?? "").trim();
        // A leading "/" runs a slash command, which also aborts a running turn.
        if (!text || text.startsWith("/"))
          return reply(400, { error: "text is empty or starts with /" });
        const [code, result] = await prompt(
          cfg,
          id,
          text,
          body.force === true,
          log,
        );
        if (code === 202)
          log.info(
            `prompt sent to ${id} (${text.length} chars${body.force ? ", forced" : ""})`,
          );
        return reply(code, result);
      }
      return reply(404, { error: "not found" });
    } catch (err) {
      log.error(String(err?.stack ?? err));
      return reply(500, { error: String(err?.message ?? err) });
    }
  });
  server.on("error", (err) =>
    log.error(`cannot listen on 127.0.0.1:${port}: ${err.message}`),
  );
  server.listen(port, "127.0.0.1", () =>
    log.info(`listening on 127.0.0.1:${port} for ${cfg.kiro_workspace}`),
  );
  context.subscriptions.push({ dispose: () => server.close() });
}

module.exports = { activate, deactivate() {} };
