// Sigma DFS tools that live beside the full-slate contest simulation:
// slate picker, quick filters, winning patterns, Late Swap, Single Lineup Simulator.
import { Component, useState, useRef, useEffect, useMemo } from "react";
import { slotsFor, slotSalary, buildSim, bestFill, SALARY_CAP } from "./engine.js";
import { parseEntries, exportEntries } from "./lateswap.js";

import { OH, CARD, BR, MU, GR, RE, YE, TX, ACC, ACC2, SURF2, LINE2, alpha } from "./theme.js";
import { BarList, Sparkline } from "./charts.jsx";
import { SampleBadge, LockCountdown } from "./hud.jsx";
const card = { background: CARD, border: "1px solid " + BR, borderRadius: 12, padding: 16, minWidth: 0 };
const sl = { fontSize: 11, fontWeight: 700, color: ACC, textTransform: "uppercase", letterSpacing: ".12em", marginBottom: 12 };
const inp = { background: SURF2, border: "1px solid " + BR, borderRadius: 6, color: TX, padding: "7px 9px", fontSize: 13, boxSizing: "border-box" };
const btn = (bg, fg) => ({ padding: "7px 13px", background: bg, color: fg || "#fff", border: "1px solid " + alpha(fg || bg, 33), borderRadius: 6, fontSize: 12, fontWeight: 700, cursor: "pointer" });
const pill = (c) => ({ display: "inline-block", padding: "2px 7px", borderRadius: 4, fontSize: 11, fontWeight: 700, background: alpha(c, 13), color: c });
const fmt = (x, d = 1) => (x == null || isNaN(x) ? "—" : Number(x).toFixed(d));
const money = (x) => "$" + Number(x || 0).toLocaleString();
const roiCol = (x) => (x >= 0 ? GR : RE);

// a tool that fails shows its error instead of taking the whole app down
export class ToolBoundary extends Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  render() {
    if (this.state.error) {
      return <div style={card} role="alert"><div style={sl}>{"This tool hit an error — other tools still work."}</div>
        <div style={{ fontSize: 12, color: MU }}>{String(this.state.error.message || this.state.error)}</div>
        <button style={{ ...btn(OH), marginTop: 10 }} onClick={() => this.setState({ error: null })}>{"Try again"}</button></div>;
    }
    return this.props.children;
  }
}

// ── starter / lock icons ────────────────────────────────────────────────────
export function StarterIcon({ p }) {
  const st = p.starterStatus || "";
  if (!st) return null;
  const confirmed = st === "confirmed";
  const tip = confirmed ? "Confirmed starter" : st === "expected" ? "Expected starter (NBA.com)" : "Projected starter (Sigma)";
  return (
    <span title={tip + (p.unexpected === "starting" ? " — unexpected start" : "")}
      style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 15, height: 15, borderRadius: 3,
        marginLeft: 5, fontSize: 10, fontWeight: 900, color: "#05070d", background: confirmed ? GR : YE, verticalAlign: "middle" }}>
      {"S"}
    </span>
  );
}
export function LockIcon({ title }) {
  return <span title={title || "Game has started: locked"} style={{ marginLeft: 4, fontSize: 11 }}>{"🔒"}</span>;
}

// ── background simulation worker ────────────────────────────────────────────
export function useSimWorker() {
  const ref = useRef(null), jobs = useRef({}), nextId = useRef(1);
  useEffect(() => {
    const w = new Worker(new URL("./simWorker.js", import.meta.url), { type: "module" });
    w.onmessage = (ev) => {
      const { id, progress, result, error } = ev.data, j = jobs.current[id];
      if (!j) return;
      if (progress != null) { j.onProgress && j.onProgress(progress); return; }
      delete jobs.current[id];
      error ? j.reject(new Error(error)) : j.resolve(result);
    };
    ref.current = w;
    return () => w.terminate();
  }, []);
  return (type, payload, onProgress) => new Promise((resolve, reject) => {
    const id = nextId.current++;
    jobs.current[id] = { resolve, reject, onProgress };
    ref.current.postMessage({ id, type, payload });
  });
}

// ── slate picker ────────────────────────────────────────────────────────────
export function SlatePicker({ slates, current, onPick, fmtClock }) {
  if (!slates.length) return <div style={card}><div style={{ color: MU }}>{"No DraftKings NBA slates are open right now."}</div></div>;
  return (
    <div>
      <div style={{ ...card, marginBottom: 12 }}>
        <div style={sl}>{"Choose your slate"}</div>
        <div style={{ fontSize: 13, color: MU }}>{"Pulled directly from DraftKings. Projections, lineups, simulations, patterns, late swap and the lineup simulator all follow the slate you pick."}</div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(300px,1fr))", gap: 12 }}>
        {slates.map((s) => (
          <div key={s.id} className="anim-in" role="button" tabIndex={0} onClick={() => onPick(s)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(s); } }}
            style={{ ...card, cursor: "pointer", borderColor: s.id === current ? LINE2 : BR, boxShadow: s.id === current ? "var(--glow)" : "none" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8, gap: 8 }}>
              <b style={{ fontSize: 16 }}>{s.name}</b>
              <span style={pill(s.kind === "showdown" ? "#a78bfa" : "#4a9eff")}>{s.kind === "showdown" ? "SHOWDOWN" : "CLASSIC"}</span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8, marginBottom: 8 }}>
              <span style={{ fontSize: 12, color: MU }}>{s.game_count + " game" + (s.game_count !== 1 ? "s" : "") + " · first lock " + fmtClock(s.start_utc || (s.games[0] && s.games[0].start_utc))}</span>
              <span style={{ fontSize: 18, color: ACC }}><LockCountdown target={s.start_utc || (s.games[0] && s.games[0].start_utc)} locked={!!s.contest_locked} /></span>
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
              {s.size_bucket ? <span style={pill(ACC2)}>{s.size_bucket.toUpperCase() + " SLATE"}</span> : null}
              {s.lineups_file ? <span style={pill(s.lineups_phase === "exact" ? GR : YE)}>{s.lineups_phase === "exact" ? "EXACT LINEUPS" : "FAST LINEUPS"}</span> : <span style={pill(MU)}>{"NO SIMS YET"}</span>}
              {s.contest_locked ? <span style={pill(YE)}>{s.locked ? "ALL LOCKED" : "LOCKED · late swap open"}</span> : null}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {s.games.map((g) => <span key={g.name} className="num" style={pill(g.locked ? MU : ACC)}>{(g.locked ? "🔒 " : "") + g.away + " @ " + g.home + " " + (g.start_et || "")}</span>)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── quick filters: player search + teams ────────────────────────────────────
export function QuickFilters({ search, setSearch, teams, selTeams, setSelTeams, extra }) {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 10 }}>
      <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="🔍 Search player…" style={{ ...inp, width: 200 }} />
      <span style={{ fontSize: 11, color: MU }}>{"Teams:"}</span>
      {teams.map((t) => {
        const on = selTeams.includes(t);
        return <button key={t} onClick={() => setSelTeams(on ? selTeams.filter((x) => x !== t) : selTeams.concat([t]))}
          style={{ ...pill(on ? OH : MU), border: "1px solid " + (on ? OH : BR), cursor: "pointer" }}>{t}</button>;
      })}
      {selTeams.length || search ? <button onClick={() => { setSearch(""); setSelTeams([]); }} style={btn(BR, MU)}>{"Clear"}</button> : null}
      {extra}
    </div>
  );
}
export function matchesQuick(names, teams, search, selTeams) {
  if (search) { const q = search.toLowerCase(); if (!names.some((n) => n.toLowerCase().includes(q))) return false; }
  if (selTeams.length && !teams.some((t) => selTeams.includes((t || "").toUpperCase()))) return false;
  return true;
}

// ── contest settings used by the two tools ──────────────────────────────────
function ContestSettings({ cfg, setCfg, showFee = true }) {
  const field = (label, key, step) => (
    <label style={{ fontSize: 11, color: MU }}>{label}
      <input type="number" step={step || 1} value={cfg[key]} onChange={(e) => setCfg({ ...cfg, [key]: e.target.value })} style={{ ...inp, width: 110, display: "block", marginTop: 3 }} />
    </label>
  );
  return (
    <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
      {field("Contest size", "fieldSize")}
      {showFee ? field("Entry fee ($)", "entryFee", 0.25) : null}
      {field("Payout to 1st (%)", "firstPct", 0.5)}
      {field("Simulations", "sims", 1000)}
    </div>
  );
}

// ── winning patterns (slate-tailored) ───────────────────────────────────────
export function PatternsView({ patterns, spotlight, slate, players, likedExposure }) {
  const kind = slate ? slate.kind : "classic";
  const bucketName = slate ? slate.size_bucket : null;
  const b = (patterns && patterns.buckets && bucketName && patterns.buckets[bucketName]) || patterns || {};
  const st = b.structure || {}, why = b.why_players_hit || {};
  const cards = (spotlight && spotlight.cards) || [];
  const strong = cards.filter((c) => c.strong), other = cards.filter((c) => !c.strong);
  const guide = players.filter((p) => p.proj > 0 && p.optExp != null).sort((a, c) => c.optExp - a.optExp).slice(0, 40);
  const Tile = ({ big, label, sub }) => (
    <div className="kpi" style={{ padding: "12px 14px" }}>
      <div className="kpi-value" style={{ fontSize: 22, color: ACC }}>{big}</div>
      <div style={{ fontSize: 13, fontWeight: 700, marginTop: 2 }}>{label}</div>
      {sub ? <div style={{ fontSize: 11, color: MU, marginTop: 2 }}>{sub}</div> : null}
    </div>
  );
  const Card = ({ c }) => (
    <div style={{ ...card, borderColor: c.strong ? alpha(OH, 55) : BR, background: c.strong ? alpha(OH, 8) : CARD, marginBottom: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "flex-start" }}>
        <b style={{ fontSize: c.strong ? 15 : 13 }}>{c.headline}</b>
        <span style={{ display: "flex", gap: 4, flexWrap: "wrap", justifyContent: "flex-end" }}>
          {c.strength != null ? <span className="num" style={pill(ACC)}>{"strength " + fmt(c.strength, 2)}</span> : null}
          {c.strong ? <span style={pill(OH)}>{"STRONG MATCH"}</span> : <span style={pill(MU)}>{c.type === "same_situation" ? "SAME SITUATION" : c.type === "winning_profile" ? "PROFILE" : "SURPRISE START"}</span>}
        </span>
      </div>
      <div style={{ fontSize: 13, color: TX, marginTop: 6 }}>{c.detail}</div>
      <div style={{ fontSize: 11, color: MU, marginTop: 6 }}>{c.team + " · " + money(c.salary) + " · proj " + fmt(c.proj) + " · own " + fmt(c.own) + "%"}</div>
    </div>
  );
  return (
    <div>
      <div style={{ ...card, marginBottom: 12 }}>
        <div style={sl}>{"Tonight's spotlight" + (slate ? " · " + slate.name : "")}</div>
        <div style={{ fontSize: 12, color: MU, marginBottom: 10 }}>{"Where tonight's slate matches what has won before. Same-situation cards only appear when the history clears a statistical test (not just a hot streak)."}</div>
        {cards.length ? <div>{strong.map((c) => <Card key={c.player} c={c} />)}{other.map((c) => <Card key={c.player} c={c} />)}</div>
          : <div style={{ color: MU, fontSize: 13 }}>{"No strong historical matches on this slate yet. Cards appear as injuries and lineups are announced."}</div>}
      </div>
      {kind === "showdown" ? <div style={{ ...card, marginBottom: 12, color: MU, fontSize: 13 }}>{"Historical winning-lineup analysis covers classic slates. Showdown uses the same projections and contest simulation."}</div> : null}
      {kind !== "showdown" && (patterns && patterns.nights) ? (
        <div style={{ ...card, marginBottom: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "flex-start" }}>
            <div style={sl}>{"What wins on slates like this" + (bucketName ? " (" + (b.label || bucketName) + ")" : "")}</div>
            <SampleBadge n={b.lineups_analysed || patterns.lineups_analysed || 0} unit="lineups" small={300} extra={(patterns.nights || 0) + " nights"} />
          </div>
          <div style={{ fontSize: 12, color: MU, marginBottom: 10 }}>{"From " + (b.slates || patterns.slates_analysed || 0) + " verified historical slates of this size: the 20 best possible lineups of each."}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(190px,1fr))", gap: 10 }}>
            <Tile big={money(st.salary_used_median)} label="Salary used (typical)" sub={fmt(st.pct_using_49k_plus, 0) + "% used $49K or more"} />
            <Tile big={fmt(st.avg_studs_9k_plus)} label="Studs ($9K+)" sub={"plus " + fmt(st.avg_value_under_5k) + " players under $5K"} />
            <Tile big={fmt(st.pct_3plus_from_one_team, 0) + "%"} label="Stacked 3+ from one team" sub={fmt(st.pct_with_bring_back, 0) + "% included both sides of a game"} />
            <Tile big={fmt(why.teammate_out_pct, 0) + "%"} label="Players with a teammate out" sub="of all players in winning lineups" />
            <Tile big={fmt(why.minutes_spike_pct, 0) + "%"} label="Got a minutes spike" sub="6+ more minutes than usual" />
            <Tile big={fmt(why.pct_outside_top20_projected_value, 0) + "%"} label="Weren't obvious values" sub="outside the top-20 projected values" />
          </div>
          <div className="grid-2" style={{ marginTop: 14 }}>
            <div>
              <div style={{ ...sl, marginBottom: 8 }}>{"Salary tiers per winning lineup"}</div>
              <BarList max={8} format={(v) => fmt(v, 2)} label="Average players per salary tier in winning lineups" items={[
                { label: "Studs ($9K+)", value: st.avg_studs_9k_plus, color: ACC },
                { label: "Mid ($5K–9K)", value: st.avg_mid_5k_9k, color: ACC },
                { label: "Value (under $5K)", value: st.avg_value_under_5k, color: ACC2 },
                { label: "Punts ($3.5K or less)", value: st.avg_punts_3_5k_or_less, color: ACC2 },
              ]} />
            </div>
            <div>
              <div style={{ ...sl, marginBottom: 8 }}>{"Why players hit"}</div>
              <BarList max={100} format={(v) => fmt(v, 0) + "%"} label="Share of winning-lineup players with each situation" items={[
                { label: "Minutes spike", value: why.minutes_spike_pct },
                { label: "High-total game", value: why.high_total_game_pct },
                { label: "Blowout game", value: why.blowout_game_pct },
                { label: "Teammate out", value: why.teammate_out_pct },
                { label: "Surprise start", value: why.surprise_start_pct },
                { label: "Overtime", value: why.overtime_pct },
              ]} />
            </div>
          </div>
        </div>) : null}
      <div style={{ ...card, marginBottom: 12 }}>
        <div style={sl}>{"Exposure guide"}</div>
        <div style={{ fontSize: 12, color: MU, marginBottom: 10 }}>{"Suggested = share of historical winning lineups that contained players with the same pre-game profile (slate size, value rank, teammate out). Compare it with your exposure in the lineups you've liked."}</div>
        {guide.length ? (
          <div style={{ overflowX: "auto" }}><table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead><tr>{["Player", "Salary", "Proj", "Own%", "Suggested", "Yours", ""].map((h) => <th key={h} style={{ textAlign: "left", color: MU, fontSize: 10, padding: "6px 8px", borderBottom: "1px solid " + BR }}>{h}</th>)}</tr></thead>
            <tbody>{guide.map((p) => {
              const mine = likedExposure[p.name] || 0, diff = mine - p.optExp;
              return (<tr key={p.name}>
                <td style={{ padding: "5px 8px" }}><b>{p.name}</b><StarterIcon p={p} /></td>
                <td style={{ padding: "5px 8px", color: OH }}>{money(p.salary)}</td>
                <td style={{ padding: "5px 8px" }}>{fmt(p.proj)}</td>
                <td style={{ padding: "5px 8px" }}>{fmt(p.own)}</td>
                <td style={{ padding: "5px 8px", fontWeight: 800 }}>{fmt(p.optExp, 0) + "%"}</td>
                <td style={{ padding: "5px 8px", color: Math.abs(diff) < 8 ? GR : YE }}>{likedExposure.__count ? fmt(mine, 0) + "%" : "—"}</td>
                <td style={{ padding: "5px 8px", fontSize: 11, color: MU }}>{likedExposure.__count ? (diff > 8 ? "over" : diff < -8 ? "under" : "on target") : ""}</td>
              </tr>);
            })}</tbody></table></div>
        ) : <div style={{ color: MU, fontSize: 13 }}>{"The exposure guide appears once the historical analysis has finished its first nights."}</div>}
      </div>
    </div>
  );
}

// ── Late Swap tool ──────────────────────────────────────────────────────────
export function LateSwapTool({ players, slate, corr, runSim, defaults }) {
  const [doc, setDoc] = useState(null), [err, setErr] = useState("");
  const [cfg, setCfg] = useState({ fieldSize: defaults.fieldSize || 10000, entryFee: defaults.entryFee || 15, firstPct: defaults.firstPct || 20, sims: 20000 });
  const [res, setRes] = useState(null), [busy, setBusy] = useState(0), [chosen, setChosen] = useState(new Set());
  const [search, setSearch] = useState(""), [selTeams, setSelTeams] = useState([]);
  const [exclude, setExclude] = useState([]), [include, setInclude] = useState([]), [onlyChanged, setOnlyChanged] = useState(false);
  const [page, setPage] = useState(0);
  const fileRef = useRef(null), dlRef = useRef(null);
  const idx = useMemo(() => { const m = {}; players.forEach((p, i) => { m[p.name] = i; }); return m; }, [players]);
  const now = Date.now();
  const teams = useMemo(() => Array.from(new Set(players.map((p) => (p.team || "").toUpperCase()))).sort(), [players]);
  function onFile(e) {
    const f = e.target.files[0]; if (!f) return;
    const r = new FileReader();
    r.onload = (ev) => {
      const d = parseEntries(ev.target.result);
      if (!d || !d.entries.length) { setErr("Couldn't find entries. Use DraftKings → My Contests → Upcoming/Live → Edit Entries → Download."); return; }
      if (slate && d.kind !== slate.kind) { setErr("These entries are " + d.kind + " but the selected slate is " + slate.kind + ". Pick the matching slate first."); }
      else setErr("");
      const fee = d.entries[0].fee;
      setDoc(d); setRes(null); setChosen(new Set());
      if (fee) setCfg((c) => ({ ...c, entryFee: fee }));
    };
    r.readAsText(f); e.target.value = "";
  }
  function run() {
    if (!doc) return;
    setBusy(0.01); setRes(null);
    const opts = { now: Date.now(), sims: Number(cfg.sims) || 20000, fieldSize: Number(cfg.fieldSize) || 10000,
      entryFee: Number(cfg.entryFee) || 0, firstPct: Number(cfg.firstPct) || 20, corr,
      exclude: exclude.map((n) => idx[n]).filter((x) => x != null), include: include.map((n) => idx[n]).filter((x) => x != null) };
    runSim("lateswap", { players, doc, opts }, (f) => setBusy(Math.max(0.01, f))).then((r) => {
      setRes(r); setBusy(0); setPage(0);
      setChosen(new Set(r.entries.filter((e) => e.changed && e.after.roi > e.before.roi).map((e) => e.entryId)));
    }).catch((e) => { setErr(e.message); setBusy(0); });
  }
  function download() {
    const csv = exportEntries(doc, res.entries, chosen, players);
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
    dlRef.current.href = url; dlRef.current.download = "DK_late_swap_" + chosen.size + "_entries.csv"; dlRef.current.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }
  const unlocked = players.filter((p) => !(p.gameStart && now >= Date.parse(p.gameStart)));
  const shown = res ? res.entries.filter((e) => {
    if (onlyChanged && !e.changed) return false;
    const lu = (e.after || e.before || { players: [] }).players.filter((p) => p >= 0);
    return matchesQuick(lu.map((p) => players[p].name), lu.map((p) => players[p].team), search, selTeams);
  }) : [];
  const PER = 25, pages = Math.max(1, Math.ceil(shown.length / PER)), pg = Math.min(page, pages - 1);
  const chosenRes = res ? res.entries.filter((e) => chosen.has(e.entryId)) : [];
  const avgSel = res ? (res.entries.reduce((s, e) => s + (chosen.has(e.entryId) && e.after ? e.after.roi : e.before ? e.before.roi : 0), 0) / Math.max(1, res.entries.filter((e) => e.before).length)) : 0;
  return (
    <div>
      <a ref={dlRef} style={{ display: "none" }} aria-hidden="true">{"dl"}</a>
      <div style={{ ...card, marginBottom: 12 }}>
        <div style={sl}>{"Late Swap"}</div>
        <div style={{ fontSize: 12, color: MU, marginBottom: 10 }}>{"Upload your DraftKings entries after lock. Players whose games have started (🔒) never move; every other slot is re-optimised with the same simulation model, using live scoring for games in progress. Then download and upload the file back to DraftKings."}</div>
        <input ref={fileRef} type="file" accept=".csv,.txt" style={{ display: "none" }} onChange={onFile} />
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
          <button onClick={() => fileRef.current.click()} style={btn(OH)}>{doc ? "✅ " + doc.entries.length + " entries loaded · replace" : "⬆ Upload DraftKings entries CSV"}</button>
          <span style={{ fontSize: 12, color: MU }}>{unlocked.length + " players still swappable on " + (slate ? slate.name : "this slate")}</span>
        </div>
        {err ? <div style={{ color: RE, fontSize: 12, marginBottom: 8 }}>{err}</div> : null}
        <ContestSettings cfg={cfg} setCfg={setCfg} />
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10, alignItems: "center" }}>
          <PlayerPicker label="Never swap in" players={unlocked} value={exclude} onChange={setExclude} />
          <PlayerPicker label="Must swap in" players={unlocked} value={include} onChange={setInclude} />
        </div>
        <button disabled={!doc || busy > 0} onClick={run} style={{ ...btn(GR), marginTop: 12, width: "100%", padding: "11px 0", fontSize: 14, opacity: !doc || busy ? 0.6 : 1 }}>
          {busy ? "Simulating… " + Math.round(busy * 100) + "%" : "Run late swap"}
        </button>
      </div>
      {res ? (
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(170px,1fr))", gap: 10, marginBottom: 12 }}>
            <Stat big={fmt(res.preROI) + "%"} label="Avg ROI before swap" col={roiCol(res.preROI)} />
            <Stat big={fmt(res.postROI) + "%"} label="Avg ROI with all best swaps" col={roiCol(res.postROI)} />
            <Stat big={fmt(avgSel) + "%"} label={"Avg ROI with selected swaps (" + chosen.size + ")"} col={roiCol(avgSel)} />
            <Stat big={res.entries.filter((e) => e.changed).length + " / " + res.entries.length} label="Entries with a better lineup" col={OH} />
          </div>
          <div style={{ ...card, marginBottom: 12 }}>
            <QuickFilters search={search} setSearch={setSearch} teams={teams} selTeams={selTeams} setSelTeams={setSelTeams}
              extra={<label style={{ fontSize: 12, color: MU }}><input type="checkbox" checked={onlyChanged} onChange={(e) => setOnlyChanged(e.target.checked)} />{" only changed"}</label>} />
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button onClick={() => setChosen(new Set(shown.filter((e) => e.changed && e.after.roi > e.before.roi).map((e) => e.entryId).concat(Array.from(chosen))))} style={btn(alpha(GR, 13), GR)}>{"👍 Select all improving (shown)"}</button>
              <button onClick={() => setChosen(new Set())} style={btn(alpha(RE, 7), RE)}>{"Clear selection"}</button>
              <button disabled={!chosen.size} onClick={download} style={{ ...btn(OH), opacity: chosen.size ? 1 : 0.5 }}>{"⬇ Download DK upload file (" + chosen.size + " swaps)"}</button>
            </div>
          </div>
          <Pager pg={pg} pages={pages} setPage={setPage} total={shown.length} />
          {shown.slice(pg * PER, pg * PER + PER).map((e) => <SwapCard key={e.entryId} e={e} players={players} chosen={chosen.has(e.entryId)}
            onToggle={() => { const n = new Set(chosen); n.has(e.entryId) ? n.delete(e.entryId) : n.add(e.entryId); setChosen(n); }} />)}
          {shown.length > 10 ? <Pager pg={pg} pages={pages} setPage={setPage} total={shown.length} /> : null}
        </div>
      ) : null}
    </div>
  );
}

function Stat({ big, label, col }) {
  return <div className="kpi" style={{ textAlign: "center" }}><div className="kpi-value" style={{ fontSize: 22, color: col }}>{big}</div><div style={{ fontSize: 11, color: MU, marginTop: 3 }}>{label}</div></div>;
}
function Pager({ pg, pages, setPage, total }) {
  if (pages <= 1) return null;
  return (
    <div style={{ display: "flex", gap: 6, justifyContent: "center", alignItems: "center", margin: "8px 0" }}>
      <button style={btn(BR, MU)} disabled={pg === 0} onClick={() => setPage(pg - 1)}>{"‹ Prev"}</button>
      <span style={{ fontSize: 12, color: MU }}>{"Page " + (pg + 1) + " of " + pages + " · " + total}</span>
      <button style={btn(BR, MU)} disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)}>{"Next ›"}</button>
    </div>
  );
}
function PlayerPicker({ label, players, value, onChange }) {
  const [q, setQ] = useState("");
  const sugg = q ? players.filter((p) => p.name.toLowerCase().includes(q.toLowerCase()) && !value.includes(p.name)).slice(0, 6) : [];
  return (
    <div style={{ position: "relative" }}>
      <div style={{ fontSize: 11, color: MU, marginBottom: 3 }}>{label}</div>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
        {value.map((n) => <span key={n} style={{ ...pill(OH), cursor: "pointer" }} onClick={() => onChange(value.filter((x) => x !== n))}>{n + " ×"}</span>)}
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="player…" style={{ ...inp, width: 130, padding: "4px 7px" }} />
      </div>
      {sugg.length ? <div style={{ position: "absolute", zIndex: 20, background: CARD, border: "1px solid " + BR, borderRadius: 6, marginTop: 2 }}>
        {sugg.map((p) => <div key={p.name} onClick={() => { onChange(value.concat([p.name])); setQ(""); }} style={{ padding: "5px 9px", cursor: "pointer", fontSize: 12 }}>{p.name}</div>)}
      </div> : null}
    </div>
  );
}
function SwapCard({ e, players, chosen, onToggle }) {
  const before = e.before ? e.before.players : [], after = e.after ? e.after.players : before;
  return (
    <div style={{ ...card, marginBottom: 8, borderColor: chosen ? alpha(GR, 53) : BR }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8, marginBottom: 8 }}>
        <div style={{ fontSize: 12, color: MU }}>{"Entry " + e.entryId + " · " + (e.contest || "")}</div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", fontSize: 13 }}>
          {e.unknown ? <span style={pill(YE)}>{"players not on this slate: pick the matching slate"}</span> : null}
          {e.before ? <span>{"ROI "}<b style={{ color: roiCol(e.before.roi) }}>{fmt(e.before.roi)}%</b>{" → "}<b style={{ color: roiCol(e.after.roi) }}>{fmt(e.after.roi)}%</b></span> : null}
          {e.changed ? <button onClick={onToggle} style={btn(chosen ? alpha(GR, 13) : "transparent", chosen ? GR : MU)}>{chosen ? "👍 Swap selected" : "Select swap"}</button>
            : <span style={pill(MU)}>{e.openSlots ? "already optimal" : "fully locked"}</span>}
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(150px,1fr))", gap: 6 }}>
        {e.slots.map((s, k) => {
          const b = before[k], a = after[k], changed = b !== a;
          const pb = b != null && b >= 0 ? players[b] : null, pa = a != null && a >= 0 ? players[a] : null;
          return (
            <div key={k} style={{ background: changed ? alpha(GR, 15) : SURF2, borderRadius: 6, padding: "6px 8px", fontSize: 12 }}>
              <div style={{ color: MU, fontSize: 10, fontWeight: 700 }}>{s.label}{s.locked ? <LockIcon /> : null}</div>
              {changed && pb ? <div style={{ color: MU, textDecoration: "line-through" }}>{pb.name}</div> : null}
              <div style={{ fontWeight: 700, color: changed ? GR : TX }}>{pa ? pa.name : s.cell || "—"}{pa ? <StarterIcon p={pa} /> : null}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// seat each player in a distinct slot (fewer players than slots is fine);
// specific slots (PG, SG…) are offered before flex slots (G, F, UTIL)
function seatPlayers(ps, masks, sim) {
  const order = masks.map((m, k) => k).sort((a, b) => bits(masks[a]) - bits(masks[b]));
  const slotOf = new Array(ps.length).fill(-1), owner = new Array(masks.length).fill(-1);
  const tryP = (j, seen) => {
    for (const k of order) {
      if (seen[k] || !(sim.code[ps[j]] & masks[k])) continue;
      seen[k] = 1;
      if (owner[k] < 0 || tryP(owner[k], seen)) { owner[k] = j; slotOf[j] = k; return true; }
    }
    return false;
  };
  for (let j = 0; j < ps.length; j++) if (!tryP(j, new Array(masks.length).fill(0))) return null;
  return slotOf;
}
function bits(x) { let c = 0; while (x) { c += x & 1; x >>= 1; } return c; }

// ── Single Lineup Simulator ─────────────────────────────────────────────────
export function SingleLineupTool({ players, slate, corr, runSim, defaults }) {
  const kind = slate ? slate.kind : "classic";
  const slots = slotsFor(kind);
  const [roster, setRoster] = useState(() => slots.map(() => ({ p: null, locked: false })));
  const [q, setQ] = useState(""), [res, setRes] = useState(null), [busy, setBusy] = useState(0), [msg, setMsg] = useState("");
  const [cfg, setCfg] = useState({ fieldSize: defaults.fieldSize || 10000, entryFee: defaults.entryFee || 15, firstPct: defaults.firstPct || 20, sims: 20000 });
  useEffect(() => { setRoster(slotsFor(kind).map(() => ({ p: null, locked: false }))); setRes(null); }, [kind, slate && slate.id]);
  const now = Date.now();
  const sim = useMemo(() => buildSim(players, 0), [players]);   // position codes / teams only
  const used = new Set(roster.map((r) => r.p).filter((p) => p != null));
  const salary = roster.reduce((s, r, k) => s + (r.p != null ? slotSalary(players[r.p], slots[k]) : 0), 0);
  const proj = roster.reduce((s, r, k) => s + (r.p != null ? players[r.p].proj * (slots[k].mult || 1) : 0), 0);
  const full = roster.every((r) => r.p != null);
  const list = players.map((p, i) => i).filter((i) => players[i].proj > 0 && !used.has(i) && (!q || players[i].name.toLowerCase().includes(q.toLowerCase())))
    .sort((a, b) => players[b].proj - players[a].proj);
  function place(i) {
    setRes(null); setMsg("");
    const next = roster.map((r) => ({ ...r }));
    const empty = next.map((r, k) => k).filter((k) => next[k].p == null);
    if (!empty.length) { setMsg("Lineup is full: remove a player first."); return; }
    if (kind === "showdown") { const k = empty[0]; next[k].p = i; setRoster(next); return; }
    // re-seat every unlocked player so the new one fits wherever positions allow
    const keep = next.map((r, k) => ({ r, k })).filter((x) => x.r.p != null && x.r.locked);
    const movable = next.filter((r) => r.p != null && !r.locked).map((r) => r.p).concat([i]);
    const openSlots = slots.filter((_, k) => !keep.some((x) => x.k === k));
    const openK = slots.map((_, k) => k).filter((k) => !keep.some((x) => x.k === k));
    const seat = seatPlayers(movable, openK.map((k) => slots[k].mask), sim);
    if (!seat) { setMsg(players[i].name + " doesn't fit any open slot."); return; }
    const out = slots.map((_, k) => { const kept = keep.find((x) => x.k === k); return kept ? { ...kept.r } : { p: null, locked: false }; });
    movable.forEach((p, j) => { out[openK[seat[j]]] = { p, locked: false }; });
    setRoster(out);
  }
  function remove(k) { if (roster[k].locked) return; setRes(null); setRoster(roster.map((r, j) => (j === k ? { p: null, locked: false } : r))); }
  function toggleLock(k) { if (roster[k].p == null) return; setRoster(roster.map((r, j) => (j === k ? { ...r, locked: !r.locked } : r))); }
  function clear() { setRes(null); setMsg(""); setRoster(roster.map((r) => (r.locked ? r : { p: null, locked: false }))); }
  function complete() {
    setRes(null); setMsg("");
    const fixed = roster.map((r, k) => ({ slot: slots[k], p: r.p, k })).filter((f) => f.p != null);
    const open = slots.filter((_, k) => roster[k].p == null);
    const score = Float64Array.from(players.map((p) => Math.max(0, p.mean != null && !isNaN(p.mean) ? p.mean : p.proj)));   // expected points
    const fill = bestFill(score, sim, players, fixed, open, {});
    if (!fill) { setMsg("No legal way to fill the open slots under the salary cap."); return; }
    let j = 0;
    setRoster(roster.map((r) => (r.p != null ? r : { p: fill[j++], locked: false })));
  }
  function runIt() {
    setBusy(0.01); setRes(null);
    const lineup = roster.map((r, k) => [r.p, slots[k].mult || 1]);
    const opts = { now, kind, corr, sims: Number(cfg.sims) || 20000, fieldSize: Number(cfg.fieldSize) || 10000,
      entryFee: Number(cfg.entryFee) || 0, firstPct: Number(cfg.firstPct) || 20, seed: 4242 };
    runSim("single", { players, lineup, opts }, (f) => setBusy(Math.max(0.01, f)))
      .then((r) => { setRes(r); setBusy(0); }).catch((e) => { setMsg(e.message); setBusy(0); });
  }
  const over = salary > SALARY_CAP;
  return (
    <div>
      <div style={{ ...card, marginBottom: 12 }}>
        <div style={sl}>{"Single Lineup Simulator"}</div>
        <div style={{ fontSize: 12, color: MU }}>{"Hand-build a lineup for " + (slate ? slate.name : "this slate") + " and simulate it against a full contest field. Separate from the 10,000-lineup pool and from late swap."}</div>
      </div>
      <div className="sim-grid">
        <div style={card}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10, fontSize: 13 }}>
            <b>{"Your lineup"}</b>
            <span><span style={{ color: over ? RE : OH, fontWeight: 800 }}>{money(salary)}</span><span style={{ color: MU }}>{" / " + money(SALARY_CAP) + " · " + money(SALARY_CAP - salary) + " left · proj " + fmt(proj)}</span></span>
          </div>
          {roster.map((r, k) => {
            const p = r.p != null ? players[r.p] : null;
            return (
              <div key={k} style={{ display: "flex", alignItems: "center", gap: 8, padding: "9px 10px", marginBottom: 6, borderRadius: 8, background: SURF2, border: "1px solid " + (r.locked ? OH : BR) }}>
                <span style={{ width: 44, fontSize: 11, fontWeight: 800, color: slots[k].mult ? "#a78bfa" : MU }}>{slots[k].label}</span>
                {p ? (
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <b>{p.name}</b><StarterIcon p={p} />{p.gameStart && now >= Date.parse(p.gameStart) ? <LockIcon /> : null}
                    <div style={{ fontSize: 11, color: MU }}>{(p.positions || []).join("/") + " · " + p.team + " · " + money(slotSalary(p, slots[k])) + " · proj " + fmt(p.proj * (slots[k].mult || 1))}</div>
                  </div>
                ) : <div style={{ flex: 1, color: MU, fontSize: 13 }}>{"empty"}</div>}
                {p ? <button title={r.locked ? "Unlock to remove" : "Remove"} disabled={r.locked} onClick={() => remove(k)}
                  style={{ width: 28, height: 28, borderRadius: 6, border: "none", background: r.locked ? BR : RE, color: "#fff", fontWeight: 900, fontSize: 16, cursor: r.locked ? "not-allowed" : "pointer", opacity: r.locked ? 0.4 : 1 }}>{"−"}</button> : null}
                {p ? <button title={r.locked ? "Unlock" : "Lock (kept through Clear)"} onClick={() => toggleLock(k)}
                  style={{ width: 28, height: 28, borderRadius: 6, border: "1px solid " + (r.locked ? OH : BR), background: r.locked ? alpha(OH, 20) : "transparent", cursor: "pointer", fontSize: 13 }}>{r.locked ? "🔒" : "🔓"}</button> : null}
              </div>
            );
          })}
          {msg ? <div style={{ color: YE, fontSize: 12, margin: "6px 0" }}>{msg}</div> : null}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
            <button onClick={complete} disabled={full} style={{ ...btn(OH), opacity: full ? 0.5 : 1 }}>{"✨ Complete lineup"}</button>
            <button onClick={clear} style={btn(alpha(RE, 13), RE)}>{"Clear"}</button>
          </div>
          {full ? (
            <div style={{ marginTop: 14, borderTop: "1px solid " + BR, paddingTop: 12 }}>
              <ContestSettings cfg={cfg} setCfg={setCfg} />
              <button onClick={runIt} disabled={over || busy > 0} style={{ ...btn(GR), marginTop: 10, width: "100%", padding: "11px 0", fontSize: 14, opacity: over || busy ? 0.6 : 1 }}>
                {busy ? "Simulating… " + Math.round(busy * 100) + "%" : over ? "Over the salary cap" : "▶ Run simulation"}
              </button>
            </div>
          ) : null}
          {res ? (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(120px,1fr))", gap: 8, marginTop: 12 }}>
              <Stat big={fmt(res.roi) + "%"} label="ROI" col={roiCol(res.roi)} />
              <Stat big={fmt(res.cash) + "%"} label="Cash rate" col={OH} />
              <Stat big={fmt(res.top1, 2) + "%"} label="Top 1%" col={OH} />
              <Stat big={fmt(res.win, 3) + "%"} label="Win" col={OH} />
              <Stat big={fmt(res.mean)} label={"Avg pts (± " + fmt(res.sd) + ")"} col={TX} />
              <Stat big={"$" + fmt(res.ev, 2)} label="Avg payout" col={TX} />
            </div>
          ) : null}
        </div>
        <div style={{ ...card, display: "flex", flexDirection: "column", maxHeight: 720 }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="🔍 Search players…" style={{ ...inp, width: "100%", marginBottom: 8 }} />
          <div style={{ overflowY: "auto", flex: 1 }}>
            {list.map((i) => {
              const p = players[i], started = p.gameStart && now >= Date.parse(p.gameStart);
              return (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, padding: "7px 4px", borderBottom: "1px solid " + alpha(BR, 38) }}>
                  <div style={{ flex: 1, minWidth: 0, fontSize: 13 }}>
                    <b>{p.name}</b><StarterIcon p={p} />{started ? <LockIcon /> : null}
                    <div style={{ fontSize: 11, color: MU }}>{(p.positions || []).join("/") + " · " + p.team + " · " + money(p.salary) + " · " + fmt(p.proj) + " pts · " + fmt(p.own) + "% own"}</div>
                  </div>
                  <button title="Add" onClick={() => place(i)} style={{ width: 28, height: 28, borderRadius: 6, border: "none", background: GR, color: "#05070d", fontWeight: 900, fontSize: 17, cursor: "pointer" }}>{"+"}</button>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Betting markets ─────────────────────────────────────────────────────────
const STAT_LABEL = { pts: "Points", reb: "Rebounds", ast: "Assists", fg3m: "Threes", blk: "Blocks", stl: "Steals", tov: "Turnovers" };
const amer = (x) => (x == null ? "—" : x > 0 ? "+" + x : String(x));
const spreadTxt = (m) => (m == null ? "—" : (m > 0 ? "-" : "+") + Math.abs(m).toFixed(1));

export function MarketsView({ doc, slate, fmtClock, onRefresh }) {
  const [q, setQ] = useState(""), [stat, setStat] = useState("all"), [sort, setSort] = useState("edge");
  if (!doc) return <div style={card}><div style={{ color: MU }}>{"Loading markets…"}</div></div>;
  if (doc.error) return <div style={card}><div style={{ color: MU }}>{"Betting markets appear once your Mac publishes them."}</div></div>;
  const teamsOnSlate = new Set((slate && slate.games || []).flatMap((g) => [g.home, g.away].map((t) => ({ GS: "GSW", NY: "NYK", SA: "SAS", NO: "NOP", PHO: "PHX", WSH: "WAS", UTAH: "UTA" }[t] || t))));
  const games = (doc.games || []).filter((g) => !teamsOnSlate.size || teamsOnSlate.has(g.home) || teamsOnSlate.has(g.away));
  let props = (doc.props || []).filter((p) => (stat === "all" || p.stat === stat) && (!q || p.player.toLowerCase().includes(q.toLowerCase())));
  if (sort === "edge") props = props.slice().sort((a, b) => Math.abs(b.edge_over) - Math.abs(a.edge_over));
  else props = props.slice().sort((a, b) => a.player.localeCompare(b.player));
  const teams = doc.teams || {};
  return (
    <div>
      <div style={{ ...card, marginBottom: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={sl}>{"Betting markets" + (slate ? " · " + slate.name : "")}</div>
          <button onClick={onRefresh} style={btn(BR, MU)}>{"↻ Refresh"}</button>
        </div>
        <div style={{ fontSize: 12, color: MU }}>
          {"Source: " + doc.source + " · updated " + fmtClock(doc.updated_at) + (doc.credits_left ? " · API credits left: " + doc.credits_left : "")}
          <div>{"Lines are de-vigged (bookmaker margin removed) with Pinnacle weighted most. These same numbers feed the player projections."}</div>
        </div>
      </div>
      {games.length ? (
        <div style={{ ...card, marginBottom: 12 }}>
          <div style={sl}>{"Implied team totals"}</div>
          <BarList label="Implied team totals from the betting lines" format={(v) => fmt(v, 1)}
            max={Math.max(1, ...games.flatMap((g) => (g.home ? [g.home_implied, g.away_implied] : Object.values(g.espn || {}).map((o) => o && o.implied))).filter((x) => typeof x === "number" && isFinite(x)))}
            items={games.flatMap((g) => (g.home
              ? [{ label: g.away, note: "@ " + g.home, value: g.away_implied }, { label: g.home, note: "vs " + g.away, value: g.home_implied }]
              : Object.entries(g.espn || {}).map(([t, o]) => ({ label: t, note: "vs " + (o && o.opp || ""), value: o && o.implied }))))
              .filter((r) => typeof r.value === "number" && isFinite(r.value)).sort((a, b) => b.value - a.value)} />
        </div>) : null}
      {games.map((g, i) => g.home ? (
        <div key={i} style={{ ...card, marginBottom: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
            <b style={{ fontSize: 15 }}>{g.away + " @ " + g.home}</b>
            <span style={{ fontSize: 12, color: MU }}>{fmtClock(g.commence) + (g.sharpest ? " · sharpest: " + g.sharpest : "")}</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(150px,1fr))", gap: 8, marginTop: 8 }}>
            <Stat big={g.home + " " + spreadTxt(g.home_margin)} label={"Fair spread" + (g.open ? " (open " + spreadTxt(g.open.home_margin) + ")" : "")} col={OH} />
            <Stat big={fmt(g.total)} label={"Fair total" + (g.open ? " (open " + fmt(g.open.total) + ")" : "")} col={OH} />
            <Stat big={g.home_win != null ? fmt(g.home_win * 100, 0) + "%" : "—"} label={g.home + " win chance"} col={OH} />
            <Stat big={fmt(g.away_implied) + " / " + fmt(g.home_implied)} label={"Implied team totals " + g.away + " / " + g.home} col={TX} />
          </div>
          <details style={{ marginTop: 8 }}><summary style={{ cursor: "pointer", fontSize: 12, color: MU }}>{"Prices by book"}</summary>
            <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse", marginTop: 6 }}><tbody>
              {Object.entries(g.books || {}).map(([k, b]) => (
                <tr key={k}><td style={{ padding: "3px 6px" }}>{b.title}</td>
                  <td style={{ padding: "3px 6px" }}>{b.spread ? g.home + " " + (b.spread.home > 0 ? "+" : "") + b.spread.home + " (" + amer(b.spread.home_price) + ")" : "—"}</td>
                  <td style={{ padding: "3px 6px" }}>{b.total ? "O/U " + b.total.point + " (" + amer(b.total.over_price) + "/" + amer(b.total.under_price) + ")" : "—"}</td>
                  <td style={{ padding: "3px 6px" }}>{b.ml ? "ML " + amer(b.ml.away) + " / " + amer(b.ml.home) : "—"}</td></tr>))}
            </tbody></table></details>
          {g.moves && g.moves.length > 1 ? <div style={{ fontSize: 11, color: MU, marginTop: 6 }}>{"Today's movement: " + g.moves.slice(-8).map((m) => fmtClock(m.t) + " " + spreadTxt(m.home_margin) + "/" + fmt(m.total)).join(" → ")}</div> : null}
          <TeamRecords teams={teams} codes={[g.away, g.home]} />
        </div>
      ) : (
        <div key={i} style={{ ...card, marginBottom: 10 }}>
          <b>{(g.teams || []).join(" vs ")}</b>
          <div style={{ fontSize: 12, color: MU }}>{"ESPN line: " + Object.entries(g.espn || {}).map(([t, o]) => o ? t + " " + (o.spread > 0 ? "+" : "") + o.spread + " (implied " + fmt(o.implied) + ")" : "").join(" · ") + (g.espn && Object.values(g.espn)[0] ? " · total " + Object.values(g.espn)[0].total : "")}</div>
          <TeamRecords teams={teams} codes={g.teams || []} />
        </div>
      ))}
      <div style={{ ...card, marginBottom: 12 }}>
        <div style={sl}>{"Player props vs. the model"}</div>
        <div style={{ fontSize: 12, color: MU, marginBottom: 8 }}>{"Fair % = market chance of the over after removing the vig. Model % = Sigma's projection before the market blend. Edge = model − market (positive favors the over)."}</div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="🔍 Player…" style={{ ...inp, width: 170 }} />
          {["all", ...Object.keys(STAT_LABEL)].map((k) => <button key={k} onClick={() => setStat(k)} style={{ ...pill(stat === k ? OH : MU), border: "1px solid " + (stat === k ? OH : BR), cursor: "pointer" }}>{k === "all" ? "All" : STAT_LABEL[k]}</button>)}
          <button onClick={() => setSort(sort === "edge" ? "name" : "edge")} style={btn(BR, MU)}>{"Sort: " + (sort === "edge" ? "biggest edge" : "name")}</button>
        </div>
        {props.length ? (
          <div style={{ overflowX: "auto", maxHeight: 520 }}><table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead><tr>{["Player", "Prop", "Line", "Market avg", "Fair over", "Model avg", "Model over", "Edge", "Best over", "Best under"].map((h) => <th key={h} style={{ textAlign: "left", color: MU, fontSize: 10, padding: "5px 6px", borderBottom: "1px solid " + BR, position: "sticky", top: 0, background: CARD }}>{h}</th>)}</tr></thead>
            <tbody>{props.slice(0, 300).map((p, i) => {
              const e = p.edge_over, col = Math.abs(e) >= 0.06 ? (e > 0 ? GR : RE) : MU;
              return (<tr key={i}>
                <td style={{ padding: "4px 6px" }}><b>{p.player}</b><span style={{ color: MU }}>{" " + (p.team || "")}</span></td>
                <td style={{ padding: "4px 6px" }}>{STAT_LABEL[p.stat]}</td>
                <td style={{ padding: "4px 6px" }}>{p.line}</td>
                <td style={{ padding: "4px 6px" }}>{fmt(p.market_mean)}</td>
                <td style={{ padding: "4px 6px" }}>{fmt(p.over_fair * 100, 0) + "%"}</td>
                <td style={{ padding: "4px 6px" }}>{fmt(p.model_mean)}</td>
                <td style={{ padding: "4px 6px" }}>{fmt(p.model_over * 100, 0) + "%"}</td>
                <td style={{ padding: "4px 6px", color: col, fontWeight: 800 }}>{(e > 0 ? "+" : "") + fmt(e * 100, 1) + "%"}</td>
                <td style={{ padding: "4px 6px" }}>{p.best_over ? p.best_over[0] + " " + p.best_over[1] + " " + amer(p.best_over[2]) : "—"}</td>
                <td style={{ padding: "4px 6px" }}>{p.best_under ? p.best_under[0] + " " + p.best_under[1] + " " + amer(p.best_under[2]) : "—"}</td>
              </tr>);
            })}</tbody></table></div>
        ) : <div className="grid-bg" style={{ color: MU, fontSize: 13, padding: 14, border: "1px dashed " + BR, borderRadius: 8, textAlign: "center" }}>{doc.source && doc.source.startsWith("ESPN") ? "No odds key configured — ESPN lines only. Player props need an odds API key on your Mac (see setup)." : "No props posted yet for this slate."}</div>}
      </div>
    </div>
  );
}

function TeamRecords({ teams, codes }) {
  const rows = codes.filter((c) => teams[c]);
  if (!rows.length) return null;
  return (
    <div style={{ marginTop: 8, display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(260px,1fr))", gap: 8 }}>
      {rows.map((c) => {
        const t = teams[c];
        return (
          <div key={c} style={{ background: SURF2, borderRadius: 8, padding: "8px 10px", fontSize: 12, minWidth: 0 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
              <span><b>{c}</b><span style={{ color: MU }}>{" this season vs closing lines"}</span></span>
              <span style={{ width: 110 }} title="Running ATS record over recent games (up = covered)">
                <Sparkline values={(t.recent || []).reduce((acc, r) => acc.concat([(acc.length ? acc[acc.length - 1] : 0) + (r.ats === "W" ? 1 : r.ats === "L" ? -1 : 0)]), [])} width={110} height={24} zero label={c + " running ATS record, recent games"} />
              </span>
            </div>
            <div>{"ATS " + t.ats + " (last 10: " + t.ats_last10 + ") · home " + t.ats_home + " · away " + t.ats_away}</div>
            <div>{"O/U " + t.ou + " (last 10: " + t.ou_last10 + ") · beats spread by " + fmt(t.avg_margin_vs_spread) + " pts/game"}</div>
            <div style={{ color: MU }}>{"Last season: ATS " + t.last_season_ats + " · O/U " + t.last_season_ou}</div>
            <div style={{ color: MU, fontSize: 11, marginTop: 3 }}>{(t.recent || []).slice(-5).map((r) => (r.home ? "vs " : "@") + r.opp + " " + r.score + " " + r.ats + "/" + (r.ou || "-")).join(" · ")}</div>
          </div>
        );
      })}
    </div>
  );
}
