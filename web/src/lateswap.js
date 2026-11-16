// Late swap: optimise each DraftKings entry after lock. Players whose games
// have started stay exactly where they are; every other slot is re-optimised
// with the same simulation model as the full-slate engine. Each entry's
// original lineup and its candidates play the same simulated contests, so the
// pre-swap vs post-swap ROI comparison is apples to apples.
import { buildSim, bestFill, rngFrom, sampleScores, sampleField, simulateContest, slotsFor, slotSalary, CLASSIC_SLOTS, SHOWDOWN_SLOTS } from "./engine.js";

function splitLine(line) {
  const out = []; let cur = "", q = false;
  for (const ch of line) { if (ch === '"') q = !q; else if (ch === "," && !q) { out.push(cur); cur = ""; } else cur += ch; }
  out.push(cur); return out;
}

// DraftKings "edit entries" CSV: Entry ID, Contest Name, Contest ID, Entry Fee, <slots>…
export function parseEntries(text) {
  const lines = text.replace(/\r/g, "").split("\n");
  let h = -1, head = null;
  for (let i = 0; i < lines.length; i++) {
    const c = splitLine(lines[i]).map((x) => x.trim().toUpperCase());
    if (c.includes("ENTRY ID") && (c.includes("UTIL") || c.includes("CPT"))) { h = i; head = c; break; }
  }
  if (h < 0) return null;
  const labels = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL", "CPT"];
  const slotCols = [];
  head.forEach((c, i) => { if (labels.includes(c)) slotCols.push({ label: c, col: i }); });
  const kind = slotCols.some((s) => s.label === "CPT") ? "showdown" : "classic";
  const iId = head.indexOf("ENTRY ID"), iFee = head.indexOf("ENTRY FEE"), iContest = head.indexOf("CONTEST NAME");
  const entries = [];
  for (let r = h + 1; r < lines.length; r++) {
    const c = splitLine(lines[r]);
    const id = (c[iId] || "").trim();
    if (!/^\d+$/.test(id)) continue;
    const slots = slotCols.map((s) => {
      const cell = (c[s.col] || "").trim();
      const m = cell.match(/\((\d+)\)/) || cell.match(/^(\d+)$/);
      return { label: s.label, col: s.col, id: m ? Number(m[1]) : null, cell };
    });
    entries.push({ row: r, cols: c, entryId: id, contest: iContest >= 0 ? c[iContest] : "",
      fee: iFee >= 0 ? Number(String(c[iFee]).replace(/[$,]/g, "")) || 0 : 0, slots });
  }
  return { kind, lines, header: h, slotCols, entries };
}

export function runLateSwap(players, doc, opts, onProgress) {
  const now = opts.now || Date.now();
  const sim = buildSim(players, now);
  const kind = doc.kind;
  const slotDefs = slotsFor(kind);
  const byId = new Map(), byCpt = new Map();
  players.forEach((p, i) => { if (p.dkId) byId.set(p.dkId, i); if (p.cptId) byCpt.set(p.cptId, i); });
  const exclude = new Set(opts.exclude || []);
  const mustHave = opts.include || [];
  const rng = rngFrom(opts.seed || 777);
  const corr = opts.corr;
  // candidate outcomes: one optimal re-fill per simulated slate
  const nCand = opts.candidateSims || 120;
  const X = sampleScores(sim, nCand, rng, corr);
  const proj = Float64Array.from(sim.mu);
  const all = [], perEntry = [];
  doc.entries.forEach((e, ei) => {
    const slotObjs = e.slots.map((s, k) => slotDefs[k] || (s.label === "CPT" ? SHOWDOWN_SLOTS[0] : CLASSIC_SLOTS.find((d) => d.label === s.label)));
    const idx = e.slots.map((s, k) => (s.id == null ? -1 : (slotObjs[k].mult ? byCpt.get(s.id) : byId.get(s.id)) ?? -1));
    const unknown = idx.some((i, k) => i < 0 && e.slots[k].id != null);
    const fixed = [], open = [], openK = [];
    e.slots.forEach((s, k) => {
      const i = idx[k];
      if (i >= 0 && sim.locked[i]) fixed.push({ slot: slotObjs[k], p: i, k });
      else if (i < 0 && s.id != null) fixed.push({ slot: slotObjs[k], p: -1, k });  // not in our pool: leave it
      else { open.push(slotObjs[k]); openK.push(k); }
    });
    const base = { entry: e, idx, fixed, open, openK, slotObjs, unknown, cands: [] };
    const toLineup = (fill) => {
      const full = idx.slice();
      openK.forEach((k, j) => { full[k] = fill[j]; });
      return full;
    };
    const original = idx.slice();
    const seen = new Set();
    const add = (full, tag) => {
      if (full.some((p) => p < 0 && !unknown)) return;
      const key = full.join(",");
      if (seen.has(key)) return;
      if (mustHave.length && tag !== "original" && !mustHave.every((m) => full.includes(m) || !open.length)) return;
      seen.add(key);
      base.cands.push({ full, tag, at: all.length });
      all.push(full.map((p, k) => [p, slotObjs[k].mult || 1]).filter(([p]) => p >= 0));
    };
    if (original.every((p) => p >= 0)) add(original, "original");
    const fixedForFill = fixed.filter((f) => f.p >= 0);
    const fixedTeams = fixedForFill.map((f) => sim.team[f.p]);
    if (open.length && !unknown) {
      const fillWith = (score) => bestFill(score, sim, players, fixedForFill, open,
        { exclude, unlockedOnly: true, fixedTeams });
      const pf = fillWith(proj);
      if (pf) add(toLineup(pf), "projection");
      for (let s = 0; s < nCand; s++) {
        const sc = X.subarray(s * sim.P, (s + 1) * sim.P);
        const f = fillWith(sc);
        if (f) add(toLineup(f), "sim");
        if (base.cands.length >= (opts.maxCandidates || 25)) break;
      }
    }
    perEntry.push(base);
    if (onProgress) onProgress(0.3 * (ei + 1) / doc.entries.length);
  });
  // every candidate of every entry plays the same simulated contests
  const field = sampleField(sim, players, kind, Math.min(opts.fieldSample || 2000, opts.fieldSize || 10000), rngFrom((opts.seed || 777) + 1));
  const res = simulateContest(sim, players, all, { ...opts, kind, field, seed: (opts.seed || 777) + 2 },
    (f) => onProgress && onProgress(0.3 + 0.7 * f));
  const out = perEntry.map((b) => {
    const orig = b.cands.find((c) => c.tag === "original");
    let best = orig;
    for (const c of b.cands) {
      const r = res[c.at];
      if (!best || r.roi > res[best.at].roi + 1e-9 || (Math.abs(r.roi - res[best.at].roi) < 1e-9 && r.mean > res[best.at].mean)) best = c;
    }
    const mk = (c) => c ? { players: c.full, ...res[c.at] } : null;
    return {
      entryId: b.entry.entryId, contest: b.entry.contest, fee: b.entry.fee, row: b.entry.row,
      slots: b.entry.slots.map((s, k) => ({ label: s.label, col: s.col, cell: s.cell, locked: b.fixed.some((f) => f.k === k) })),
      unknown: b.unknown, openSlots: b.open.length,
      before: mk(orig), after: mk(best),
      changed: !!(orig && best && best.full.join(",") !== orig.full.join(",")),
    };
  });
  const avg = (k) => { const v = out.filter((o) => o[k]).map((o) => o[k].roi); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0; };
  return { kind, entries: out, preROI: avg("before"), postROI: avg("after") };
}

// rebuild the DraftKings upload file: only chosen swaps change, everything else
// (entry ids, locked players, untouched entries) stays byte-for-byte the same
export function exportEntries(doc, results, chosen, players) {
  const lines = doc.lines.slice();
  results.forEach((r) => {
    if (!chosen.has(r.entryId) || !r.changed) return;
    const cols = splitLine(lines[r.row]);
    r.slots.forEach((s, k) => {
      const p = r.after.players[k];
      if (s.locked || p == null || p < 0) return;
      const pl = players[p];
      const id = s.label === "CPT" ? pl.cptId : pl.dkId;
      cols[s.col] = `${pl.name} (${id})`;
    });
    lines[r.row] = cols.map((c) => (c.includes(",") ? `"${c}"` : c)).join(",");
  });
  return lines.join("\n");
}

export { slotSalary };
