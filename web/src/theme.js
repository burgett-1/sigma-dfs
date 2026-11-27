// Sigma HUD theme tokens for inline styles. Values are CSS variables defined in
// theme.css, so the light/dark switch needs no re-render of style objects.
export const OH = "var(--brand)";          // brand orange: logo and primary CTA
export const BG = "var(--bg)";
export const CARD = "var(--surface-solid)";
export const BR = "var(--line)";
export const MU = "var(--text-muted)";
export const GR = "var(--pos)";
export const RE = "var(--neg)";
export const YE = "var(--warn)";
export const TX = "var(--text)";
export const ACC = "var(--accent)";        // primary data colour
export const ACC2 = "var(--accent-2)";     // secondary series
export const SURF2 = "var(--surface-2)";
export const LINE2 = "var(--line-strong)";
export const MONO = "var(--font-mono)";

// position colours stay the same in both themes
export const POS_COLORS = { PG: "#4a9eff", SG: "#a78bfa", SF: "#34d399", PF: "#fb923c", C: "#f43f5e" };

// translucent version of any colour (hex or var(...)): alpha(GR, 13) ~ the old GR+"22"
export function alpha(c, pct) {
  return "color-mix(in srgb, " + c + " " + pct + "%, transparent)";
}

const KEY = "sigma_theme";
export function getTheme() {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark") return v;
  } catch (e) { /* storage unavailable (private mode) */ }
  try {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch (e) { return "dark"; }
}
export function setTheme(t) {
  const v = t === "light" ? "light" : "dark";
  try { document.documentElement.dataset.theme = v; } catch (e) { /* no DOM */ }
  try { localStorage.setItem(KEY, v); } catch (e) { /* storage unavailable */ }
  return v;
}
