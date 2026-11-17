// Local parent page for the token bridge. Dashboard and parent are different
// origins. The served dashboard's origin hash is stubbed to this parent.
// A decoy parent on a third origin must receive nothing, which fails if the
// dashboard posts with targetOrigin '*'.

import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PIN = "246801";
const BEARER = "fixture-session";
const FIXTURE = path.join(ROOT, ".cursor/skills/verify-aeries-dashboard/helpers/fixture_api.py");

const PARENT_HTML = `<!DOCTYPE html>
<meta charset="utf-8">
<title>bridge parent</title>
<style>
  html, body { margin: 0; height: 100%; background: #111; }
  iframe { width: 1400px; height: 1000px; border: 0; background: #fff; }
  #log { position: absolute; left: -9999px; }
</style>
<pre id="log"></pre>
<iframe id="dash" title="dashboard"></iframe>
<script>
const params = new URLSearchParams(location.search);
const childUrl = params.get('child');
const childOrigin = new URL(childUrl).origin;
const initial = params.get('token');
const saved = { token: initial ? initial : null };
const frame = document.getElementById('dash');
window.__log = [];
window.__saved = saved;
function note(entry) {
  window.__log.push(entry);
  document.getElementById('log').textContent = JSON.stringify(window.__log);
}
window.addEventListener('message', (event) => {
  if (event.origin !== childOrigin) return;
  if (event.source !== frame.contentWindow) return;
  const data = event.data;
  if (!data || typeof data !== 'object' || typeof data.type !== 'string') return;
  const entry = { type: data.type, keys: Object.keys(data) };
  if (Object.prototype.hasOwnProperty.call(data, 'token')) entry.token = data.token;
  note(entry);
  if (data.type === 'ad-token-request') {
    frame.contentWindow.postMessage({ type: 'ad-token', token: saved.token }, childOrigin);
  } else if (data.type === 'ad-token-save' && typeof data.token === 'string') {
    saved.token = data.token;
  } else if (data.type === 'ad-token-clear') {
    saved.token = null;
  }
});
frame.src = childUrl;
</script>
`;

function log(message) {
  process.stderr.write(`${message}\n`);
}

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

function listen(handler) {
  const server = http.createServer(handler);
  return new Promise((resolve, reject) => {
    server.listen(0, "127.0.0.1", () => resolve(server));
    server.on("error", reject);
  });
}

function originOf(server) {
  const { port } = server.address();
  return `http://127.0.0.1:${port}`;
}

function htmlResponse(res, body) {
  res.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(body);
}

async function waitFor(fn, label, timeout = 10000) {
  const start = Date.now();
  let last = "";
  while (Date.now() - start < timeout) {
    try {
      const value = await fn();
      if (value) return value;
      last = JSON.stringify(value);
    } catch (error) {
      last = error.message || String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`timeout: ${label}: ${last}`);
}

async function pageState(send, sessionId) {
  return evaluate(send, sessionId, `(() => {
    const screen = document.getElementById('lockScreen');
    const pin = document.getElementById('pinInput');
    return {
      href: location.href,
      ready: document.readyState,
      framed: window.parent !== window,
      pin: !!(pin && screen && !screen.hidden),
      standing: !!(screen && screen.hidden && [...document.querySelectorAll('h2')].some((node) => node.textContent === 'Current standing')),
      title: document.title,
    };
  })()`);
}

function portFree(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: "127.0.0.1", port }, () => {
      socket.end();
      resolve(false);
    });
    socket.on("error", () => resolve(true));
  });
}

const children = [];
const servers = [];
const dirs = [];

function track(child) {
  children.push(child);
  return child;
}

async function cleanup() {
  for (const server of servers) {
    await new Promise((resolve) => server.close(() => resolve()));
  }
  for (const child of children) {
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch {
      try { child.kill("SIGKILL"); } catch { /* already gone */ }
    }
  }
  for (const dir of dirs) {
    rmSync(dir, { recursive: true, force: true });
  }
}

function connectCdp(url) {
  const ws = new WebSocket(url);
  let seq = 0;
  const pending = new Map();
  const sessions = new Map();
  const contexts = [];
  const worlds = new Map();
  const opened = new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", () => reject(new Error("chrome websocket failed")), { once: true });
  });
  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.method === "Target.attachedToTarget") {
      sessions.set(msg.params.sessionId, msg.params.targetInfo);
      send("Runtime.enable", {}, msg.params.sessionId).catch(() => {});
      send("Page.enable", {}, msg.params.sessionId).catch(() => {});
    }
    if (msg.method === "Target.detachedFromTarget") {
      sessions.delete(msg.params.sessionId);
    }
    if (msg.method === "Runtime.executionContextCreated") {
      const ctx = msg.params.context;
      const aux = ctx.auxData || {};
      contexts.push({
        id: ctx.id,
        sessionId: msg.sessionId || "",
        frameId: aux.frameId || "",
        isDefault: !!(aux.isDefault || aux.type === "default"),
      });
    }
    if (!msg.id || !pending.has(msg.id)) return;
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
  });
  function send(method, params = {}, sessionId) {
    const id = ++seq;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify(payload));
    });
  }
  return { opened, send, sessions, contexts, worlds, close: () => ws.close() };
}

async function evaluate(send, sessionId, expression, contextId) {
  const params = {
    expression,
    awaitPromise: true,
    returnByValue: true,
  };
  if (contextId) params.contextId = contextId;
  const result = await send("Runtime.evaluate", params, sessionId);
  if (result.exceptionDetails) {
    const text = result.exceptionDetails.text || "evaluate failed";
    const desc = result.exceptionDetails.exception?.description || "";
    throw new Error(`${text} ${desc}`.trim());
  }
  return result.result?.value;
}

function sessionFor(sessions, urlPrefix) {
  let found = null;
  for (const [id, info] of sessions) {
    if (info.url && info.url.startsWith(urlPrefix)) found = id;
  }
  return found;
}

async function main() {
  assert(!PARENT_HTML.includes("'*'"), "harness parent posted with a wildcard");
  assert(!PARENT_HTML.includes('"*"'), "harness parent posted with a wildcard");
  assert(PARENT_HTML.includes("postMessage({ type: 'ad-token', token: saved.token }, childOrigin)"), "harness reply target");

  if (!(await portFree(8787))) {
    throw new Error("127.0.0.1:8787 is already in use; the fixture API needs that port");
  }

  const work = mkdtempSync(path.join(tmpdir(), "token-bridge-"));
  dirs.push(work);
  const fixtureLog = path.join(work, "fixture.log");
  const fixture = track(spawn("python3", [FIXTURE], {
    detached: true,
    stdio: ["ignore", "pipe", "pipe"],
  }));
  const fixtureOut = [];
  fixture.stdout.on("data", (chunk) => fixtureOut.push(chunk));
  fixture.stderr.on("data", (chunk) => fixtureOut.push(chunk));

  const parentServer = await listen((req, res) => htmlResponse(res, PARENT_HTML));
  const decoyServer = await listen((req, res) => htmlResponse(res, PARENT_HTML));
  servers.push(parentServer, decoyServer);
  const parentOrigin = originOf(parentServer);
  const decoyOrigin = originOf(decoyServer);

  const pageSource = readFileSync(path.join(ROOT, "index.html"), "utf8");
  assert(!pageSource.includes("ts" + ".net"), "page source names a tailnet host");
  assert(!pageSource.includes("casa" + "smacmini"), "page source names the mini");
  const hashMatch = pageSource.match(/const PARENT_ORIGIN_SHA256 = '([0-9a-f]{64})'/);
  assert(hashMatch, "origin hash missing");
  const stubHash = createHash("sha256").update(parentOrigin).digest("hex");
  const dashHtml = pageSource.replace(hashMatch[1], stubHash);
  assert(dashHtml.includes(`const PARENT_ORIGIN_SHA256 = '${stubHash}'`), "stubbed hash missing");
  assert(!dashHtml.includes("postMessage(message, '*')"), "served page posts to a wildcard");
  const dashboard = (req, res) => htmlResponse(res, dashHtml);
  const dashServer = await listen(dashboard);
  const decoyDashServer = await listen(dashboard);
  servers.push(dashServer, decoyDashServer);
  const dashOrigin = originOf(dashServer);
  const decoyDashOrigin = originOf(decoyDashServer);

  await waitFor(async () => {
    const res = await fetch("http://127.0.0.1:8787/health");
    const body = await res.json();
    return body.ok === true && body.fixture === true;
  }, "fixture health");
  writeFileSync(fixtureLog, Buffer.concat(fixtureOut));

  const debugPort = await new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
    probe.on("error", reject);
  });
  const chromeLog = path.join(work, "chrome.log");
  const chrome = track(spawn("google-chrome", [
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--disable-extensions",
    `--remote-debugging-port=${debugPort}`,
    "--remote-allow-origins=*",
    `--user-data-dir=${path.join(work, "profile")}`,
    "--site-per-process",
    "--window-size=1400,1000",
    "about:blank",
  ], { detached: true, stdio: ["ignore", "pipe", "pipe"] }));
  const chromeOut = [];
  chrome.stdout.on("data", (chunk) => chromeOut.push(chunk));
  chrome.stderr.on("data", (chunk) => chromeOut.push(chunk));

  const version = await waitFor(async () => {
    const res = await fetch(`http://127.0.0.1:${debugPort}/json/version`);
    return res.json();
  }, "chrome debugger");
  const cdp = connectCdp(version.webSocketDebuggerUrl);
  await cdp.opened;
  await cdp.send("Target.setDiscoverTargets", { discover: true });
  await cdp.send("Target.setAutoAttach", {
    autoAttach: true,
    waitForDebuggerOnStart: false,
    flatten: true,
  });

  async function openPage(url) {
    const created = await cdp.send("Target.createTarget", { url });
    const sessionId = await waitFor(
      () => sessionFor(cdp.sessions, url),
      `page ${url}`,
    );
    return { targetId: created.targetId, sessionId };
  }

  function walkFrames(node, out = []) {
    if (!node) return out;
    if (node.frame) out.push(node.frame);
    for (const child of node.childFrames || []) walkFrames(child, out);
    return out;
  }

  async function evalChild(parentSessionId, urlPrefix, expression) {
    await cdp.send("Page.enable", {}, parentSessionId);
    await cdp.send("Runtime.enable", {}, parentSessionId);
    const { frameTree } = await cdp.send("Page.getFrameTree", {}, parentSessionId);
    const frame = walkFrames(frameTree).find((item) => item.url && item.url.startsWith(urlPrefix));
    if (!frame) return null;
    const matches = cdp.contexts.filter((item) => item.frameId === frame.id && item.isDefault);
    const main = matches[matches.length - 1];
    if (main && main.sessionId) {
      return evaluate(cdp.send, main.sessionId, expression, main.id);
    }
    const { targetInfos } = await cdp.send("Target.getTargets");
    const info = targetInfos.find((item) => item.url && item.url.startsWith(urlPrefix));
    if (info) {
      const known = [...cdp.sessions.entries()].find(([, item]) => item.targetId === info.targetId);
      const sessionId = known ? known[0] : (await cdp.send("Target.attachToTarget", {
        targetId: info.targetId,
        flatten: true,
      })).sessionId;
      if (sessionId) return evaluate(cdp.send, sessionId, expression);
    }
    return null;
  }

  const linkProbe = `(() => {
    const link = document.createElement('a');
    link.href = 'https://example.com/leave-dashboard';
    link.id = 'probe-link';
    link.textContent = 'Leave';
    document.body.appendChild(link);
    link.addEventListener('click', (event) => event.preventDefault());
    const before = location.href;
    link.click();
    const target = (link.getAttribute('target') || '').toLowerCase();
    link.remove();
    return {
      target,
      stayed: location.href === before,
      opensOut: target === '_blank' || target === '_top',
    };
  })()`;

  async function shot(sessionId, name) {
    const image = await cdp.send("Page.captureScreenshot", { format: "png" }, sessionId);
    writeFileSync(path.join(work, name), Buffer.from(image.data, "base64"));
  }

  log("direct");
  const direct = await openPage(`${dashOrigin}/`);
  const directState = await waitFor(async () => {
    const state = await pageState(cdp.send, direct.sessionId);
    return state.ready === "complete" && state.pin && state.framed === false ? state : null;
  }, "direct lock screen");
  assert(directState.framed === false, "direct visit was framed");
  assert(directState.pin === true, "direct visit did not show the PIN field");
  const directLink = await evaluate(cdp.send, direct.sessionId, linkProbe);
  assert(directLink.stayed === true, "direct link navigated the page");
  assert(directLink.opensOut === false, `direct visit retargeted a link to ${directLink.target}`);
  await evaluate(cdp.send, direct.sessionId, `(() => {
    document.getElementById('pinInput').value = ${JSON.stringify(PIN)};
    document.getElementById('keepUnlocked').checked = true;
    document.getElementById('unlockBtn').click();
  })()`);
  await waitFor(async () => {
    const state = await pageState(cdp.send, direct.sessionId);
    if (state.standing) return state;
    const error = await evaluate(cdp.send, direct.sessionId, `(() => {
      const node = document.getElementById('lockError');
      return node && !node.hidden ? node.textContent : '';
    })()`);
    if (error) throw new Error(`unlock failed: ${error}`);
    return null;
  }, "direct unlock");
  await evaluate(cdp.send, direct.sessionId, `(() => {
    document.getElementById('settingsBtn').click();
    document.getElementById('lockBtn').click();
  })()`);
  await waitFor(
    () => evaluate(cdp.send, direct.sessionId, `(() => {
      const screen = document.getElementById('lockScreen');
      return screen && !screen.hidden && !!document.getElementById('pinInput');
    })()`),
    "direct lock",
  );
  await evaluate(cdp.send, direct.sessionId, `(() => { localStorage.clear(); sessionStorage.clear(); })()`);
  await cdp.send("Target.closeTarget", { targetId: direct.targetId });
  for (const [id, info] of cdp.sessions) {
    if (info.targetId === direct.targetId || (info.url && info.url.startsWith(`${dashOrigin}/`))) {
      cdp.sessions.delete(id);
    }
  }

  log("decoy");
  const decoy = await openPage(`${decoyOrigin}/?child=${encodeURIComponent(`${decoyDashOrigin}/`)}&token=`);
  await waitFor(async () => {
    const state = await evalChild(decoy.sessionId, `${decoyDashOrigin}/`, `(() => {
      const screen = document.getElementById('lockScreen');
      const pin = document.getElementById('pinInput');
      return {
        ready: document.readyState,
        framed: window.parent !== window,
        pin: !!(pin && screen && !screen.hidden),
      };
    })()`);
    return state && state.ready === "complete" && state.framed === true && state.pin ? state : null;
  }, "decoy child stays locked");
  await new Promise((resolve) => setTimeout(resolve, 400));
  const decoyLog = await evaluate(cdp.send, decoy.sessionId, `window.__log`);
  assert(Array.isArray(decoyLog) && decoyLog.length === 0, `decoy received ${JSON.stringify(decoyLog)}`);

  log("parent");
  const parent = await openPage(`${parentOrigin}/?child=${encodeURIComponent(`${dashOrigin}/`)}&token=${encodeURIComponent(BEARER)}`);
  const originProbe = await waitFor(async () => {
    const probe = await evalChild(parent.sessionId, `${dashOrigin}/`, `(() => ({
      ancestors: location.ancestorOrigins ? [...location.ancestorOrigins] : null,
      subtle: !!(crypto && crypto.subtle),
      ready: document.readyState,
    }))()`);
    return probe && probe.ready === "complete" ? probe : null;
  }, "origin probe");
  assert(originProbe.subtle === true, "crypto.subtle missing in the frame");
  assert(originProbe.ancestors && originProbe.ancestors[0] === parentOrigin, "frame did not see the parent origin");
  const standingExpr = `(() => {
    const screen = document.getElementById('lockScreen');
    return !!(screen && screen.hidden && [...document.querySelectorAll('h2')].some((node) => node.textContent === 'Current standing'));
  })()`;
  await waitFor(async () => {
    const standing = await evalChild(parent.sessionId, `${dashOrigin}/`, standingExpr);
    return standing || null;
  }, "bridge restore");
  const framedLink = await evalChild(parent.sessionId, `${dashOrigin}/`, linkProbe);
  assert(framedLink.opensOut === true, `framed link target was ${framedLink && framedLink.target}`);
  assert(framedLink.stayed === true, "framed link navigated the dashboard frame");
  await shot(parent.sessionId, "bridge-restored.png");
  const restored = await evaluate(cdp.send, parent.sessionId, `window.__log`);
  assert(restored.some((entry) => entry.type === "ad-token-request" && entry.keys.join(",") === "type"), JSON.stringify(restored));
  assert(!JSON.stringify(restored).includes(PIN), "pin showed up in the parent log");

  await evalChild(parent.sessionId, `${dashOrigin}/`, `(() => {
    document.getElementById('settingsBtn').click();
    document.getElementById('lockBtn').click();
  })()`);
  await waitFor(async () => {
    const log = await evaluate(cdp.send, parent.sessionId, `window.__log`);
    const locked = await evalChild(parent.sessionId, `${dashOrigin}/`, `(() => {
      const screen = document.getElementById('lockScreen');
      return screen && !screen.hidden;
    })()`);
    return locked && log.some((entry) => entry.type === "ad-token-clear" && entry.keys.join(",") === "type");
  }, "bridge clear");
  const savedAfterClear = await evaluate(cdp.send, parent.sessionId, `window.__saved.token`);
  assert(savedAfterClear === null, "parent kept a token after clear");
  await shot(parent.sessionId, "bridge-locked.png");

  await evalChild(parent.sessionId, `${dashOrigin}/`, `(() => {
    document.getElementById('pinInput').value = ${JSON.stringify(PIN)};
    document.getElementById('keepUnlocked').checked = true;
    document.getElementById('lockForm').requestSubmit();
  })()`);
  const saved = await waitFor(async () => {
    const log = await evaluate(cdp.send, parent.sessionId, `window.__log`);
    const hit = log.find((entry) => entry.type === "ad-token-save");
    if (!hit) return null;
    const standing = await evalChild(parent.sessionId, `${dashOrigin}/`, standingExpr);
    return standing ? hit : null;
  }, "bridge save");
  assert(saved.token === BEARER, `save token was ${saved.token}`);
  assert(saved.keys.includes("token") && !saved.keys.includes("pin"), JSON.stringify(saved.keys));
  const finalLog = await evaluate(cdp.send, parent.sessionId, `JSON.stringify(window.__log)`);
  assert(!finalLog.includes(PIN), "pin leaked into the parent log");
  await shot(parent.sessionId, "bridge-saved.png");

  writeFileSync(chromeLog, Buffer.concat(chromeOut));
  cdp.close();
  console.log("direct visit: PIN screen, links stay in the page, unlock, lock");
  console.log("framed links: new tab or top, frame stays");
  console.log("decoy parent: 0 messages");
  console.log("parent restore: ad-token-request then Current standing");
  console.log("parent lock: ad-token-clear");
  console.log("parent save: bearer token, pin absent");
  console.log(`screenshots ${work}`);
  console.log("harness ok");
}

main()
  .catch((error) => {
    console.error(error && error.stack || error);
    process.exitCode = 1;
  })
  .finally(cleanup);
