// Browser pool generator (src/poolgen.js) on small synthetic slates.
import * as E from "../src/engine.js";
import { generatePool, validLineup, lineupKey } from "../src/poolgen.js";

let ok = true;
const check = (c, m) => { if (!c) { ok = false; console.log("FAIL", m); } else console.log("ok  ", m); };

function slate(nTeams, seed) {
  const pos = [["PG"], ["SG"], ["SF"], ["PF"], ["C"], ["PG", "SG"], ["SF", "PF"], ["PF", "C"], ["SG", "SF"], ["C"]];
  const sal = [10400, 8200, 8800, 7000, 8000, 5200, 4600, 3900, 3400, 3000];
  const teams = ["BOS", "LAL", "GSW", "NYK", "MIA", "DEN"].slice(0, nTeams);
  const r = E.rngFrom(seed), players = [];
  for (let t = 0; t < teams.length; t++) for (let i = 0; i < 10; i++) {
    const proj = sal[i] / 1000 * (4 + r() * 1.5);
    players.push({ name: teams[t] + i, team: teams[t], opp: teams[t ^ 1], positions: pos[i], salary: sal[i], proj, stdDev: proj * 0.33,
      own: 2 + r() * 30, dkId: 1000 + players.length, cptId: 5000 + players.length, cptSalary: Math.round(sal[i] * 1.5),
      gameStart: "2099-01-01T00:00:00Z" });
  }
  return players;
}
const NOW = Date.parse("2026-01-01T00:00:00Z");
const rules = { min_salary: 45000, max_per_team: 4, max_per_game: 6 };
const players = slate(4, 7);           // 40 players, 4 teams, 2 games
const opts = { kind: "classic", lineups: 300, sims: 2000, fieldSample: 300, fieldSize: 5000, seed: 11, rules, corr: { team: 0.02, opp: 0.05 }, now: NOW, slateId: 1 };
const doc = generatePool(players, opts);
const L = doc.lineups;
check(L.length >= 100, "at least 100 lineups (" + L.length + ")");
check(new Set(L.map((l) => lineupKey(l.ids))).size === L.length, "lineups are unique");
const byId = {}; players.forEach((p, i) => { byId[p.dkId] = i; });
const sim = E.buildSim(players, NOW), slots = E.slotsFor("classic"), masks = slots.map((s) => s.mask);
let legal = true;
for (const l of L) {
  const idx = l.ids.map((id) => byId[id]);
  const teamMax = Math.max(...Object.values(idx.reduce((m, p) => { m[players[p].team] = (m[players[p].team] || 0) + 1; return m; }, {})));
  if (!(l.salary <= 50000 && l.salary >= rules.min_salary && E.canAssign(idx.map((p) => sim.code[p]), masks) && teamMax <= rules.max_per_team
    && validLineup(idx, players, sim, slots, rules))) { legal = false; break; }
}
check(legal, "every lineup obeys salary, slots and the team cap");
check(doc.exact === false && doc.source === "browser" && doc.phase === "browser" && !("portfolio_cover" in doc), "doc labelled as an approximate browser run");
check(L.every((l, i) => l.rank === i + 1) && L.every((l, i) => i === 0 || L[i - 1].win >= l.win), "ranked 1..N by win");
check(doc.slots.join(",") === "PG,SG,SF,PF,C,G,F,UTIL" && doc.sims === 2000 && doc.slate_id === 1, "slots, sims and slate id recorded");
const again = generatePool(players, opts);
check(JSON.stringify(again.lineups) === JSON.stringify(L), "deterministic for a fixed seed");
const other = generatePool(players, { ...opts, seed: 12 });
check(JSON.stringify(other.lineups) !== JSON.stringify(L), "a different seed gives a different pool");

// showdown: one game, captain first with the CPT id
const sd = slate(2, 3);
const sdoc = generatePool(sd, { kind: "showdown", lineups: 60, sims: 1000, fieldSample: 200, fieldSize: 2000, seed: 5, now: NOW });
const cptIds = new Set(sd.map((p) => p.cptId));
check(sdoc.lineups.length >= 20 && sdoc.lineups.every((l) => cptIds.has(l.ids[0]) && l.ids.slice(1).every((id) => !cptIds.has(id))), "showdown lineups lead with a captain id");
check(sdoc.slots[0] === "CPT" && sdoc.lineups.every((l) => l.salary <= 50000), "showdown slots and salary");

// too few players: friendly error
let msg = "";
try { generatePool(players.slice(0, 6), opts); } catch (e) { msg = e.message; }
check(/Not enough valid lineups/.test(msg), "too few players throws the friendly error");

// Analyst (Phase 2): players projected at 0 (deep bench: median 0, long right tail, like
// Drew Timme / Gary Trent Jr. on slate 156302) must never be in a browser lineup;
// the Mac pool never uses them (0 of its 10,000 lineups do).
{
  const base = slate(4, 21);
  const TAIL = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0.7,1.54,2.72,4.48,6.42,9.03,13.81,18.12,22.65,34.05];
  const zeros = ["BOS", "LAL", "GSW", "NYK"].map((t, i) => ({ name: "Bench" + t, team: t, opp: ["LAL", "BOS", "NYK", "GSW"][i],
    positions: [["C"], ["PG"], ["SF"], ["PF"]][i], salary: 3000, proj: 0, mean: 2.6, stdDev: 5.1, q: TAIL, own: 0.1,
    dkId: 9000 + i, cptId: 9100 + i, cptSalary: 4500, gameStart: "2099-01-01T00:00:00Z" }));
  const ps = base.concat(zeros);
  const zdoc = generatePool(ps, { kind: "classic", lineups: 400, sims: 1000, fieldSample: 200, fieldSize: 2000, seed: 3, now: NOW,
    rules: { min_salary: 48500, max_per_team: 4, max_per_game: 6 } });
  const zid = new Set(zeros.map((p) => p.dkId));
  const bad = zdoc.lineups.filter((l) => l.ids.some((id) => zid.has(id))).length;
  check(bad === 0, "no 0-projection player in any browser lineup (" + bad + " of " + zdoc.lineups.length + " lineups had one)");
}

if (!ok) { console.log("poolgen tests FAILED"); process.exit(1); }
console.log("poolgen tests passed");
