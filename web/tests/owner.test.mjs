// Owner setup code + signed run requests (src/owner.js).
import * as O from "../src/owner.js";

let ok = true;
const check = (c, m) => { if (!c) { ok = false; console.log("FAIL", m); } else console.log("ok  ", m); };

// shared test vector (P2-2.7): the same string is pinned in tests/test_owner.py
const KEY = O.b64urlEncode(Uint8Array.from({ length: 32 }, (_, i) => i));
const VECTOR_SIG = "EPWtd6ya9r9DPyRQPy84DjvndYNSYPXOfM8wfzNVpiY";
const code = (o) => O.PREFIX + O.b64urlEncode(new TextEncoder().encode(JSON.stringify(o)));
const GOOD = { s: "https://ntfy.sh", t: "sigma-own-" + "ab".repeat(16), k: KEY };

check(KEY === "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", "base64url encodes the vector key like Python");
check(O.b64urlDecode(KEY).length === 32 && O.b64urlDecode(KEY)[31] === 31, "base64url round-trips");
const p = O.parseSetupCode(code(GOOD));
check(p.ok && p.cfg.server === GOOD.s && p.cfg.topic === GOOD.t && p.cfg.key === KEY, "parseSetupCode round-trips a valid code");
check(O.parseSetupCode("  " + code(GOOD) + "\n").ok, "surrounding whitespace is ignored");
check(O.parseSetupCode(code({ ...GOOD, s: "http://localhost:8080" })).ok, "http://localhost allowed for dev");
check(!O.parseSetupCode("sigma-owner-v2:" + code(GOOD).slice(O.PREFIX.length)).ok, "rejects a bad prefix");
check(!O.parseSetupCode(O.PREFIX + "%%%").ok && !O.parseSetupCode(O.PREFIX + O.b64urlEncode(new TextEncoder().encode("{nope"))).ok, "rejects bad JSON");
check(!O.parseSetupCode(code({ ...GOOD, s: "http://ntfy.sh" })).ok, "rejects a non-https server");
check(!O.parseSetupCode(code({ ...GOOD, k: KEY.slice(0, 20) })).ok, "rejects a short key");
check(!O.parseSetupCode(code({ ...GOOD, t: "short" })).ok && !O.parseSetupCode(code({ ...GOOD, t: "bad topic with spaces!!!!" })).ok, "rejects a bad topic");
check(O.canonical(156302, "0123456789abcdef", 1764200000) === "sigma-run-v1\n156302\n0123456789abcdef\n1764200000", "canonical string");
check(O.maskTopic(GOOD.t) === "sigma-own-••••abab" && !O.maskTopic(GOOD.t).includes(GOOD.t), "topic masked");

const sig = await O.signRun({ key: KEY }, 156302, "0123456789abcdef", 1764200000);
check(sig === VECTOR_SIG, "signRun gives the pinned vector signature");
const b = await O.buildRunBody(p.cfg, "156302");
const body = JSON.parse(b.body);
check(body.v === 1 && body.action === "run" && body.slate_id === 156302 && /^[0-9a-f]{16}$/.test(body.req) && Number.isInteger(body.ts) && typeof body.sig === "string",
  "buildRunBody contains every field with an integer ts");
check(body.sig === (await O.signRun(p.cfg, 156302, body.req, body.ts)), "body signature matches its fields");
check(!b.body.includes(KEY) && O.runUrl(p.cfg) === "https://ntfy.sh/" + GOOD.t, "key not in the body; POST URL from the stored config");
check(O.randomReq() !== O.randomReq(), "request ids are random");

// localStorage helpers never throw
const store = {};
globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
check(O.loadOwner() === null && O.saveOwner(p.cfg) && O.loadOwner().topic === GOOD.t, "save/load");
O.clearOwner();
check(O.loadOwner() === null, "forget clears it");
globalThis.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
check(O.loadOwner() === null && O.saveOwner(p.cfg) === false, "blocked storage handled");
O.clearOwner();

// Analyst (Phase 2): adversarial setup codes and signing edges
for (const [why, c] of [
  ["javascript: server", code({ ...GOOD, s: "javascript:alert(1)" })],
  ["http://localhost.evil.com", code({ ...GOOD, s: "http://localhost.evil.com" })],
  ["data: server", code({ ...GOOD, s: "data:text/html,x" })],
  ["topic with path", code({ ...GOOD, t: "../../sigma-own-" + "ab".repeat(8) })],
  ["topic with query", code({ ...GOOD, t: "sigma-own-abababababab?x=1" })],
  ["33-byte key", code({ ...GOOD, k: O.b64urlEncode(new Uint8Array(33)) })],
  ["key not a string", code({ ...GOOD, k: 12345 })],
  ["array JSON", O.PREFIX + O.b64urlEncode(new TextEncoder().encode("[1,2]"))],
  ["null JSON", O.PREFIX + O.b64urlEncode(new TextEncoder().encode("null"))],
  ["empty", ""], ["undefined", undefined], ["prefix only", O.PREFIX],
]) check(!O.parseSetupCode(c).ok, "rejects setup code: " + why);
check(O.parseSetupCode(code({ ...GOOD, s: "https://ntfy.sh/" })).cfg.server === "https://ntfy.sh", "trailing slash trimmed from server");
check(O.runUrl({ server: "https://ntfy.sh/", topic: GOOD.t }) === "https://ntfy.sh/" + GOOD.t, "POST URL is server/topic only (no key, no query)");
{
  const cfg = O.parseSetupCode(code(GOOD)).cfg;
  const s1 = await O.signRun(cfg, 156302, "0123456789abcdef", 1764200000);
  const s2 = await O.signRun(cfg, 156303, "0123456789abcdef", 1764200000);
  const s3 = await O.signRun({ ...cfg, key: O.b64urlEncode(new Uint8Array(32)) }, 156302, "0123456789abcdef", 1764200000);
  check(s1 === VECTOR_SIG && s2 !== s1 && s3 !== s1 && !/[=+/]/.test(s1), "signature depends on slate and key; base64url without padding");
  const b = await O.buildRunBody(cfg, "156302");
  const j = JSON.parse(b.body);
  check(j.slate_id === 156302 && Number.isInteger(j.ts) && /^[0-9a-f]{16}$/.test(j.req) && !b.body.includes(cfg.key) && !b.body.includes(cfg.topic),
    "string slate id coerced to int; body carries no key or topic");
  check(Math.abs(j.ts - Date.now() / 1000) < 5, "ts is the current unix time in seconds");
}

if (!ok) { console.log("owner tests FAILED"); process.exit(1); }
console.log("owner tests passed");
