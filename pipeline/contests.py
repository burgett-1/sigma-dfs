"""Your DraftKings contest results — private, admin only.

On DraftKings, open a finished contest and use "Export Lineups to CSV" (the file
is called contest-standings-<contest id>.csv, sometimes zipped). Every few
minutes the updater copies any such file it finds in ~/Downloads (or that you
drop into ~/SigmaDFS-Private/drop) into the private folder, which is never
published: it lives outside the repository and the data branch.

From those files:

  * report.html (private folder): each contest, how everyone finished, where
    your entries placed, what the top 1% looked like (total ownership, how many
    sub-10%-owned players) versus the whole field.
  * own_calibration.json (private folder): real %Drafted versus the ownership the
    model projected for that slate, fitted as a small correction that is shrunk
    hard toward "no change" while the sample is small (it can never carry more
    than half the weight). The field the simulator builds lineups against uses it.
"""
from __future__ import annotations

import csv
import html
import io
import json
import logging
import math
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import ET
from .names import norm_name

log = logging.getLogger(__name__)

PRIVATE = Path(os.environ.get("SIGMA_PRIVATE_DIR", Path.home() / "SigmaDFS-Private"))
CONTESTS = PRIVATE / "contests"
SNAPSHOTS = PRIVATE / "projections"
OWN_CAL = PRIVATE / "own_calibration.json"
FILE_RE = re.compile(r"contest-standings-(\d+)", re.I)
SLOT_RE = re.compile(r"(?:^|\s)(CPT|PG|SG|SF|PF|C|G|F|UTIL)\s+")
LOW_OWN = 10.0          # "low owned" means under 10% drafted
SHRINK_N = 600          # player observations needed before the correction gets half its weight


def _import_dirs() -> list[Path]:
    raw = os.environ.get("SIGMA_CONTEST_IMPORT", str(Path.home() / "Downloads"))
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p] + [PRIVATE / "drop"]


# ── importing ────────────────────────────────────────────────────────────────
def import_new() -> list[str]:
    """Copy new contest-standings exports into the private folder. Returns new ids."""
    CONTESTS.mkdir(parents=True, exist_ok=True)
    (PRIVATE / "drop").mkdir(exist_ok=True)
    have = {m.group(1) for f in CONTESTS.glob("*.csv") if (m := FILE_RE.search(f.name))}
    new = []
    for d in _import_dirs():
        try:
            files = sorted(d.glob("contest-standings-*"))
        except OSError as e:       # macOS may need permission for ~/Downloads
            log.info("cannot read %s: %s", d, e)
            continue
        for f in files:
            m = FILE_RE.search(f.name)
            if not m or m.group(1) in have:
                continue
            try:
                text = _read_export(f)
            except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as e:
                log.info("skipping %s: %s", f.name, e)
                continue
            if not text or parse(text) is None:
                continue
            (CONTESTS / f"contest-standings-{m.group(1)}.csv").write_text(text)
            stamp = datetime.fromtimestamp(f.stat().st_mtime, ET).date().isoformat()
            _meta_update(m.group(1), {"imported_from": f.name, "file_date": stamp})
            have.add(m.group(1))
            new.append(m.group(1))
    return new


def _read_export(f: Path) -> str:
    if f.suffix.lower() == ".zip":
        with zipfile.ZipFile(f) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
            return z.read(name).decode("utf-8-sig", errors="replace") if name else ""
    if f.suffix.lower() == ".csv":
        return f.read_text(encoding="utf-8-sig", errors="replace")
    return ""


def _meta_path() -> Path:
    return CONTESTS / "index.json"


def _meta() -> dict:
    try:
        return json.loads(_meta_path().read_text())
    except (OSError, ValueError):
        return {}


def _meta_update(cid: str, fields: dict) -> None:
    m = _meta()
    m.setdefault(cid, {}).update(fields)
    _meta_path().write_text(json.dumps(m, indent=1))


# ── parsing ──────────────────────────────────────────────────────────────────
def split_lineup(s: str) -> list[tuple[str, str]]:
    """'C Nikola Jokic F LeBron James ...' -> [('C','Nikola Jokic'), ('F','LeBron James'), ...]"""
    s = " " + (s or "").strip() + " "
    marks = list(SLOT_RE.finditer(s))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(s)
        name = s[m.end():end].strip()
        if name and name.upper() != "LOCKED":
            out.append((m.group(1), name))
    return out


def parse(text: str) -> dict | None:
    """DraftKings contest standings export -> entries and the player %Drafted table."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or "Rank" not in rows[0] or "Lineup" not in rows[0]:
        return None
    head = [h.strip() for h in rows[0]]
    ix = {h: i for i, h in enumerate(head) if h}
    own, fpts = {}, {}
    if "Player" in ix and "%Drafted" in ix:
        for r in rows[1:]:
            if len(r) > ix["%Drafted"] and r[ix["Player"]].strip():
                try:
                    k = norm_name(r[ix["Player"]])
                    own[k] = float(r[ix["%Drafted"]].strip().rstrip("%"))
                    if "FPTS" in ix and len(r) > ix["FPTS"]:
                        fpts[k] = float(r[ix["FPTS"]] or 0)
                except ValueError:
                    pass
    entries = []
    for r in rows[1:]:
        if len(r) <= ix["Lineup"] or not r[ix["Rank"]].strip():
            continue
        try:
            rank, pts = int(r[ix["Rank"]]), float(r[ix["Points"]])
        except (ValueError, KeyError):
            continue
        name = r[ix["EntryName"]].strip() if "EntryName" in ix else ""
        user = re.sub(r"\s*\(\d+/\d+\)\s*$", "", name)
        entries.append({"rank": rank, "points": pts, "user": user, "entry": name,
                        "players": split_lineup(r[ix["Lineup"]])})
    if not entries:
        return None
    entries.sort(key=lambda e: e["rank"])
    kind = "showdown" if any(s == "CPT" for e in entries[:50] for s, _ in e["players"]) else "classic"
    return {"entries": entries, "own": own, "fpts": fpts, "kind": kind}


def load_all() -> list[dict]:
    out, meta = [], _meta()
    for f in sorted(CONTESTS.glob("contest-standings-*.csv")):
        cid = FILE_RE.search(f.name).group(1)
        try:
            res = parse(f.read_text(errors="replace"))
        except OSError:
            continue
        if res:
            out.append({"id": cid, **meta.get(cid, {}), **res})
    return out


def my_username(contests: list[dict]) -> str | None:
    """Your DraftKings username: SIGMA_DK_USERNAME if set, otherwise the one name
    that has entries in every contest you exported (needs 2+ contests)."""
    env = os.environ.get("SIGMA_DK_USERNAME", "").strip()
    if env:
        return env
    if len(contests) < 2:
        return None
    common = None
    for c in contests:
        users = {e["user"].lower() for e in c["entries"]}
        common = users if common is None else common & users
    if common and len(common) == 1:
        lower = common.pop()
        return next(e["user"] for e in contests[0]["entries"] if e["user"].lower() == lower)
    return None


# ── analysis ─────────────────────────────────────────────────────────────────
def contest_stats(c: dict, me: str | None) -> dict:
    E = c["entries"]
    n = len(E)
    own = c["own"]

    def lu_own(e):
        vals = []
        for slot, name in e["players"]:
            o = own.get(norm_name(name))
            if o is not None:
                vals.append(o)
        return vals

    def profile(es):
        tot, low = [], []
        for e in es:
            v = lu_own(e)
            if len(v) == len(e["players"]) and v:
                tot.append(sum(v))
                low.append(sum(1 for x in v if x < LOW_OWN))
        return {"lineups": len(tot), "avg_total_own": round(float(np.mean(tot)), 1) if tot else None,
                "avg_low_owned": round(float(np.mean(low)), 2) if low else None,
                "pct_with_2plus_low": round(100 * float(np.mean([x >= 2 for x in low])), 1) if low else None}

    top_n = max(1, n // 100)
    mine = [e for e in E if me and e["user"].lower() == me.lower()]
    win = E[0]
    return {
        "id": c["id"], "date": c.get("file_date"), "kind": c["kind"], "entries": n,
        "winning_score": win["points"],
        "top1pct_cutoff": E[top_n - 1]["points"],
        "median_score": float(np.median([e["points"] for e in E])),
        "winner": [{"slot": s, "name": nm, "own": own.get(norm_name(nm)), "fpts": c["fpts"].get(norm_name(nm))}
                   for s, nm in win["players"]],
        "winner_total_own": round(sum(lu_own(win)), 1),
        "top1pct": profile(E[:top_n]), "field": profile(E),
        "mine": {"entries": len(mine),
                 "best_rank": min((e["rank"] for e in mine), default=None),
                 "best_pct": round(100 * min(e["rank"] for e in mine) / n, 2) if mine else None,
                 "median_pct": round(100 * float(np.median([e["rank"] for e in mine])) / n, 1) if mine else None,
                 "profile": profile(mine)},
    }


def _snapshot_for(c: dict) -> dict | None:
    """The model's projected ownership for the slate this contest was on (best name overlap)."""
    names = set(c["own"])
    if not names:
        return None
    best, best_score = None, 0.0
    for f in SNAPSHOTS.glob("*/*.json"):
        try:
            snap = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        players = set(snap.get("players", {}))
        hit = len(names & players)
        if hit < 0.8 * len(names):
            continue
        # same slate = the contest's players cover the snapshot too (a main-slate
        # snapshot also contains every night-slate player, so it scores lower here)
        score = hit / max(len(players), 1)
        if c.get("file_date") and snap.get("date") not in (c["file_date"], None):
            score *= 0.5
        if score > best_score:
            best, best_score = snap, score
    return best


def fit_own_calibration(contests: list[dict]) -> dict:
    """log(real %Drafted) vs log(projected %) across every player in every contest,
    shrunk toward no change: weight = n / (n + 600), at most 0.5."""
    xs, ys, matched = [], [], 0
    for c in contests:
        snap = _snapshot_for(c)
        if not snap:
            continue
        matched += 1
        for k, real in c["own"].items():
            p = snap["players"].get(k)
            if p and p[1] > 0:
                xs.append(math.log(p[1] + 0.5))
                ys.append(math.log(real + 0.5))
    n = len(xs)
    if n < 40:
        return {"used": False, "n": n, "contests_matched": matched,
                "note": "needs contest exports from slates the model projected"}
    b, a = np.polyfit(xs, ys, 1)
    w = min(0.5, n / (n + SHRINK_N))
    x, y = np.array(xs), np.array(ys)
    pred_raw, pred_fit = np.exp(x) - 0.5, np.exp(a + b * x) - 0.5
    real = np.exp(y) - 0.5
    return {"used": True, "n": n, "contests_matched": matched, "weight": round(w, 3),
            "slope": round(float(1 + (b - 1) * w), 4), "slope_raw": round(float(b), 4),
            "intercept": round(float(a * w), 4), "intercept_raw": round(float(a), 4),
            "mae_model": round(float(np.abs(pred_raw - real).mean()), 2),
            "mae_fitted_in_sample": round(float(np.abs(pred_fit - real).mean()), 2)}


def load_own_calibration() -> dict:
    try:
        return json.loads(OWN_CAL.read_text())
    except (OSError, ValueError):
        return {}


def snapshot(slate_id: int, day: str, rows: list[dict]) -> None:
    """Keep the model's pre-lock projection/ownership for later comparison (private)."""
    path = SNAPSHOTS / day / f"{slate_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    players = {norm_name(r["name"]): [round(r["proj"], 2), round(r.get("own", 0.0), 2), r["salary"]]
               for r in rows if r.get("proj", 0) > 0}
    path.write_text(json.dumps({"slate_id": slate_id, "date": day, "players": players}))


# ── report ───────────────────────────────────────────────────────────────────
def refresh() -> dict:
    """Import, analyse, write the private report and the ownership correction."""
    if not PRIVATE.parent.exists():
        return {}
    new = import_new()
    contests = load_all()
    if not contests:
        return {"contests": 0}
    me = my_username(contests)
    stats = [contest_stats(c, me) for c in contests]
    cal = fit_own_calibration(contests)
    OWN_CAL.write_text(json.dumps(cal, indent=1))
    (PRIVATE / "report.json").write_text(json.dumps({"me": me, "contests": stats, "own_calibration": cal}, indent=1))
    (PRIVATE / "report.html").write_text(render(stats, me, cal))
    return {"contests": len(contests), "new": new}


def _agg(stats, key, sub):
    vals = [s[key][sub] for s in stats if s[key].get(sub) is not None]
    return round(float(np.mean(vals)), 2) if vals else None


def render(stats: list[dict], me: str | None, cal: dict) -> str:
    e = html.escape
    rows = []
    for s in sorted(stats, key=lambda s: (s.get("date") or "", s["id"]), reverse=True):
        winner = ", ".join(f"{e(p['name'])} ({p['own']:.1f}%)" if p["own"] is not None else e(p["name"])
                           for p in s["winner"])
        m = s["mine"]
        rows.append(f"<tr><td>{e(s.get('date') or '')}</td><td>{s['id']}</td><td>{s['kind']}</td>"
                    f"<td>{s['entries']:,}</td><td>{s['winning_score']:.2f}</td><td>{s['top1pct_cutoff']:.2f}</td>"
                    f"<td>{s['winner_total_own']}</td>"
                    f"<td>{s['top1pct']['avg_low_owned']} / {s['field']['avg_low_owned']}</td>"
                    f"<td>{m['entries']}</td><td>{m['best_rank'] or ''}</td><td>{m['best_pct'] or ''}</td>"
                    f"<td class=w>{winner}</td></tr>")
    summary = (f"<p>Across {len(stats)} contests, the top 1% averaged <b>{_agg(stats, 'top1pct', 'avg_low_owned')}</b> "
               f"players under {LOW_OWN:.0f}% owned (field: {_agg(stats, 'field', 'avg_low_owned')}); "
               f"{_agg(stats, 'top1pct', 'pct_with_2plus_low')}% of top-1% lineups had two or more "
               f"(field: {_agg(stats, 'field', 'pct_with_2plus_low')}%). Average total ownership: top 1% "
               f"{_agg(stats, 'top1pct', 'avg_total_own')}%, field {_agg(stats, 'field', 'avg_total_own')}%.</p>")
    if cal.get("used"):
        calp = (f"<p>Ownership check: {cal['n']} player results from {cal['contests_matched']} contests. "
                f"Model ownership error {cal['mae_model']} pts. Correction slope {cal['slope_raw']} "
                f"applied at weight {cal['weight']} (effective slope {cal['slope']}).</p>")
    else:
        calp = f"<p>Ownership check: {e(cal.get('note', 'not enough data yet'))} ({cal.get('n', 0)} players).</p>"
    who = e(me) if me else "unknown — set SIGMA_DK_USERNAME in ~/.sigma-dfs.env, or export 2+ contests"
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Sigma DFS — my contests</title>
<style>body{{font:14px -apple-system,system-ui,sans-serif;margin:16px;background:#fff;color:#111}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}
th{{background:#f4f4f4;position:sticky;top:0}}td.w{{font-size:12px;color:#444}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}th{{background:#222}}td{{border-color:#333}}td.w{{color:#aaa}}}}</style>
<h1>My contests (private)</h1><p>Your DraftKings username: <b>{who}</b>. Updated {datetime.now(ET):%b %-d %-I:%M %p} ET.</p>
{summary}{calp}
<table><tr><th>Date</th><th>Contest</th><th>Type</th><th>Entries</th><th>Winning score</th><th>Top 1% cutoff</th>
<th>Winner total own %</th><th>Low-owned per lineup (top 1% / field)</th><th>My entries</th><th>My best rank</th><th>Best top %</th><th>Winning lineup</th></tr>
{''.join(rows)}</table>"""
