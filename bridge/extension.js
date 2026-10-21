// Lets kiro_signal.py's relay type the owner's Signal messages into one Kiro chat.
//
// It listens only in the window whose folder is `kiro_workspace` in ~/.kiro-signal/config.json,
// on 127.0.0.1. Every request needs the token in bridge_token.txt and a 127.0.0.1 Host header,
// so a web page can't reach it through DNS rebinding.
//
// Kiro offers no documented way to do this, so the bridge uses three internals, checked on Kiro
// 1.4.10, which an update can break:
//   - the command kiroAgent.sessions.sendPrompt, which resolves when the turn ends;
//   - each chat's session.json, whose status is "in_progress" while a turn runs;
//   - the window's agent server, which takes session/cancel, as the chat's Stop button sends it,
//     and session/prompt with image blocks, as the chat sends a picture you drag in.

const vscode = require('vscode');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

const HOME =
  process.env.KIRO_SIGNAL_HOME || path.join(os.homedir(), '.kiro-signal');
const KIRO_SESSIONS = path.join(os.homedir(), '.kiro', 'sessions');
const TOKEN_FILE = path.join(HOME, 'bridge_token.txt');
const SESSION_ID = /^sess_[0-9a-f-]{36}$/;
const MAX_BODY_BYTES = 20 * 1024 * 1024; // a prompt can carry photos, base64
const IMAGE_TYPES = /^image\/(png|jpeg|gif|webp)$/;
const KIRO_BUSY = /already in-flight/i;
const turnsWeStarted = new Map(); // session id to the promise of its turn

const samePath = (a, b) =>
  path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function token() {
  if (!fs.existsSync(TOKEN_FILE)) {
    fs.mkdirSync(HOME, { recursive: true });
    fs.writeFileSync(TOKEN_FILE, crypto.randomBytes(32).toString('hex'), {
      mode: 0o600,
    });
  }
  return fs.readFileSync(TOKEN_FILE, 'utf8').trim();
}

function sessionRecord(id) {
  for (const store of fs.readdirSync(KIRO_SESSIONS)) {
    const file = path.join(KIRO_SESSIONS, store, id, 'session.json');
    if (fs.existsSync(file)) return JSON.parse(fs.readFileSync(file, 'utf8'));
  }
  return undefined;
}

function busy(id) {
  return turnsWeStarted.has(id) || sessionRecord(id)?.status === 'in_progress';
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const parts = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('body too large'));
        req.destroy();
      } else parts.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(parts).toString('utf8')));
    req.on('error', reject);
  });
}

// Kiro refuses a prompt within a moment or not at all, so a turn that hasn't failed after 3 s is
// in. Returns the refusal, or null.
function track(id, turn, log, onEnd = () => {}) {
  turnsWeStarted.set(id, turn);
  turn
    .then(
      () => log.info(`turn ended in ${id}`),
      (err) => log.warn(`turn in ${id} failed: ${err?.message ?? err}`),
    )
    .finally(() => {
      onEnd();
      if (turnsWeStarted.get(id) === turn) turnsWeStarted.delete(id);
    });
  return Promise.race([
    turn.then(
      () => null,
      (err) => err,
    ),
    sleep(3000).then(() => null),
  ]);
}

// A JSON-RPC connection to this window's agent server, beside the chat's own.
async function agentServer(workspace, log) {
  const servers =
    (await vscode.commands.executeCommand(
      'kiro.agentRegistry.getAgentEndpoints',
    )) ?? [];
  const server = servers.find((s) =>
    (s.folders ?? []).some((f) => samePath(f.path, workspace)),
  );
  if (!server) throw new Error('no Kiro agent server for this workspace');
  const ws = new WebSocket(
    `ws://127.0.0.1:${server.port}?token=${encodeURIComponent(server.token)}`,
  );
  const waiting = new Map();
  let nextId = 1;
  ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if ('method' in msg) {
      if (msg.id !== undefined) {
        // A question meant for the chat's own connection; this one can't answer it.
        log.info(`the agent server asked the bridge: ${msg.method}`);
        ws.send(
          JSON.stringify({
            jsonrpc: '2.0',
            id: msg.id,
            error: { code: -32601, message: 'not handled by the bridge' },
          }),
        );
      }
      return;
    }
    const call = waiting.get(msg.id);
    if (!call) return;
    waiting.delete(msg.id);
    msg.error
      ? call.reject(new Error(msg.error.message))
      : call.resolve(msg.result);
  };
  ws.onclose = () => {
    for (const call of waiting.values())
      call.reject(new Error('the agent server closed the connection'));
    waiting.clear();
  };
  await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("the agent server didn't answer")),
      10000,
    );
    ws.onopen = () => {
      clearTimeout(timer);
      resolve();
    };
    ws.onerror = () => reject(new Error("can't reach the agent server"));
  });
  const request = (method, params) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      waiting.set(id, { resolve, reject });
      ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
    });
  await request('initialize', {
    protocolVersion: 1,
    clientCapabilities: {},
    clientInfo: { name: 'kiro-signal-bridge', version: '1.1.0' },
  });
  return {
    request,
    notify: (method, params) =>
      ws.send(JSON.stringify({ jsonrpc: '2.0', method, params })),
    close: () => ws.close(),
  };
}

function sendPrompt(id, text, log) {
  return track(
    id,
    Promise.resolve(
      vscode.commands.executeCommand('kiroAgent.sessions.sendPrompt', id, text),
    ),
    log,
  );
}

// sendPrompt takes text only, so a prompt with photos goes to the agent server directly.
async function sendPromptWithImages(workspace, id, text, images, log) {
  const server = await agentServer(workspace, log);
  const blocks = [
    { type: 'text', text },
    ...images.map((i) => ({
      type: 'image',
      data: i.data,
      mimeType: i.mimeType,
    })),
  ];
  return track(
    id,
    server.request('session/prompt', { sessionId: id, prompt: blocks }),
    log,
    server.close,
  );
}

async function stopTurn(workspace, id, log) {
  const server = await agentServer(workspace, log);
  try {
    server.notify('session/cancel', { sessionId: id });
    await sleep(300); // let the frame go out before the socket closes
  } finally {
    server.close();
  }
}

// [status, body]: 202 in, 409 busy, 504 the forced stop didn't take within 60 s.
async function prompt(cfg, id, text, images, force, log) {
  if (busy(id)) {
    if (!force) return [409, { error: 'session is mid-turn' }];
    await stopTurn(cfg.kiro_workspace, id, log);
    log.info(`stopped the running turn in ${id}`);
  }
  const deadline = Date.now() + (force ? 60000 : 0); // a stopped turn takes a moment to wind down
  for (;;) {
    if (!busy(id)) {
      const refusal = images.length
        ? await sendPromptWithImages(cfg.kiro_workspace, id, text, images, log)
        : await sendPrompt(id, text, log);
      if (!refusal) return [202, { session: id, accepted: true }];
      if (!KIRO_BUSY.test(String(refusal?.message ?? refusal)))
        return [502, { error: String(refusal?.message ?? refusal) }];
    }
    if (Date.now() >= deadline)
      return force
        ? [504, { error: "the running turn didn't stop" }]
        : [409, { error: 'session is mid-turn' }];
    await sleep(500);
  }
}

function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(path.join(HOME, 'config.json'), 'utf8'));
  } catch {
    return undefined;
  }
}

function activate(context) {
  const cfg = readConfig();
  const folders = (vscode.workspace.workspaceFolders ?? []).map(
    (f) => f.uri.fsPath,
  );
  if (
    !cfg?.kiro_workspace ||
    !folders.some((f) => samePath(f, cfg.kiro_workspace))
  )
    return;

  const log = vscode.window.createOutputChannel('Kiro Signal Bridge', {
    log: true,
  });
  context.subscriptions.push(log);
  const port = Number(
    new URL(cfg.kiro_bridge || 'http://127.0.0.1:39276').port,
  );
  const secret = Buffer.from(token());

  const server = http.createServer(async (req, res) => {
    const reply = (code, body) => {
      res.writeHead(code, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(body));
    };
    try {
      if (req.headers.host !== `127.0.0.1:${port}`)
        return reply(403, { error: 'host not allowed' });
      const given = Buffer.from(
        String(req.headers.authorization || '').replace(/^Bearer /, ''),
      );
      if (
        given.length !== secret.length ||
        !crypto.timingSafeEqual(given, secret)
      )
        return reply(401, { error: 'bad token' });
      const url = new URL(req.url, `http://127.0.0.1:${port}`);

      if (req.method === 'GET' && url.pathname === '/health')
        return reply(200, { ok: true, workspace: cfg.kiro_workspace });

      const body =
        req.method === 'POST' ? JSON.parse((await readBody(req)) || '{}') : {};
      const id =
        req.method === 'POST' ? body.session : url.searchParams.get('session');
      if (!SESSION_ID.test(String(id)))
        return reply(400, { error: 'session must be a sess_<uuid> id' });
      const record = sessionRecord(id);
      if (!record) return reply(404, { error: `no session ${id}` });

      if (req.method === 'GET' && url.pathname === '/status')
        return reply(200, {
          session: id,
          title: record.title,
          status: record.status,
          busy: busy(id),
        });
      if (req.method === 'POST' && url.pathname === '/prompt') {
        const text = String(body.text ?? '').trim();
        if (!text || text.startsWith('/'))
          return reply(400, { error: 'text is empty or starts with /' });
        const images = Array.isArray(body.images) ? body.images : [];
        if (
          !images.every(
            (i) =>
              IMAGE_TYPES.test(String(i?.mimeType)) &&
              typeof i?.data === 'string',
          )
        )
          return reply(400, {
            error: 'images must be {mimeType: image/png|jpeg|gif|webp, data: base64}',
          });
        const [code, result] = await prompt(
          cfg,
          id,
          text,
          images,
          body.force === true,
          log,
        );
        if (code === 202)
          log.info(
            `prompt sent to ${id} (${text.length} chars, ${images.length} photo(s)${body.force ? ', forced' : ''})`,
          );
        return reply(code, result);
      }
      return reply(404, { error: 'not found' });
    } catch (err) {
      log.error(String(err?.stack ?? err));
      return reply(500, { error: String(err?.message ?? err) });
    }
  });
  server.on('error', (err) =>
    log.error(`can't listen on 127.0.0.1:${port}: ${err.message}`),
  );
  server.listen(port, '127.0.0.1', () =>
    log.info(`listening on 127.0.0.1:${port} for ${cfg.kiro_workspace}`),
  );
  context.subscriptions.push({ dispose: () => server.close() });
}

module.exports = { activate, deactivate() {} };
