// New data-driven tools: Leverage Radar, Pool Insights, Ownership Lab, Model
// Accuracy, and the expanded projection row (PlayerOutcome).
import { useMemo, useState } from "react";
import { ACC, ACC2, GR, RE, YE, MU, BR, TX, POS_COLORS, alpha } from "./theme.js";
import { Scatter, BarList, Histogram, LineChart, QuantileFan, ChartEmpty, CountUp } from "./charts.jsx";
import { SectionHeader, SampleBadge, EmptyState } from "./hud.jsx";
import { histogram, parseQuantiles, quantileBox, realityCheck, rowsToObjects, validateContestHistory, fmtNum, fmtCompact } from "./datautil.js";
import { QuickFilters, matchesQuick } from "./tools.jsx";

function fin(x) { return typeof x === "number" && isFinite(x); }
function posOf(p) { return (p.positions && p.positions[0]) || p.pos || ""; }
function Kpi({ label, value, sub, primary }) {
  return (
    <div className={"kpi" + (primary ? " kpi-primary" : "")}>
      <div className="hud-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub ? <div className="kpi-sub">{sub}</div> : null}
    </div>
  );
}

// ── Leverage Radar ───────────────────────────────────────────────────────────
export function LeverageRadar({ players }) {
  const [pos, setPos] = useState("ALL"), [search, setSearch] = useState(""), [teamSel, setTeamSel] = useState([]);
  const [sel, setSel] = useState(null);
  const teams = useMemo(() => Array.from(new Set((players || []).map((p) => (p.team || "").toUpperCase()))).filter(Boolean).sort(), [players]);
  const pool = (players || []).filter((p) => p.proj > 0 && (pos === "ALL" || (p.positions || [p.pos]).indexOf(pos) !== -1)
    && matchesQuick([p.name], [p.team], search, teamSel));
  if (!players || !players.length) return <EmptyState title="No slate loaded" text="Pick a slate (or replay an archived one) to see leverage." />;
  const hasOpt = pool.some((p) => p.optPct > 0);
  const pts = pool.map((p) => ({ id: p.name, x: p.own || 0, y: hasOpt ? (p.optPct || 0) : p.proj, r: p.salary, color: POS_COLORS[posOf(p)] || ACC,
    label: p.name + " · own " + fmtNum(p.own) + "% · " + (hasOpt ? "optimal " + fmtNum(p.optPct) + "%" : "proj " + fmtNum(p.proj)) + " · $" + (p.salary || 0).toLocaleString() }));
  const lev = pool.filter((p) => fin(p.lev)).map((p) => ({ id: p.name, label: p.name, note: posOf(p) + " · " + (p.team || ""), value: p.lev,
    color: p.lev >= 0 ? GR : RE }));
  const top = lev.filter((r) => r.value > 0).sort((a, b) => b.value - a.value).slice(0, 10);
  const bottom = lev.filter((r) => r.value < 0).sort((a, b) => a.value - b.value).slice(0, 10);
  const levMax = Math.max(1, ...lev.map((r) => Math.abs(r.value)));
  const fmtLev = (v) => (v > 0 ? "+" : "") + fmtNum(v);
  return (
    <div className="stack">
      <div className="panel anim-in">
        <SectionHeader label="Leverage radar" title={hasOpt ? "Ownership vs. optimal %" : "Ownership vs. projection"}
          sub={hasOpt ? "Optimal % = how often a player is in the simulation-optimal lineups. Above the diagonal the field is under-owning him." : "Optimal % appears after simulations run. Showing projection instead."}
          right={<span className="chip" style={{ background: alpha(ACC, 12), color: ACC }}>{pool.length + " players"}</span>} />
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
          {["ALL", "PG", "SG", "SF", "PF", "C"].map((p) => <button key={p} className="tab" aria-selected={pos === p} onClick={() => setPos(p)}>{p}</button>)}
        </div>
        <QuickFilters search={search} setSearch={setSearch} teams={teams} selTeams={teamSel} setSelTeams={setTeamSel} />
        <Scatter points={pts} diagonal={hasOpt} selected={sel} onSelect={setSel} className="chart-scatter"
          xLabel="Projected ownership %" yLabel={hasOpt ? "Optimal %" : "Projection (pts)"}
          quadrants={["Under-owned vs optimal", "Over-owned"]} label="Leverage scatter: ownership against optimal percentage" />
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 6, fontSize: 11 }} className="muted">
          {Object.keys(POS_COLORS).map((k) => <span key={k}><span className="live-dot" style={{ background: POS_COLORS[k], marginRight: 4 }} />{k}</span>)}
          <span>{"· bubble size = salary · click a player to highlight"}</span>
        </div>
      </div>
      <div className="grid-2">
        <div className="panel anim-in" style={{ "--i": 1 }}>
          <SectionHeader label="Positive leverage" title="Field is under-owning" sub="Optimal % minus projected ownership" />
          <BarList items={top} max={levMax} format={fmtLev} selected={sel} onSelect={setSel} empty={hasOpt ? "No positive leverage on this filter." : "Needs simulations."} />
        </div>
        <div className="panel anim-in" style={{ "--i": 2 }}>
          <SectionHeader label="Negative leverage" title="Field is over-owning" sub="Popular beyond what the sims support" />
          <BarList items={bottom} max={levMax} format={fmtLev} selected={sel} onSelect={setSel} empty={hasOpt ? "No negative leverage on this filter." : "Needs simulations."} />
        </div>
      </div>
    </div>
  );
}

// ── Pool Insights (top of the Lineups tab) ───────────────────────────────────
const COVER_N = ["1", "5", "20", "50", "150", "500"];
export function PoolInsights({ lineups, likedIds, poolInfo }) {
  const live = (lineups || []).filter((l) => l.live);
  const stats = useMemo(() => {
    if (!live.length) return null;
    const win = live.map((l) => l.pWin), own = live.map((l) => l.sumOwn);
    return { win: histogram(win, { bins: 40, min: 0 }), own: histogram(own, { bins: 40 }), n: live.length };
  }, [lineups]);
  if (!stats) return null;
  const liked = live.filter((l) => likedIds && likedIds.has(l.id));
  const marks = liked.slice(0, 150).map((l) => l.pWin);
  const cover = poolInfo && poolInfo.cover;
  const coverPts = cover ? COVER_N.filter((k) => fin(cover[k])).map((k) => [Number(k), cover[k]]) : [];
  return (
    <div className="panel anim-in" style={{ marginBottom: 12 }}>
      <SectionHeader label="Pool insights" title={stats.n.toLocaleString() + " simulated lineups"}
        sub="How the pool's win chances and ownership are spread, and how much entering more lineups helps."
        right={<div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {poolInfo && poolInfo.source === "browser" ? <span className="chip" style={{ background: alpha(YE, 14), color: YE }} title="Built in this browser: greedy lineups and fewer simulations">{"APPROX"}</span>
            : poolInfo ? <span className="chip" style={{ background: alpha(poolInfo.phase === "exact" ? GR : YE, 14), color: poolInfo.phase === "exact" ? GR : YE }}>{poolInfo.phase === "exact" ? "EXACT" : "FAST BUILD"}</span> : null}
          {poolInfo && poolInfo.generated_at ? <span className="chip num" style={{ background: alpha(ACC, 10), color: ACC }}>{"built " + new Date(poolInfo.generated_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span> : null}
          {poolInfo && poolInfo.field ? <span className="chip num" style={{ background: alpha(ACC2, 12), color: ACC2 }}>{"field " + fmtCompact(poolInfo.field)}</span> : null}
          {poolInfo && poolInfo.sims ? <span className="chip num" style={{ background: alpha(ACC2, 12), color: ACC2 }}>{fmtCompact(poolInfo.sims) + " sims"}</span> : null}
        </div>} />
      <div className={coverPts.length ? "grid-3" : "grid-2"}>
        <div>
          <div className="hud-label">{"Win % distribution"}</div>
          <Histogram counts={stats.win.counts} edges={stats.win.edges} marks={marks} height={150} xLabel="win % per lineup" fmt={(v) => v.toFixed(3)}
            label="Histogram of lineup win percentages" />
          <div className="muted" style={{ fontSize: 11 }}>{liked.length ? "Violet lines: your " + liked.length + " liked lineups" + (liked.length > 150 ? " (first 150)" : "") : "Like lineups to mark them here."}</div>
        </div>
        <div>
          <div className="hud-label">{"Total ownership"}</div>
          <Histogram counts={stats.own.counts} edges={stats.own.edges} height={150} xLabel="sum of projected own %" color={ACC2} label="Histogram of lineup total ownership" />
          <div className="muted" style={{ fontSize: 11 }}>{"Lower = more contrarian."}</div>
        </div>
        {coverPts.length ? <div>
          <div className="hud-label">{"Portfolio coverage"}</div>
          <LineChart series={[{ name: "best portfolio", points: coverPts, color: GR }]} xLog height={150} xLabel="lineups entered"
            yFmt={(v) => v.toFixed(0) + "%"} label="Share of simulated contests won by at least one lineup" empty="Coverage appears after an exact run." />
          <div className="muted" style={{ fontSize: 11 }}>{"% of simulated contests won by at least one of the lineups" + (poolInfo && poolInfo.coverSims ? " (" + fmtCompact(poolInfo.coverSims) + " sims)" : "")}</div>
        </div> : null}
      </div>
    </div>
  );
}

// ── Ownership Lab ────────────────────────────────────────────────────────────
const SIZE_LABEL = { "single-entry": "Single entry", small: "Small (<1k)", mid: "Mid (1k–20k)", large: "Large (20k+)" };
function avgOf(list, f) { const v = list.map(f).filter(fin); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; }

export function OwnershipLab({ doc, players }) {
  const [q, setQ] = useState("");
  const check = useMemo(() => (doc && !doc.error ? validateContestHistory(doc) : null), [doc]);
  const rows = useMemo(() => (check && check.ok ? rowsToObjects(doc.players.columns, doc.players.rows) : []), [doc, check]);
  if (!doc) return <div className="panel"><div className="skeleton" style={{ height: 160 }} /></div>;
  if (doc.error && doc.kind === "format") return <EmptyState title="Contest history file is in an unexpected format" text="The published contest_history.json couldn't be read as JSON. It is rebuilt on the Mac's next contest import." />;
  if (doc.error) return <EmptyState title="Contest history isn't published yet" text="It appears after the Mac imports your DraftKings contest exports. Only aggregate numbers are published: no usernames, entry IDs or lineups." />;
  if (!check.ok) return <EmptyState title="Contest history file is in an unexpected format" text={check.reason} />;
  const contests = doc.contests || [];
  const byKey = {};
  contests.forEach((c) => { byKey[c.key] = c; });
  const cal = doc.calibration || {};
  const samples = doc.samples || {};
  const bySize = cal.by_size || {}, single = bySize["single-entry"] || {}, multi = bySize.multi || {};
  const matched = rows.filter((r) => fin(r.proj_own) && fin(r.own));
  const pts = matched.map((r, i) => ({ id: r.contest + "|" + r.name, x: r.proj_own, y: r.own, r: r.salary || 0,
    color: r.own > r.proj_own ? GR : RE, label: r.name + " (" + ((byKey[r.contest] || {}).date || r.contest) + "): projected " + fmtNum(r.proj_own) + "% → actual " + fmtNum(r.own) + "%" }));
  const bucketItems = (cal.buckets || []).map((b) => ({ label: b.label, note: "n=" + b.n, value: b.mean_actual, compare: b.mean_proj }));
  const salItems = (cal.by_salary || []).map((b) => ({ label: b.label, note: "n=" + b.n, value: b.mean_actual, compare: b.mean_proj }));
  const metrics = [
    ["Avg total ownership", (c) => c.avg_total_own, (v) => fmtNum(v) + "%"],
    ["Avg players under 10% owned", (c) => c.avg_low_owned, (v) => fmtNum(v, 2)],
    ["Lineups with 2+ under 10%", (c) => c.pct_2plus_low, (v) => fmtNum(v, 0) + "%"],
    ["Avg salary used", (c) => c.avg_salary, (v) => (fin(v) ? "$" + Math.round(v).toLocaleString() : "—")],
  ];
  const needle = q.trim().toLowerCase();
  const found = needle.length >= 2 ? rows.filter((r) => String(r.name).toLowerCase().indexOf(needle) !== -1).slice(0, 60) : [];
  const reality = (players || []).length && contests.length >= 3
    ? players.filter((p) => p.own > 0).slice().sort((a, b) => b.own - a.own).slice(0, 15).map((p) => ({ p, r: realityCheck(p.own, cal.buckets) }))
    : [];
  return (
    <div className="stack">
      <div className="panel anim-in">
        <SectionHeader label="Ownership lab" title="Model ownership vs. what DraftKings players actually drafted"
          sub={doc.note} right={<SampleBadge n={samples.contests || contests.length} unit="contests" small={20} extra={(samples.player_rows || 0).toLocaleString() + " player results"} />} />
        <div className="hud-strip" style={{ margin: 0, padding: 0 }}>
          <Kpi label="Contests" value={<CountUp value={contests.length} />} sub={(samples.nights || 0) + " slate nights"} />
          <Kpi label="Matched pairs" value={<CountUp value={cal.n || 0} />} sub={(cal.contests_matched || 0) + " contests with a snapshot"} />
          <Kpi label="Ownership MAE" primary value={fin(cal.mae) ? <CountUp value={cal.mae} format={(v) => v.toFixed(2)} /> : "—"} sub="% points, per player" />
          <Kpi label="Bias" value={fin(cal.bias) ? (cal.bias > 0 ? "+" : "") + cal.bias.toFixed(2) : "—"} sub="projected − actual" />
          <Kpi label="Single entry MAE" value={fin(single.mae) ? single.mae.toFixed(2) : "—"} sub={"n=" + (single.n || 0)} />
          <Kpi label="Multi-entry MAE" value={fin(multi.mae) ? multi.mae.toFixed(2) : "—"} sub={"n=" + (multi.n || 0)} />
        </div>
      </div>

      <div className="panel anim-in" style={{ "--i": 1 }}>
        <SectionHeader label="Contests" title="Score and field profile per contest" sub="Contest IDs are withheld and entry counts rounded." />
        {contests.length ? <div className="grid-auto">
          {contests.slice().reverse().map((c) => {
            const h = c.scores && c.scores.hist;
            const edges = h ? h.counts.map((_, i) => h.start + i * h.step).concat([h.start + h.counts.length * h.step]) : [];
            return (
              <div key={c.key} className="kpi" style={{ padding: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 6, alignItems: "center" }}>
                  <b className="num">{c.date || "—"}</b>
                  <span className="chip" style={{ background: alpha(c.size_bucket === "large" ? ACC : c.size_bucket === "single-entry" ? ACC2 : YE, 14), color: c.size_bucket === "large" ? ACC : c.size_bucket === "single-entry" ? ACC2 : YE }}>{SIZE_LABEL[c.size_bucket] || c.size_bucket}</span>
                </div>
                <div className="muted" style={{ fontSize: 11, margin: "2px 0 6px" }}>{"≈" + (c.entries_approx || 0).toLocaleString() + " entries · max " + c.max_entries + " per user · " + c.kind}</div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 6, fontSize: 11 }}>
                  <div><div className="muted">{"Win"}</div><b className="num" style={{ color: ACC }}>{fmtNum(c.scores && c.scores.win, 2)}</b></div>
                  <div><div className="muted">{"Top 1%"}</div><b className="num">{fmtNum(c.scores && c.scores.top1pct, 2)}</b></div>
                  <div><div className="muted">{"Median"}</div><b className="num">{fmtNum(c.scores && c.scores.median, 2)}</b></div>
                </div>
                {h ? <Histogram counts={h.counts} edges={edges} height={90} marks={[c.scores.top1pct]} label={"Score distribution for the contest on " + c.date} /> : null}
              </div>
            );
          })}
        </div> : <ChartEmpty text="No NBA contests yet." />}
      </div>

      <div className="grid-2">
        <div className="panel anim-in" style={{ "--i": 2 }}>
          <SectionHeader label="Model vs reality" title="Projected vs actual ownership" sub="Each dot is one player in one contest. Above the line = drafted more than projected." />
          <Scatter points={pts} diagonal xLabel="Projected own %" yLabel="Actual own %" className="chart-scatter" quadrants={["Drafted more than projected", "Drafted less"]}
            label="Scatter of projected against actual ownership" />
        </div>
        <div className="panel anim-in" style={{ "--i": 3 }}>
          <SectionHeader label="Calibration" title="By projected-ownership bucket" sub="Mean actual vs mean projected own %" />
          <BarList items={bucketItems} valueLabel="actual" compareLabel="projected" format={(v) => fmtNum(v) + "%"} />
          <div className="hud-label" style={{ marginTop: 12, marginBottom: 6 }}>{"By salary tier"}</div>
          <BarList items={salItems} valueLabel="actual" compareLabel="projected" format={(v) => fmtNum(v) + "%"} />
        </div>
      </div>

      <div className="panel anim-in" style={{ "--i": 4 }}>
        <SectionHeader label="What the top 1% did" title="Top 1% of each contest vs the whole field" sub="Averaged across contests" />
        <div className="grid-2">
          {metrics.map(([lbl, f, fmt]) => {
            const t = avgOf(contests, (c) => c.top1pct && f(c.top1pct)), fl = avgOf(contests, (c) => c.field && f(c.field));
            return <div key={lbl}><div className="hud-label" style={{ marginBottom: 4 }}>{lbl}</div>
              <BarList items={[{ label: "Top 1% / field", value: t, compare: fl }]} format={fmt} valueLabel="top 1%" compareLabel="field" empty="Needs snapshot salaries." /></div>;
          })}
        </div>
      </div>

      <div className="panel anim-in" style={{ "--i": 5 }}>
        <SectionHeader label="Player lookup" title="A player's ownership in each contest" />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search a player (e.g. Quickley)…" aria-label="Search player"
          style={{ width: "100%", maxWidth: 360, background: "var(--surface-2)", border: "1px solid " + BR, borderRadius: 8, padding: "8px 10px", fontSize: 13, color: TX }} />
        {needle.length >= 2 ? (found.length ? <div className="scroll-x" style={{ marginTop: 10 }}><table className="data-table">
          <thead><tr>{["Date", "Player", "Actual own", "By slot", "Projected own", "Proj", "Salary", "FPTS"].map((h) => <th key={h} className="hud-label" style={{ textAlign: "left", padding: "6px 9px", borderBottom: "1px solid " + BR }}>{h}</th>)}</tr></thead>
          <tbody>{found.map((r, i) => (
            <tr key={i}>
              <td className="num">{(byKey[r.contest] || {}).date || "—"}</td>
              <td><b>{r.name}</b></td>
              <td className="num" style={{ color: ACC }}>{fmtNum(r.own, 2) + "%"}</td>
              <td className="num muted">{Object.entries(r.own_by_slot || {}).map(([s, v]) => s + " " + fmtNum(v, 1)).join(" · ")}</td>
              <td className="num">{fin(r.proj_own) ? fmtNum(r.proj_own, 1) + "%" : "—"}</td>
              <td className="num">{fmtNum(r.proj)}</td>
              <td className="num">{fin(r.salary) ? "$" + r.salary.toLocaleString() : "—"}</td>
              <td className="num">{fmtNum(r.fpts, 2)}</td>
            </tr>))}</tbody></table></div> : <div className="muted" style={{ marginTop: 10, fontSize: 13 }}>{"No player matches."}</div>) : null}
      </div>

      {reality.length ? <div className="panel anim-in" style={{ "--i": 6 }}>
        <SectionHeader label="Reality check" title="Tonight's most popular players, history-adjusted"
          sub="Model own scaled by how that ownership range actually came in at DraftKings. Hidden for buckets with under 30 observations." />
        <div className="scroll-x"><table className="data-table">
          <thead><tr>{["Player", "Model own", "History-adjusted", "Bucket"].map((h) => <th key={h} className="hud-label" style={{ textAlign: "left", padding: "6px 9px", borderBottom: "1px solid " + BR }}>{h}</th>)}</tr></thead>
          <tbody>{reality.map(({ p, r }) => (
            <tr key={p.name}>
              <td><b>{p.name}</b><span className="muted">{" " + (p.team || "")}</span></td>
              <td className="num">{fmtNum(p.own) + "%"}</td>
              <td className="num" style={{ color: r ? (r.adj > p.own ? GR : RE) : MU }}>{r ? fmtNum(r.adj) + "%" : "—"}</td>
              <td className="muted" style={{ fontSize: 11 }}>{r ? r.bucket + " · n=" + r.n : "thin sample"}</td>
            </tr>))}</tbody></table></div>
      </div> : null}
    </div>
  );
}

// ── Model Accuracy ───────────────────────────────────────────────────────────
export function ModelAccuracy({ audit, meta }) {
  if (!audit) return <div className="panel"><div className="skeleton" style={{ height: 160 }} /></div>;
  if (audit.error || !audit.raw_model) return <EmptyState title="Accuracy report not available yet." text="The Mac publishes a backtest of the projection model (audit.json) after its nightly run." />;
  const rm = audit.raw_model || {}, dk = rm.dk_points || {}, mn = rm.minutes || {};
  const sb = (audit.distribution && audit.distribution.held_out && audit.distribution.held_out.share_below) || {};
  const calPts = Object.keys(sb).map((k) => [Number(k) * 100, Number(sb[k]) * 100]).filter((p) => fin(p[0]) && fin(p[1])).sort((a, b) => a[0] - b[0]);
  const boom = (rm.boom_calibration || []).map((b) => ({ label: b.predicted, note: "n=" + (b.n || 0).toLocaleString(), value: b.actual, compare: b.mean_pred }));
  const bust = (rm.bust_calibration || []).map((b) => ({ label: b.predicted, note: "n=" + (b.n || 0).toLocaleString(), value: b.actual, compare: b.mean_pred }));
  const bt = meta && meta.factors && meta.factors.backtest;
  const corr = meta && meta.correlation;
  return (
    <div className="stack">
      <div className="panel anim-in">
        <SectionHeader label="Model accuracy" title="How the projections have performed" sub={"Backtest through " + (audit.date || "—") + " on games the model had not seen."}
          right={<SampleBadge n={audit.player_games || rm.player_games || 0} unit="player-games" small={1000} extra={(audit.nights || rm.nights || 0) + " nights"} />} />
        <div className="hud-strip" style={{ margin: 0, padding: 0 }}>
          <Kpi label="DK points MAE" primary value={fin(dk.mae) ? <CountUp value={dk.mae} format={(v) => v.toFixed(2)} /> : "—"} sub="avg miss, fantasy pts" />
          <Kpi label="Bias" value={fin(dk.bias) ? (dk.bias > 0 ? "+" : "") + dk.bias.toFixed(2) : "—"} sub="projected − actual" />
          <Kpi label="Correlation" value={fin(dk.corr) ? <CountUp value={dk.corr} format={(v) => v.toFixed(3)} /> : "—"} sub="projection vs actual" />
          <Kpi label="Minutes MAE" value={fin(mn.mae) ? mn.mae.toFixed(2) : "—"} sub={fin(mn.corr) ? "corr " + mn.corr.toFixed(3) : ""} />
          <Kpi label="Nights" value={<CountUp value={audit.nights || rm.nights || 0} />} sub="in the backtest" />
          <Kpi label="Player-games" value={<CountUp value={audit.player_games || rm.player_games || 0} format={(v) => Math.round(v).toLocaleString()} />} sub="scored" />
        </div>
      </div>
      <div className="grid-2">
        <div className="panel anim-in" style={{ "--i": 1 }}>
          <SectionHeader label="Calibration" title="Outcome curve calibration" sub="Share of real results that fell below each predicted percentile. On the dashed line = perfectly calibrated." />
          <LineChart series={[{ name: "actual share below", points: calPts, color: ACC }]} diagonal xLabel="predicted percentile" yLabel="actual %"
            xFmt={(v) => v.toFixed(0)} yFmt={(v) => v.toFixed(0)} height={240} label="Calibration of the predicted outcome percentiles" empty="No held-out calibration yet." />
        </div>
        <div className="panel anim-in" style={{ "--i": 2 }}>
          <SectionHeader label="Boom & bust" title="Predicted vs actual rates" sub="Boom = beat the boom line; bust = fell short of value." />
          <div className="hud-label" style={{ marginBottom: 6 }}>{"Boom"}</div>
          <BarList items={boom} valueLabel="actual" compareLabel="predicted" format={(v) => fmtNum(v) + "%"} color={GR} />
          <div className="hud-label" style={{ margin: "12px 0 6px" }}>{"Bust"}</div>
          <BarList items={bust} valueLabel="actual" compareLabel="predicted" format={(v) => fmtNum(v) + "%"} color={RE} />
        </div>
      </div>
      <div className="panel anim-in" style={{ "--i": 3 }}>
        <SectionHeader label="Factors" title="Situational factor backtest" sub="Mean absolute error on held-out games, with and without the situational factors." />
        {bt ? <BarList items={[{ label: "Baseline", value: bt.mae_baseline, color: MU }, { label: "With factors", value: bt.mae_with_factors, color: ACC }]}
          max={Math.max(bt.mae_baseline || 0, bt.mae_with_factors || 0) * 1.1} format={(v) => fmtNum(v, 3)} />
          : <ChartEmpty text="No factor backtest published yet." height={60} />}
        {bt ? <div className="muted" style={{ fontSize: 11 }}>{(bt.test_games || 0).toLocaleString() + " test games"}</div> : null}
        {corr ? <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>{"Score correlation used in sims: teammates " + fmtNum(corr.team, 3) + " · opponents " + fmtNum(corr.opp, 3) + (corr.team_games ? " (" + corr.team_games.toLocaleString() + " team-games)" : "")}</div> : null}
      </div>
    </div>
  );
}

// ── PlayerOutcome: expanded row in the projections table ─────────────────────
export function PlayerOutcome({ p }) {
  const q = parseQuantiles(p && p.q);
  const box = quantileBox(q);
  const tiles = [
    ["Floor", fmtNum(p.floor)], ["Median", box ? fmtNum(box.p50) : fmtNum(p.proj)], ["Ceiling", fmtNum(p.ceiling)],
    ["Boom", p.boom > 0 ? fmtNum(p.boom) + "%" : "—"], ["Bust", p.bust > 0 ? fmtNum(p.bust) + "%" : "—"],
    ["Own / optimal", fmtNum(p.own) + "% / " + (p.optPct > 0 ? fmtNum(p.optPct) + "%" : "—")],
  ];
  return (
    <div style={{ padding: "8px 4px 12px" }}>
      <div className="grid-2" style={{ alignItems: "center" }}>
        <div>
          <div className="hud-label">{"Outcome distribution · " + p.name}</div>
          <QuantileFan q={q} label={"Simulated fantasy points for " + p.name} />
          {box ? <div className="muted num" style={{ fontSize: 11 }}>{"80% of outcomes between " + box.p10.toFixed(1) + " and " + box.p90.toFixed(1) + " pts"}</div> : null}
        </div>
        <div className="grid-3">
          {tiles.map(([l, v]) => <div key={l} className="kpi"><div className="hud-label">{l}</div><div className="kpi-value" style={{ fontSize: 18 }}>{v}</div></div>)}
        </div>
      </div>
    </div>
  );
}
