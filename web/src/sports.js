// Sport registry helpers (pure ES module, no imports). The registry itself is
// web/src/sports/registry.json, shared with the pipeline (pipeline/sports);
// App.jsx imports it and passes plain spec objects to the engine and worker.
// Every rule here mirrors pipeline/sports/{__init__,slots,corr}.py; the shared
// fixture tests/fixtures/sports/conformance.json checks that both agree.

const TITLE = (s) => (s ? s.charAt(0) + s.slice(1).toLowerCase() : "");

// ── validation (same rules, same order, as pipeline.sports.validate) ─────────
export function validateRegistry(reg) {
  const errs = [];
  if (!reg || typeof reg !== "object" || !reg.sports || typeof reg.sports !== "object" || Array.isArray(reg.sports)) {
    return ["registry must be an object with a 'sports' object"];
  }
  const codes = new Set();
  for (const [key, s] of Object.entries(reg.sports)) {
    if (!s || typeof s !== "object" || Array.isArray(s)) { errs.push(`${key}: entry must be an object`); continue; }
    if (s.code !== key) errs.push(`${key}: code ${JSON.stringify(s.code)} must equal its key`);
    if (codes.has(s.code)) errs.push(`${key}: duplicate code`);
    codes.add(s.code);
    for (const gt of Object.keys(s.game_types || {})) {
      if (!/^\d+$/.test(gt)) errs.push(`${key}: game_types key ${JSON.stringify(gt)} is not an integer string`);
    }
    for (const [kname, k] of Object.entries(s.kinds || {})) {
      const pos = k.positions || [];
      if (!(typeof k.cap === "number" && k.cap > 0)) errs.push(`${key}.${kname}: cap must be > 0`);
      for (const sl of k.slots || []) {
        const p = sl.pos;
        if (p !== "*" && (!Array.isArray(p) || !p.length || !p.every((x) => pos.includes(x)))) {
          errs.push(`${key}.${kname}: slot ${sl.label} positions ${JSON.stringify(p)} not within ${JSON.stringify(pos)}`);
        }
      }
      if (kname === "showdown") {
        const nMult = (k.slots || []).filter((sl) => sl.mult).length;
        if (nMult !== 1) errs.push(`${key}.${kname}: showdown needs exactly one multiplier slot (has ${nMult})`);
      }
    }
    const corr = s.corr || {};
    if (corr.model === "factor") {
      for (const [p, ld] of Object.entries(corr.loadings || {})) {
        const ss = Object.values(ld).reduce((a, v) => a + Number(v) * Number(v), 0);
        if (ss >= 0.95) errs.push(`${key}: loadings for ${p} have sum of squares ${ss.toFixed(3)} >= 0.95`);
      }
    }
  }
  return errs;
}

// ── slot specs ──────────────────────────────────────────────────────────────
function maskOf(pos, positions) {
  if (pos === "*") return (1 << positions.length) - 1;
  let m = 0;
  for (const p of pos) m |= 1 << positions.indexOf(p);
  return m;
}

export function hallNeed(masks, k) {
  const out = {};
  for (let U = 1; U < (1 << k) - 1; U++) out[U] = masks.filter((m) => (m & ~U) === 0).length;
  return out;
}

// Plain-object spec for (sport, kind); the same fields as the Python SlotSpec.
export function specFor(reg, sport, kind) {
  const s = reg && reg.sports && reg.sports[sport];
  if (!s) return null;
  const k = (s.kinds || {})[kind || "classic"];
  if (!k) return null;
  const positions = k.positions.slice();
  const slots = k.slots.map((sl) => {
    const o = { label: sl.label, mask: maskOf(sl.pos, positions) };
    if (sl.mult) o.mult = sl.mult;
    return o;
  });
  const masks = slots.map((x) => x.mask);
  const cpt = slots.find((x) => x.mult);
  const playProb = {};
  for (const [st, p] of Object.entries(s.play_prob || {})) if (st) playProb[TITLE(st)] = p;
  return {
    sport, kind: kind || "classic", positions, slots, size: slots.length, cap: k.cap,
    hallNeed: hallNeed(masks, positions.length), minGames: k.min_games || 1, minTeams: k.min_teams || 1,
    fieldMinSalary: k.field_min_salary || 0, poolMinSalary: k.pool_min_salary || 0,
    stackRules: k.stack_rules || null, lateSwap: k.late_swap || "per_game",
    enforceRules: k.enforce_rules !== false, cptMult: cpt ? cpt.mult : 1, playProb,
    corr: s.corr || null, teamSport: s.team_sport !== false,
  };
}

export function posCodeFor(tokens, spec) {
  const list = typeof tokens === "string" ? tokens.toUpperCase().split("/") : (tokens || []);
  let c = 0;
  for (const t of list) { const i = spec.positions.indexOf(String(t).trim().toUpperCase()); if (i >= 0) c |= 1 << i; }
  return c;
}

// Slot eligibility of one salary-CSV row, or null (excluded: fail closed). Same
// rule as pipeline.sports.slots.eligibility.
export function eligibility(rosterPosition, position, spec) {
  const rp = String(rosterPosition || "").split("/").map((t) => t.trim().toUpperCase()).filter(Boolean);
  const labels = new Set(spec.slots.map((s) => s.label));
  const bits = (code) => spec.positions.filter((p, b) => code & (1 << b));
  if (spec.slots.some((s) => s.mult)) {
    const code = posCodeFor(position || "", spec);
    if (!code) return null;
    return { code, base: bits(code), slots: [...labels].sort() };
  }
  const base = rp.filter((t) => spec.positions.includes(t));
  const code = posCodeFor(base, spec);
  if (!code) return null;
  const expected = new Set(spec.slots.filter((s) => s.mask & code).map((s) => s.label));
  const inCsv = new Set(rp.filter((t) => labels.has(t)));
  const unknown = rp.filter((t) => !spec.positions.includes(t) && !labels.has(t));
  if (unknown.length || expected.size !== inCsv.size || [...expected].some((x) => !inCsv.has(x))) return null;
  const order = [];
  for (const s of spec.slots) if (expected.has(s.label) && !order.includes(s.label)) order.push(s.label);
  return { code, base: bits(code), slots: order };
}

// can players with these codes take distinct slots (augmenting paths)?
export function fits(codes, masks) {
  if (codes.length > masks.length) return false;
  const match = new Array(masks.length).fill(-1);
  const tryP = (i, seen) => {
    for (let s = 0; s < masks.length; s++) {
      if (seen[s] || !(codes[i] & masks[s])) continue;
      seen[s] = 1;
      if (match[s] < 0 || tryP(match[s], seen)) { match[s] = i; return true; }
    }
    return false;
  };
  for (let i = 0; i < codes.length; i++) if (!tryP(i, new Array(masks.length).fill(0))) return false;
  return true;
}

// DraftKings legality (+ registry stack rules) of a complete lineup in slot order.
// players: [{id, code, salary, cptSalary?, team, opp, game}]. Same verdicts as
// pipeline.sports.slots.lineup_legal on the conformance fixture.
export function lineupLegal(players, spec, opts = {}) {
  if (!players || players.length !== spec.size) return false;
  if (new Set(players.map((p) => p.id)).size !== players.length) return false;
  let salary = 0;
  for (let k = 0; k < spec.size; k++) {
    const p = players[k], sl = spec.slots[k];
    if (!(p.code & sl.mask)) return false;
    salary += sl.mult ? (p.cptSalary || Math.round(p.salary * sl.mult)) : p.salary;
  }
  if (salary > spec.cap) return false;
  if (!spec.enforceRules) return true;
  if (new Set(players.map((p) => p.team)).size < spec.minTeams) return false;
  if (spec.slots.some((s) => s.mult)) return true;
  if (new Set(players.map((p) => p.game)).size < spec.minGames) return false;
  const sr = spec.stackRules;
  if (!sr) return true;
  const bit = (pos) => (spec.positions.includes(pos) ? 1 << spec.positions.indexOf(pos) : 0);
  const qb = bit("QB"), dst = bit("DST"), catcher = bit("WR") | bit("TE"), skill = catcher | bit("RB");
  const qbs = players.filter((p) => p.code & qb);
  if (sr.no_dst_vs_qb && dst) {
    for (const d of players.filter((p) => p.code & dst)) if (qbs.some((q) => q.opp === d.team)) return false;
  }
  if (opts.stack === false) return true;
  const m = sr.qb_stack_min || 0, b = sr.bring_back_min || 0;
  for (const q of qbs) {
    if (m && players.filter((p) => p !== q && (p.code & catcher) && p.team === q.team).length < m) return false;
    if (b && players.filter((p) => (p.code & skill) && p.team === q.opp).length < b) return false;
  }
  return true;
}

// ── factor correlation (same construction as pipeline.sports.corr) ─────────
// players: [{position, team, opp, game}] -> { keys, rows: [[[k, l], ...]], idio }
export function factorLoadings(players, corr) {
  const scope = {};
  for (const f of (corr && corr.factors) || []) scope[f.name] = f.scope;
  const lds = (corr && corr.loadings) || {};
  const keys = new Map(), rows = [], idio = new Float64Array(players.length);
  players.forEach((p, i) => {
    const pos = String(p.position || "").split("/")[0].toUpperCase();
    const acc = new Map();
    for (const [name, val] of Object.entries(lds[pos] || {})) {
      const opp = name.startsWith("opp:");
      const f = opp ? name.slice(4) : name;
      const sc = scope[f];
      if (!sc) continue;
      const key = sc === "game" ? `game:${p.game}` : `team:${f}:${opp ? p.opp : p.team}`;
      if (!keys.has(key)) keys.set(key, keys.size);
      const k = keys.get(key);
      acc.set(k, (acc.get(k) || 0) + Number(val));
    }
    const row = [...acc.entries()];
    rows.push(row);
    const ss = row.reduce((a, [, l]) => a + l * l, 0);
    idio[i] = Math.sqrt(Math.max(1 - ss, 1e-6));
  });
  return { keys: [...keys.keys()], rows, idio };
}

export function impliedCorr(players, spec) {
  const { rows } = factorLoadings(players, spec.corr || spec);
  const n = players.length, C = [];
  for (let i = 0; i < n; i++) {
    C.push(new Array(n).fill(0));
    for (let j = 0; j < n; j++) {
      if (i === j) { C[i][j] = 1; continue; }
      let v = 0;
      for (const [k, l] of rows[i]) for (const [k2, l2] of rows[j]) if (k === k2) v += l * l2;
      C[i][j] = v;
    }
  }
  return C;
}

// ── entries files, data paths, sports.json ──────────────────────────────────
// {sport, kind} whose slot labels equal the header's slot columns (as a multiset)
export function detectEntriesSport(headerCells, reg) {
  const cells = (headerCells || []).map((c) => String(c).trim().toUpperCase());
  if (!cells.includes("ENTRY ID") || !reg || !reg.sports) return null;
  const order = Object.keys(reg.sports);
  // enabled sports first, so a shared header (e.g. CPT + 5 UTIL: NBA and CFB) resolves to a live sport
  order.sort((a, b) => (reg.sports[b].enabled ? 1 : 0) - (reg.sports[a].enabled ? 1 : 0));
  for (const code of order) {
    for (const kind of Object.keys(reg.sports[code].kinds || {})) {
      const sp = specFor(reg, code, kind);
      if (sp && slotColumnsMatch(cells, sp)) return { sport: code, kind };
    }
  }
  return null;
}

export function slotColumnsMatch(cells, spec) {
  const labels = new Set(spec.slots.map((s) => s.label));
  const got = cells.filter((c) => labels.has(c)).sort();
  const want = spec.slots.map((s) => s.label).sort();
  return got.length === want.length && got.every((x, i) => x === want[i]);
}

export function sportBase(reg, code) {
  const s = reg && reg.sports && reg.sports[code];
  return s ? (s.data_prefix || "") : "";
}

// data/sports.json -> [{code, label, base, status, ...}] for the switcher. Absent,
// empty or malformed -> NBA only (the switcher then hides and NBA runs as before).
export function sportsIndex(doc, reg) {
  const nbaOnly = [{ code: "NBA", label: "NBA", base: "", status: "live", enabled: true, scoring_verified: true }];
  if (!doc || typeof doc !== "object" || !Array.isArray(doc.sports)) return nbaOnly;
  const known = (reg && reg.sports) || {};
  const out = [];
  for (const s of doc.sports) {
    if (!s || typeof s !== "object" || typeof s.code !== "string" || !(s.code in known)) continue;
    if (out.some((o) => o.code === s.code)) continue;
    out.push({
      code: s.code, label: typeof s.label === "string" ? s.label : known[s.code].label,
      base: s.code === "NBA" ? "" : sportBase(reg, s.code),
      status: ["live", "no_slate", "off_season"].includes(s.status) ? s.status : "no_slate",
      enabled: s.enabled !== false, in_season: !!s.in_season, slates: Number(s.slates) || 0,
      next_lock_utc: typeof s.next_lock_utc === "string" ? s.next_lock_utc : null,
      season_note: typeof s.season_note === "string" ? s.season_note : "",
      scoring_verified: s.scoring_verified !== false,
    });
  }
  if (!out.some((o) => o.code === "NBA")) out.unshift(nbaOnly[0]);
  return out;
}

// The sport a page opens on: ?sport= first, then the saved choice, then NBA.
// Only enabled registry sports count (a disabled stub such as MLB never opens).
export function pickInitialSport(urlParam, saved, reg) {
  const ok = (c) => !!(c && reg && reg.sports && reg.sports[c] && reg.sports[c].enabled);
  const u = urlParam ? String(urlParam).trim().toUpperCase() : "";
  if (ok(u)) return u;
  const s = saved ? String(saved).trim().toUpperCase() : "";
  if (ok(s)) return s;
  return "NBA";
}

// `code` if the loaded sports index lists it, else "NBA" (index absent or
// malformed = NBA only), so a viewer is never left on a sport with no switcher.
export function listedSport(code, list) {
  if (!code || code === "NBA") return "NBA";
  return Array.isArray(list) && list.length > 1 && list.some((s) => s && s.code === code) ? code : "NBA";
}
