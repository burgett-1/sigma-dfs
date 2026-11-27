"""Public, aggregate-only history of DraftKings NBA contests (data/contest_history.json).

Built from the private contest-standings exports (see contests.py). The output is
safe to publish: it holds contest-level score/ownership profiles and player-level
ownership only. It never contains usernames, entry ids, per-entry lineups, the
owner's own results, or DraftKings contest ids (contests are keyed c1..cN in date
order and entry counts are rounded), so the set of contests the owner played can't
be matched back to a DraftKings account.

    python -m pipeline.contest_history --src DIR [--src DIR ...] [--snapshots DIR] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import contests as C
from .config import DATA_DIR, ET
from .names import norm_name

log = logging.getLogger(__name__)

VERSION = 1
COLUMNS = ["contest", "name", "own", "own_by_slot", "fpts", "proj_own", "proj", "salary"]
OWN_BUCKETS = [("0–5%", 0, 5), ("5–10%", 5, 10), ("10–20%", 10, 20), ("20–35%", 20, 35), ("35%+", 35, None)]
SALARY_TIERS = [("<4k", 0, 4000), ("4–6k", 4000, 6000), ("6–8k", 6000, 8000), ("8–10k", 8000, 10000),
                ("10k+", 10000, None)]
MAX_ENTRY_RE = re.compile(r"\((\d+)/(\d+)\)\s*$")
NOTE = "Aggregate results from DraftKings contest exports. No usernames, entry IDs or lineups."


# ── loading ──────────────────────────────────────────────────────────────────
def _file_dates(folder: Path) -> dict:
    """contest id -> file_date from a private index.json next to the files (if any)."""
    out = {}
    for p in (folder / "index.json", C.CONTESTS / "index.json"):
        try:
            for cid, m in json.loads(p.read_text()).items():
                if isinstance(m, dict) and m.get("file_date"):
                    out.setdefault(str(cid), m["file_date"])
        except (OSError, ValueError, AttributeError):
            continue
    return out


def _read(f: Path) -> str:
    if f.suffix.lower() == ".zip":
        with zipfile.ZipFile(f) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
            return z.read(name).decode("utf-8-sig", errors="replace") if name else ""
    return f.read_bytes().decode("utf-8-sig", errors="replace")


def load_dir(paths: list[Path]) -> list[dict]:
    """Parse every contest-standings-*.csv/.zip in the given folders.

    Returns parsed contests ({"id", "file_date", **contests.parse(...)}) and, for
    files that could not be used, {"skipped": reason}. Never raises per file.
    Duplicate contest ids (same export in two folders) are kept once."""
    out, seen = [], set()
    for folder in paths:
        folder = Path(folder).expanduser()
        try:
            files = sorted(f for f in folder.glob("contest-standings-*")
                           if f.suffix.lower() in (".csv", ".zip"))
        except OSError:
            continue
        dates = _file_dates(folder)
        for f in files:
            m = C.FILE_RE.search(f.name)
            if not m:
                continue
            cid = m.group(1)
            if cid in seen:
                continue
            try:
                text = _read(f)
            except (OSError, zipfile.BadZipFile, UnicodeDecodeError, KeyError):
                out.append({"skipped": "malformed"})
                continue
            if not text.strip():
                out.append({"skipped": "empty"})
                continue
            try:
                res = C.parse(text)
            except Exception:          # a broken file must never stop the build
                res = None
            if res is None:
                first = text.splitlines()[0] if text.splitlines() else ""
                out.append({"skipped": "empty" if ("Rank" in first and "Lineup" in first) else "malformed"})
                continue
            seen.add(cid)              # only a successful parse claims the id (a corrupt copy elsewhere can't hide a good one)
            fd = dates.get(cid)
            if not fd:
                try:
                    fd = datetime.fromtimestamp(f.stat().st_mtime, ET).date().isoformat()
                except OSError:
                    fd = None
            out.append({"id": cid, "file_date": fd, **res})
    return out


def _snapshots(snapshots_dir: Path | None) -> list[dict]:
    snaps = []
    if not snapshots_dir:
        return snaps
    for f in sorted(Path(snapshots_dir).expanduser().glob("*/*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        players = d.get("players") if isinstance(d, dict) else None
        if isinstance(players, dict) and players:
            snaps.append({"date": d.get("date") or f.parent.name, "players": players})
    return snaps


def _match(c: dict, snaps: list[dict]) -> tuple[dict | None, float]:
    """Best snapshot for the contest's slate (same rule as contests._snapshot_for):
    80%+ of the contest's players must be in it; prefer snapshots the contest covers
    best, and snapshots from the slate night (file date or the day before)."""
    names = set(c.get("own") or {})
    if not names:
        return None, 0.0
    fd = c.get("file_date")
    near = set()
    if fd:
        try:
            d = date.fromisoformat(fd)
            near = {fd, (d - timedelta(days=1)).isoformat()}
        except ValueError:
            pass
    best, best_score, best_cov = None, 0.0, 0.0
    for s in snaps:
        players = set(s["players"])
        hit = len(names & players)
        if hit < 0.8 * len(names):
            continue
        cov = hit / max(len(players), 1)
        score = cov * (1.0 if not near or s["date"] in near else 0.5)
        if score > best_score:
            best, best_score, best_cov = s, score, cov
    return best, best_cov


# ── building ─────────────────────────────────────────────────────────────────
def _sig2(n: int) -> int:
    """Round to 2 significant figures (70,198 -> 70,000; 583 -> 580; 32 -> 32)."""
    if n < 100:
        return int(n)
    d = 10 ** (int(math.floor(math.log10(n))) - 1)
    return int(round(n / d) * d)


def _r(v, nd=2):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), nd)


def _profile(entries: list[dict], own: dict, salary: dict | None) -> dict:
    tot, low, sal = [], [], []
    for e in entries:
        keys = [norm_name(nm) for _, nm in e["players"]]
        vals = [own.get(k) for k in keys]
        if keys and all(v is not None for v in vals):
            tot.append(sum(vals))
            low.append(sum(1 for v in vals if v < C.LOW_OWN))
        if salary and keys:
            ss = [salary.get(k) for k in keys]
            if all(isinstance(x, (int, float)) and x > 0 for x in ss):
                sal.append(sum(ss))
    return {"avg_total_own": _r(np.mean(tot), 1) if tot else None,
            "avg_low_owned": _r(np.mean(low), 2) if low else None,
            "pct_2plus_low": _r(100 * np.mean([x >= 2 for x in low]), 1) if low else None,
            "avg_salary": int(round(float(np.mean(sal)))) if sal else None}


def _scores(pts: list[float]) -> dict:
    a = np.sort(np.array(pts, dtype=float))[::-1]
    n = len(a)
    top1 = a[max(1, n // 100) - 1]
    top10 = a[max(1, n // 10) - 1]
    lo = math.floor(a.min() / 10) * 10
    hi = math.floor(a.max() / 10) * 10
    counts = [0] * (int((hi - lo) / 10) + 1)
    for v in a:
        counts[min(len(counts) - 1, int((v - lo) // 10))] += 1
    return {"win": _r(a[0]), "top1pct": _r(top1), "top10pct": _r(top10),
            "p75": _r(np.percentile(a, 75)), "median": _r(np.median(a)), "p25": _r(np.percentile(a, 25)),
            "hist": {"start": int(lo), "step": 10, "counts": counts}}


def _bucket_rows(pairs, spec, key) -> list[dict]:
    out = []
    for label, lo, hi in spec:
        sel = [p for p in pairs if p[key] is not None and p[key] >= lo and (hi is None or p[key] < hi)]
        row = {"label": label, "lo": lo, "hi": hi, "n": len(sel),
               "mean_proj": _r(np.mean([p["proj"] for p in sel])) if sel else None,
               "mean_actual": _r(np.mean([p["actual"] for p in sel])) if sel else None}
        out.append(row)
    return out


def _err(sel) -> dict:
    if not sel:
        return {"n": 0, "mae": None, "bias": None}
    d = np.array([p["proj"] - p["actual"] for p in sel])
    return {"n": len(sel), "mae": _r(np.abs(d).mean()), "bias": _r(d.mean())}


def build(contests: list[dict], snapshots_dir: Path | None, nba_names: set[str] | None = None) -> dict:
    """Parsed contests -> the public document (schema version 1). Pure apart from
    reading the snapshot folder."""
    snaps = _snapshots(snapshots_dir)
    known = set(nba_names or ())
    for s in snaps:
        known |= set(s["players"])
    skipped: dict[tuple, int] = {}
    keep, seen = [], set()
    for c in contests or []:
        if not isinstance(c, dict):
            continue
        if c.get("skipped"):
            k = (c["skipped"], c.get("detected"))
            skipped[k] = skipped.get(k, 0) + 1
            continue
        cid = str(c.get("id", ""))
        if cid and cid in seen:
            continue
        seen.add(cid)
        sport = c.get("sport") or C.detect_sport(c.get("slots_seen", []), c.get("lineup_tokens", []))
        if sport == "UNKNOWN" and c.get("kind") == "showdown":
            sport = C.detect_sport(c.get("slots_seen", []), c.get("lineup_tokens", []),
                                   names=list(c.get("own") or {}), nba_names=known)
        if sport != "NBA":
            k = ("unknown_sport" if sport == "UNKNOWN" else "not_nba", sport)
            skipped[k] = skipped.get(k, 0) + 1
            continue
        entries = c.get("entries") or []
        scored = [e for e in entries if e.get("players")]
        if not scored:
            skipped[("empty", "NBA")] = skipped.get(("empty", "NBA"), 0) + 1
            continue
        keep.append((c, entries, scored))

    rows_out, docs, pairs, nights = [], [], [], set()
    staged = []
    for c, entries, scored in keep:
        snap, cov = _match(c, snaps)
        fd = c.get("file_date")
        if snap:
            day, src = snap["date"], "snapshot"
        else:
            src = "file"
            try:
                day = (date.fromisoformat(fd) - timedelta(days=1)).isoformat() if fd else None
            except ValueError:
                day = None
        staged.append((day or "", str(c.get("id", "")), c, entries, scored, snap, cov, day, src))
    staged.sort(key=lambda t: (t[0], t[1]))

    for i, (_, _, c, entries, scored, snap, cov, day, src) in enumerate(staged, 1):
        key = f"c{i}"
        own = c.get("own") or {}
        max_n = 1
        for e in entries:
            m = MAX_ENTRY_RE.search(e.get("entry") or "")
            if m:
                max_n = max(max_n, int(m.group(2)))
        n = len(entries)
        bucket = "single-entry" if max_n == 1 else ("small" if n < 1000 else "mid" if n <= 20000 else "large")
        sp = snap["players"] if snap else {}
        salary = {k: v[2] for k, v in sp.items() if isinstance(v, list) and len(v) > 2} if snap else None
        scored.sort(key=lambda e: e["rank"])
        top = scored[:max(1, len(scored) // 100)]
        docs.append({
            "key": key, "date": day, "date_source": src, "kind": c.get("kind", "classic"),
            "size_bucket": bucket, "entries_approx": _sig2(n), "max_entries": max_n,
            "scored_entries_pct": _r(100 * len(scored) / n, 1) if n else None,
            "players": len(own), "own_total": _r(sum(own.values()), 1),
            "scores": _scores([e["points"] for e in scored]),
            "top1pct": _profile(top, own, salary), "field": _profile(scored, own, salary),
            "snapshot_match": {"coverage": _r(cov, 2)} if snap else None,
        })
        if day:
            nights.add(day)
        names = c.get("names") or {}
        slots = c.get("own_slots") or {}
        fpts = c.get("fpts") or {}
        for k, o in sorted(own.items(), key=lambda kv: -kv[1]):
            p = sp.get(k) if snap else None
            p = p if isinstance(p, list) and len(p) >= 3 else None
            rows_out.append([key, names.get(k, k), _r(o), {s: _r(v) for s, v in (slots.get(k) or {}).items()},
                             _r(fpts.get(k)), _r(p[1]) if p else None, _r(p[0]) if p else None,
                             p[2] if p else None])
        if snap:
            for k, p in sp.items():
                if not (isinstance(p, list) and len(p) >= 3):
                    continue
                if k in own:
                    actual = own[k]
                elif cov >= 0.6:
                    actual = 0.0
                else:
                    continue
                pairs.append({"proj": float(p[1]), "actual": float(actual), "salary": p[2],
                              "size": "single-entry" if bucket == "single-entry" else "multi"})

    fit = C.fit_from_pairs([(p["proj"], p["actual"]) for p in pairs if p["proj"] > 0],
                           sum(1 for d in docs if d["snapshot_match"]))
    err = _err(pairs)
    calibration = {
        "n": len(pairs), "contests_matched": sum(1 for d in docs if d["snapshot_match"]),
        "mae": err["mae"], "bias": err["bias"], "bias_def": "projected minus actual, % points",
        "buckets": _bucket_rows(pairs, OWN_BUCKETS, "proj"),
        "by_salary": _bucket_rows(pairs, SALARY_TIERS, "salary"),
        "by_size": {"single-entry": _err([p for p in pairs if p["size"] == "single-entry"]),
                    "multi": _err([p for p in pairs if p["size"] == "multi"])},
        "fit": {"used": bool(fit.get("used")), "slope_raw": fit.get("slope_raw"), "weight": fit.get("weight"),
                "slope": fit.get("slope"), "intercept": fit.get("intercept")},
    }
    return {
        "version": VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sport": "NBA",
        "note": NOTE,
        "contests": docs,
        "players": {"columns": COLUMNS, "rows": rows_out},
        "calibration": calibration,
        "skipped": [{"reason": r, "detected": d, "count": n} for (r, d), n in sorted(skipped.items(), key=str)],
        "samples": {"contests": len(docs), "player_rows": len(rows_out), "nights": len(nights)},
    }


def _same_content(a: dict, b: dict) -> bool:
    """Equal apart from generated_at."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return {k: v for k, v in a.items() if k != "generated_at"} == {k: v for k, v in b.items() if k != "generated_at"}


def write_public(contests: list[dict], snapshots_dir: Path | None, out_path: Path) -> dict:
    """Build and write atomically (tmp file then rename).

    Skips the write when the existing file differs only in generated_at, so the
    live loop doesn't create a data-branch commit every pass. The returned doc
    carries no extra keys; whether it was written is logged and available as
    write_public.last_written (True/False)."""
    doc = build(contests, snapshots_dir)
    out_path = Path(out_path)
    try:
        old = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    # compare through a JSON round-trip so tuples/floats match what is on disk
    if old is not None and _same_content(old, json.loads(json.dumps(doc))):
        write_public.last_written = False
        log.info("contest_history unchanged; not rewritten")
        return doc
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))
    os.replace(tmp, out_path)
    write_public.last_written = True
    log.info("contest_history written: %s contests", doc["samples"]["contests"])
    return doc


write_public.last_written = None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", action="append", required=True, help="folder with contest-standings-*.csv/.zip")
    ap.add_argument("--snapshots", default=str(C.SNAPSHOTS), help="private projection snapshots folder")
    ap.add_argument("--out", default=str(DATA_DIR / "contest_history.json"))
    a = ap.parse_args(argv)
    loaded = load_dir([Path(p) for p in a.src])
    doc = write_public(loaded, Path(a.snapshots).expanduser(), Path(a.out).expanduser())
    sk = sum(s["count"] for s in doc["skipped"])
    print(f"contest_history: {doc['samples']['contests']} NBA contests, {sk} skipped, "
          f"{doc['samples']['player_rows']} player rows, {doc['calibration']['n']} calibration pairs -> {a.out}"
          f" ({'written' if write_public.last_written else 'unchanged, not rewritten'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
