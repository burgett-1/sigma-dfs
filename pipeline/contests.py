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
import time
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import DATA_DIR, ET
from .names import norm_name

log = logging.getLogger(__name__)

PRIVATE = Path(os.environ.get("SIGMA_PRIVATE_DIR", Path.home() / "SigmaDFS-Private"))
CONTESTS = PRIVATE / "contests"
SNAPSHOTS = PRIVATE / "projections"
OWN_CAL = PRIVATE / "own_calibration.json"
# aggregate-only public file (no usernames, entry ids, lineups or DK contest ids); see contest_history.py
PUBLIC_OUT = DATA_DIR / "contest_history.json"
FILE_RE = re.compile(r"contest-standings-(\d+)", re.I)
SLOT_RE = re.compile(r"(?:^|\s)(CPT|PG|SG|SF|PF|C|G|F|UTIL)\s+")
LOW_OWN = 10.0          # "low owned" means under 10% drafted
SHRINK_N = 600          # player observations needed before the correction gets half its weight

# NFL lineups are re-split with their own slot tokens once detected (NBA parsing is untouched)
SPORT_SLOT_RE = {"NFL": re.compile(r"(?:^|\s)(CPT|QB|RB|WR|TE|FLEX|DST)\s+")}
NBA_SLOTS = {"PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"}
NBA_CORE = {"PG", "SG", "SF", "PF", "G", "F"}
NFL_SLOTS = {"QB", "RB", "WR", "TE", "FLEX", "DST"}
MLB_SLOTS = {"P", "SP", "RP", "1B", "2B", "3B", "SS", "OF"}


def _import_dirs() -> list[Path]:
    raw = os.environ.get("SIGMA_CONTEST_IMPORT", str(Path.home() / "Downloads"))
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p] + [PRIVATE / "drop"]


# ── importing ────────────────────────────────────────────────────────────────
SETTLE_SECONDS = 60        # a file modified more recently may still be downloading
WATCH_EVERY = 60.0         # watch() scans the import folders at most this often
_WATCH = {"at": 0.0}
ENTRY_HISTORY_RE = re.compile(r"entry-history", re.I)


def accepted() -> set[str]:
    """Sports whose contest exports are imported: enabled sports with a pipeline."""
    from . import sports
    return set(sports.enabled()) & {"NBA", "NFL"}


def sport_rosters() -> dict[str, set[str]]:
    """Name keys per sport used to resolve {CPT, FLEX} showdowns: the NFL private
    snapshots plus the nflverse history names."""
    out = {"NFL": snapshot_names(SNAPSHOTS / "nfl")}
    try:
        from .sports.nfl import data as nfl_data
        from .sports.paths import paths
        h = nfl_data.load_history(paths("NFL", DATA_DIR).history)
        out["NFL"] |= {norm_name(str(n)) for n in h.get("name", [])}
    except Exception:  # noqa: BLE001
        pass
    return out


def _accept_sport(res: dict) -> str:
    """The contest's sport. Showdowns share slot names with other sports, so an
    unknown showdown must match the private snapshot names (NBA) or a sport's
    roster names (NFL/NHL) to count."""
    sport = res.get("sport")
    if sport == "UNKNOWN" and res.get("kind") == "showdown":
        sport = detect_sport(res.get("slots_seen", []), res.get("lineup_tokens", []),
                             names=list(res.get("own") or {}), nba_names=snapshot_names(),
                             rosters=sport_rosters() if "FLEX" in res.get("slots_seen", []) else None)
    return sport or "UNKNOWN"


def import_new(now: float | None = None) -> list[str]:
    """Copy new NBA contest-standings exports into the private folder. Returns new ids.

    Skipped (and remembered in index.json "rejected", so they aren't re-read every
    pass; a changed size, mtime or file name gets one retry): 0-byte files,
    partial downloads (.crdownload/.download/.part, or modified < 60 s ago),
    zips without a CSV, files parse() rejects, and non-NBA contests. Originals
    are never moved or deleted."""
    now = time.time() if now is None else now
    CONTESTS.mkdir(parents=True, exist_ok=True)
    (PRIVATE / "drop").mkdir(exist_ok=True)
    have = {m.group(1) for f in CONTESTS.glob("*.csv") if (m := FILE_RE.search(f.name))}
    meta = _meta()
    rejected = dict(meta.get("rejected") or {})
    ok_sports = accepted()
    dirty = False
    new = []
    for d in _import_dirs():
        try:
            files = sorted(d.glob("contest-standings-*"))
        except OSError as e:       # macOS may need permission for ~/Downloads
            log.info("cannot read %s: %s", d, e)
            continue
        for f in files:
            m = FILE_RE.search(f.name)
            if not m or m.group(1) in have or ENTRY_HISTORY_RE.search(f.name):
                continue
            cid = m.group(1)
            try:
                st = f.stat()
            except OSError:
                continue
            sig = {"file": f.name, "size": st.st_size, "mtime": int(st.st_mtime)}
            prev = rejected.get(cid)
            retry = bool(prev and str(prev.get("reason", "")).startswith("not_nba:")
                         and prev["reason"].split(":", 1)[1] in ok_sports and not prev.get("retried"))
            if prev and all(prev.get(k) == v for k, v in sig.items()) and not retry:
                continue                                   # same file already rejected
            reason = None
            if f.suffix.lower() not in (".csv", ".zip"):
                reason = "partial"
            elif st.st_size == 0:
                reason = "empty"
            elif now - st.st_mtime < SETTLE_SECONDS:
                continue                                   # still being written: look again next pass
            else:
                try:
                    text = _read_export(f)
                except (OSError, zipfile.BadZipFile, UnicodeDecodeError, KeyError):
                    text, reason = None, "unreadable"
                if reason is None and not text:
                    reason = "no_csv"
                res = parse(text) if reason is None else None
                if reason is None and res is None:
                    reason = "malformed"
                sport = None
                if reason is None:
                    sport = _accept_sport(res)
                    if sport not in ok_sports:
                        reason = "not_nba:" + sport
            if reason:
                rejected[cid] = {"reason": reason, **sig, **({"retried": True} if retry else {})}
                dirty = True
                log.info("contest export %s not imported: %s", f.name, reason)
                continue
            (CONTESTS / f"contest-standings-{cid}.csv").write_text(text, encoding="utf-8")
            stamp = datetime.fromtimestamp(st.st_mtime, ET).date().isoformat()
            _meta_update(cid, {"imported_from": f.name, "file_date": stamp, "sport": sport})
            if rejected.pop(cid, None) is not None:
                dirty = True
            have.add(cid)
            new.append(cid)
    if dirty:
        m2 = _meta()
        m2["rejected"] = rejected
        _meta_path().write_text(json.dumps(m2, indent=1))
    return new


def watch(now: float | None = None) -> list[str]:
    """Called every live-loop iteration: at most one scan per minute; when new
    exports were imported, refresh the private report, the ownership calibration
    and the public contest_history.json. Returns the new ids."""
    now = time.time() if now is None else now
    if now - _WATCH["at"] < WATCH_EVERY:
        return []
    _WATCH["at"] = now
    if not PRIVATE.parent.exists():
        return []
    new = import_new()                  # file ages are judged against the real clock
    if new:
        log.info("imported contest exports %s", new)
        refresh()
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
def split_lineup(s: str, slot_re: re.Pattern | None = None) -> list[tuple[str, str]]:
    """'C Nikola Jokic F LeBron James ...' -> [('C','Nikola Jokic'), ('F','LeBron James'), ...]"""
    s = " " + (s or "").strip() + " "
    marks = list((slot_re or SLOT_RE).finditer(s))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(s)
        name = s[m.end():end].strip()
        if name and name.upper() != "LOCKED":
            out.append((m.group(1), name))
    return out


def parse(text: str) -> dict | None:
    """DraftKings contest standings export -> entries and the player %Drafted table.

    The player section has one row per (player, roster slot), so a player's real
    ownership is the SUM of his rows (e.g. PG 28.73 + G 19.73 + UTIL 4.17 = 52.63)."""
    text = (text or "").lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or "Rank" not in rows[0] or "Lineup" not in rows[0]:
        return None
    head = [h.strip().lstrip("\ufeff") for h in rows[0]]
    ix = {h: i for i, h in enumerate(head) if h}
    own, own_slots, fpts, names, slots_seen = {}, {}, {}, {}, set()
    if "Player" in ix and "%Drafted" in ix:
        for r in rows[1:]:
            if len(r) <= ix["%Drafted"] or not r[ix["Player"]].strip():
                continue
            try:
                pct = float(r[ix["%Drafted"]].strip().rstrip("%"))
            except ValueError:
                continue
            if not math.isfinite(pct):
                continue
            player = r[ix["Player"]].strip()
            k = norm_name(player)
            names.setdefault(k, player)
            own[k] = own.get(k, 0.0) + pct
            slot = r[ix["Roster Position"]].strip().upper() if "Roster Position" in ix and len(r) > ix["Roster Position"] else ""
            if slot:
                slots_seen.add(slot)
                d = own_slots.setdefault(k, {})
                d[slot] = round(d.get(slot, 0.0) + pct, 4)
            if "FPTS" in ix and len(r) > ix["FPTS"]:
                try:
                    fpts[k] = float(r[ix["FPTS"]] or 0)
                except ValueError:
                    pass
    own = {k: round(v, 4) for k, v in own.items()}
    entries = []
    for r in rows[1:]:
        if "Rank" not in ix or len(r) <= ix["Lineup"] or not r[ix["Rank"]].strip():
            continue
        try:
            rank, pts = int(r[ix["Rank"]]), float(r[ix["Points"]])
        except (ValueError, KeyError, IndexError):
            continue
        name = r[ix["EntryName"]].strip() if "EntryName" in ix else ""
        user = re.sub(r"\s*\(\d+/\d+\)\s*$", "", name)
        entries.append({"rank": rank, "points": pts, "user": user, "entry": name,
                        "players": split_lineup(r[ix["Lineup"]]), "_lineup": r[ix["Lineup"]]})
    if not entries:
        return None
    entries.sort(key=lambda e: e["rank"])
    tokens = {s for e in entries[:200] for s, _ in e["players"]}
    kind = "showdown" if "CPT" in slots_seen or any(s == "CPT" for e in entries[:50] for s, _ in e["players"]) else "classic"
    sport = detect_sport(slots_seen, tokens)
    # NFL (and {CPT, FLEX} showdowns, never NBA) use their own slot tokens
    resplit = SPORT_SLOT_RE.get(sport) or (SPORT_SLOT_RE["NFL"] if sport == "UNKNOWN" and "FLEX" in slots_seen else None)
    if resplit is not None:
        for e in entries:
            e["players"] = split_lineup(e.pop("_lineup", ""), resplit)
    for e in entries:
        e.pop("_lineup", None)
    return {"entries": entries, "own": own, "fpts": fpts, "kind": kind, "own_slots": own_slots,
            "names": names, "slots_seen": sorted(slots_seen), "lineup_tokens": sorted(tokens),
            "sport": sport,
            "own_total": round(sum(own.values()), 2)}


def detect_sport(slots_seen, lineup_tokens=(), names=None, nba_names=None, rosters=None) -> str:
    """DraftKings sport code ("NBA", "NFL", "CFB", "NHL", "MLB", "GOLF", "MMA", "NAS"),
    "OTHER" or "UNKNOWN", from the roster slots a contest uses. In order:
    S-FLEX -> CFB; DST/TE, or QB with FLEX -> NFL; exactly {G} / {F} / {D} -> GOLF /
    MMA / NAS; W or D -> NHL; MLB slots -> MLB; showdowns; then the NBA rule.

    Showdown slots are shared between sports: {CPT, UTIL} is NBA only if at least
    half its players are in a known NBA name set; {CPT, FLEX} is NFL or NHL by the
    same test against `rosters` ({sport: name keys}); otherwise "UNKNOWN"."""
    s = {str(x).strip().upper() for x in (set(slots_seen) | set(lineup_tokens)) if str(x).strip()}
    if not s:
        return "UNKNOWN"
    if "S-FLEX" in s:
        return "CFB"
    if s & {"DST", "TE"} or {"QB", "FLEX"} <= s:
        return "NFL"
    for only, sport in (({"G"}, "GOLF"), ({"F"}, "MMA"), ({"D"}, "NAS")):
        if s == only:
            return sport
    if s & {"W", "D"}:
        return "NHL"
    if s & MLB_SLOTS:
        return "MLB"
    if "CPT" in s and s <= {"CPT", "FLEX"}:
        keys = {norm_name(n) for n in names or ()}
        for sport in ("NFL", "NHL"):
            known = set((rosters or {}).get(sport) or ())
            if keys and known and len(keys & known) >= 0.5 * len(keys):
                return sport
        return "UNKNOWN"
    if "CPT" in s and s <= {"CPT", "UTIL"}:
        if names and nba_names:
            keys = {norm_name(n) for n in names}
            if keys and len(keys & set(nba_names)) >= 0.5 * len(keys):
                return "NBA"
        return "UNKNOWN"
    if s <= NBA_SLOTS and len(s & NBA_CORE) >= 3:
        return "NBA"
    return "OTHER"


def is_nba(c: dict) -> bool:
    """Contests parsed before sport detection existed (no "sport" key) count as NBA."""
    return c.get("sport", "NBA") == "NBA"


def snapshot_names(snapshots_dir: Path | None = None) -> set[str]:
    """Every player key in the private projection snapshots (all NBA)."""
    out: set[str] = set()
    for f in Path(snapshots_dir or SNAPSHOTS).glob("*/*.json"):
        try:
            out |= set(json.loads(f.read_text()).get("players", {}))
        except (OSError, ValueError, AttributeError):
            continue
    return out


def load_all() -> list[dict]:
    out, meta = [], _meta()
    for f in sorted(CONTESTS.glob("contest-standings-*.csv")):
        cid = FILE_RE.search(f.name).group(1)
        try:
            res = parse(f.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
        if res:
            out.append({"id": cid, **meta.get(cid, {}), **res})
    if any(c.get("sport") == "UNKNOWN" and c.get("kind") == "showdown" for c in out):
        nba = snapshot_names()
        rosters = sport_rosters() if any("FLEX" in c.get("slots_seen", []) for c in out) else None
        for c in out:
            if c.get("sport") == "UNKNOWN" and c.get("kind") == "showdown":
                c["sport"] = detect_sport(c.get("slots_seen", []), c.get("lineup_tokens", []),
                                          names=list(c["own"]), nba_names=nba, rosters=rosters)
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
def contest_stats(c: dict, me: str | None) -> dict | None:
    """Private per-contest summary. Non-NBA contests return None."""
    if not is_nba(c):
        return None
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
    """log(real %Drafted) vs log(projected %) across every player in every NBA contest,
    shrunk toward no change: weight = n / (n + 600), at most 0.5."""
    pairs, matched = [], 0
    for c in contests:
        if not is_nba(c):
            continue
        snap = _snapshot_for(c)
        if not snap:
            continue
        matched += 1
        for k, real in c["own"].items():
            p = snap["players"].get(k)
            if p and p[1] > 0:
                pairs.append((p[1], real))
    return fit_from_pairs(pairs, matched)


def fit_from_pairs(pairs: list[tuple[float, float]], matched: int = 0) -> dict:
    """The calibration fit on (projected own %, real own %) pairs; projected must be > 0."""
    xs = [math.log(p + 0.5) for p, _ in pairs]
    ys = [math.log(r + 0.5) for _, r in pairs]
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


def snapshot(slate_id: int, day: str, rows: list[dict], sport: str = "NBA") -> None:
    """Keep the model's pre-lock projection/ownership for later comparison (private).
    NBA: projections/{day}/{id}.json (as always); other sports: projections/{sport}/{day}/."""
    base = SNAPSHOTS if sport == "NBA" else SNAPSHOTS / sport.lower()
    path = base / day / f"{slate_id}.json"
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
    me = my_username(contests)          # all sports: it is the same DraftKings account
    nba = [c for c in contests if is_nba(c)]
    stats = [s for s in (contest_stats(c, me) for c in nba) if s]
    cal = fit_own_calibration(nba)
    skipped = len(contests) - len(nba)
    OWN_CAL.write_text(json.dumps(cal, indent=1))
    (PRIVATE / "report.json").write_text(json.dumps({"me": me, "contests": stats, "own_calibration": cal,
                                                     "skipped_non_nba": skipped}, indent=1))
    (PRIVATE / "report.html").write_text(render(stats, me, cal, skipped))
    try:
        from . import contest_history
        contest_history.write_public(contests, SNAPSHOTS, PUBLIC_OUT)
    except Exception as e:      # the public summary must never break the live loop
        log.warning("contest_history: %s", e)
    if "NFL" in accepted():
        try:
            refresh_nfl(contests)
        except Exception as e:  # noqa: BLE001 - never break the live loop
            log.warning("NFL contest history: %s", e)
    return {"contests": len(contests), "new": new}


def refresh_nfl(contests: list[dict]) -> dict:
    """NFL: the public aggregate data/nfl/contest_history.json and the private
    ownership-shape calibration (own_calibration_nfl.json, no names)."""
    from . import contest_history
    from .sports.nfl import data as nfl_data
    from .sports.nfl import ownership as nfl_own
    from .sports.paths import paths
    hist = nfl_data.load_history(paths("NFL", PUBLIC_OUT.parent).history)
    games = None
    try:
        games = nfl_data.games(None) if (paths("NFL").cache / "games.csv").exists() else None
    except Exception:  # noqa: BLE001
        games = None
    nfl = [c for c in contests if c.get("sport") == "NFL"]
    doc = contest_history.write_public(contests, SNAPSHOTS / "nfl", PUBLIC_OUT.parent / "nfl" / "contest_history.json",
                                       sport="NFL", context=contest_history.nfl_context(hist, games))
    cal = nfl_own.calibrate(nfl, hist, games)
    (PRIVATE / "own_calibration_nfl.json").write_text(json.dumps(cal, indent=1))
    return doc


def _agg(stats, key, sub):
    vals = [s[key][sub] for s in stats if s[key].get(sub) is not None]
    return round(float(np.mean(vals)), 2) if vals else None


def render(stats: list[dict], me: str | None, cal: dict, skipped: int = 0) -> str:
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
    skip_note = f"<p>Skipped {skipped} non-NBA contest files.</p>" if skipped else ""
    who = e(me) if me else "unknown — set SIGMA_DK_USERNAME in ~/.sigma-dfs.env, or export 2+ contests"
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Sigma DFS — my contests</title>
<style>body{{font:14px -apple-system,system-ui,sans-serif;margin:16px;background:#fff;color:#111}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}
th{{background:#f4f4f4;position:sticky;top:0}}td.w{{font-size:12px;color:#444}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}th{{background:#222}}td{{border-color:#333}}td.w{{color:#aaa}}}}</style>
<h1>My contests (private)</h1><p>Your DraftKings username: <b>{who}</b>. Updated {datetime.now(ET):%b %-d %-I:%M %p} ET.</p>
{summary}{calp}{skip_note}
<table><tr><th>Date</th><th>Contest</th><th>Type</th><th>Entries</th><th>Winning score</th><th>Top 1% cutoff</th>
<th>Winner total own %</th><th>Low-owned per lineup (top 1% / field)</th><th>My entries</th><th>My best rank</th><th>Best top %</th><th>Winning lineup</th></tr>
{''.join(rows)}</table>"""
