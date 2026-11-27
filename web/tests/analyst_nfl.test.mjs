// Analyst (Phase 3 Wave 1): browser engine on NFL DraftKings salary files, checked
// by an independent validator that reads the raw CSV (Roster Position semantics).
// Uses the synthetic fixtures, plus the real public DK files when ANALYST_DK_DIR
// points at them (sal_21_*.csv classic, sal_96_*.csv showdown). Synthetic ids only.
//   node tests/analyst_nfl.test.mjs
import { readFileSync, existsSync, readdirSync } from "node:fs";
import * as E from "../src/engine.js";
import * as PG from "../src/poolgen.js";
import * as LS from "../src/lateswap.js";
import * as SP from "../src/sports.js";

const root = new URL("../../", import.meta.url);
const REG = JSON.parse(readFileSync(new URL("web/src/sports/registry.json", root), "utf8"));
const C = SP.specFor(REG, "NFL", "classic"), S = SP.specFor(REG, "NFL", "showdown");
let fails = 0;
const check = (ok, msg) => { console.log((ok ? "ok   " : "FAIL ") + msg); if (!ok) fails++; };

function raw(text) {               // id -> raw CSV row (validator side)
  const lines = text.replace(/^﻿/, "").replace(/\r/g, "").trim().split("\n");
  const h = lines[0].split(",");
  const by = new Map();
  for (const l of lines.slice(1)) {
    const r = l.split(","), g = (k) => r[h.indexOf(k)];
    const gi = g("Game Info").split(" ")[0], [away, home] = gi.split("@"), team = g("TeamAbbrev");
    by.set(Number(g("ID")), { pos: g("Position"), slots: new Set(g("Roster Position").split("/")), salary: Number(g("Salary")),
      team, opp: team === away ? home : away, game: gi, name: g("Name").trim(), avg: Number(g("AvgPointsPerGame")) || 0,
      start: g("Game Info").split(" ").slice(1, 3).join(" ") });
  }
  return by;
}
function players(by, kind) {       // engine-side player objects
  const m = new Map();
  for (const [id, r] of by) {
    const key = r.name + "|" + r.team;
    const p = m.get(key) || { name: r.name, team: r.team, opp: r.opp, positions: [r.pos], corrGame: r.game,
      gameStart: r.start.includes("01:00PM") ? "2026-11-29T18:00:00Z" : "2026-11-29T21:25:00Z",
      proj: Math.max(r.avg, 0.5), own: 2 + (r.salary / 1000) * 2, stdDev: Math.max(r.avg, 0.5) * 0.6 };
    if (r.slots.has("CPT")) { p.cptId = id; p.cptSalary = r.salary; } else { p.dkId = id; p.salary = r.salary; }
    m.set(key, p);
  }
  return [...m.values()].filter((p) => (kind === "showdown" ? p.cptId && p.dkId : E.posCode(p.positions, C)));
}
const ORDER = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"];
function classicViol(ids, by, stack = true) {
  const v = [];
  if (ids.length !== 9 || new Set(ids).size !== 9) return ["size/dupes"];
  if (ids.some((i) => !by.has(i))) return ["id not in CSV"];
  const P = ids.map((i) => by.get(i));
  P.forEach((p, k) => { if (!p.slots.has(ORDER[k])) v.push(`${p.pos} in ${ORDER[k]}`); });
  if (P.filter((p) => p.pos === "QB").length !== 1 || P.filter((p) => p.pos === "DST").length !== 1) v.push("QB/DST count");
  if (P.reduce((a, p) => a + p.salary, 0) > 50000) v.push("over cap");
  if (new Set(P.map((p) => p.game)).size < 2) v.push("one game");
  const qb = P.find((p) => p.pos === "QB"), d = P.find((p) => p.pos === "DST");
  if (qb && d && d.team === qb.opp) v.push("DST faces own QB");
  if (stack && qb && !P.some((p) => ["WR", "TE"].includes(p.pos) && p.team === qb.team)) v.push("QB not stacked");
  return v;
}
function sdViol(ids, by) {
  if (ids.length !== 6 || new Set(ids).size !== 6 || ids.some((i) => !by.has(i))) return ["size/ids"];
  const P = ids.map((i) => by.get(i)), v = [];
  if (!P[0].slots.has("CPT") || P.slice(1).some((p) => !p.slots.has("FLEX"))) v.push("slot rows");
  if (new Set(P.map((p) => p.name + p.team)).size !== 6) v.push("captain also FLEX");
  const flexSal = [...by.values()].find((p) => p.slots.has("FLEX") && p.name === P[0].name && p.team === P[0].team);
  if (!flexSal || P[0].salary !== Math.round(1.5 * flexSal.salary)) v.push("CPT salary");
  if (P.reduce((a, p) => a + p.salary, 0) > 50000) v.push("over cap");
  if (new Set(P.map((p) => p.team)).size < 2) v.push("one team");
  return v;
}

function runClassic(label, text) {
  const by = raw(text), pl = players(by, "classic");
  const now = Date.parse("2026-11-26T15:00:00Z");
  const t0 = Date.now();
  const pool = PG.generatePool(pl, { kind: "classic", spec: C, lineups: 150, sims: 1000, fieldSample: 400, seed: 7, now });
  const ms = Date.now() - t0;
  const bad = pool.lineups.map((l) => classicViol(l.ids, by)).filter((x) => x.length);
  check(pool.lineups.length >= 50 && bad.length === 0, `${label}: generatePool ${pool.lineups.length} lineups in ${ms} ms, ${bad.length} illegal ${JSON.stringify(bad.slice(0, 2))}`);
  const sim = E.buildSim(pl, now, C);
  const field = E.sampleField(sim, pl, "classic", 400, E.rngFrom(3), C);
  const fb = field.map((lu) => { const ix = E.assignSlots(lu.map((x) => x[0]), C.slots, pl, sim); return ix ? classicViol(ix.map((p) => pl[p].dkId), by, false) : ["unassignable"]; })
    .filter((x) => x.length);
  check((label.startsWith("real") || field.length >= 200) && fb.length === 0, `${label}: sampleField ${field.length} lineups, ${fb.length} illegal ${JSON.stringify(fb.slice(0, 2))}`);
  // late swap: two synthetic entries of a legal pool lineup; early games started
  const lu = pool.lineups[0].ids;
  const hdr = "Entry ID,Contest Name,Contest ID,Entry Fee," + ORDER.join(",") + ",,Instructions";
  const nameOf = (id) => pl.find((p) => p.dkId === id).name;
  const rows = [hdr, ...["9100000001", "9100000002"].map((eid) => [eid, "Synthetic NFL", "9900001", "$5", ...lu.map((id) => `${nameOf(id)} (${id})`), "", ""].join(","))];
  const doc = LS.parseEntries(rows.join("\n"), [C, S]);
  check(doc && doc.kind === "classic" && doc.entries.length === 2, `${label}: entries header parsed as NFL classic`);
  const started = pl.map((p) => (p.gameStart === "2026-11-29T18:00:00Z" ? { ...p, liveFP: 9, progress: 0.4 } : p));
  const res = LS.runLateSwap(started, doc, { now: Date.parse("2026-11-29T19:00:00Z"), spec: C, sims: 600, fieldSize: 1000, entryFee: 5, candidateSims: 30 });
  let okFixed = true, okLegal = true;
  for (const e of res.entries) {
    e.slots.forEach((s, k) => { if (s.locked && e.after.players[k] !== e.before.players[k]) okFixed = false; });
    const ids = e.after.players.map((p) => started[p].dkId);
    if (classicViol(ids, by, false).length) okLegal = false;
  }
  check(okFixed && okLegal, `${label}: late swap keeps started players and stays DK-legal (${res.entries.filter((e) => e.changed).length} changed)`);
  const out = LS.exportEntries(doc, res.entries, new Set(res.entries.map((e) => e.entryId)), started).split("\n");
  check(out[0] === hdr, `${label}: export keeps header`);
}
function runShowdown(label, text) {
  const by = raw(text), pl = players(by, "showdown");
  const now = Date.parse("2026-11-26T15:00:00Z");
  const pool = PG.generatePool(pl, { kind: "showdown", spec: S, lineups: 100, sims: 800, fieldSample: 300, seed: 5, now });
  const bad = pool.lineups.map((l) => sdViol(l.ids, by)).filter((x) => x.length);
  check(pool.lineups.length >= 30 && bad.length === 0, `${label}: showdown pool ${pool.lineups.length} lineups, ${bad.length} illegal ${JSON.stringify(bad.slice(0, 2))}`);
  const sim = E.buildSim(pl, now, S);
  const field = E.sampleField(sim, pl, "showdown", 300, E.rngFrom(4), S);
  check(field.length >= 150, `${label}: showdown field ${field.length} of 300 requested`);
}

runClassic("synthetic 4-game", readFileSync(new URL("tests/fixtures/nfl/dk_classic_4g.csv", root), "utf8"));
runShowdown("synthetic showdown", readFileSync(new URL("tests/fixtures/nfl/dk_showdown.csv", root), "utf8"));
const dir = process.env.ANALYST_DK_DIR;
if (dir && existsSync(dir)) {
  for (const f of readdirSync(dir)) {
    if (/^sal_21_\d+\.csv$/.test(f)) runClassic("real " + f.split("_")[1] + " classic", readFileSync(dir + "/" + f, "utf8"));
    if (/^sal_96_\d+\.csv$/.test(f)) runShowdown("real showdown", readFileSync(dir + "/" + f, "utf8"));
  }
} else console.log("skip real DK files (ANALYST_DK_DIR not set)");
// Retest 1 (F3): the opening sport is an enabled sport, and an unlisted one falls back to NBA
{
  const P = SP.pickInitialSport, L = SP.listedSport;
  const cases = [[["NFL", null], "NFL"], [["nfl", null], "NFL"], [["MLB", null], "NBA"], [["XX", null], "NBA"],
    [[null, "NFL"], "NFL"], [[null, "MLB"], "NBA"], [["MLB", "NFL"], "NFL"], [[null, null], "NBA"], [["<script>", null], "NBA"]];
  check(cases.every(([a, w]) => P(a[0], a[1], REG) === w), "pickInitialSport: only enabled sports (MLB/unknown -> NBA)");
  const both = [{ code: "NBA" }, { code: "NFL" }];
  check(L("NFL", null) === "NBA" && L("NFL", [{ code: "NBA" }]) === "NBA" && L("NFL", both) === "NFL" && L("NFL", "junk") === "NBA",
    "listedSport: absent / NBA-only / malformed index -> NBA");
  const app = readFileSync(new URL("web/src/App.jsx", root), "utf8");
  check(/SPORT\.gen\+\+/.test(app) && (app.match(/gen!==SPORT\.gen|gen===SPORT\.gen|live\(\)/g) || []).length >= 10,
    "App: switchSport bumps SPORT.gen and responses check it (static)");
}
// Retest 1 (F2 guard): an empty field never yields NaN/Infinity
{
  const by = raw(readFileSync(new URL("tests/fixtures/nfl/dk_showdown.csv", root), "utf8")), pl = players(by, "showdown");
  const sim = E.buildSim(pl, Date.parse("2026-11-26T15:00:00Z"), S);
  const lu = [[0, 1.5], [1, 1], [2, 1], [3, 1], [4, 1], [5, 1]];
  const r = E.simulateContest(sim, pl, [lu], { sims: 500, fieldSize: 1000, entryFee: 5, kind: "showdown", spec: S, seed: 1, field: [] });
  const vals = r.flatMap((x) => Object.values(x).filter((v) => typeof v === "number"));
  check(vals.length > 0 && vals.every(Number.isFinite), `empty-field simulateContest: ${vals.length} numbers, all finite`);
}
// published NFL slate CSVs (projection format, the model's own ownership): field fill rate
const proj = process.env.ANALYST_NFL_PROJ_DIR;
if (proj && existsSync(proj)) {
  for (const f of readdirSync(proj).filter((x) => x.endsWith(".csv"))) {
    const lines = readFileSync(proj + "/" + f, "utf8").trim().split("\n"), h = lines[0].split(",");
    const pl = lines.slice(1).map((l) => { const c = l.split(","), g = (k) => c[h.indexOf(k)];
      return { name: g("Name"), salary: +g("Salary"), positions: g("Position").split("/"), team: g("Team"), opp: g("Opp"), proj: +g("Proj"),
        stdDev: +g("StdDev"), own: +g("Own"), dkId: +g("DKID"), cptId: +g("CptID") || null, cptSalary: +g("CptSalary") || null,
        gameStart: "2099-01-01T00:00:00Z", corrGame: g("CorrGame") }; }).filter((p) => p.proj > 0);
    const kind = pl.some((p) => p.cptId) ? "showdown" : "classic", spec = kind === "showdown" ? S : C;
    const sim = E.buildSim(pl, Date.parse("2026-11-26T15:00:00Z"), spec);
    const field = E.sampleField(sim, pl, kind, 400, E.rngFrom(3), spec);
    check(field.length >= 380, `published ${kind} slate ${f}: sampleField filled ${field.length} of 400 (model ownership)`);
    const rawFile = dir && `${dir}/sal_${kind === "showdown" ? 96 : 21}_${f.replace(".csv", "")}.csv`;
    if (rawFile && existsSync(rawFile)) {
      const by = raw(readFileSync(rawFile, "utf8"));
      const ids = field.map((lu) => lu.map(([p, m], k) => (kind === "showdown" && k === 0 ? pl[p].cptId : pl[p].dkId)));
      const bad = kind === "showdown" ? ids.filter((x) => sdViol(x, by).length) : [];
      const sal = ids.map((x) => x.reduce((a, i) => a + by.get(i).salary, 0)).sort((a, b) => a - b);
      const med = sal[Math.floor(sal.length / 2)];
      check(bad.length === 0 && (kind !== "showdown" || med >= 47000), `published ${kind} ${f}: field ${bad.length} illegal, median salary ${med}, min ${sal[0]}`);
    }
  }
} else console.log("skip published-slate field check (ANALYST_NFL_PROJ_DIR not set)");
if (fails) { console.log(`${fails} FAILED`); process.exit(1); }
console.log("analyst NFL tests passed");
