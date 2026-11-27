// Sport registry helpers + the shared conformance fixture (JS half) + NFL
// legality / correlation in engine, poolgen and lateswap. Node, no deps.
import { readFileSync, existsSync, readdirSync } from "node:fs";
import * as SP from "../src/sports.js";
import * as E from "../src/engine.js";
import * as PG from "../src/poolgen.js";
import * as LS from "../src/lateswap.js";
import { parseSportsIndex, synthSlate } from "../src/datautil.js";

const root = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, root), "utf8");
const REG = JSON.parse(read("web/src/sports/registry.json"));
const FIX = JSON.parse(read("tests/fixtures/sports/conformance.json"));
let ok = true;
const check = (c, m) => { if (!c) { ok = false; console.log("FAIL", m); } else console.log("ok  ", m); };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── registry ────────────────────────────────────────────────────────────────
check(SP.validateRegistry(REG).length === 0, "registry validates");
FIX.malformed_registries.forEach((c) => check(SP.validateRegistry(c.registry).length > 0, "rejects: " + c.label));
const nbaC = SP.specFor(REG, "NBA", "classic"), nbaS = SP.specFor(REG, "NBA", "showdown");
check(same(nbaC.slots, E.CLASSIC_SLOTS) && same(nbaS.slots, E.SHOWDOWN_SLOTS), "NBA spec slots == engine legacy slots");
check(nbaC.cap === E.SALARY_CAP && nbaC.playProb.Questionable === 0.7 && nbaC.playProb.Probable === 0.97, "NBA cap / play prob");

// ── conformance ─────────────────────────────────────────────────────────────
let el = 0;
for (const c of FIX.eligibility) {
  const r = SP.eligibility(c.roster_position, c.position, SP.specFor(REG, c.sport, c.kind));
  if (same(r ? r.slots : null, c.slots)) el++; else console.log("  eligibility mismatch", c, r);
}
check(el === FIX.eligibility.length, `eligibility ${el}/${FIX.eligibility.length}`);
let lg = 0;
for (const c of FIX.legality) {
  const sp = SP.specFor(REG, c.sport, c.kind);
  const players = c.players.map((p) => ({ ...p, code: SP.posCodeFor(p.pos, sp), cptSalary: p.cpt_salary }));
  if (SP.lineupLegal(players, sp) === c.legal) lg++; else console.log("  legality mismatch", c.label);
}
check(lg === FIX.legality.length, `legality ${lg}/${FIX.legality.length}`);
const M = SP.impliedCorr(FIX.implied_corr.players, SP.specFor(REG, "NFL", "classic"));
let maxd = 0;
M.forEach((row, i) => row.forEach((v, j) => { maxd = Math.max(maxd, Math.abs(v - FIX.implied_corr.matrix[i][j])); }));
check(maxd <= FIX.implied_corr.tol, "implied NFL correlation matrix matches Python (max diff " + maxd + ")");
let eh = 0;
for (const c of FIX.entries_headers) if (same(SP.detectEntriesSport(c.header, REG), c.expect)) eh++;
check(eh === FIX.entries_headers.length, `entries headers ${eh}/${FIX.entries_headers.length}`);

// ── sports.json ─────────────────────────────────────────────────────────────
for (const [label, doc] of [["absent", null], ["empty", {}], ["malformed", { sports: "x" }], ["junk rows", { sports: [1, { code: "XYZ" }] }]]) {
  const idx = parseSportsIndex(doc, REG);
  check(idx.length === 1 && idx[0].code === "NBA" && idx[0].base === "", "sports.json " + label + " -> NBA only");
}
const idx = parseSportsIndex({ sports: [{ code: "NBA", status: "live" }, { code: "NFL", status: "live", scoring_verified: false }] }, REG);
check(idx.map((s) => s.code).join() === "NBA,NFL" && idx[1].base === "nfl/" && !idx[1].scoring_verified, "sports.json NBA + NFL");
// F3: never strand a viewer on a sport the index doesn't list
check(SP.pickInitialSport("nfl", null, REG) === "NFL" && SP.pickInitialSport(null, "NFL", REG) === "NFL", "initial sport from ?sport= or the saved choice");
check(SP.pickInitialSport("MLB", null, REG) === "NBA" && SP.pickInitialSport("XYZ", "MLB", REG) === "NBA" && SP.pickInitialSport(null, null, REG) === "NBA",
  "?sport=MLB (disabled stub) / unknown / nothing -> NBA");
const nbaNfl = parseSportsIndex({ sports: [{ code: "NBA" }, { code: "NFL" }] }, REG);
check(SP.listedSport("NFL", nbaNfl) === "NFL" && SP.listedSport("NFL", null) === "NBA" &&
  SP.listedSport("NFL", parseSportsIndex(null, REG)) === "NBA" && SP.listedSport("NFL", parseSportsIndex({ sports: [{ code: "NBA" }] }, REG)) === "NBA",
  "a sport missing from sports.json (absent, NBA-only) falls back to NBA");
// F5: every per-sport response handler in App.jsx is guarded by the sport generation
{
  const app = read("web/src/App.jsx");
  const guards = (app.match(/gen!==SPORT\.gen|gen===SPORT\.gen|!live\(\)|if\(live\(\)\)/g) || []).length;
  check(/SPORT\.gen\+\+/.test(app) && guards >= 12, `App.jsx drops responses from a superseded sport (${guards} guards)`);
}
check(synthSlate(5, [], "nfl/").spotlight_file === null && synthSlate(5, []).spotlight_file === "spotlight/5.json", "archived slate base-aware");

// ── fail closed ─────────────────────────────────────────────────────────────
const nflC = SP.specFor(REG, "NFL", "classic"), nflS = SP.specFor(REG, "NFL", "showdown");
check(E.posCode(["K"], nflC) === 0 && E.posCode([], undefined) === 0 && E.posCode(["XX"]) === 0, "unknown position -> code 0");

// ── NFL players from the synthetic DK salary files ─────────────────────────
function parseCsv(text, spec) {
  const [head, ...lines] = text.trim().split("\n");
  const h = head.split(",");
  const col = (r, k) => r[h.indexOf(k)];
  const byName = new Map();
  for (const line of lines) {
    const r = line.split(",");
    const name = col(r, "Name"), team = col(r, "TeamAbbrev"), info = col(r, "Game Info").split(" ")[0];
    const [away, home] = info.split("@");
    const p = byName.get(name) || { name, team, opp: team === away ? home : away, positions: [col(r, "Position")],
      corrGame: info, gameStart: "2099-01-01T00:00:00Z", proj: Number(col(r, "AvgPointsPerGame")), own: 5 + (name.length % 7) * 3 };
    if (col(r, "Roster Position") === "CPT") { p.cptId = Number(col(r, "ID")); p.cptSalary = Number(col(r, "Salary")); }
    else { p.dkId = Number(col(r, "ID")); p.salary = Number(col(r, "Salary")); }
    p.stdDev = p.proj * 0.6;
    byName.set(name, p);
  }
  return [...byName.values()].filter((p) => spec.slots.some((s) => s.mult) || E.posCode(p.positions, spec));
}
const classic = parseCsv(read("tests/fixtures/nfl/dk_classic_4g.csv"), nflC);
const showdown = parseCsv(read("tests/fixtures/nfl/dk_showdown.csv"), nflS);
const ORDER = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"];
const byId = new Map(classic.map((p) => [p.dkId, p]));
// independent DraftKings NFL classic validator (not the code under test)
function dkLegal(ids) {
  if (ids.length !== 9 || new Set(ids).size !== 9 || ids.some((i) => !byId.has(i))) return false;
  const ps = ids.map((i) => byId.get(i));
  for (let k = 0; k < 9; k++) {
    const pos = ps[k].positions[0];
    if (ORDER[k] === "FLEX" ? !["RB", "WR", "TE"].includes(pos) : pos !== ORDER[k]) return false;
  }
  if (ps.reduce((a, p) => a + p.salary, 0) > 50000) return false;
  return new Set(ps.map((p) => p.corrGame)).size >= 2;
}
const stacked = (ids) => { const ps = ids.map((i) => byId.get(i)); const qb = ps.find((p) => p.positions[0] === "QB");
  return ps.some((p) => p !== qb && ["WR", "TE"].includes(p.positions[0]) && p.team === qb.team); };
const dstVsQb = (ids) => { const ps = ids.map((i) => byId.get(i)); const qb = ps.find((p) => p.positions[0] === "QB");
  const d = ps.find((p) => p.positions[0] === "DST"); return d.team === qb.opp; };

const pool = PG.generatePool(classic, { kind: "classic", spec: nflC, lineups: 120, sims: 800, fieldSample: 300, seed: 3, now: Date.parse("2026-11-26T15:00:00Z") });
check(same(pool.slots, ORDER), "poolgen NFL slot labels");
check(pool.lineups.length >= 50 && pool.lineups.every((l) => dkLegal(l.ids)), `poolgen: ${pool.lineups.length} NFL lineups, 100% DK-legal`);
check(pool.lineups.every((l) => stacked(l.ids) && !dstVsQb(l.ids)), "poolgen: every lineup stacks its QB, no DST facing it");

const simC = E.buildSim(classic, Date.parse("2026-11-26T15:00:00Z"), nflC);
const field = E.sampleField(simC, classic, "classic", 300, E.rngFrom(5), nflC);
check(field.length > 100 && field.every((lu) => {
  const idx = E.assignSlots(lu.map((x) => x[0]), nflC.slots, classic, simC);
  return idx && dkLegal(idx.map((p) => classic[p].dkId)) && !dstVsQb(idx.map((p) => classic[p].dkId));
}), `sampleField: ${field.length} NFL field lineups legal`);

// copula: empirical latent correlation of a QB and his WR1 vs the implied value (20k sims)
const qi = classic.findIndex((p) => p.name === "Buf Qb A"), wi = classic.findIndex((p) => p.name === "Buf Wr A");
const di = classic.findIndex((p) => p.name === "Dolphins");
const n = 20000, X = E.sampleScores({ ...simC, Q: new Array(simC.P).fill(null), pplay: new Float64Array(simC.P).fill(1) }, n, E.rngFrom(9));
const corr = (a, b) => { let sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0;
  for (let s = 0; s < n; s++) { const x = X[s * simC.P + a], y = X[s * simC.P + b]; sa += x; sb += y; saa += x * x; sbb += y * y; sab += x * y; }
  const ma = sa / n, mb = sb / n; return (sab / n - ma * mb) / Math.sqrt((saa / n - ma * ma) * (sbb / n - mb * mb)); };
const implied = SP.impliedCorr([classic[qi], classic[wi], classic[di]].map((p) => ({ position: p.positions[0], team: p.team, opp: p.opp, game: p.corrGame })), nflC);
const qw = corr(qi, wi), qd = corr(qi, di);
check(Math.abs(qw - implied[0][1]) <= 0.06 && qw > 0.25, `QB-own WR score corr ${qw.toFixed(3)} (implied latent ${implied[0][1].toFixed(3)})`);
check(qd < -0.1 && Math.abs(qd - implied[0][2]) <= 0.06, `QB-opp DST score corr ${qd.toFixed(3)} (implied ${implied[0][2].toFixed(3)})`);
let m = 0; for (let s = 0; s < n; s++) m += X[s * simC.P + wi]; m /= n;
check(Math.abs(m / simC.mu[wi] - 1) < 0.03, "factor path keeps the mean (" + (m / simC.mu[wi]).toFixed(3) + ")");

// showdown: legal captain lineups, 1.5x captain in the contest sim
const simS = E.buildSim(showdown, Date.parse("2026-11-26T15:00:00Z"), nflS);
const sdPool = PG.generatePool(showdown, { kind: "showdown", spec: nflS, lineups: 80, sims: 600, fieldSample: 200, seed: 4, now: Date.parse("2026-11-26T15:00:00Z") });
const cptIds = new Map(showdown.map((p) => [p.cptId, p])), flexIds = new Map(showdown.map((p) => [p.dkId, p]));
check(sdPool.slots.join() === "CPT,FLEX,FLEX,FLEX,FLEX,FLEX" && sdPool.lineups.every((l) => {
  const [c, ...f] = l.ids; const ps = [cptIds.get(c), ...f.map((i) => flexIds.get(i))];
  return ps.every(Boolean) && new Set(ps).size === 6 && l.salary <= 50000 && new Set(ps.map((p) => p.team)).size === 2 &&
    l.salary === ps[0].cptSalary + ps.slice(1).reduce((a, p) => a + p.salary, 0);
}), `showdown pool: ${sdPool.lineups.length} legal CPT + 5 FLEX lineups`);
const one = E.simulateContest(simS, showdown, [[[0, 1.5], [1, 1], [2, 1], [3, 1], [4, 1], [5, 1]]], { sims: 2000, fieldSize: 500, kind: "showdown", spec: nflS, seed: 2 });
const exp = 1.5 * simS.mu[0] + [1, 2, 3, 4, 5].reduce((a, i) => a + simS.mu[i], 0);
check(Math.abs(one[0].mean / exp - 1) < 0.05, "contest sim scores the captain at 1.5x");

// F2: the registry-sport showdown field reaches the requested size, legal, spending like the public
function showdownFieldCheck(label, pl) {
  const sim = E.buildSim(pl, Date.parse("2026-11-26T15:00:00Z"), nflS);
  const f = E.sampleField(sim, pl, "showdown", 400, E.rngFrom(4), nflS);
  const sal = f.map((lu) => lu.reduce((a, [p, m]) => a + (m > 1 ? pl[p].cptSalary : pl[p].salary), 0));
  const legal = f.every((lu, k) => new Set(lu.map((x) => x[0])).size === 6 && sal[k] <= 50000 && lu[0][1] === 1.5 &&
    new Set(lu.map((x) => pl[x[0]].team)).size === 2);
  const sorted = sal.slice().sort((a, b) => a - b), med = sorted[Math.floor(sorted.length / 2)];
  check(f.length >= 380 && legal && med >= 47000, `${label}: showdown field ${f.length}/400, legal, median $${med}`);
}
showdownFieldCheck("synthetic", showdown);
const emptyField = E.simulateContest(simS, showdown, [[[0, 1.5], [1, 1], [2, 1], [3, 1], [4, 1], [5, 1]]],
  { sims: 200, fieldSize: 500, entryFee: 20, firstPct: 20, kind: "showdown", spec: nflS, field: [], seed: 3 });
check(Object.values(emptyField[0]).every((v) => Number.isFinite(v)), "simulateContest with an empty field returns finite numbers");
const dkDir = process.env.ANALYST_DK_DIR;
if (dkDir && existsSync(dkDir)) {
  for (const f of readdirSync(dkDir).filter((x) => /^sal_96_\d+\.csv$/.test(x))) {
    const text = readFileSync(dkDir + "/" + f, "utf8").replace(/^﻿/, "").replace(/\r/g, "");
    showdownFieldCheck("real " + f, parseCsv(text, nflS));
  }
}

// ── late swap / entries ─────────────────────────────────────────────────────
const hdr = "Entry ID,Contest Name,Contest ID,Entry Fee,QB,RB,RB,WR,WR,WR,TE,FLEX,DST,,Instructions";
const pick = (pos, team) => classic.find((p) => p.positions[0] === pos && (!team || p.team === team));
const of = (pos, k) => classic.filter((p) => p.positions[0] === pos)[k];
// a legal $44,200 entry: BUF QB + BUF WR1, cheap RBs / WRs, KC TE, PHI WR at FLEX, KC DST
const lu = [pick("QB", "BUF"), of("RB", 2), of("RB", 5), pick("WR", "BUF"), of("WR", 3), of("WR", 13),
  pick("TE", "KC"), of("WR", 24), pick("DST", "KC")];
check(dkLegal(lu.map((p) => p.dkId)), "late-swap test entry is legal to start with");
const row = ["4100001", "NFL $20", "999", "$20", ...lu.map((p) => `${p.name} (${p.dkId})`), "", ""].join(",");
const text = [hdr, row, ",,,,,,,,,,,,,,Position,Name + ID"].join("\n");
const docN = LS.parseEntries(text, [nflC, nflS]);
check(docN && docN.kind === "classic" && docN.entries.length === 1 && docN.slotCols.length === 9, "NFL classic entries header detected");
const sdText = "Entry ID,Contest Name,Contest ID,Entry Fee,CPT,FLEX,FLEX,FLEX,FLEX,FLEX\n";
check(LS.parseEntries(sdText, [nflC, nflS]).kind === "showdown", "NFL showdown entries header detected");
const nbaHdr = "Entry ID,Contest Name,Contest ID,Entry Fee,PG,SG,SF,PF,C,G,F,UTIL";
check(LS.parseEntries(nbaHdr, [nflC, nflS]) === null && SP.detectEntriesSport(nbaHdr.split(","), REG).sport === "NBA",
  "an NBA file is rejected for NFL and identified as NBA (switch sport)");
const started = classic.map((p) => (p.team === "BUF" || p.team === "MIA" ? { ...p, gameStart: "2026-11-29T18:00:00Z", liveFP: 8, progress: 0.5 } : p));
const res = LS.runLateSwap(started, docN, { now: Date.parse("2026-11-29T19:00:00Z"), spec: nflC, sims: 800, fieldSize: 2000, entryFee: 20, candidateSims: 40 });
const e = res.entries[0];
const fixedOk = e.slots.every((s, k) => !s.locked || e.after.players[k] === e.before.players[k]);
check(fixedOk && e.slots[0].locked && !e.slots[6].locked, "players in started games stay fixed");
const afterIds = e.after.players.map((p) => started[p].dkId);
check(e.changed && dkLegal(afterIds), "swapped NFL lineup is DK-legal");
const outCsv = LS.exportEntries(docN, res.entries, new Set([e.entryId]), started);
check(outCsv.split("\n")[0] === hdr && outCsv.split("\n").length === 3, "exported CSV keeps the original columns and order");

// ── NBA slates: the fail-closed change drops nobody ─────────────────────────
const slDir = new URL("data/slates/", root);
if (existsSync(slDir)) {
  let players = 0, zero = 0;
  for (const f of readdirSync(slDir).filter((x) => x.endsWith(".csv"))) {
    const [head, ...lines] = readFileSync(new URL(f, slDir), "utf8").trim().split("\n");
    const pi = head.split(",").indexOf("Position");
    for (const l of lines) { players++; if (!E.posCode(l.split(",")[pi].split("/"))) zero++; }
  }
  check(players > 0 && zero === 0, `NBA slate CSVs: ${players} players, ${zero} with position code 0`);
} else console.log("skip NBA slate CSV check (no local data/slates)");

console.log(ok ? "sports tests passed" : "SPORTS TESTS FAILED");
if (!ok) process.exit(1);
