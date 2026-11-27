// Pure data helpers for the Sigma HUD tools. No React, no DOM: testable under Node.
import { sportsIndex } from "./sports.js";

// The 25 percentiles each player's outcome curve is published at (same order as
// engine.js U_SAMPLE / pipeline distribution.U_SAMPLE).
export const Q_LEVELS = [0, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6,
  0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.975, 0.99, 0.999];
const BOX_IDX = { p10: 4, p25: 7, p50: 12, p75: 17, p90: 20 };

function isNum(x) { return typeof x === "number" && isFinite(x); }

// "0.00;3.79;…" (or an array) -> 25 numbers, or null when malformed
export function parseQuantiles(str) {
  if (str == null) return null;
  const arr = Array.isArray(str) ? str.map(Number) : String(str).split(";").map((s) => (s.trim() === "" ? NaN : Number(s)));
  if (arr.length !== Q_LEVELS.length) return null;
  for (let i = 0; i < arr.length; i++) if (!isNum(arr[i])) return null;
  return arr;
}

export function quantileBox(q) {
  const a = parseQuantiles(q);
  if (!a) return null;
  return { p10: a[BOX_IDX.p10], p25: a[BOX_IDX.p25], p50: a[BOX_IDX.p50], p75: a[BOX_IDX.p75], p90: a[BOX_IDX.p90] };
}

// equal-width bins; edges has bins+1 entries
export function histogram(values, opts) {
  const o = opts || {};
  const bins = Math.max(1, Math.floor(o.bins || 20));
  const v = (values || []).filter(isNum);
  if (!v.length) return { edges: [], counts: [] };
  let lo = isNum(o.min) ? o.min : v.reduce((a, b) => Math.min(a, b), Infinity);
  let hi = isNum(o.max) ? o.max : v.reduce((a, b) => Math.max(a, b), -Infinity);
  if (!(hi > lo)) { hi = lo + 1; }
  const w = (hi - lo) / bins;
  const counts = new Array(bins).fill(0);
  for (let i = 0; i < v.length; i++) {
    const x = v[i];
    if (x < lo || x > hi) continue;
    counts[Math.min(bins - 1, Math.floor((x - lo) / w))]++;
  }
  const edges = [];
  for (let i = 0; i <= bins; i++) edges.push(lo + i * w);
  return { edges, counts };
}

// Scale a model ownership by how that ownership range actually came in at
// DraftKings (contest_history calibration buckets). null when the bucket is thin.
export function realityCheck(own, buckets, minN) {
  const need = minN == null ? 30 : minN;
  if (!isNum(own) || !Array.isArray(buckets)) return null;
  const b = buckets.find((x) => x && own >= x.lo && (x.hi == null || own < x.hi));
  if (!b || !(b.n >= need) || !isNum(b.mean_proj) || !isNum(b.mean_actual) || b.mean_proj <= 0) return null;
  const adj = Math.min(100, Math.max(0, own * (b.mean_actual / b.mean_proj)));
  return { adj, bucket: b.label, n: b.n };
}

// live (<= 5 min) / stale (<= 20 min) / offline / unknown
export function freshness(iso, nowMs) {
  if (!iso) return "unknown";
  const t = Date.parse(iso);
  if (!isFinite(t)) return "unknown";
  const now = isNum(nowMs) ? nowMs : Date.now();
  const mins = (now - t) / 60000;
  if (mins <= 5) return "live";
  if (mins <= 20) return "stale";
  return "offline";
}

export function validateContestHistory(doc) {
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return { ok: false, reason: "not an object" };
  if (doc.version !== 1) return { ok: false, reason: "unsupported version " + doc.version };
  if (!Array.isArray(doc.contests)) return { ok: false, reason: "contests missing" };
  const p = doc.players;
  if (!p || !Array.isArray(p.columns) || !Array.isArray(p.rows)) return { ok: false, reason: "players table missing" };
  const n = p.columns.length;
  for (let i = 0; i < p.rows.length; i++) {
    if (!Array.isArray(p.rows[i]) || p.rows[i].length !== n) return { ok: false, reason: "row " + i + " has the wrong width" };
  }
  return { ok: true, reason: "" };
}

export function rowsToObjects(columns, rows) {
  if (!Array.isArray(columns) || !Array.isArray(rows)) return [];
  return rows.filter(Array.isArray).map((r) => {
    const o = {};
    for (let i = 0; i < columns.length; i++) o[columns[i]] = r[i] === undefined ? null : r[i];
    return o;
  });
}

// A slate object (same shape as slates.json entries) for a slate that is no longer
// listed, built from its projection rows (parsed players). Paths stay relative to
// the sport's data base ("" for NBA, "nfl/" ...); only NBA publishes spotlight files.
export function synthSlate(id, players, base) {
  const nid = Number(id);
  const ps = players || [];
  const games = {}, order = [];
  let first = null;
  for (const p of ps) {
    const team = (p.team || p.Team || "").toUpperCase(), opp = (p.opp || p.Opp || "").toUpperCase();
    const start = p.gameStart || p.GameStartUTC || "";
    const cg = p.corrGame || p.CorrGame || "";
    let away, home;
    if (cg && cg.indexOf("@") > 0) { const s = cg.split("@"); away = s[0].toUpperCase(); home = s[1].toUpperCase(); }
    else if (team && opp) { const s = [team, opp].sort(); away = s[0]; home = s[1]; }
    else continue;
    const key = away + "@" + home;
    if (!games[key]) { games[key] = { name: key, away, home, start_utc: start || null, locked: true }; order.push(key); }
    else if (start && (!games[key].start_utc || start < games[key].start_utc)) games[key].start_utc = start;
    if (start && (!first || start < first)) first = start;
  }
  const gl = order.map((k) => games[k]).sort((a, b) => String(a.start_utc || "").localeCompare(String(b.start_utc || "")));
  const showdown = ps.some((p) => p.cptId || p.CptID);
  return {
    id: nid, name: "Slate " + nid + " (archived)", main: false,
    kind: showdown ? "showdown" : "classic",
    file: "slates/" + nid + ".csv", lineups_file: "lineups/" + nid + ".json",
    spotlight_file: base ? null : "spotlight/" + nid + ".json",
    games: gl, game_count: gl.length, start_utc: first,
    size_bucket: gl.length <= 3 ? "small" : gl.length <= 6 ? "medium" : "large",
    contest_locked: true, locked: true, archived: true,
  };
}

export function fmtNum(v, digits) {
  const d = digits == null ? 1 : digits;
  if (v == null || v === "" || !isFinite(Number(v))) return "—";
  return Number(v).toFixed(d);
}

export function fmtCompact(n) {
  if (n == null || !isFinite(Number(n))) return "—";
  // pick the unit after rounding, so 999999 -> "1.0M" (not "1000k") and 9999 -> "10k"
  // the sign comes from the rounded value, so -0.4 -> "0" (never "-0")
  const x = Number(n), a = Math.abs(x), sign = x < 0 && Math.round(a) !== 0 ? "-" : "";
  if (Math.round(a) < 1000) return sign + String(Math.round(a));
  const k1 = (a / 1e3).toFixed(1);                 // e.g. "1.5", "10.0"
  if (Number(k1) < 10) return sign + k1 + "k";
  const k = Math.round(a / 1e3);
  if (k < 1000) return sign + k + "k";
  return sign + (a / 1e6).toFixed(1) + "M";
}

// data/sports.json -> the sport switcher's list; absent/empty/malformed -> NBA only
export function parseSportsIndex(doc, reg) {
  return sportsIndex(doc, reg);
}
