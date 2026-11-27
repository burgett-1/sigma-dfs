// Zero-dependency SVG charts for the Sigma HUD. Every chart scales through its
// viewBox (width 100%), has an aria-label, a hover tooltip and an empty state.
import { useEffect, useRef, useState } from "react";
import { ACC, ACC2, MU, GR, RE, BR, TX, alpha } from "./theme.js";

const Q_LEVELS = [0, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6,
  0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.975, 0.99, 0.999];

function fin(x) { return typeof x === "number" && isFinite(x); }
function fmtTick(v) {
  const a = Math.abs(v);
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (a >= 1e4) return Math.round(v / 1e3) + "k";
  if (a >= 100 || Number.isInteger(v)) return String(Math.round(v));
  if (a >= 10) return v.toFixed(0);
  if (a >= 1) return v.toFixed(1);
  return v.toFixed(2);
}
export function niceTicks(lo, hi, n) {
  const count = n || 5;
  if (!fin(lo) || !fin(hi)) return [];
  if (hi === lo) return [lo];
  const span = hi - lo, raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

export function ChartEmpty({ text, height }) {
  return (
    <div role="note" className="muted grid-bg" style={{ minHeight: height || 120, display: "flex", alignItems: "center", justifyContent: "center",
      textAlign: "center", fontSize: 12, padding: 12, border: "1px dashed " + BR, borderRadius: 8 }}>
      {text || "No data yet."}
    </div>
  );
}

// wrapper that positions a tooltip over an SVG drawn in a W×H viewBox
function useTip() {
  const [tip, setTip] = useState(null);
  return [tip, setTip];
}
function Tip({ tip, W, H }) {
  if (!tip) return null;
  return <div className="chart-tip" style={{ left: (tip.x / W * 100) + "%", top: (tip.y / H * 100) + "%" }}>{tip.text}</div>;
}

// ── Sparkline ────────────────────────────────────────────────────────────────
export function Sparkline({ values, width, height, color, label, zero }) {
  const W = width || 120, H = height || 28;
  const v = (values || []).filter(fin);
  if (v.length < 2) return <span className="muted" style={{ fontSize: 11 }}>{"—"}</span>;
  let lo = Math.min.apply(null, v), hi = Math.max.apply(null, v);
  if (zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (hi === lo) hi = lo + 1;
  const x = (i) => 2 + i * (W - 4) / (v.length - 1), y = (s) => H - 2 - (s - lo) / (hi - lo) * (H - 4);
  const d = v.map((s, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(s).toFixed(1)).join(" ");
  return (
    <svg className="chart" viewBox={"0 0 " + W + " " + H} style={{ maxWidth: W }} role="img" aria-label={label || "trend"}>
      <title>{label || ""}</title>
      {zero ? <line x1={0} x2={W} y1={y(0)} y2={y(0)} stroke={BR} strokeDasharray="2 2" /> : null}
      <path d={d} fill="none" stroke={color || ACC} strokeWidth={1.6} strokeLinejoin="round" />
      <circle cx={x(v.length - 1)} cy={y(v[v.length - 1])} r={2.2} fill={color || ACC} />
    </svg>
  );
}

// ── QuantileBox: p10–p90 whisker, p25–p75 box, median tick (fits a table cell) ──
export function QuantileBox({ box, min, max, width, height }) {
  const W = width || 120, H = height || 18;
  if (!box || !fin(box.p10) || !fin(box.p90)) return <span className="muted">{"—"}</span>;
  const lo = fin(min) ? min : box.p10, hi = fin(max) && max > lo ? max : Math.max(box.p90, lo + 1);
  const x = (v) => 2 + Math.max(0, Math.min(1, (v - lo) / (hi - lo))) * (W - 4);
  const lbl = "p10 " + box.p10.toFixed(1) + " · p25 " + box.p25.toFixed(1) + " · median " + box.p50.toFixed(1) + " · p75 " + box.p75.toFixed(1) + " · p90 " + box.p90.toFixed(1);
  return (
    <svg className="chart" viewBox={"0 0 " + W + " " + H} style={{ width: W, maxWidth: "100%" }} role="img" aria-label={"Outcome range: " + lbl}>
      <title>{lbl}</title>
      <line x1={x(box.p10)} x2={x(box.p90)} y1={H / 2} y2={H / 2} stroke={MU} strokeWidth={1} />
      <line x1={x(box.p10)} x2={x(box.p10)} y1={H / 2 - 3} y2={H / 2 + 3} stroke={MU} />
      <line x1={x(box.p90)} x2={x(box.p90)} y1={H / 2 - 3} y2={H / 2 + 3} stroke={MU} />
      <rect x={x(box.p25)} y={3} width={Math.max(1, x(box.p75) - x(box.p25))} height={H - 6} rx={2} fill={alpha(ACC, 25)} stroke={ACC} strokeWidth={1} />
      <line x1={x(box.p50)} x2={x(box.p50)} y1={2} y2={H - 2} stroke={TX} strokeWidth={1.6} />
    </svg>
  );
}

// ── QuantileFan: the full 25-point outcome curve drawn as a density profile ──
export function QuantileFan({ q, height, label }) {
  const W = 520, H = height || 150, pad = { l: 8, r: 8, t: 10, b: 22 };
  const [tip, setTip] = useTip();
  if (!q || q.length !== Q_LEVELS.length) return <ChartEmpty text="No outcome curve for this player." height={H} />;
  const lo = q[0], hi = Math.max(q[q.length - 1], lo + 1);
  const x = (v) => pad.l + (v - lo) / (hi - lo) * (W - pad.l - pad.r);
  const segs = [];
  let dmax = 0;
  for (let i = 1; i < q.length; i++) {
    const w = Math.max(q[i] - q[i - 1], (hi - lo) / 200);
    const dens = (Q_LEVELS[i] - Q_LEVELS[i - 1]) / w;
    dmax = Math.max(dmax, dens);
    segs.push({ x0: q[i - 1], x1: q[i - 1] + w, dens, u0: Q_LEVELS[i - 1], u1: Q_LEVELS[i] });
  }
  const yb = H - pad.b, yh = yb - pad.t;
  const ticks = niceTicks(lo, hi, 6);
  return (
    <div style={{ position: "relative" }}>
      <svg className="chart" viewBox={"0 0 " + W + " " + H} role="img" aria-label={label || "Outcome distribution"} onMouseLeave={() => setTip(null)}>
        {segs.map((s, i) => {
          const mid = s.u0 >= 0.25 && s.u1 <= 0.75, tail = s.u1 <= 0.1 || s.u0 >= 0.9;
          const h = s.dens / dmax * yh;
          return <rect key={i} className="tip-target" x={x(s.x0)} y={yb - h} width={Math.max(0.8, x(s.x1) - x(s.x0))} height={h}
            fill={mid ? alpha(ACC, 55) : tail ? alpha(ACC2, 25) : alpha(ACC, 25)}
            onMouseMove={() => setTip({ x: x((s.x0 + s.x1) / 2), y: yb - h, text: Math.round(s.u0 * 1000) / 10 + "–" + Math.round(s.u1 * 1000) / 10 + "th pct: " + s.x0.toFixed(1) + "–" + q[i + 1].toFixed(1) + " pts" })} />;
        })}
        <line x1={x(q[12])} x2={x(q[12])} y1={pad.t} y2={yb} stroke={TX} strokeWidth={1.5} />
        <line x1={pad.l} x2={W - pad.r} y1={yb} y2={yb} stroke={BR} />
        {ticks.map((t) => <text key={t} x={x(t)} y={H - 6} textAnchor="middle">{fmtTick(t)}</text>)}
        <text x={x(q[12]) + 4} y={pad.t + 9}>{"median " + q[12].toFixed(1)}</text>
      </svg>
      <Tip tip={tip} W={W} H={H} />
    </div>
  );
}

// ── Histogram ────────────────────────────────────────────────────────────────
export function Histogram({ counts, edges, marks, color, height, label, xLabel, fmt, empty }) {
  const W = 520, H = height || 160, pad = { l: 34, r: 8, t: 8, b: xLabel ? 32 : 22 };
  const [tip, setTip] = useTip();
  if (!counts || !counts.length || !edges || edges.length !== counts.length + 1) return <ChartEmpty text={empty || "No data yet."} height={H} />;
  const lo = edges[0], hi = edges[edges.length - 1];
  const cmax = Math.max(1, Math.max.apply(null, counts));
  const x = (v) => pad.l + (v - lo) / (hi - lo || 1) * (W - pad.l - pad.r);
  const y = (c) => H - pad.b - c / cmax * (H - pad.t - pad.b);
  const f = fmt || fmtTick;
  const yt = niceTicks(0, cmax, 3);
  return (
    <div style={{ position: "relative" }}>
      <svg className="chart" viewBox={"0 0 " + W + " " + H} role="img" aria-label={label || "Histogram"} onMouseLeave={() => setTip(null)}>
        {yt.map((t) => <g key={t}><line x1={pad.l} x2={W - pad.r} y1={y(t)} y2={y(t)} stroke={BR} /><text x={pad.l - 4} y={y(t) + 3} textAnchor="end">{fmtTick(t)}</text></g>)}
        {counts.map((c, i) => (
          <rect key={i} className="tip-target" x={x(edges[i]) + 0.5} y={y(c)} width={Math.max(0.5, x(edges[i + 1]) - x(edges[i]) - 1)} height={H - pad.b - y(c)}
            fill={alpha(color || ACC, 70)} onMouseMove={() => setTip({ x: x((edges[i] + edges[i + 1]) / 2), y: y(c), text: f(edges[i]) + "–" + f(edges[i + 1]) + ": " + c.toLocaleString() })} />
        ))}
        {(marks || []).filter(fin).map((m, i) => <line key={"m" + i} x1={x(m)} x2={x(m)} y1={pad.t} y2={H - pad.b} stroke={ACC2} strokeWidth={1.2} opacity={0.8} />)}
        {niceTicks(lo, hi, 6).map((t) => <text key={"x" + t} x={x(t)} y={H - pad.b + 13} textAnchor="middle">{f(t)}</text>)}
        {xLabel ? <text x={(pad.l + W - pad.r) / 2} y={H - 3} textAnchor="middle">{xLabel}</text> : null}
      </svg>
      <Tip tip={tip} W={W} H={H} />
    </div>
  );
}

// ── Scatter (optional y=x diagonal, bubble size, colour) ─────────────────────
export function Scatter({ points, xLabel, yLabel, diagonal, height, selected, onSelect, label, quadrants, xMax, yMax, className }) {
  const W = 560, H = height || 340, pad = { l: 40, r: 12, t: 12, b: 34 };
  const [tip, setTip] = useTip();
  const pts = (points || []).filter((p) => fin(p.x) && fin(p.y));
  if (!pts.length) return <ChartEmpty text="Nothing to plot yet." height={180} />;
  let xm = fin(xMax) ? xMax : Math.max.apply(null, pts.map((p) => p.x)), ym = fin(yMax) ? yMax : Math.max.apply(null, pts.map((p) => p.y));
  if (diagonal) { xm = ym = Math.max(xm, ym); }
  xm = xm > 0 ? xm * 1.05 : 1; ym = ym > 0 ? ym * 1.05 : 1;
  const x = (v) => pad.l + v / xm * (W - pad.l - pad.r), y = (v) => H - pad.b - v / ym * (H - pad.t - pad.b);
  const rmax = Math.max.apply(null, pts.map((p) => p.r || 0)) || 1;
  const rad = (p) => (p.r ? 3 + Math.sqrt(p.r / rmax) * 7 : 4.5);
  const sorted = pts.slice().sort((a, b) => (a.id === selected) - (b.id === selected));
  return (
    <div style={{ position: "relative" }} className={className}>
      <svg className="chart" viewBox={"0 0 " + W + " " + H} role="img" aria-label={label || "Scatter plot"} onMouseLeave={() => setTip(null)}>
        {niceTicks(0, ym, 5).map((t) => <g key={"y" + t}><line x1={pad.l} x2={W - pad.r} y1={y(t)} y2={y(t)} stroke={BR} /><text x={pad.l - 5} y={y(t) + 3} textAnchor="end">{fmtTick(t)}</text></g>)}
        {niceTicks(0, xm, 6).map((t) => <text key={"x" + t} x={x(t)} y={H - pad.b + 14} textAnchor="middle">{fmtTick(t)}</text>)}
        {diagonal ? <line x1={x(0)} y1={y(0)} x2={x(xm)} y2={y(xm)} stroke={MU} strokeDasharray="4 4" /> : null}
        {quadrants && diagonal ? <text x={x(xm * 0.04)} y={y(ym * 0.92)} style={{ fill: GR }}>{quadrants[0]}</text> : null}
        {quadrants && diagonal ? <text x={x(xm * 0.96)} y={y(ym * 0.06)} textAnchor="end" style={{ fill: RE }}>{quadrants[1]}</text> : null}
        {sorted.map((p, i) => {
          const on = selected != null && p.id === selected;
          return <circle key={p.id != null ? p.id : i} className="tip-target" cx={x(p.x)} cy={y(p.y)} r={rad(p) + (on ? 2 : 0)}
            fill={alpha(p.color || ACC, on ? 85 : 45)} stroke={on ? TX : (p.color || ACC)} strokeWidth={on ? 2 : 1}
            onMouseMove={() => setTip({ x: x(p.x), y: y(p.y) - rad(p), text: p.label || (p.x.toFixed(1) + ", " + p.y.toFixed(1)) })}
            onClick={onSelect ? () => onSelect(on ? null : p.id) : undefined} style={onSelect ? { cursor: "pointer" } : undefined} />;
        })}
        {xLabel ? <text x={(pad.l + W - pad.r) / 2} y={H - 4} textAnchor="middle">{xLabel}</text> : null}
        {yLabel ? <text x={10} y={(pad.t + H - pad.b) / 2} textAnchor="middle" transform={"rotate(-90 10 " + (pad.t + H - pad.b) / 2 + ")"}>{yLabel}</text> : null}
      </svg>
      <Tip tip={tip} W={W} H={H} />
    </div>
  );
}

// ── DivergingBar: centred at 0, positive green / negative red (table cell) ───
export function DivergingBar({ value, max, width, digits }) {
  const W = width || 90, H = 14;
  if (!fin(value)) return <span className="muted">{"—"}</span>;
  const m = fin(max) && max > 0 ? max : Math.max(1, Math.abs(value));
  const half = (W - 2) / 2, len = Math.min(1, Math.abs(value) / m) * half;
  const col = value >= 0 ? GR : RE;
  const txt = (value > 0 ? "+" : "") + value.toFixed(digits == null ? 1 : digits);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <svg viewBox={"0 0 " + W + " " + H} style={{ width: W, height: H, display: "block" }} role="img" aria-label={"Leverage " + txt}>
        <title>{"Leverage " + txt}</title>
        <rect x={1} y={H / 2 - 1} width={W - 2} height={2} fill={BR} />
        <rect x={value >= 0 ? 1 + half : 1 + half - len} y={2} width={Math.max(1, len)} height={H - 4} rx={2} fill={col} />
        <line x1={1 + half} x2={1 + half} y1={0} y2={H} stroke={MU} />
      </svg>
      <span className="num" style={{ color: col, fontSize: 12, minWidth: 38 }}>{txt}</span>
    </span>
  );
}

// ── BarList: horizontal labelled bars, optional comparison bar ───────────────
export function BarList({ items, max, format, color, compareColor, valueLabel, compareLabel, onSelect, selected, empty, label }) {
  const rows = (items || []).filter((r) => r && (fin(r.value) || fin(r.compare)));
  if (!rows.length) return <ChartEmpty text={empty || "No data yet."} height={80} />;
  const f = format || ((v) => (fin(v) ? fmtTick(v) : "—"));
  const m = fin(max) && max > 0 ? max : Math.max.apply(null, rows.map((r) => Math.max(Math.abs(r.value || 0), Math.abs(r.compare || 0)))) || 1;
  const pct = (v) => Math.min(100, Math.abs(v || 0) / m * 100) + "%";
  return (
    <div role="list" aria-label={label || "Bar list"}>
      {valueLabel || compareLabel ? (
        <div style={{ display: "flex", gap: 12, fontSize: 11, marginBottom: 6 }} className="muted">
          {valueLabel ? <span><span className="live-dot" style={{ background: color || ACC, marginRight: 4 }} />{valueLabel}</span> : null}
          {compareLabel ? <span><span className="live-dot" style={{ background: compareColor || ACC2, marginRight: 4 }} />{compareLabel}</span> : null}
        </div>
      ) : null}
      {rows.map((r, i) => {
        const on = selected != null && r.id === selected;
        const c = r.color || color || ACC;
        return (
          <div key={r.id != null ? r.id : r.label + i} role="listitem" onClick={onSelect ? () => onSelect(on ? null : r.id) : undefined}
            style={{ marginBottom: 7, cursor: onSelect ? "pointer" : "default", borderRadius: 6, padding: on ? "3px 5px" : 0, background: on ? alpha(ACC, 10) : "transparent" }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 12 }}>
              <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.label}{r.note ? <span className="muted" style={{ fontSize: 11 }}>{" " + r.note}</span> : null}</span>
              <span className="num" style={{ whiteSpace: "nowrap" }}>
                <span style={{ color: c }}>{f(r.value)}</span>
                {fin(r.compare) ? <span className="muted">{" / "}<span style={{ color: compareColor || ACC2 }}>{f(r.compare)}</span></span> : null}
              </span>
            </div>
            <div style={{ height: 6, background: alpha(BR, 100), borderRadius: 3, overflow: "hidden", marginTop: 3, position: "relative" }}>
              <div className="bar-fill" style={{ width: pct(r.value), height: "100%", background: c, borderRadius: 3 }} />
              {r.marker != null && fin(r.marker) ? <div title={"suggested " + f(r.marker)} style={{ position: "absolute", top: -2, bottom: -2, left: pct(r.marker), width: 2, background: TX }} /> : null}
            </div>
            {fin(r.compare) ? <div style={{ height: 4, background: alpha(BR, 100), borderRadius: 3, overflow: "hidden", marginTop: 2 }}>
              <div className="bar-fill" style={{ width: pct(r.compare), height: "100%", background: compareColor || ACC2, borderRadius: 3 }} />
            </div> : null}
          </div>
        );
      })}
    </div>
  );
}

// ── LineChart: multi-series, optional diagonal, optional log-x ───────────────
export function LineChart({ series, diagonal, xLabel, yLabel, height, xLog, xTicks, yMin, yMax, xFmt, yFmt, label, empty }) {
  const W = 520, H = height || 220, pad = { l: 40, r: 12, t: 10, b: xLabel ? 34 : 22 };
  const [tip, setTip] = useTip();
  const ss = (series || []).map((s) => ({ ...s, points: (s.points || []).filter((p) => fin(p[0]) && fin(p[1]) && (!xLog || p[0] > 0)) })).filter((s) => s.points.length);
  if (!ss.length) return <ChartEmpty text={empty || "No data yet."} height={H} />;
  const all = ss.flatMap((s) => s.points);
  let x0 = Math.min.apply(null, all.map((p) => p[0])), x1 = Math.max.apply(null, all.map((p) => p[0]));
  let y0 = fin(yMin) ? yMin : Math.min(0, Math.min.apply(null, all.map((p) => p[1])));
  let y1 = fin(yMax) ? yMax : Math.max.apply(null, all.map((p) => p[1]));
  if (diagonal) { x0 = y0 = Math.min(x0, y0); x1 = y1 = Math.max(x1, y1); }
  if (x1 === x0) x1 = x0 + 1;
  if (y1 === y0) y1 = y0 + 1;
  const tx = (v) => (xLog ? Math.log10(v) : v);
  const x = (v) => pad.l + (tx(v) - tx(x0)) / (tx(x1) - tx(x0)) * (W - pad.l - pad.r);
  const y = (v) => H - pad.b - (v - y0) / (y1 - y0) * (H - pad.t - pad.b);
  const fx = xFmt || fmtTick, fy = yFmt || fmtTick;
  const xt = xTicks || (xLog ? all.map((p) => p[0]).filter((v, i, a) => a.indexOf(v) === i) : niceTicks(x0, x1, 6));
  const palette = [ACC, ACC2, GR, RE];
  return (
    <div style={{ position: "relative" }}>
      {ss.length > 1 ? <div style={{ display: "flex", gap: 12, fontSize: 11, marginBottom: 4, flexWrap: "wrap" }} className="muted">
        {ss.map((s, i) => <span key={s.name}><span className="live-dot" style={{ background: s.color || palette[i % 4], marginRight: 4 }} />{s.name}</span>)}
      </div> : null}
      <svg className="chart" viewBox={"0 0 " + W + " " + H} role="img" aria-label={label || "Line chart"} onMouseLeave={() => setTip(null)}>
        {niceTicks(y0, y1, 4).map((t) => <g key={"y" + t}><line x1={pad.l} x2={W - pad.r} y1={y(t)} y2={y(t)} stroke={BR} /><text x={pad.l - 5} y={y(t) + 3} textAnchor="end">{fy(t)}</text></g>)}
        {xt.map((t) => <text key={"x" + t} x={x(t)} y={H - pad.b + 14} textAnchor="middle">{fx(t)}</text>)}
        {diagonal ? <line x1={x(x0)} y1={y(x0)} x2={x(x1)} y2={y(x1)} stroke={MU} strokeDasharray="4 4" /> : null}
        {ss.map((s, i) => {
          const c = s.color || palette[i % 4];
          const d = s.points.map((p, k) => (k ? "L" : "M") + x(p[0]).toFixed(1) + " " + y(p[1]).toFixed(1)).join(" ");
          return (
            <g key={s.name || i}>
              <path d={d} fill="none" stroke={c} strokeWidth={2} strokeLinejoin="round" strokeDasharray={s.dashed ? "5 4" : undefined} />
              {s.points.map((p, k) => <circle key={k} className="tip-target" cx={x(p[0])} cy={y(p[1])} r={3} fill={c}
                onMouseMove={() => setTip({ x: x(p[0]), y: y(p[1]), text: (s.name ? s.name + ": " : "") + fx(p[0]) + " → " + fy(p[1]) })} />)}
            </g>
          );
        })}
        {xLabel ? <text x={(pad.l + W - pad.r) / 2} y={H - 4} textAnchor="middle">{xLabel}</text> : null}
        {yLabel ? <text x={10} y={(pad.t + H - pad.b) / 2} textAnchor="middle" transform={"rotate(-90 10 " + (pad.t + H - pad.b) / 2 + ")"}>{yLabel}</text> : null}
      </svg>
      <Tip tip={tip} W={W} H={H} />
    </div>
  );
}

// ── CountUp: animates to the value (600ms), instant under reduced motion ────
function reducedMotion() {
  try { return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; }
}
export function CountUp({ value, format, duration }) {
  const f = format || ((v) => String(Math.round(v)));
  const [shown, setShown] = useState(fin(value) ? (reducedMotion() ? value : 0) : null);
  const prev = useRef(0), raf = useRef(null);
  useEffect(() => {
    if (!fin(value)) { setShown(null); return undefined; }
    if (reducedMotion() || typeof requestAnimationFrame === "undefined") { prev.current = value; setShown(value); return undefined; }
    const from = prev.current, to = value, dur = duration || 600, t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      setShown(from + (to - from) * e);
      if (k < 1) raf.current = requestAnimationFrame(step); else prev.current = to;
    };
    raf.current = requestAnimationFrame(step);
    return () => { if (raf.current) cancelAnimationFrame(raf.current); prev.current = to; };
  }, [value]);
  return <span className="num">{shown == null ? "—" : f(shown)}</span>;
}
