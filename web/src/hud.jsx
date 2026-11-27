// HUD primitives: KPI strip, lock countdown, live dot, source health, theme toggle,
// empty / stale / offline states.
import { useEffect, useState } from "react";
import { ACC, ACC2, GR, RE, YE, MU, alpha, getTheme, setTheme } from "./theme.js";
import { CountUp } from "./charts.jsx";
import { freshness, fmtCompact } from "./datautil.js";

// one shared 1-second clock for every countdown on the page
const subs = new Set();
let timer = null;
function useNow(active) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const f = (t) => setNow(t);
    subs.add(f);
    if (!timer) timer = setInterval(() => { const t = Date.now(); subs.forEach((g) => g(t)); }, 1000);
    return () => { subs.delete(f); if (!subs.size && timer) { clearInterval(timer); timer = null; } };
  }, [active]);
  return now;
}

function ago(iso, now) {
  const t = Date.parse(iso || "");
  if (!isFinite(t)) return "—";
  const m = Math.round((now - t) / 60000);
  return m < 1 ? "just now" : m < 60 ? m + " min ago" : m < 48 * 60 ? Math.round(m / 60) + " hr ago" : Math.round(m / 1440) + " days ago";
}

const FRESH_COL = { live: GR, stale: YE, offline: RE, unknown: MU };
export function LiveDot({ state, title }) {
  const c = FRESH_COL[state] || MU;
  return <span className={"live-dot" + (state === "live" ? " pulse" : "")} title={title || state} aria-label={"data " + (state || "unknown")}
    style={{ background: c, color: c, boxShadow: state === "live" ? "0 0 8px " + c : "none" }} />;
}

export function SourceHealth({ sources }) {
  const entries = Object.entries(sources || {});
  if (!entries.length) return null;
  return (
    <span style={{ display: "inline-flex", gap: 3, alignItems: "center", flexWrap: "wrap" }} aria-label="Data sources">
      {entries.map(([name, s]) => (
        <span key={name} className="live-dot" title={name + ": " + (s && s.ok ? "ok" : "problem") + (s && s.detail ? " · " + s.detail : "")}
          style={{ width: 6, height: 6, background: s && s.ok ? GR : RE }} />
      ))}
    </span>
  );
}

export function LockCountdown({ target, locked, archived }) {
  const t = Date.parse(target || "");
  const now = useNow(!locked && !archived && isFinite(t));
  if (archived) return <span className="num">{"ARCHIVED"}</span>;
  if (locked) return <span className="num" style={{ color: YE }}>{"LOCKED"}</span>;
  if (!isFinite(t)) return <span className="num">{"—"}</span>;
  let s = Math.floor((t - now) / 1000);
  if (s <= 0) return <span className="num" style={{ color: YE }}>{"LOCKED"}</span>;
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const pad = (x) => String(x).padStart(2, "0");
  return <span className="num" style={{ color: d === 0 && h === 0 && m < 15 ? YE : undefined }}>{(d ? d + "d " : "") + pad(h) + ":" + pad(m) + ":" + pad(s)}</span>;
}

function Tile({ label, children, sub, primary, i }) {
  return (
    <div className={"kpi anim-in" + (primary ? " kpi-primary" : "")} style={{ "--i": i || 0 }}>
      <div className="hud-label">{label}</div>
      <div className="kpi-value">{children}</div>
      {sub ? <div className="kpi-sub">{sub}</div> : null}
    </div>
  );
}

export function HudStrip({ meta, slate, playerCount, topWin, poolInfo, approx, owner }) {
  const now = useNow(true);
  const fr = slate && slate.archived ? "unknown" : freshness(meta && meta.updated_at, now);
  const lockTarget = slate ? (slate.start_utc || (slate.games && slate.games[0] && slate.games[0].start_utc)) : (meta && meta.next_lock_utc);
  const sims = poolInfo && poolInfo.sims;
  return (
    <div className="hud-strip glass" role="region" aria-label="Slate status">
      <Tile label="Slate" i={0} sub={slate ? (slate.kind === "showdown" ? "Showdown" : "Classic") + " · " + (slate.game_count || (slate.games || []).length) + " games" : (meta && meta.message) || "Waiting for data"}>
        <span style={{ fontFamily: "var(--font-ui)", fontSize: 16 }}>{slate ? slate.name : "No slate"}</span>
      </Tile>
      <Tile label="Lock" i={1} sub={slate && slate.archived ? "replaying a past slate" : lockTarget ? "first game " + new Date(lockTarget).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "no game scheduled"}>
        <LockCountdown target={lockTarget} locked={!!(slate && slate.contest_locked && !slate.archived)} archived={!!(slate && slate.archived)} />
      </Tile>
      <Tile label="Players" i={2} sub={meta && meta.frozen_players ? meta.frozen_players + " locked in" : "in the pool"}>
        <CountUp value={playerCount || 0} />
      </Tile>
      <Tile label={<span>{"Top win % "}{approx ? <span className="chip" style={{ background: alpha(YE, 14), color: YE, marginLeft: 4 }}>{"APPROX"}</span> : null}{owner ? <span className="chip" style={{ background: alpha("var(--brand)", 16), color: "var(--brand)", marginLeft: 4 }}>{"OWNER"}</span> : null}</span>}
        i={3} primary sub={poolInfo ? (poolInfo.count || 0).toLocaleString() + " lineups · " + (poolInfo.source === "browser" ? "your browser run" : poolInfo.phase === "exact" ? "exact" : "fast build") : "run simulations"}>
        {typeof topWin === "number" && isFinite(topWin) ? <CountUp value={topWin} format={(v) => v.toFixed(3) + "%"} /> : "—"}
      </Tile>
      <Tile label="Field" i={4} sub={sims ? fmtCompact(sims) + " sims · " + fmtCompact(poolInfo.field || 0) + " field" : "no sims yet"}>
        {poolInfo && typeof poolInfo.fieldWin === "number" ? <CountUp value={poolInfo.fieldWin} format={(v) => v.toFixed(1) + "%"} /> : "—"}
      </Tile>
      <Tile label="Data" i={5} sub={<span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>{fr === "offline" ? "Mac updater quiet" : "sources"}<SourceHealth sources={meta && meta.sources} /></span>}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 16 }}>
          <LiveDot state={fr} />{slate && slate.archived ? "archived" : meta ? ago(meta.updated_at, now) : "—"}
        </span>
      </Tile>
    </div>
  );
}

export function ThemeToggle() {
  const [t, setT] = useState(() => getTheme());
  const next = t === "light" ? "dark" : "light";
  return (
    <button className="btn" onClick={() => setT(setTheme(next))} aria-label={"Switch to " + next + " theme"} title={"Switch to " + next + " theme"}>
      <span aria-hidden="true" style={{ display: "inline-block", width: 12, height: 12, borderRadius: "50%", border: "2px solid currentColor", background: t === "light" ? "transparent" : "currentColor" }} />
      {t === "light" ? "Light" : "Dark"}
    </button>
  );
}

export function SectionHeader({ title, sub, right, label }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
      <div style={{ minWidth: 0 }}>
        {label ? <div className="hud-label" style={{ color: ACC }}>{label}</div> : null}
        <div className="panel-title">{title}</div>
        {sub ? <div className="muted" style={{ fontSize: 12 }}>{sub}</div> : null}
      </div>
      {right || null}
    </div>
  );
}

export function SampleBadge({ n, unit, extra, small }) {
  const thin = small != null ? n < small : n < 30;
  return (
    <span className="chip" style={{ background: alpha(thin ? YE : ACC, 14), color: thin ? YE : ACC }} title="Sample size behind these numbers">
      {"n = " + (n == null ? 0 : n.toLocaleString()) + " " + (unit || "") + (extra ? " · " + extra : "") + (thin ? " · small sample" : "")}
    </span>
  );
}

export function EmptyState({ title, text, action, onAction, actions }) {
  return (
    <div className="panel grid-bg" style={{ textAlign: "center", padding: "36px 20px" }} role="status">
      <div className="logo" style={{ fontSize: 40, marginBottom: 8 }}>{"Σ"}</div>
      {title ? <div style={{ fontWeight: 700, marginBottom: 6 }}>{title}</div> : null}
      {text ? <div className="muted" style={{ fontSize: 13, maxWidth: 520, margin: "0 auto" }}>{text}</div> : null}
      {action && onAction ? <button className="btn btn-accent" style={{ marginTop: 14 }} onClick={onAction}>{action}</button> : null}
      {actions && actions.length ? <div style={{ display: "flex", gap: 8, justifyContent: "center", flexWrap: "wrap", marginTop: 14 }}>
        {actions.map((a) => <button key={a.label} className={"btn" + (a.primary ? " btn-accent" : "")} onClick={a.onClick}>{a.label}</button>)}
      </div> : null}
    </div>
  );
}

// the existing "no update from your Mac" warning (> 20 min); archived slates don't show it
export function StaleBanner({ meta, archived, fmtClock, minsAgo }) {
  if (archived || !meta || !meta.updated_at) return null;
  if (Date.now() - Date.parse(meta.updated_at) <= 20 * 60000) return null;
  return (
    <div style={{ fontSize: 12, color: "#fff", background: RE, borderRadius: 6, padding: "6px 8px", marginBottom: 8 }}>
      {"⚠ No update from your Mac since " + fmtClock(meta.updated_at) + " (" + minsAgo(meta.updated_at) + "). Make sure it's on and awake; the updater restarts by itself."}
    </div>
  );
}

// main-area hero for days with no DraftKings slate open
export function SlateOffline({ meta, lastSlateId, onReplay, onGo, err, sport, seasonNote, nbaTools }) {
  const next = meta && meta.next_lock_utc;
  const code = sport || "NBA";
  const fallback = code === "NBA" ? "No NBA slate is open on DraftKings right now."
    : seasonNote || "No DraftKings " + code + " slate in the next 6 days.";
  const showNba = nbaTools !== false;
  return (
    <div className="panel grid-bg anim-in" style={{ padding: "32px 22px", textAlign: "center", borderColor: "var(--line-strong)" }} role="status">
      <div className="hud-label" style={{ color: ACC }}>{"Slate offline"}</div>
      <div className="logo" style={{ fontSize: 56, margin: "8px 0" }}>{"Σ"}</div>
      <div style={{ fontSize: 20, fontWeight: 700 }}>{(meta && meta.message) || fallback}</div>
      <div className="muted" style={{ fontSize: 13, marginTop: 6, maxWidth: 560, marginLeft: "auto", marginRight: "auto" }}>
        {next ? "Next lock " + new Date(next).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) + ". " : ""}
        {"Projections appear automatically once the next slate is posted. Meanwhile, the history tools still work."}
      </div>
      {err ? <div style={{ color: RE, fontSize: 12, marginTop: 10 }}>{err}</div> : null}
      <div style={{ display: "flex", gap: 8, justifyContent: "center", flexWrap: "wrap", marginTop: 18 }}>
        {lastSlateId ? <button className="btn btn-brand" onClick={() => onReplay(lastSlateId)}>{"Replay last slate"}</button> : null}
        <button className="btn btn-accent" onClick={() => onGo("ownership")}>{"Ownership Lab"}</button>
        <button className="btn" onClick={() => onGo("accuracy")}>{"Model accuracy"}</button>
        {showNba ? <button className="btn" onClick={() => onGo("patterns")}>{"Winning patterns"}</button> : null}
        {showNba ? <button className="btn" onClick={() => onGo("markets")}>{"Betting markets"}</button> : null}
        {showNba ? <button className="btn" onClick={() => onGo("starters")}>{"Starting lineups"}</button> : null}
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 14 }}>
        {"Tip: open any past slate with "}<span className="num" style={{ color: ACC2 }}>{"?slate=<id>"}</span>{" in the address bar."}
      </div>
    </div>
  );
}

// ── sport switcher ───────────────────────────────────────────────────────────
// One chip per sport in data/sports.json (DK code + status dot). Hidden when the
// index is absent or lists only NBA, so a single-sport site looks as before.
const STATUS_DOT = { live: "var(--pos)", no_slate: "var(--warn)", off_season: "var(--text-muted)" };
const STATUS_TEXT = { live: "slate open", no_slate: "no slate right now", off_season: "off-season" };
export function SportSwitcher({ sports, current, onPick }) {
  if (!sports || sports.length < 2) return null;
  function onKey(e, i) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const n = sports.length, j = (i + (e.key === "ArrowRight" ? 1 : n - 1)) % n;
    onPick(sports[j].code);
    const el = e.currentTarget.parentNode && e.currentTarget.parentNode.children[j];
    if (el) el.focus();
  }
  return (
    <div className="sport-switch" role="tablist" aria-label="Sport">
      {sports.map((s, i) => {
        const on = s.code === current;
        const tip = s.label + ": " + (STATUS_TEXT[s.status] || s.status) + (s.season_note ? " · " + s.season_note : "");
        return (
          <button key={s.code} role="tab" aria-selected={on} tabIndex={on ? 0 : -1} title={tip} aria-label={tip}
            className={"sport-chip" + (on ? " on" : "") + (s.status === "off_season" ? " dim" : "")}
            onClick={() => onPick(s.code)} onKeyDown={(e) => onKey(e, i)}>
            <span className="sport-dot" style={{ background: STATUS_DOT[s.status] || "var(--text-muted)" }} aria-hidden="true" />
            {s.code === "GOLF" ? "PGA" : s.code}
          </button>
        );
      })}
    </div>
  );
}

// shown next to the sport chip (and in Model Accuracy) while a sport's scoring
// table is not yet confirmed against the DraftKings client
export function ScoringBadge({ verified }) {
  if (verified !== false) return null;
  return <span className="chip" style={{ background: alpha(YE, 14), color: YE, alignSelf: "center" }}
    title="This sport's DraftKings scoring comes from published guides and has not been confirmed in the DraftKings client yet.">{"Scoring unverified"}</span>;
}
