// Smoke tests for the pure data helpers (the Analyst extends this file).
import assert from "node:assert/strict";
import * as D from "../src/datautil.js";

let ok = true;
const check = (name, fn) => {
  try { fn(); console.log("ok  ", name); } catch (e) { ok = false; console.log("FAIL", name, "-", e.message); }
};

const Q = "0.00;3.79;10.27;18.40;28.11;30.89;35.35;38.02;41.71;44.26;47.44;50.08;51.43;53.89;56.62;60.70;62.71;64.56;67.67;69.95;76.05;82.09;87.90;94.04;109.48";
check("parseQuantiles reads 25 values and rejects bad input", () => {
  assert.equal(D.parseQuantiles(Q).length, 25);
  assert.equal(D.parseQuantiles("1;2;3"), null);
  assert.equal(D.parseQuantiles(""), null);
  assert.equal(D.parseQuantiles(null), null);
});
check("quantileBox picks p10/p25/p50/p75/p90", () => {
  const b = D.quantileBox(Q);
  assert.deepEqual(b, { p10: 28.11, p25: 38.02, p50: 51.43, p75: 64.56, p90: 76.05 });
});
check("histogram empty and normal", () => {
  assert.deepEqual(D.histogram([], { bins: 5 }), { edges: [], counts: [] });
  const h = D.histogram([0, 1, 2, 3, 4], { bins: 5 });
  assert.equal(h.counts.reduce((a, b) => a + b, 0), 5);
  assert.equal(h.edges.length, 6);
});
check("realityCheck scales, clamps and needs n", () => {
  const buckets = [{ label: "0–5%", lo: 0, hi: 5, n: 100, mean_proj: 1, mean_actual: 2 }, { label: "35%+", lo: 35, hi: null, n: 40, mean_proj: 50, mean_actual: 200 }];
  assert.equal(D.realityCheck(2, buckets).adj, 4);
  assert.equal(D.realityCheck(60, buckets).adj, 100);
  assert.equal(D.realityCheck(2, [{ ...buckets[0], n: 5 }]), null);
});
check("freshness states", () => {
  const now = Date.parse("2026-11-26T12:00:00Z");
  assert.equal(D.freshness("2026-11-26T11:58:00Z", now), "live");
  assert.equal(D.freshness("2026-11-26T11:45:00Z", now), "stale");
  assert.equal(D.freshness("2026-11-26T10:00:00Z", now), "offline");
  assert.equal(D.freshness(null, now), "unknown");
});
check("validateContestHistory", () => {
  assert.equal(D.validateContestHistory({ version: 1, contests: [], players: { columns: ["a", "b"], rows: [[1, 2]] } }).ok, true);
  assert.equal(D.validateContestHistory({ version: 2, contests: [], players: { columns: [], rows: [] } }).ok, false);
  assert.equal(D.validateContestHistory({ version: 1, contests: [], players: { columns: ["a", "b"], rows: [[1]] } }).ok, false);
});
check("synthSlate classic and showdown", () => {
  const ps = [{ team: "NYK", opp: "CLE", gameStart: "2026-11-26T00:30:00+00:00", corrGame: "CLE@NYK" }, { team: "CLE", opp: "NYK", gameStart: "2026-11-26T00:30:00+00:00", corrGame: "CLE@NYK" }];
  const s = D.synthSlate("156302", ps);
  assert.equal(s.id, 156302); assert.equal(s.kind, "classic"); assert.equal(s.archived, true); assert.equal(s.games.length, 1);
  assert.equal(s.games[0].away, "CLE");
  assert.equal(D.synthSlate(1, [{ ...ps[0], cptId: 9 }]).kind, "showdown");
});
check("fmtCompact", () => {
  assert.equal(D.fmtCompact(1000000), "1.0M");
  assert.equal(D.fmtCompact(70000), "70k");
});

// ── Analyst: edge cases and real fixtures ─────────────────────────────────────
check("parseQuantiles edge cases", () => {
  const vals = Q.split(";").map(Number);
  assert.deepEqual(D.parseQuantiles(vals), vals);                      // array input
  assert.equal(D.parseQuantiles(Q + ";1"), null);                      // 26 values
  assert.equal(D.parseQuantiles(Q.replace("3.79", "x")), null);        // NaN token
  assert.equal(D.parseQuantiles(Q.replace("3.79", "")), null);         // blank token
  assert.equal(D.parseQuantiles(Q.replace("3.79", "Infinity")), null); // non-finite
  assert.equal(D.parseQuantiles(" " + Q.split(";").join(" ; ") + " ").length, 25); // whitespace ok
  assert.equal(D.quantileBox("1;2"), null);
  // p25 / p75 really are the 0.25 / 0.75 levels of Q_LEVELS
  assert.equal(D.Q_LEVELS.length, 25);
  assert.equal(D.Q_LEVELS[7], 0.25); assert.equal(D.Q_LEVELS[17], 0.75); assert.equal(D.Q_LEVELS[12], 0.5);
  assert.equal(D.Q_LEVELS[4], 0.1); assert.equal(D.Q_LEVELS[20], 0.9);
});
check("histogram edge cases", () => {
  const same = D.histogram([7, 7, 7], { bins: 4 });                  // zero range
  assert.equal(same.counts.reduce((a, b) => a + b, 0), 3);
  const clip = D.histogram([-5, 0, 5, 10, 15], { bins: 2, min: 0, max: 10 });
  assert.deepEqual(clip.counts, [1, 2]);                               // out-of-range dropped, max goes in last bin
  assert.deepEqual(clip.edges, [0, 5, 10]);
  const junk = D.histogram([1, NaN, null, "3", undefined, 2, Infinity], { bins: 1 });
  assert.equal(junk.counts[0], 2);                                     // only finite numbers counted
  assert.equal(D.histogram([1, 2], { bins: -3 }).counts.length, 1);   // bins clamps to >= 1
  assert.equal(D.histogram([1, 2], { bins: 0 }).counts.length, 20);   // 0 means "default" (20)
  assert.equal(D.histogram([1, 2], { bins: 2.7 }).counts.length, 2);
  assert.deepEqual(D.histogram(null, {}), { edges: [], counts: [] });
  const big = Array.from({ length: 10000 }, (_, i) => i / 100);
  const t0 = Date.now(); const hb = D.histogram(big, { bins: 40 });
  assert.equal(hb.counts.reduce((a, b) => a + b, 0), 10000);
  assert.ok(Date.now() - t0 < 200, "10k values bin fast");
});
check("realityCheck edge cases", () => {
  const b = [{ label: "0–5%", lo: 0, hi: 5, n: 30, mean_proj: 1, mean_actual: 2 },
             { label: "5–10%", lo: 5, hi: 10, n: 29, mean_proj: 7, mean_actual: 10 },
             { label: "10–20%", lo: 10, hi: 20, n: 99, mean_proj: 0, mean_actual: 3 },
             { label: "20–35%", lo: 20, hi: 35, n: 99, mean_proj: null, mean_actual: 3 }];
  assert.deepEqual(D.realityCheck(3, b), { adj: 6, bucket: "0–5%", n: 30 }); // n == minN accepted
  assert.equal(D.realityCheck(5, b), null);                           // boundary -> 5–10% bucket, n 29 < 30
  assert.equal(D.realityCheck(5, b, 10).bucket, "5–10%");
  assert.equal(D.realityCheck(12, b), null);                          // mean_proj 0 guarded
  assert.equal(D.realityCheck(25, b), null);                          // null mean guarded
  assert.equal(D.realityCheck(50, b), null);                          // no bucket
  assert.equal(D.realityCheck(-1, b), null);
  assert.equal(D.realityCheck("3", b), null);
  assert.equal(D.realityCheck(3, null), null);
  assert.equal(D.realityCheck(0, b).adj, 0);
});
check("freshness boundaries", () => {
  const now = Date.parse("2026-11-26T12:00:00Z");
  assert.equal(D.freshness("2026-11-26T11:55:00Z", now), "live");    // exactly 5 min
  assert.equal(D.freshness("2026-11-26T11:40:00Z", now), "stale");   // exactly 20 min
  assert.equal(D.freshness("2026-11-26T11:39:59Z", now), "offline");
  assert.equal(D.freshness("2026-11-26T12:03:00Z", now), "live");    // clock skew (future)
  assert.equal(D.freshness("not a date", now), "unknown");
  assert.equal(D.freshness("", now), "unknown");
  assert.equal(D.freshness(undefined, now), "unknown");
});
check("validateContestHistory rejects bad shapes", () => {
  for (const bad of [null, undefined, 1, "x", [], { version: "1", contests: [], players: { columns: [], rows: [] } },
    { version: 1, players: { columns: [], rows: [] } }, { version: 1, contests: [] },
    { version: 1, contests: [], players: { columns: ["a"], rows: [5] } },
    { version: 1, contests: {}, players: { columns: ["a"], rows: [] } }]) {
    const r = D.validateContestHistory(bad);
    assert.equal(r.ok, false, JSON.stringify(bad)); assert.ok(r.reason.length > 0);
  }
});
check("rowsToObjects", () => {
  assert.deepEqual(D.rowsToObjects(["a", "b"], [[1, 2], [3], "junk", null]), [{ a: 1, b: 2 }, { a: 3, b: null }]);
  assert.deepEqual(D.rowsToObjects(null, [[1]]), []);
  assert.deepEqual(D.rowsToObjects(["a"], undefined), []);
});
check("synthSlate edge cases", () => {
  const s = D.synthSlate("12", []);
  assert.equal(s.id, 12); assert.deepEqual(s.games, []); assert.equal(s.start_utc, null);
  assert.equal(s.file, "slates/12.csv"); assert.equal(s.lineups_file, "lineups/12.json"); assert.equal(s.spotlight_file, "spotlight/12.json");
  assert.equal(s.contest_locked, true);
  // raw CSV-style keys, no CorrGame: teams sorted into one game, earliest start kept
  const t = D.synthSlate(5, [{ Team: "bos", Opp: "atl", GameStartUTC: "2026-11-26T01:00:00Z" },
                              { Team: "atl", Opp: "bos", GameStartUTC: "2026-11-26T00:30:00Z" },
                              { Team: "", Opp: "" }]);
  assert.equal(t.games.length, 1); assert.equal(t.games[0].name, "ATL@BOS");
  assert.equal(t.games[0].start_utc, "2026-11-26T00:30:00Z"); assert.equal(t.start_utc, "2026-11-26T00:30:00Z");
  assert.equal(D.synthSlate(5, [{ Team: "A", Opp: "B", CptID: "77" }]).kind, "showdown");
});
check("fmtNum / fmtCompact", () => {
  assert.equal(D.fmtNum(1.234), "1.2"); assert.equal(D.fmtNum(52.63, 2), "52.63");
  assert.equal(D.fmtNum(null), "—"); assert.equal(D.fmtNum(""), "—"); assert.equal(D.fmtNum("abc"), "—"); assert.equal(D.fmtNum(NaN), "—");
  assert.equal(D.fmtNum("2.5", 0), "3");
  assert.equal(D.fmtCompact(0), "0"); assert.equal(D.fmtCompact(999), "999"); assert.equal(D.fmtCompact(1500), "1.5k");
  assert.equal(D.fmtCompact(-2000000), "-2.0M"); assert.equal(D.fmtCompact(null), "—"); assert.equal(D.fmtCompact("x"), "—");
});
check("fmtCompact rounds up across unit boundaries (999,999 -> 1.0M, 9,999 -> 10k)", () => {
  assert.equal(D.fmtCompact(999999), "1.0M");
  assert.equal(D.fmtCompact(9999), "10k");
});

check("Retest 1: fmtCompact boundary table (R2)", () => {
  const cases = [[999, "999"], [999.4, "999"], [999.5, "1.0k"], [1000, "1.0k"], [1500, "1.5k"], [9949, "9.9k"], [9960, "10k"],
    [9999, "10k"], [10000, "10k"], [70198, "70k"], [999499, "999k"], [999500, "1.0M"], [999999, "1.0M"], [1e6, "1.0M"],
    [1049999, "1.0M"], [-1500, "-1.5k"], [-999999, "-1.0M"], [0, "0"]];
  for (const [n, s] of cases) assert.equal(D.fmtCompact(n), s, String(n));
});
check("Retest 1: fmtCompact never prints \"-0\" (small negatives round to 0, as before R2)", () => {
  assert.equal(D.fmtCompact(-0.4), "0");
});
check("Retest 1: valid-JSON-but-wrong-shape bodies fail validation (S3)", () => {
  for (const body of ["[]", "null", "{}", "1", '"x"', '{"version":1}']) {
    assert.equal(D.validateContestHistory(JSON.parse(body)).ok, false, body);
  }
});

// Retest 2: run App.jsx's real contest_history.json body handler (extracted from source) on odd bodies
{
  const fs0 = await import("node:fs");
  const src = fs0.readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const m = src.match(/fetchText\("contest_history\.json"\)\.then\((function\(t\)\{[\s\S]*?\}\);\}\))/);
  check("Retest 2: App history handler maps null/array/primitive/invalid bodies to the format state (BUG-9)", () => {
    assert.ok(m, "handler found in App.jsx");
    const handlerSrc = m[1].slice(0, -1);          // drop the closing ")" of .then(
    const run = (body, prev) => {
      let state = prev;
      const setHistoryDoc = (f) => { state = typeof f === "function" ? f(state) : f; };
      // eslint-disable-next-line no-new-func
      new Function("setHistoryDoc", "return " + handlerSrc)(setHistoryDoc)(body);
      return state;
    };
    for (const body of ["null", "[]", "[1,2]", "1", "0", '"x"', "true", "false", "{not json", "", "<html>404</html>"]) {
      assert.deepEqual(run(body, null), { error: true, kind: "format" }, JSON.stringify(body));
    }
    const good = { version: 1, contests: [], players: { columns: ["a"], rows: [] } };
    assert.deepEqual(run(JSON.stringify(good), null), good);
    assert.deepEqual(run("null", good), good, "a bad refresh keeps the previous good doc");
  });
}
check("Retest 2: fmtCompact sign after rounding (BUG-8)", () => {
  for (const [n, s] of [[-0.4, "0"], [-0.6, "-1"], [0.4, "0"], [-999.6, "-1.0k"], [-0, "0"]]) assert.equal(D.fmtCompact(n), s, String(n));
});

// Real fixtures (gitignored repo data/, copied from the data branch); skipped when absent.
const fs = await import("node:fs");
const url = await import("node:url");
const dataDir = url.fileURLToPath(new URL("../../data/", import.meta.url));
const has = (p) => { try { return fs.statSync(dataDir + p).isFile(); } catch (e) { return false; } };
if (has("contest_history.json")) {
  check("real contest_history.json validates and round-trips", () => {
    const doc = JSON.parse(fs.readFileSync(dataDir + "contest_history.json", "utf8"));
    assert.equal(D.validateContestHistory(doc).ok, true);
    const rows = D.rowsToObjects(doc.players.columns, doc.players.rows);
    assert.equal(rows.length, doc.samples.player_rows);
    const q = rows.find((r) => r.name === "Immanuel Quickley" && r.own_by_slot && r.own_by_slot.PG === 28.73);
    assert.ok(q && Math.abs(q.own - 52.63) < 0.01);
    assert.equal(doc.calibration.buckets.length, 5);
    // every bucket's realityCheck works for an own value inside it
    for (const b of doc.calibration.buckets) {
      const r = D.realityCheck(b.lo + 1, doc.calibration.buckets);
      if (b.n >= 30) assert.ok(r && r.adj >= 0 && r.adj <= 100, b.label); else assert.equal(r, null);
    }
    const broken = { ...doc, players: { ...doc.players, rows: doc.players.rows.map((r, i) => (i === 3 ? r.slice(0, 5) : r)) } };
    assert.equal(D.validateContestHistory(broken).ok, false);
  });
} else console.log("skip real contest_history.json (absent)");
if (has("slates/156302.csv")) {
  check("real slate 156302: quantiles parse and synthSlate finds 6 games", () => {
    const lines = fs.readFileSync(dataDir + "slates/156302.csv", "utf8").trim().split(/\r?\n/);
    const head = lines[0].split(",");
    const rows = lines.slice(1).map((l) => { const c = l.split(","); const o = {}; head.forEach((h, i) => { o[h] = c[i]; }); return o; });
    assert.ok(rows.length > 100);
    const live = rows.filter((r) => Number(r.Proj) > 0), zero = rows.filter((r) => !(Number(r.Proj) > 0));
    assert.equal(live.filter((r) => D.quantileBox(r.Quantiles)).length, live.length, "every projected row has a valid 25-value curve");
    assert.ok(rows.filter((r) => !r.Quantiles).every((r) => D.quantileBox(r.Quantiles) === null), "blank curves -> null -> '—'");
    assert.ok(zero.length > 0);
    for (const r of live) { const b = D.quantileBox(r.Quantiles); assert.ok(b.p10 <= b.p25 && b.p25 <= b.p50 && b.p50 <= b.p75 && b.p75 <= b.p90); }
    const s = D.synthSlate(156302, rows);
    assert.equal(s.games.length, 6); assert.equal(s.kind, "classic"); assert.equal(s.archived, true);
    assert.ok(s.start_utc && s.start_utc.startsWith("2026-11-2"));
  });
} else console.log("skip real slate 156302 (absent)");

if (!ok) { console.log("datautil tests FAILED"); process.exit(1); }
console.log("datautil tests passed");
