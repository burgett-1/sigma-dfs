// Owner mode: signed "Run on my Mac" requests (hmac-v1). The setup code comes from
// `python -m pipeline.owner_setup` on the owner's Mac and is stored only in this
// browser. The key is used solely as an HMAC input (Web Crypto, non-extractable);
// it is never logged, rendered, or put in a URL.
export const PREFIX = "sigma-owner-v1:";
const STORE = "sigma_owner_v1";
const TOPIC_RE = /^[A-Za-z0-9_-]{20,64}$/;
const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

export function b64urlEncode(bytes) {
  let out = "", i = 0;
  for (; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + B64[(n >> 6) & 63] + B64[n & 63];
  }
  if (i < bytes.length) {
    const n = (bytes[i] << 16) | ((i + 1 < bytes.length ? bytes[i + 1] : 0) << 8);
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + (i + 1 < bytes.length ? B64[(n >> 6) & 63] : "");
  }
  return out;
}

export function b64urlDecode(s) {
  const str = String(s || "").replace(/=+$/, "");
  if (!/^[A-Za-z0-9_-]*$/.test(str) || str.length % 4 === 1) return null;
  const out = [];
  let buf = 0, bits = 0;
  for (const ch of str) {
    buf = (buf << 6) | B64.indexOf(ch); bits += 6;
    if (bits >= 8) { bits -= 8; out.push((buf >> bits) & 255); }
  }
  return new Uint8Array(out);
}

function validServer(s) {
  return typeof s === "string" && s.length < 200 && (/^https:\/\/[^\s/]+/.test(s) || /^http:\/\/localhost(:\d+)?(\/|$)/.test(s));
}

// "sigma-owner-v1:<b64url JSON {s, t, k}>" -> {ok, cfg:{server, topic, key}} or {ok:false, error}
export function parseSetupCode(code) {
  const c = String(code || "").trim();
  if (!c.startsWith(PREFIX)) return { ok: false, error: "That isn't a Sigma owner setup code (it should start with " + PREFIX + ")." };
  const raw = b64urlDecode(c.slice(PREFIX.length));
  let d = null;
  try { d = raw ? JSON.parse(new TextDecoder().decode(raw)) : null; } catch (e) { d = null; }
  if (!d || typeof d !== "object") return { ok: false, error: "The setup code is damaged. Copy it again from the Mac." };
  if (!validServer(d.s)) return { ok: false, error: "The setup code's server must start with https://." };
  if (typeof d.t !== "string" || !TOPIC_RE.test(d.t)) return { ok: false, error: "The setup code's topic is invalid." };
  const key = b64urlDecode(d.k);
  if (!key || key.length !== 32) return { ok: false, error: "The setup code's key is invalid." };
  return { ok: true, cfg: { server: d.s.replace(/\/+$/, ""), topic: d.t, key: d.k } };
}

export function loadOwner() {
  try {
    const v = JSON.parse(localStorage.getItem(STORE) || "null");
    if (v && validServer(v.server) && TOPIC_RE.test(v.topic || "")) { const k = b64urlDecode(v.key); if (k && k.length === 32) return v; }
  } catch (e) { /* storage unavailable or corrupt */ }
  return null;
}
export function saveOwner(cfg) {
  try { localStorage.setItem(STORE, JSON.stringify({ server: cfg.server, topic: cfg.topic, key: cfg.key })); return true; } catch (e) { return false; }
}
export function clearOwner() {
  try { localStorage.removeItem(STORE); } catch (e) { /* nothing stored */ }
}

export function canonical(slateId, req, ts) {
  return "sigma-run-v1\n" + slateId + "\n" + req + "\n" + ts;
}

function cryptoApi() {
  const c = typeof globalThis !== "undefined" ? globalThis.crypto : null;
  if (!c || !c.subtle || !c.getRandomValues) throw new Error("This browser can't sign requests (Web Crypto unavailable).");
  return c;
}

// base64url(HMAC-SHA256(key, canonical)), key imported non-extractable for this call only
export async function signRun(cfg, slateId, req, ts) {
  const c = cryptoApi();
  const k = await c.subtle.importKey("raw", b64urlDecode(cfg.key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await c.subtle.sign("HMAC", k, new TextEncoder().encode(canonical(slateId, req, ts)));
  return b64urlEncode(new Uint8Array(sig));
}

export function randomReq() {
  const b = new Uint8Array(8);
  cryptoApi().getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

// the POST body: {"v":1,"action":"run","slate_id","req","ts","sig"}
export async function buildRunBody(cfg, slateId, req, ts) {
  const r = req || randomReq();
  const t = ts != null ? ts : Math.floor(Date.now() / 1000);
  const sig = await signRun(cfg, Number(slateId), r, t);
  return { req: r, ts: t, body: JSON.stringify({ v: 1, action: "run", slate_id: Number(slateId), req: r, ts: t, sig }) };
}

export function runUrl(cfg) {
  return cfg.server.replace(/\/+$/, "") + "/" + cfg.topic;
}

export function maskTopic(topic) {
  const t = String(topic || "");
  return "sigma-own-••••" + t.slice(-4);
}
