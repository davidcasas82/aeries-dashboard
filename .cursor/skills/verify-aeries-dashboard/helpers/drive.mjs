#!/usr/bin/env node
// Browser harness for the static dashboard. Talks to the Chrome this run started.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

const STATE = "/tmp/aeries-dashboard-verify/state.json";

function args(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token.startsWith("--")) {
      const key = token.slice(2);
      const next = argv[i + 1];
      if (next == null || next.startsWith("--")) out[key] = true;
      else {
        out[key] = next;
        i += 1;
      }
    } else out._.push(token);
  }
  return out;
}

function state() {
  return JSON.parse(readFileSync(STATE, "utf8"));
}

async function connect() {
  const info = state();
  const list = await (await fetch(`http://127.0.0.1:${info.chrome_port}/json/list`)).json();
  const page = list.find((target) => target.type === "page" && target.webSocketDebuggerUrl);
  if (!page) throw new Error("chrome has no page target");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", () => reject(new Error("chrome websocket failed")), { once: true });
  });
  let seq = 0;
  const pending = new Map();
  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (!msg.id || !pending.has(msg.id)) return;
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
  });
  const send = (method, params = {}) => {
    const id = ++seq;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });
  };
  return { ws, send };
}

async function evaluate(send, expression) {
  const result = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    const text = result.exceptionDetails.text || "evaluate failed";
    const desc = result.exceptionDetails.exception?.description || "";
    throw new Error(`${text} ${desc}`.trim());
  }
  return result.result?.value;
}

function jsString(value) {
  return JSON.stringify(String(value));
}

const FIND = `
function norm(value) {
  return String(value || "").replace(/\\s+/g, " ").trim();
}
function accessibleName(el) {
  const labelled = el.getAttribute && el.getAttribute("aria-label");
  if (labelled) return norm(labelled);
  if (el.id) {
    const label = document.querySelector('label[for="' + el.id + '"]');
    if (label) return norm(label.innerText);
  }
  return norm(el.innerText || el.value || "");
}
function matchesRole(el, role) {
  const explicit = el.getAttribute && el.getAttribute("role");
  if (explicit) return explicit === role;
  const tag = el.tagName;
  if (role === "button") return tag === "BUTTON";
  if (role === "heading") return /^H[1-6]$/.test(tag);
  if (role === "dialog") return tag === "DIALOG" || el.getAttribute("role") === "dialog";
  if (role === "checkbox") return tag === "INPUT" && el.type === "checkbox";
  if (role === "textbox") return tag === "INPUT" || tag === "TEXTAREA";
  return false;
}
function findEl(selector, role, name) {
  const root = selector ? [...document.querySelectorAll(selector)] : [...document.querySelectorAll("button, a, input, h1, h2, h3, [role]")];
  const want = name ? norm(name) : "";
  const hits = root.filter((el) => {
    if (role && !matchesRole(el, role)) return false;
    if (want && accessibleName(el) !== want) return false;
    return true;
  });
  return { hits, want };
}
`;

async function withPage(fn) {
  const { ws, send } = await connect();
  try {
    await send("Emulation.setDeviceMetricsOverride", {
      width: 1400,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    });
    return await fn(send);
  } finally {
    ws.close();
  }
}

async function main() {
  const parsed = args(process.argv.slice(2));
  const command = parsed._[0];
  if (!command) {
    console.error("usage: drive.mjs open|fill|click|wait|visible|text|press|screenshot");
    process.exit(2);
  }

  if (command === "open") {
    const url = parsed.url || state().base_url;
    await withPage(async (send) => {
      await send("Page.enable");
      await send("Page.navigate", { url });
      const ok = await waitFor(send, "#pinInput", "", 15000);
      if (!ok) throw new Error("lock screen did not appear");
    });
    console.log("opened");
    return;
  }

  if (command === "fill") {
    if (!parsed.selector || parsed.value == null) throw new Error("fill needs --selector and --value");
    await withPage(async (send) => {
      const expr = `(() => { const el = document.querySelector(${jsString(parsed.selector)}); if (!el) return "missing"; el.focus(); el.value = ${jsString(parsed.value)}; el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); return "filled"; })()`;
      const status = await evaluate(send, expr);
      if (status !== "filled") throw new Error(`fill ${parsed.selector}: ${status}`);
    });
    console.log("filled");
    return;
  }

  if (command === "click") {
    await withPage(async (send) => {
      const expr = `(() => { ${FIND} const found = findEl(${parsed.selector ? jsString(parsed.selector) : "null"}, ${parsed.role ? jsString(parsed.role) : "null"}, ${parsed.name ? jsString(parsed.name) : "null"}); if (found.hits.length !== 1) return "matches:" + found.hits.length; const el = found.hits[0]; el.scrollIntoView({ block: "center" }); el.click(); return "clicked"; })()`;
      const status = await evaluate(send, expr);
      if (status !== "clicked") throw new Error(`click failed (${status})`);
    });
    console.log("clicked");
    return;
  }

  if (command === "wait") {
    const timeout = Number(parsed.timeout || 10000);
    const ok = await withPage((send) => waitFor(send, parsed.selector || "body", parsed.text || "", timeout));
    if (!ok) {
      const detail = await withPage((send) => evaluate(send, `(() => { const err = document.querySelector("#lockError"); return JSON.stringify({ lock: !!document.querySelector("#lockScreen:not([hidden])"), error: err && !err.hidden ? err.textContent : "", title: document.title }); })()`));
      throw new Error(`wait timed out ${detail}`);
    }
    console.log("ready");
    return;
  }

  if (command === "visible") {
    if (!parsed.text) throw new Error("visible needs --text");
    await withPage(async (send) => {
      const expr = `(() => document.body.innerText.includes(${jsString(parsed.text)}))()`;
      const ok = await evaluate(send, expr);
      if (!ok) throw new Error("text not visible");
    });
    console.log("visible");
    return;
  }

  if (command === "text") {
    if (!parsed.selector) throw new Error("text needs --selector");
    const value = await withPage((send) => evaluate(send, `(() => { const el = document.querySelector(${jsString(parsed.selector)}); return el ? (el.innerText || el.textContent || "") : ""; })()`));
    process.stdout.write(String(value).trim() + "\n");
    return;
  }

  if (command === "press") {
    if (!parsed.key) throw new Error("press needs --key");
    await withPage(async (send) => {
      const key = parsed.key;
      const expr = `(() => { const event = new KeyboardEvent("keydown", { key: ${jsString(key)}, bubbles: true }); document.dispatchEvent(event); return "pressed"; })()`;
      await evaluate(send, expr);
    });
    console.log("pressed");
    return;
  }

  if (command === "screenshot") {
    if (!parsed.path) throw new Error("screenshot needs --path");
    mkdirSync(dirname(parsed.path), { recursive: true });
    const data = await withPage(async (send) => {
      await send("Page.enable");
      const shot = await send("Page.captureScreenshot", { format: "png" });
      return shot.data;
    });
    writeFileSync(parsed.path, Buffer.from(data, "base64"));
    console.log(parsed.path);
    return;
  }

  throw new Error(`unknown command ${command}`);
}

async function waitFor(send, selector, text, timeout) {
  const started = Date.now();
  const expr = `(() => {
    const nodes = [...document.querySelectorAll(${jsString(selector)})];
    const want = ${jsString(text)};
    return nodes.some((el) => {
      if (el.hidden || el.closest("[hidden]")) return false;
      if (el.getAttribute("aria-hidden") === "true") return false;
      const blob = el.innerText || el.textContent || "";
      if (want && !blob.includes(want)) return false;
      return true;
    });
  })()`;
  while (Date.now() - started < timeout) {
    try {
      if (await evaluate(send, expr)) return true;
    } catch {
      // page may still be navigating
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return false;
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
