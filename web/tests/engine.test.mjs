import * as E from "../src/engine.js";
const pos = [["PG"],["SG"],["SF"],["PF"],["C"],["PG","SG"],["SF","PF"],["PF","C"],["SG","SF"],["C"]];
const sal = [10400,8200,8800,7000,8000,5200,4600,3900,3400,3000];
const players = [];
const teams = ["BOS","LAL","GSW","NYK","MIA","DEN"];
const r = E.rngFrom(7);
for (let t = 0; t < 6; t++) for (let i = 0; i < 10; i++) {
  const proj = sal[i] / 1000 * (4 + r() * 1.5);
  players.push({ name: teams[t] + i, team: teams[t], opp: teams[t ^ 1], positions: pos[i], salary: sal[i], proj, stdDev: proj * 0.33,
    own: 2 + r() * 30, dkId: 1000 + players.length, cptId: 5000 + players.length, gameStart: t < 2 ? "2026-01-01T00:00:00Z" : "2099-01-01T00:00:00Z",
    liveFP: t < 2 ? 10 : null, progress: t < 2 ? 0.5 : null });
}
const now = Date.parse("2026-01-01T01:00:00Z");
const sim = E.buildSim(players, now);
let ok = true; const check = (c, m) => { if (!c) { ok = false; console.log("FAIL", m); } else console.log("ok  ", m); };
check(sim.locked[0] === 1 && sim.locked[20] === 0, "players in started games are locked");
check(Math.abs(sim.mu[0] - (10 + players[0].proj * 0.5)) < 1e-9, "locked player = live points + rest-of-game projection");
// sampling mean
const X = E.sampleScores(sim, 20000, E.rngFrom(3), { team: 0.02, opp: 0.05 });
let m = 0; for (let s = 0; s < 20000; s++) m += X[s * sim.P + 25]; m /= 20000;
check(Math.abs(m / sim.mu[25] - 1) < 0.03, "simulated mean matches projection (" + (m / sim.mu[25]).toFixed(3) + ")");
// payouts sum to 90% of fees
const pay = E.payoutTable({ fieldSize: 1000, entryFee: 10, firstPct: 20 });
check(Math.abs(pay.reduce((a, b) => a + b, 0) - 9000) < 1e-6, "prize pool = 90% of entry fees");
// complete a lineup by projection
const score = Float64Array.from(players.map((p) => p.proj));
const fill = E.bestFill(score, sim, players, [], E.CLASSIC_SLOTS, {});
const salUsed = fill.reduce((s, p) => s + players[p].salary, 0);
check(fill.length === 8 && new Set(fill).size === 8 && salUsed <= 50000, "complete lineup is legal ($" + salUsed + ")");
check(fill.every((p, k) => sim.code[p] & E.CLASSIC_SLOTS[k].mask), "every player fits his slot");
// brute-force optimum on a reduced pool to check the filler
const fixed = fill.slice(0, 5).map((p, k) => ({ slot: E.CLASSIC_SLOTS[k], p }));
const open = E.CLASSIC_SLOTS.slice(5);
const f2 = E.bestFill(score, sim, players, fixed, open, {});
let budget = 50000 - fixed.reduce((s, f) => s + players[f.p].salary, 0), best = 0;
const used = new Set(fixed.map((f) => f.p));
for (let a = 0; a < 60; a++) for (let b = 0; b < 60; b++) for (let c = 0; c < 60; c++) {
  if (new Set([a, b, c]).size < 3 || used.has(a) || used.has(b) || used.has(c)) continue;
  if (!(sim.code[a] & 3) || !(sim.code[b] & 12)) continue;
  if (players[a].salary + players[b].salary + players[c].salary > budget) continue;
  best = Math.max(best, score[a] + score[b] + score[c]);
}
const got = f2.reduce((s, p) => s + score[p], 0);
check(Math.abs(got - best) < 1e-6, "3-slot fill is exactly optimal (" + got.toFixed(2) + " vs " + best.toFixed(2) + ")");
// contest: a strong lineup must beat a weak one on ROI
const strong = fill.map((p) => [p, 1]);
const weakIdx = players.map((p, i) => i).sort((a, b) => score[a] - score[b]);
const weak = E.bestFill(Float64Array.from(players.map((p) => -p.proj + 100)), sim, players, [], E.CLASSIC_SLOTS, {}).map((p) => [p, 1]);
const res = E.simulateContest(sim, players, [strong, weak], { sims: 4000, fieldSize: 5000, entryFee: 20, firstPct: 20, kind: "classic", corr: { team: 0.02, opp: 0.05 } });
check(res[0].roi > res[1].roi && res[0].mean > res[1].mean, `strong ROI ${res[0].roi.toFixed(1)}% > weak ${res[1].roi.toFixed(1)}%`);
check(res[0].win >= 0 && res[0].win <= 100 && res[0].cash <= 100, "win/cash are probabilities");
// showdown
const sdPlayers = players.filter((p) => p.team === "MIA" || p.team === "DEN");
const sdSim = E.buildSim(sdPlayers, now);
const sdFill = E.bestFill(Float64Array.from(sdPlayers.map((p) => p.proj)), sdSim, sdPlayers, [], E.SHOWDOWN_SLOTS, {});
const sdSal = E.slotSalary(sdPlayers[sdFill[0]], E.SHOWDOWN_SLOTS[0]) + sdFill.slice(1).reduce((s, p) => s + sdPlayers[p].salary, 0);
check(sdFill.length === 6 && new Set(sdFill).size === 6 && sdSal <= 50000, "showdown lineup legal ($" + sdSal + ")");
const field = E.sampleField(sdSim, sdPlayers, "showdown", 200, E.rngFrom(4));
check(field.length > 100 && field[0][0][1] === 1.5, "showdown field has 1.5x captains");
console.log(ok ? "ALL OK" : "FAILURES"); if (!ok) process.exit(1);
