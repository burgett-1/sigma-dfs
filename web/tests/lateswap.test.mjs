import * as E from "../src/engine.js";
import * as LS from "../src/lateswap.js";
const pos = [["PG"],["SG"],["SF"],["PF"],["C"],["PG","SG"],["SF","PF"],["PF","C"],["SG","SF"],["C"]];
const sal = [10400,8200,8800,7000,8000,5200,4600,3900,3400,3000];
const teams = ["BOS","LAL","GSW","NYK","MIA","DEN"];
const r = E.rngFrom(11); const players = [];
for (let t = 0; t < 6; t++) for (let i = 0; i < 10; i++) {
  const proj = sal[i] / 1000 * (4 + r() * 1.5);
  players.push({ name: `${teams[t]} P${i}`, team: teams[t], opp: teams[t ^ 1], positions: pos[i], salary: sal[i], proj, stdDev: proj * 0.33,
    own: 2 + r() * 30, dkId: 1000 + players.length, gameStart: t < 2 ? "2026-01-01T00:00:00Z" : "2099-01-01T00:00:00Z", liveFP: t < 2 ? 12 : null, progress: t < 2 ? 0.4 : null });
}
const id = (n) => players.find((p) => p.name === n).dkId;
const lu1 = ["BOS P0","LAL P1","GSW P9","NYK P9","MIA P9","DEN P8","GSW P8","NYK P7"];   // locked: BOS P0, LAL P1
const lu2 = ["LAL P5","BOS P1","LAL P2","BOS P3","LAL P4","BOS P8","LAL P6","BOS P7"];   // all locked
const lu3 = ["MIA P0","DEN P1","MIA P8","DEN P7","MIA P4","DEN P5","MIA P6","DEN P9"];   // all open
const rows = ["Entry ID,Contest Name,Contest ID,Entry Fee,PG,SG,SF,PF,C,G,F,UTIL,,Instructions"];
[lu1, lu2, lu3].forEach((lu, k) => rows.push([`${4000000 + k}`, "NBA $20 GPP", "999", "$20", ...lu.map((n) => `${n} (${id(n)})`), "", ""].join(",")));
rows.push(",,,,,,,,,,,,,Position,Name + ID,Name,ID,Roster Position,Salary");
const doc = LS.parseEntries(rows.join("\n"));
let ok = true; const check = (c, m) => { if (!c) { ok = false; console.log("FAIL", m); } else console.log("ok  ", m); };
check(doc && doc.kind === "classic" && doc.entries.length === 3 && doc.entries[0].slots[0].id === id("BOS P0"), "parsed DK entries file");
const now = Date.parse("2026-01-01T01:00:00Z");
const res = LS.runLateSwap(players, doc, { now, sims: 3000, fieldSize: 5000, entryFee: 20, firstPct: 20, corr: { team: 0.02, opp: 0.05 } });
const [e1, e2, e3] = res.entries;
check(e1.after.players[0] === e1.before.players[0] && e1.after.players[1] === e1.before.players[1], "locked players never move (entry 1)");
check(e1.slots[0].locked && !e1.slots[2].locked, "lock flags per slot");
check(!e2.changed && e2.openSlots === 0, "fully locked entry untouched");
check(e1.changed && e1.after.roi >= e1.before.roi, `entry 1 improved: ROI ${e1.before.roi.toFixed(1)}% to ${e1.after.roi.toFixed(1)}%`);
check(e3.after.roi >= e3.before.roi, `entry 3: ROI ${e3.before.roi.toFixed(1)}% to ${e3.after.roi.toFixed(1)}%`);
check(res.postROI >= res.preROI, `average ROI ${res.preROI.toFixed(1)}% to ${res.postROI.toFixed(1)}%`);
const sim = E.buildSim(players, now);
for (const e of [e1, e3]) {
  const lu = e.after.players; const s = lu.reduce((a, p) => a + players[p].salary, 0);
  check(new Set(lu).size === 8 && s <= 50000 && lu.every((p, k) => sim.code[p] & E.CLASSIC_SLOTS[k].mask), `swapped lineup legal ($${s})`);
  check(lu.every((p, k) => e.slots[k].locked || !sim.locked[p]), "no newly added player is from a started game");
}
const added = e3.after.players.find((p) => !e3.before.players.includes(p));
if (added != null) {
  const res2 = LS.runLateSwap(players, doc, { now, sims: 1500, fieldSize: 5000, entryFee: 20, firstPct: 20, exclude: [added] });
  check(res2.entries.every((e) => !e.after.players.includes(added) || e.before.players.includes(added)), "excluded player never added");
}
const csv = LS.exportEntries(doc, res.entries, new Set([e1.entryId]), players);
const out = LS.parseEntries(csv);
check(out.entries[0].slots.map((s) => s.id).join() === e1.after.players.map((p) => players[p].dkId).join(), "export writes the swapped lineup");
check(out.entries[2].slots.map((s) => s.id).join() === doc.entries[2].slots.map((s) => s.id).join(), "unchosen entries unchanged");
check(csv.split("\n")[0] === rows[0] && csv.split("\n").length === rows.length, "file layout preserved");
console.log(ok ? "ALL OK" : "FAILURES"); if (!ok) process.exit(1);
