// Sigma DFS browser simulation engine (used by Late Swap and the Single Lineup
// Simulator; runs inside a Web Worker). Same model as the server engine:
// correlated, right-skewed player outcomes (game + team factors, gamma
// marginals), Questionable players can sit, field drafted from projected
// ownership, DraftKings-style payout curve.

export const CLASSIC_SLOTS = [
  { label: "PG", mask: 1 }, { label: "SG", mask: 2 }, { label: "SF", mask: 4 }, { label: "PF", mask: 8 },
  { label: "C", mask: 16 }, { label: "G", mask: 3 }, { label: "F", mask: 12 }, { label: "UTIL", mask: 31 },
];
export const SHOWDOWN_SLOTS = [
  { label: "CPT", mask: 31, mult: 1.5 }, { label: "UTIL", mask: 31 }, { label: "UTIL", mask: 31 },
  { label: "UTIL", mask: 31 }, { label: "UTIL", mask: 31 }, { label: "UTIL", mask: 31 },
];
export const SALARY_CAP = 50000;
const POS = ["PG", "SG", "SF", "PF", "C"];
const PLAY_PROB = { Questionable: 0.7, Probable: 0.97 };

export function posCode(positions) {
  let c = 0;
  for (const p of positions || []) { const i = POS.indexOf(p); if (i >= 0) c |= 1 << i; }
  return c || 31;
}
export function slotsFor(kind) { return kind === "showdown" ? SHOWDOWN_SLOTS : CLASSIC_SLOTS; }
export function slotSalary(p, slot) { return slot.mult ? (p.cptSalary || Math.round(p.salary * 1.5)) : p.salary; }
export function slotId(p, slot) { return slot.mult ? p.cptId : p.dkId; }

// ── random numbers ──────────────────────────────────────────────────────────
export function rngFrom(seed) {
  let a = seed >>> 0 || 1;
  const next = () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  let spare = null;
  next.normal = () => {
    if (spare !== null) { const s = spare; spare = null; return s; }
    let u = 0, v = 0; while (u === 0) u = next(); v = next();
    const r = Math.sqrt(-2 * Math.log(u)); spare = r * Math.sin(2 * Math.PI * v); return r * Math.cos(2 * Math.PI * v);
  };
  next.gumbel = () => -Math.log(-Math.log(next() || 1e-12));
  return next;
}

// ── players → simulation arrays ─────────────────────────────────────────────
// players: parsed projection rows. Players already playing (late swap) use
// their live points so far plus the projection for the rest of the game.
export function buildSim(players, now) {
  const teams = {}, games = {};
  const P = players.length;
  const mu = new Float64Array(P), sd = new Float64Array(P), pplay = new Float64Array(P);
  // per-player outcome percentiles at U_SAMPLE (learned from history), when published
  const Q = new Array(P).fill(null);
  const team = new Int32Array(P), game = new Int32Array(P), locked = new Uint8Array(P), code = new Int32Array(P);
  players.forEach((p, i) => {
    const t = (p.team || "").toUpperCase(), o = (p.opp || "").toUpperCase();
    if (!(t in teams)) teams[t] = Object.keys(teams).length;
    const gk = p.corrGame || [t, o].sort().join("@");
    if (!(gk in games)) games[gk] = Object.keys(games).length;
    team[i] = teams[t]; game[i] = games[gk];
    code[i] = posCode(p.positions);
    const started = p.gameStart && now >= Date.parse(p.gameStart);
    locked[i] = started ? 1 : 0;
    const mean = p.mean != null && !isNaN(p.mean) ? p.mean : p.proj;   // proj is the median
    const s = p.stdDev > 0 ? p.stdDev : mean * 0.3;
    if (started && p.liveFP != null && !isNaN(p.liveFP)) {
      const left = Math.max(0, 1 - (p.progress || 0));
      mu[i] = p.liveFP + mean * left; sd[i] = s * Math.sqrt(left); pplay[i] = 1;
    } else {
      mu[i] = Math.max(0, mean); sd[i] = Math.max(s, 0.15 * mean);
      pplay[i] = started ? 1 : (PLAY_PROB[p.injury] || 1);
      if (Array.isArray(p.q) && p.q.length === U_SAMPLE.length && p.q.every((v) => !isNaN(v))) Q[i] = p.q;
    }
  });
  return { P, mu, sd, pplay, Q, team, game, locked, code, nTeam: Object.keys(teams).length, nGame: Object.keys(games).length };
}

// percentiles the pipeline publishes (distribution.U_SAMPLE)
export const U_SAMPLE = [0, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6,
  0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.975, 0.99, 0.999];
export function normCdf(z) {   // Abramowitz-Stegun 7.1.26, error < 2e-7
  const x = Math.abs(z) / Math.SQRT2, t = 1 / (1 + 0.3275911 * x);
  const e = 1 - t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429)))) * Math.exp(-x * x);
  return 0.5 * (1 + (z < 0 ? -e : e));
}
export function fromCurve(q, u) {
  let lo = 0, hi = U_SAMPLE.length - 1;
  if (u >= U_SAMPLE[hi]) return q[hi];
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (U_SAMPLE[m] <= u) lo = m; else hi = m; }
  const w = (u - U_SAMPLE[lo]) / (U_SAMPLE[hi] - U_SAMPLE[lo]);
  return q[lo] + w * (q[hi] - q[lo]);
}

// n correlated simulated slates: Float32Array of n*P
export function sampleScores(sim, n, rng, corr) {
  const { P, mu, sd, pplay, Q, team, game, nTeam, nGame } = sim;
  const rg = Math.min(Math.max((corr && corr.opp) || 0.04, 0), 0.3);
  const rt = Math.min(Math.max(((corr && corr.team) || 0.04) - rg, 0), 0.3);
  const a = Math.sqrt(rg), b = Math.sqrt(rt), c = Math.sqrt(Math.max(1e-6, 1 - rg - rt));
  const k = new Float64Array(P), h = new Float64Array(P);
  for (let i = 0; i < P; i++) { const kk = sd[i] > 0 && mu[i] > 0 ? Math.max((mu[i] / sd[i]) ** 2, 1) : 1e9; k[i] = kk; h[i] = 1 / (9 * kk); }
  const out = new Float32Array(n * P);
  const G = new Float64Array(nGame), T = new Float64Array(nTeam);
  for (let s = 0; s < n; s++) {
    for (let g = 0; g < nGame; g++) G[g] = rng.normal();
    for (let t = 0; t < nTeam; t++) T[t] = rng.normal();
    const base = s * P;
    for (let i = 0; i < P; i++) {
      if (mu[i] <= 0) { out[base + i] = 0; continue; }
      if (pplay[i] < 1 && rng() > pplay[i]) { out[base + i] = 0; continue; }
      const z = a * G[game[i]] + b * T[team[i]] + c * rng.normal();
      if (Q && Q[i]) { out[base + i] = fromCurve(Q[i], normCdf(z)); continue; }
      const w = Math.max(1 - h[i] + z * Math.sqrt(h[i]), 0);
      out[base + i] = sd[i] > 0 ? mu[i] * w * w * w : mu[i];
    }
  }
  return out;
}

// ── payouts ─────────────────────────────────────────────────────────────────
// Same curve as the original Sigma optimizer: geometric decay from 1st place
// down to the entry fee, prize pool = 90% of entry fees.
export function payoutTable({ fieldSize, entryFee, firstPct, table }) {
  if (table && table.length) {
    const mx = table[table.length - 1].place, P = new Array(mx).fill(0);
    table.forEach((r) => { P[r.place - 1] = r.prize; }); return P;
  }
  const pool = fieldSize * entryFee * 0.9, pct = firstPct || 20;
  const paid = Math.max(1, Math.min(Math.round(fieldSize * pct / 100), fieldSize));
  const fp = pool * (pct / 100), rr = paid > 1 ? Math.pow(entryFee / fp, 1 / (paid - 1)) : 1;
  const raw = []; let sm = 0;
  for (let i = 0; i < paid; i++) { raw.push(fp * Math.pow(rr, i)); sm += raw[i]; }
  return raw.map((x) => (x * pool) / Math.max(sm, 1e-9));
}

// ── legality ────────────────────────────────────────────────────────────────
const fillCache = new Map();
// can players with these position codes occupy these slot masks (one each)?
export function canAssign(codes, masks) {
  const key = codes.slice().sort((x, y) => x - y).join(",") + "|" + masks.slice().sort((x, y) => x - y).join(",");
  if (fillCache.has(key)) return fillCache.get(key);
  const match = new Array(masks.length).fill(-1);
  const tryP = (i, seen) => {
    for (let s = 0; s < masks.length; s++) {
      if (seen[s] || !(codes[i] & masks[s])) continue;
      seen[s] = 1;
      if (match[s] < 0 || tryP(match[s], seen)) { match[s] = i; return true; }
    }
    return false;
  };
  let ok = codes.length <= masks.length;
  for (let i = 0; ok && i < codes.length; i++) ok = tryP(i, new Array(masks.length).fill(0));
  if (fillCache.size > 50000) fillCache.clear();
  fillCache.set(key, ok);
  return ok;
}

// assign chosen players to the open slots (late games into flex slots)
export function assignSlots(chosen, openSlots, players, sim) {
  const order = chosen.slice().sort((x, y) => (Date.parse(players[y].gameStart || 0) || 0) - (Date.parse(players[x].gameStart || 0) || 0));
  const idx = openSlots.map((s, k) => k).sort((x, y) => popcount(openSlots[y].mask) - popcount(openSlots[x].mask));
  const res = new Array(openSlots.length).fill(-1);
  const used = new Set();
  const bt = (k) => {
    if (k === idx.length) return true;
    const s = openSlots[idx[k]];
    for (const p of order) {
      if (used.has(p) || !(sim.code[p] & s.mask)) continue;
      used.add(p); res[idx[k]] = p;
      if (bt(k + 1)) return true;
      used.delete(p);
    }
    return false;
  };
  return bt(0) ? res : null;
}
function popcount(x) { let c = 0; while (x) { c += x & 1; x >>= 1; } return c; }

// ── fill open slots to maximise a score vector ──────────────────────────────
// fixed: [{slot, p}] already in the lineup (locked or user-locked).
// open: slot objects to fill. Returns array of player indices for `open` (slot order) or null.
// Showdown CPT is handled by evaluating each candidate captain.
export function bestFill(score, sim, players, fixed, open, opts = {}) {
  const cap = opts.cap || SALARY_CAP;
  const exclude = opts.exclude || new Set();
  const usedFixed = new Set(fixed.map((f) => f.p));
  let budget = cap;
  fixed.forEach((f) => { budget -= slotSalary(players[f.p], f.slot); });
  const avail = [];
  for (let i = 0; i < sim.P; i++) {
    if (usedFixed.has(i) || exclude.has(i) || (opts.unlockedOnly && sim.locked[i])) continue;
    if (score[i] <= 0) continue;
    avail.push(i);
  }
  if (!open.length) return [];
  const cptSlot = open.findIndex((s) => s.mult);
  if (cptSlot >= 0) {
    // showdown: try the best captains, fill the rest as UTIL
    const rest = open.filter((_, k) => k !== cptSlot);
    let best = null, bestV = -1;
    const cands = avail.slice().sort((x, y) => score[y] - score[x]).slice(0, 12);
    for (const c of cands) {
      const cs = slotSalary(players[c], open[cptSlot]);
      if (cs > budget) continue;
      const fill = fillGeneric(score, sim, players, avail.filter((x) => x !== c), rest, budget - cs, opts);
      if (!fill) continue;
      const v = 1.5 * score[c] + fill.reduce((s, p) => s + score[p], 0);
      if (v > bestV) { bestV = v; best = [c, ...fill]; }
    }
    if (!best) return null;
    const out = new Array(open.length); let r = 0;
    open.forEach((s, k) => { out[k] = k === cptSlot ? best[0] : best[1 + r++]; });
    return out;
  }
  const chosen = fillGeneric(score, sim, players, avail, open, budget, opts);
  return chosen ? assignSlots(chosen, open, players, sim) : null;
}

function fillGeneric(score, sim, players, avail, open, budget, opts) {
  const masks = open.map((s) => s.mask);
  const n = open.length;
  const sal = (p) => players[p].salary;
  const minSal = avail.map(sal).sort((x, y) => x - y);
  const cheapest = (k) => { let s = 0; for (let i = 0; i < k && i < minSal.length; i++) s += minSal[i]; return s; };
  const teamMax = opts.maxPerTeam || 99;
  const teamCount = (lu) => { const c = {}; let m = 0; lu.forEach((p) => { c[sim.team[p]] = (c[sim.team[p]] || 0) + 1; m = Math.max(m, c[sim.team[p]]); }); return m; };
  const baseTeams = opts.fixedTeams || [];
  const legal = (lu) => canAssign(lu.map((p) => sim.code[p]), masks) && teamCount(lu.concat(baseTeams)) <= teamMax;
  let best = null, bestV = -1;
  for (const mode of ["value", "points"]) {
    const order = avail.slice().sort((x, y) => mode === "value" ? score[y] / sal(y) - score[x] / sal(x) : score[y] - score[x]);
    let lu = [], spent = 0;
    for (const p of order) {
      if (lu.length === n) break;
      if (spent + sal(p) + cheapest(n - lu.length - 1) > budget) continue;
      if (!legal(lu.concat([p]))) continue;
      lu.push(p); spent += sal(p);
    }
    if (lu.length < n) continue;
    lu = improve(lu, score, avail, budget, legal, sal, 10);
    const v = lu.reduce((s, p) => s + score[p], 0);
    if (v > bestV) { bestV = v; best = lu; }
  }
  // Greedy fills can paint themselves into a corner on small slates (salary left
  // only for positions that are already filled). Always also start from the
  // cheapest legal lineup, which is feasible whenever any lineup is, and climb.
  const cheap = cheapestLegal(avail, masks, sal, sim, budget, legal);
  if (cheap) {
    const lu = improve(cheap, score, avail, budget, legal, sal, 30);
    const v = lu.reduce((s, p) => s + score[p], 0);
    if (v > bestV) { bestV = v; best = lu; }
  }
  return best;
}

function cheapestLegal(avail, masks, sal, sim, budget, legal) {
  // fill the most restrictive slots first with the cheapest eligible player
  const order = masks.map((m, k) => k).sort((a, b) => popcount(masks[a]) - popcount(masks[b]));
  const byPrice = avail.slice().sort((x, y) => sal(x) - sal(y));
  const lu = [];
  for (const k of order) {
    const p = byPrice.find((q) => !lu.includes(q) && (sim.code[q] & masks[k]) && legal(lu.concat([q])));
    if (p == null) return null;
    lu.push(p);
  }
  return lu.reduce((s, p) => s + sal(p), 0) <= budget ? lu : null;
}

function improve(lu, score, avail, budget, legal, sal, maxPass = 10) {
  const inLu = new Set(lu);
  for (let pass = 0; pass < maxPass; pass++) {
    let improved = false;
    let spent = lu.reduce((s, p) => s + sal(p), 0);
    // single swaps
    for (let a = 0; a < lu.length; a++) {
      let bestB = -1, bestG = 1e-9;
      for (const b of avail) {
        if (inLu.has(b) || spent - sal(lu[a]) + sal(b) > budget) continue;
        const g = score[b] - score[lu[a]];
        if (g <= bestG) continue;
        const t = lu.slice(); t[a] = b;
        if (legal(t)) { bestB = b; bestG = g; }
      }
      if (bestB >= 0) { inLu.delete(lu[a]); inLu.add(bestB); spent += sal(bestB) - sal(lu[a]); lu[a] = bestB; improved = true; }
    }
    // pair swaps among the top candidates
    if (!improved && lu.length >= 2) {
      const top = avail.filter((p) => !inLu.has(p)).sort((x, y) => score[y] - score[x]).slice(0, 30);
      let best = null, bestG = 1e-9;
      for (let a = 0; a < lu.length; a++) for (let b = a + 1; b < lu.length; b++) {
        const room = budget - spent + sal(lu[a]) + sal(lu[b]), base = score[lu[a]] + score[lu[b]];
        for (let i = 0; i < top.length; i++) for (let j = i + 1; j < top.length; j++) {
          const g = score[top[i]] + score[top[j]] - base;
          if (g <= bestG || sal(top[i]) + sal(top[j]) > room) continue;
          const t = lu.slice(); t[a] = top[i]; t[b] = top[j];
          if (legal(t)) { best = [a, b, top[i], top[j]]; bestG = g; }
        }
      }
      if (best) { const [a, b, i, j] = best; inLu.delete(lu[a]); inLu.delete(lu[b]); inLu.add(i); inLu.add(j); lu[a] = i; lu[b] = j; improved = true; }
    }
    if (!improved) break;
  }
  return lu;
}

// ── field ───────────────────────────────────────────────────────────────────
export function sampleField(sim, players, kind, n, rng) {
  const slots = slotsFor(kind);
  const w = players.map((p) => Math.log(Math.max(p.own || 0.05, 0.05) / 100));
  const out = [];
  let tries = 0;
  while (out.length < n && tries < n * 30) {
    tries++;
    const keys = w.map((x) => x + rng.gumbel());
    const order = keys.map((_, i) => i).filter((i) => sim.mu[i] > 0).sort((x, y) => keys[y] - keys[x]);
    if (kind === "showdown") {
      const cpt = order[0]; let spent = slotSalary(players[cpt], slots[0]); const u = [];
      for (const p of order.slice(1)) { if (u.length === 5) break; if (spent + players[p].salary > SALARY_CAP) continue; u.push(p); spent += players[p].salary; }
      if (u.length === 5 && spent >= 45000 && new Set([cpt, ...u].map((p) => sim.team[p])).size === 2) out.push([[cpt, 1.5], ...u.map((p) => [p, 1])]);
      continue;
    }
    const masks = slots.map((s) => s.mask), lu = []; let spent = 0;
    const minSal = Math.min(...players.map((p) => p.salary));
    for (const p of order) {
      if (lu.length === 8) break;
      if (spent + players[p].salary + (7 - lu.length) * minSal > SALARY_CAP) continue;
      if (!canAssign(lu.concat([p]).map((q) => sim.code[q]), masks)) continue;
      lu.push(p); spent += players[p].salary;
    }
    if (lu.length === 8 && spent >= 48500) out.push(lu.map((p) => [p, 1]));
  }
  return out;
}

// ── contest simulation: ROI for each candidate lineup ───────────────────────
// lineups: arrays of [playerIndex, multiplier]. All candidates face the same
// simulated outcomes (fair comparison). Field lineups stand in for the full
// field; ranks are scaled to the contest size.
export function simulateContest(sim, players, lineups, opts, onProgress) {
  const sims = opts.sims || 20000;
  const fieldSize = Math.max(2, opts.fieldSize || 10000);
  const fee = opts.entryFee || 0;
  const rng = rngFrom(opts.seed || 12345);
  const field = opts.field || sampleField(sim, players, opts.kind, Math.min(opts.fieldSample || 2000, fieldSize), rng);
  const pay = payoutTable(opts);
  const F = field.length, L = lineups.length, P = sim.P;
  const sum = new Float64Array(L), sum2 = new Float64Array(L), win = new Float64Array(L), cash = new Float64Array(L),
    top1 = new Float64Array(L), payout = new Float64Array(L);
  const top1cut = Math.max(1, Math.round(fieldSize * 0.01));
  const fs = new Float64Array(F);
  const cum = new Float64Array(fieldSize + 2);      // cumulative prize by rank
  for (let r = 0; r <= fieldSize; r++) cum[r + 1] = cum[r] + (r < pay.length ? pay[r] : 0);
  const batch = 500;
  for (let done = 0; done < sims; done += batch) {
    const n = Math.min(batch, sims - done);
    const X = sampleScores(sim, n, rng, opts.corr);
    for (let s = 0; s < n; s++) {
      const base = s * P;
      for (let f = 0; f < F; f++) { let v = 0; const lu = field[f]; for (let k = 0; k < lu.length; k++) v += X[base + lu[k][0]] * lu[k][1]; fs[f] = v; }
      fs.sort(); // ascending
      for (let l = 0; l < L; l++) {
        let v = 0; const lu = lineups[l]; for (let k = 0; k < lu.length; k++) v += X[base + lu[k][0]] * lu[k][1];
        sum[l] += v; sum2[l] += v * v;
        // number of field lineups beating v (binary search on ascending array)
        let lo = 0, hi = F; while (lo < hi) { const m = (lo + hi) >> 1; if (fs[m] > v) hi = m; else lo = m + 1; }
        // beating b of the F sampled lineups places you, in the real field, somewhere
        // between ranks b*N/F+1 and (b+1)*N/F: average the prize over that range
        const beaten = F - lo;
        const r0 = (beaten * fieldSize) / F, r1 = ((beaten + 1) * fieldSize) / F;
        const a0 = Math.floor(r0), a1 = Math.max(a0 + 1, Math.ceil(r1));
        payout[l] += (cum[Math.min(a1, cum.length - 1)] - cum[Math.min(a0, cum.length - 1)]) / (a1 - a0);
        if (beaten === 0) win[l] += 1 / Math.max(1, r1);          // chance of first within that range
        if (r0 < top1cut) top1[l] += Math.min(1, (top1cut - r0) / (r1 - r0));
        if (r0 < pay.length) cash[l] += Math.min(1, (pay.length - r0) / (r1 - r0));
      }
    }
    if (onProgress) onProgress(Math.min(1, (done + n) / sims));
  }
  return lineups.map((_, l) => {
    const mean = sum[l] / sims;
    const ev = payout[l] / sims;
    return {
      mean, sd: Math.sqrt(Math.max(sum2[l] / sims - mean * mean, 0)),
      win: (win[l] / sims) * 100, top1: (top1[l] / sims) * 100, cash: (cash[l] / sims) * 100,
      ev, roi: fee > 0 ? ((ev - fee) / fee) * 100 : 0,
    };
  });
}
