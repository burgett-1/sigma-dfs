// Runs: per-visitor browser simulations (approximate, private to this browser)
// and the owner-only signed "Run on my Mac" setup panel.
import { useEffect, useRef, useState } from "react";
import { ACC, ACC2, GR, RE, YE, MU, BR, TX, OH, alpha } from "./theme.js";
import { SIZES } from "./poolgen.js";
import { parseSetupCode, saveOwner, clearOwner, maskTopic } from "./owner.js";

// One dedicated worker per run, so Cancel can terminate() it outright. The shared
// useSimWorker (Late Swap, Lineup Simulator) is not touched.
export function useBrowserRun() {
  const wref = useRef(null), done = useRef(null), tick = useRef(0);
  const [state, setState] = useState({ phase: "idle", progress: 0, error: "" });
  useEffect(() => () => { if (wref.current) { wref.current.terminate(); wref.current = null; } }, []);
  function stop() {
    if (wref.current) { wref.current.terminate(); wref.current = null; }
  }
  function cancel() {
    const wasRunning = !!wref.current;
    stop();
    if (done.current) { done.current(null); done.current = null; }
    // "Cancelled." only when a run was actually stopped (slate changes also call cancel)
    setState(wasRunning ? { phase: "cancelled", progress: 0, error: "" } : { phase: "idle", progress: 0, error: "" });
  }
  function start(players, opts) {
    return new Promise((resolve) => {
      stop();
      if (done.current) { done.current(null); done.current = null; }
      if (typeof Worker === "undefined") {
        setState({ phase: "error", progress: 0, error: "Your browser can't run simulations in the background." });
        resolve(null);
        return;
      }
      let w;
      try { w = new Worker(new URL("./simWorker.js", import.meta.url), { type: "module" }); } catch (e) {
        setState({ phase: "error", progress: 0, error: "Your browser can't run simulations in the background." });
        resolve(null);
        return;
      }
      wref.current = w;
      done.current = resolve;
      setState({ phase: "running", progress: 0, error: "" });
      const finish = (value, st) => {
        if (wref.current === w) { w.terminate(); wref.current = null; }
        setState(st);
        if (done.current === resolve) { done.current = null; resolve(value); }
      };
      w.onmessage = (ev) => {
        const { progress, result, error } = ev.data || {};
        if (progress != null) {
          const t = Date.now();
          if (t - tick.current >= 100) { tick.current = t; setState((s) => (s.phase === "running" ? { ...s, progress } : s)); }
          return;
        }
        if (error) finish(null, { phase: "error", progress: 0, error: String(error) });
        else finish(result, { phase: "done", progress: 1, error: "" });
      };
      w.onerror = (e) => { finish(null, { phase: "error", progress: 0, error: (e && e.message) || "The simulation stopped unexpectedly." }); };
      w.postMessage({ id: 1, type: "pool", payload: { players, opts } });
    });
  }
  return { start, cancel, state };
}

function defaultSize() {
  try {
    if ((navigator.hardwareConcurrency || 8) <= 4 || window.innerWidth < 700) return "quick";
  } catch (e) { /* no navigator */ }
  return "standard";
}
const SIZE_LABEL = { quick: "Quick", standard: "Standard", bigger: "Bigger" };

export function BrowserRunPanel({ run, players, slate, corr, rules, onDone, lastRun, spec }) {
  const [size, setSize] = useState(defaultSize);
  const st = run.state;
  const busy = st.phase === "running";
  const can = !!slate && players && players.length >= (spec ? spec.size : 8);
  const sz = SIZES[size];
  function go() {
    const opts = { kind: slate.kind === "showdown" ? "showdown" : "classic", corr, rules, lineups: sz.lineups, sims: sz.sims,
      fieldSample: sz.fieldSample, fieldSize: 10000, seed: (Date.now() % 2147483647) >>> 0, slateId: slate.id };
    if (spec) opts.spec = spec;              // sport rules (sports.js specFor); NBA passes none
    run.start(players, opts).then((doc) => { if (doc) onDone(doc); });
  }
  return (
    <div>
      <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }} role="group" aria-label="Browser run size">
        {Object.keys(SIZES).map((k) => (
          <button key={k} className="tab" aria-selected={size === k} disabled={busy} onClick={() => setSize(k)} style={{ flex: 1, padding: "6px 8px" }}
            title={SIZES[k].lineups.toLocaleString() + " lineups · " + SIZES[k].sims.toLocaleString() + " simulations"}>{SIZE_LABEL[k]}</button>
        ))}
      </div>
      {busy ? (
        <div>
          <div style={{ height: 8, background: alpha(BR, 100), borderRadius: 4, overflow: "hidden" }} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(st.progress * 100)}>
            <div className="bar-fill" style={{ width: Math.round(st.progress * 100) + "%", height: "100%", background: "linear-gradient(90deg," + ACC + "," + ACC2 + ")" }} />
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 6, fontSize: 12 }}>
            <span className="num" style={{ color: ACC }}>{(st.progress < 0.4 ? "Building lineups… " : "Simulating contests… ") + Math.round(st.progress * 100) + "%"}</span>
            <button className="btn" onClick={run.cancel}>{"Cancel"}</button>
          </div>
        </div>
      ) : (
        <button onClick={go} disabled={!can} className="btn btn-accent" style={{ width: "100%", padding: "11px 0", fontSize: 14 }}>{"▶ Run in my browser"}</button>
      )}
      <div style={{ fontSize: 11, color: MU, marginTop: 6 }}>
        {"Approximate: built in your browser with a greedy optimizer and about " + sz.sims.toLocaleString() + " simulations (" + sz.lineups.toLocaleString()
          + " lineups). The shared pool uses exactly-solved lineups and 1,000,000 contests. Your results stay in this browser and aren't shared."}
      </div>
      {st.phase === "error" ? <div style={{ fontSize: 12, color: RE, marginTop: 6 }}>{st.error}</div> : null}
      {st.phase === "cancelled" ? <div style={{ fontSize: 12, color: MU, marginTop: 6 }}>{"Cancelled."}</div> : null}
      {lastRun ? <div style={{ fontSize: 11, color: MU, marginTop: 6 }}>
        <span className="chip" style={{ background: alpha(YE, 14), color: YE, marginRight: 6 }}>{"APPROX"}</span>
        {lastRun.lineups.length.toLocaleString() + " lineups · seed " + lastRun.seed + ". Results are lost on reload — export to CSV to keep them."}
      </div> : null}
    </div>
  );
}

export function RunSourceSwitch({ source, onChange, hasShared, hasBrowser }) {
  if (!hasBrowser) return null;
  const opt = (k, label, enabled) => (
    <button key={k} role="tab" className="tab" aria-selected={source === k} disabled={!enabled} onClick={() => onChange(k)}>{label}</button>
  );
  return (
    <div className="tabbar" role="tablist" aria-label="Lineup pool source" style={{ marginBottom: 10 }}>
      {opt("shared", "Shared pool (Mac · exact)", hasShared)}
      {opt("browser", "My browser run (approx)", hasBrowser)}
    </div>
  );
}

export function OwnerChip() {
  return <span className="chip" style={{ background: alpha(OH, 16), color: OH }} title="Owner mode is on in this browser">{"OWNER"}</span>;
}

// Shown only for #owner or when owner mode is already saved in this browser.
export function OwnerPanel({ owner, onChange, macAuth }) {
  const [code, setCode] = useState(""), [err, setErr] = useState("");
  function save() {
    const r = parseSetupCode(code);
    if (!r.ok) { setErr(r.error); return; }
    if (!saveOwner(r.cfg)) { setErr("This browser won't store it (private mode or storage blocked)."); return; }
    setCode(""); setErr("");
    try { if (window.location.hash === "#owner") window.history.replaceState(null, "", window.location.pathname + window.location.search); } catch (e) { /* no history */ }
    onChange(r.cfg);
  }
  function forget() { clearOwner(); setCode(""); setErr(""); onChange(null); }
  return (
    <div className="glass" style={{ padding: 16, minWidth: 0 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: OH, textTransform: "uppercase", letterSpacing: ".12em", marginBottom: 10 }}>{"Owner"}</div>
      {owner ? (
        <div style={{ fontSize: 12 }}>
          <div style={{ color: GR, fontWeight: 700 }}>{"Owner mode on · Mac runs signed (hmac-v1)"}</div>
          <div className="num" style={{ color: MU, marginTop: 2 }}>{maskTopic(owner.topic)}</div>
          {macAuth !== "hmac-v1" ? <div style={{ color: YE, marginTop: 4 }}>{"Mac hasn't advertised signed runs yet (update and restart the updater)."}</div> : null}
          <button className="btn" style={{ marginTop: 8 }} onClick={forget}>{"Forget on this browser"}</button>
        </div>
      ) : (
        <div>
          <div style={{ fontSize: 12, color: MU, marginBottom: 6 }}>{"Paste the setup code printed by "}<span className="num" style={{ color: TX }}>{"python -m pipeline.owner_setup"}</span>{" on your Mac."}</div>
          <input type="password" autoComplete="off" spellCheck={false} value={code} onChange={(e) => setCode(e.target.value)} placeholder="sigma-owner-v1:…" aria-label="Owner setup code"
            style={{ width: "100%", background: "var(--surface-2)", border: "1px solid " + BR, borderRadius: 8, padding: "8px 10px", fontSize: 13, color: TX }} />
          {err ? <div style={{ color: RE, fontSize: 12, marginTop: 6 }}>{err}</div> : null}
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button className="btn btn-accent" onClick={save} disabled={!code.trim()}>{"Save"}</button>
            <button className="btn" onClick={forget}>{"Forget"}</button>
          </div>
          {macAuth !== "hmac-v1" ? <div style={{ color: MU, fontSize: 11, marginTop: 6 }}>{"Mac hasn't been set up for signed runs yet."}</div> : null}
        </div>
      )}
    </div>
  );
}
