// Per-visitor lineup pool, built entirely in the visitor's browser (Web Worker).
// Approximate by design: each lineup is the greedy best fill (engine.bestFill) for
// one simulated slate outcome, then every lineup is contest-simulated against an
// ownership-drawn field (engine.simulateContest). The shared Mac pool uses exactly
// solved lineups and 1,000,000 contests. Output has the same shape as
// lineups/{id}.json so every Lineups feature (filters, likes, export) works on it.
import { buildSim, sampleScores, bestFill, simulateContest, rngFrom, slotsFor, slotSalary, slotId, rulesOk, SALARY_CAP } from "./engine.js";

export const SIZES = {
  quick: { lineups: 1000, sims: 5000, fieldSample: 1000 },
  standard: { lineups: 2000, sims: 10000, fieldSample: 2000 },
  bigger: { lineups: 5000, sims: 25000, fieldSample: 2000 },
};
const MIN_UNIQUE = 20;
const BATCH = 100;      // outcomes sampled per sampleScores call

export function lineupKey(ids) {
  return ids.map(Number).slice().sort((a, b) => a - b).join("-");
}

// DraftKings legality plus the slate's own rules (min salary, max per team / game)
// and, with a sport spec, its game/team minimums and stack rules
export function validLineup(idx, players, sim, slots, rules, spec) {
  spec = spec || sim.spec;
  if (!idx || idx.length !== slots.length) return false;
  const r = rules || {};
  const showdown = slots.some((s) => s.mult);
  const seen = new Set();
  let salary = 0;
  const teams = {}, games = {};
  for (let k = 0; k < slots.length; k++) {
    const p = idx[k];
    if (p == null || p < 0 || p >= players.length || seen.has(p)) return false;
    seen.add(p);
    if (!(sim.code[p] & slots[k].mask)) return false;
    if (!slotId(players[p], slots[k])) return false;            // can't be exported without a DK id
    salary += slotSalary(players[p], slots[k]);
    teams[sim.team[p]] = (teams[sim.team[p]] || 0) + 1;
    games[sim.game[p]] = (games[sim.game[p]] || 0) + 1;
  }
  const minSal = r.min_salary != null ? r.min_salary : spec ? spec.poolMinSalary : showdown ? 45000 : 48500;
  if (salary > (spec ? spec.cap : SALARY_CAP) || salary < minSal) return false;
  const tc = Object.values(teams), gc = Object.values(games);
  if (showdown) return tc.length >= 2;
  if (gc.length < (spec ? spec.minGames : 2)) return false;
  if (spec && spec.enforceRules && !rulesOk(idx, sim)) return false;
  if (r.max_per_team && Math.max(...tc) > r.max_per_team) return false;
  if (r.max_per_game && Math.max(...gc) > r.max_per_game) return false;
  return true;
}

export function generatePool(allPlayers, opts, onProgress) {
  const o = opts || {};
  // only players projected to play can be drafted (a 0-projection player is "not expected to play");
  // every index below refers to this filtered array, and lineups are emitted as DK ids
  const players = (allPlayers || []).filter((p) => p && p.proj > 0);
  const kind = o.kind || "classic";
  const want = Math.max(1, o.lineups || SIZES.standard.lineups);
  const sims = Math.max(100, o.sims || SIZES.standard.sims);
  const fieldSize = o.fieldSize || 10000;
  const fieldSample = Math.min(o.fieldSample || SIZES.standard.fieldSample, fieldSize);
  const seed = (o.seed >>> 0) || 1;
  const rules = o.rules || {};
  const prog = typeof onProgress === "function" ? onProgress : () => {};
  const t0 = Date.now();
  const spec = o.spec || null;
  const sim = buildSim(players, o.now || Date.now(), spec || undefined);
  const rng = rngFrom(seed);
  const slots = slotsFor(kind, spec);
  const fillOpts = kind === "showdown" ? {} : { maxPerTeam: rules.max_per_team || undefined };

  // 1. generate: one greedy best lineup per simulated outcome
  const seenKeys = new Set(), lus = [];
  const maxTries = want * 6;
  let tries = 0;
  while (lus.length < want && tries < maxTries) {
    const n = Math.min(BATCH, maxTries - tries);
    const X = sampleScores(sim, n, rng, o.corr);
    for (let s = 0; s < n && lus.length < want; s++) {
      tries++;
      const score = X.subarray(s * sim.P, (s + 1) * sim.P);
      const idx = bestFill(score, sim, players, [], slots, fillOpts);
      if (!validLineup(idx, players, sim, slots, rules, spec)) continue;
      const key = lineupKey(idx.map((p, k) => slotId(players[p], slots[k])));
      if (seenKeys.has(key)) continue;
      seenKeys.add(key);
      lus.push(idx);
    }
    prog(0.4 * Math.min(1, Math.max(lus.length / want, tries / maxTries)));
  }
  if (lus.length < MIN_UNIQUE) throw new Error("Not enough valid lineups — check the slate's players");
  const t1 = Date.now();

  // 2. contest-simulate every lineup against an ownership-drawn field
  const cand = lus.map((idx) => idx.map((p, k) => [p, slots[k].mult || 1]));
  const res = simulateContest(sim, players, cand, {
    sims, fieldSize, fieldSample, kind, corr: o.corr, seed: seed + 1, entryFee: o.entryFee || 20, firstPct: o.firstPct || 20, spec,
  }, (f) => prog(0.4 + 0.6 * f));
  const t2 = Date.now();

  // 3. same document shape as lineups/{id}.json
  const rows = lus.map((idx, l) => {
    let proj = 0, own = 0, salary = 0, low = 0;
    idx.forEach((p, k) => {
      const pl = players[p], m = slots[k].mult || 1;
      proj += (pl.proj || 0) * m; own += pl.own || 0; salary += slotSalary(pl, slots[k]);
      if ((pl.own || 0) < 10) low++;
    });
    const r = res[l];
    return { ids: idx.map((p, k) => slotId(players[p], slots[k])), win: round(r.win, 4), mean: round(r.mean, 2), sd: round(r.sd, 2),
      proj: round(proj, 2), salary, own: round(own, 1), low_owned: low, pf: null, dupes: null };
  });
  rows.sort((a, b) => b.win - a.win || b.mean - a.mean);
  rows.forEach((r, i) => { r.rank = i + 1; });
  prog(1);
  return {
    lineups: rows, sims, field_size: fieldSize, field_sample: fieldSample, exact: false, phase: "browser", source: "browser",
    kind, slots: slots.map((s) => s.label), rules, seed, generated_at: new Date(t2).toISOString(),
    generate_seconds: round((t1 - t0) / 1000, 1), contest_seconds: round((t2 - t1) / 1000, 1),
    slate_id: o.slateId != null ? o.slateId : null, tries,
  };
}

function round(x, d) { const m = Math.pow(10, d); return Math.round((Number(x) || 0) * m) / m; }
