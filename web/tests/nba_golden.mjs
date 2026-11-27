// NBA golden hash: proves the browser engine's NBA behaviour is unchanged.
//   node web/tests/nba_golden.mjs <srcDir>       (srcDir = web/src, or main's web/src from git archive)
// Prints one SHA-256 over the outputs of bestFill, sampleScores (seeded),
// sampleField, simulateContest, generatePool, parseEntries and runLateSwap on the
// engine.test.mjs / lateswap.test.mjs NBA fixtures, all called WITHOUT a spec.
// The same hash for main and the branch = identical NBA results.
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const src = resolve(process.argv[2] || new URL("../src", import.meta.url).pathname);
const load = (f) => import(pathToFileURL(resolve(src, f)).href);
const E = await load("engine.js");
const LS = await load("lateswap.js");
const PG = await load("poolgen.js");

const pos = [["PG"], ["SG"], ["SF"], ["PF"], ["C"], ["PG", "SG"], ["SF", "PF"], ["PF", "C"], ["SG", "SF"], ["C"]];
const sal = [10400, 8200, 8800, 7000, 8000, 5200, 4600, 3900, 3400, 3000];
const teams = ["BOS", "LAL", "GSW", "NYK", "MIA", "DEN"];
function fixture(seed, live) {
  const r = E.rngFrom(seed), players = [];
  for (let t = 0; t < 6; t++) for (let i = 0; i < 10; i++) {
    const proj = sal[i] / 1000 * (4 + r() * 1.5);
    players.push({ name: `${teams[t]} P${i}`, team: teams[t], opp: teams[t ^ 1], positions: pos[i], salary: sal[i], proj, stdDev: proj * 0.33,
      own: 2 + r() * 30, dkId: 1000 + players.length, cptId: 5000 + players.length,
      gameStart: t < 2 ? "2026-01-01T00:00:00Z" : "2099-01-01T00:00:00Z", liveFP: t < 2 ? live : null, progress: t < 2 ? 0.5 : null });
  }
  return players;
}
const out = [];
const put = (label, v) => out.push(label + "=" + JSON.stringify(v, (k, x) => (x instanceof Float32Array || x instanceof Float64Array ? Array.from(x) : x)));
const now = Date.parse("2026-01-01T01:00:00Z");

// engine fixture
const players = fixture(7, 10);
const sim = E.buildSim(players, now);
put("codes", Array.from(sim.code));
const X = E.sampleScores(sim, 400, E.rngFrom(3), { team: 0.02, opp: 0.05 });
put("scores", Array.from(X.subarray(0, 4000)).map((v) => Math.round(v * 1e4)));
const score = Float64Array.from(players.map((p) => p.proj));
put("bestFill", E.bestFill(score, sim, players, [], E.CLASSIC_SLOTS, {}));
put("bestFillLocked", E.bestFill(score, sim, players, [{ slot: E.CLASSIC_SLOTS[0], p: 0 }], E.CLASSIC_SLOTS.slice(1), { unlockedOnly: true }));
const sdPlayers = players.filter((p) => p.team === "MIA" || p.team === "DEN");
const sdSim = E.buildSim(sdPlayers, now);
put("bestFillShowdown", E.bestFill(Float64Array.from(sdPlayers.map((p) => p.proj)), sdSim, sdPlayers, [], E.SHOWDOWN_SLOTS, {}));
put("field", E.sampleField(sim, players, "classic", 200, E.rngFrom(9)));
put("fieldShowdown", E.sampleField(sdSim, sdPlayers, "showdown", 100, E.rngFrom(9)));
const lus = [E.bestFill(score, sim, players, [], E.CLASSIC_SLOTS, {}).map((p) => [p, 1])];
put("contest", E.simulateContest(sim, players, lus, { sims: 2000, fieldSize: 1000, entryFee: 10, firstPct: 20, kind: "classic", corr: { team: 0.02, opp: 0.05 }, seed: 5 })
  .map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, Math.round(v * 1e6)]))));
const pool = PG.generatePool(fixture(7, null).map((p) => ({ ...p, gameStart: "2099-01-01T00:00:00Z" })),
  { lineups: 60, sims: 500, fieldSample: 200, seed: 4, now, corr: { team: 0.02, opp: 0.05 } });
put("pool", pool.lineups.map((l) => [l.ids, l.win, l.mean, l.salary]));

// late-swap fixture
const lp = fixture(11, 12);
const id = (n) => lp.find((p) => p.name === n).dkId;
const rows = ["Entry ID,Contest Name,Contest ID,Entry Fee,PG,SG,SF,PF,C,G,F,UTIL,,Instructions"];
[["BOS P0", "LAL P1", "GSW P9", "NYK P9", "MIA P9", "DEN P8", "GSW P8", "NYK P7"],
 ["MIA P0", "DEN P1", "MIA P8", "DEN P7", "MIA P4", "DEN P5", "MIA P6", "DEN P9"]]
  .forEach((lu, k) => rows.push([`${4000000 + k}`, "NBA $20 GPP", "999", "$20", ...lu.map((n) => `${n} (${id(n)})`), "", ""].join(",")));
const doc = LS.parseEntries(rows.join("\n"));
put("entries", { kind: doc.kind, slots: doc.slotCols, ids: doc.entries.map((e) => e.slots.map((s) => s.id)) });
const res = LS.runLateSwap(lp, doc, { now, sims: 1500, fieldSize: 5000, entryFee: 20, firstPct: 20, corr: { team: 0.02, opp: 0.05 } });
put("lateswap", res.entries.map((e) => [e.changed, e.after && e.after.players, Math.round((e.after ? e.after.roi : 0) * 1e6)]));
const sdRows = ["Entry ID,Contest Name,Contest ID,Entry Fee,CPT,UTIL,UTIL,UTIL,UTIL,UTIL"];
put("entriesShowdown", LS.parseEntries(sdRows.join("\n")).kind);

const hash = createHash("sha256").update(out.join("\n")).digest("hex");
console.log(hash);
