"""DraftKings public lobby + draftables endpoints (salaries, positions, start times).

Every sport in the registry (pipeline/sports) goes through the same code:
`list_draft_groups(now, sport)` filters the lobby by (sport, GameTypeId) and
keeps the ContestTypeId needed for the salary-CSV URL (they differ for NFL
classic: gt 1 / ct 21); `load_salary_csv(slate, text, sport)` reads slot
eligibility from the CSV's Roster Position column. NBA (the default) runs
exactly the code path it always has.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import ET
from .. import health
from ..http import get_json, get_text
from ..names import team_code

log = logging.getLogger(__name__)

LOBBY_BASE = "https://www.draftkings.com/lobby/getcontests"
LOBBY_URL = LOBBY_BASE + "?sport=NBA"
DRAFTABLES_URL = "https://api.draftkings.com/draftgroups/v1/draftgroups/{dg}/draftables"
# The salary file DraftKings offers on every contest page ("Export to CSV"): names,
# positions, salaries, games and the player IDs DraftKings uses for lineup uploads.
SALARY_CSV_URL = "https://www.draftkings.com/lineup/getavailableplayerscsv?contestTypeId={ct}&draftGroupId={dg}"
# DraftKings contest types we build for. Others in the NBA lobby (Tiers, "Points"
# scoring, second-half, in-game, pick'em…) use different rules and are skipped.
# (NBA view of the registry's game_types; other sports read the registry.)
CONTEST_TYPES = {70: "classic", 81: "showdown"}
MIN_CSV_BYTES = 200        # DraftKings serves a header-only file until a group is populated

seen: list[dict] = []  # NBA draft groups the lobby listed on the last call (for meta.json)
seen_by_sport: dict[str, list[dict]] = {"NBA": seen}
# contest styles we don't build for (showdown IS supported; it's detected from the draftables)
_SKIP_WORDS = ("tiers", "snake", "pick6", "in-game", "best ball", "arcade")


def _cfg(sport: str) -> dict:
    from .. import sports
    return sports.get(sport)


def lobby_url(sport: str = "NBA") -> str:
    return LOBBY_URL if sport == "NBA" else f"{LOBBY_BASE}?sport={_cfg(sport).get('dk_sport', sport)}"


@dataclass
class Slate:
    draft_group_id: int
    name: str
    start_utc: datetime
    game_count: int
    featured: bool
    kind: str = "classic"          # "classic" or "showdown" (CPT + 5 UTIL)
    contest_type: int = 70
    players: list[dict] = field(default_factory=list)
    games: list[dict] = field(default_factory=list)
    excluded: dict = field(default_factory=dict)   # fail-closed exclusions by reason (registry sports)


def parse_dt(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    s = str(raw)
    if s.startswith("/Date("):  # "/Date(1731715200000)/"
        return datetime.fromtimestamp(int(s[6:-2].split("-")[0]) / 1000, tz=timezone.utc)
    s = s.replace("Z", "+00:00")
    # trim 7-digit fractional seconds DK sometimes sends
    if "." in s:
        head, tail = s.split(".", 1)
        frac = "".join(ch for ch in tail if ch.isdigit())
        tz = tail[len(frac):]
        s = f"{head}.{frac[:6]}{tz}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def list_classic_draft_groups(now: datetime) -> list[Slate]:
    """Kept for compatibility: every NBA draft group (classic and showdown) for today."""
    return list_draft_groups(now)


def list_draft_groups(now: datetime, sport: str = "NBA") -> list[Slate]:
    """Draft groups (classic + showdown) for `sport` inside its lobby window:
    NBA ("dfs_day"): first game on the current ET 'DFS day'; "lookahead" sports:
    now - keep_after_start_hours <= start < now + lookahead_hours.
    Note: DraftKings removes a group from the lobby once it locks; the runners
    remember the groups they have seen so late swap keeps working after lock."""
    cfg = _cfg(sport)
    lob = cfg.get("lobby") or {}
    game_types = cfg.get("game_types") or {}
    skip = tuple(lob.get("skip_words") or _SKIP_WORDS)
    try:
        data = get_json(lobby_url(sport))
        health.ok("DraftKings lobby")
    except Exception as e:  # noqa: BLE001
        health.fail("DraftKings lobby", e)
        raise
    groups = data.get("DraftGroups") or data.get("draftGroups") or []
    if lob.get("window", "dfs_day") == "dfs_day":
        day = dfs_day(now)
        start_win = datetime.combine(day, datetime.min.time(), ET) + timedelta(hours=6)
        end_win = start_win + timedelta(hours=24)
    else:
        start_win = now - timedelta(hours=float(lob.get("keep_after_start_hours", 0)))
        end_win = now + timedelta(hours=float(lob.get("lookahead_hours", 24)))
    classic_ct = int(next((v.get("csv_ct") for v in game_types.values() if v.get("kind") == "classic"), 70) or 70)
    out: list[Slate] = []
    seen_list = seen_by_sport.setdefault(sport, [])
    seen_list.clear()
    for g in groups:
        seen_list.append({k: g.get(k) for k in ("DraftGroupId", "ContestTypeId", "GameTypeId", "GameCount",
                                                "DraftGroupTag", "ContestStartTimeSuffix", "StartDate")})
        tag = " ".join(str(g.get(k) or "") for k in (
            "DraftGroupTag", "ContestStartTimeSuffix", "GameType", "ContestTypeName")).lower()
        if any(w in tag for w in skip):
            continue
        gt = g.get("GameTypeId") if g.get("GameTypeId") is not None else g.get("ContestTypeId")
        ct = g.get("ContestTypeId") or gt
        if gt is not None:
            gcfg = game_types.get(str(int(gt)))
            if gcfg is None:
                continue
            kind = gcfg.get("kind", "classic")
            contest_type = int(gcfg.get("csv_ct") or ct)
        else:                                   # no type ids at all: treated as classic
            kind, contest_type = "classic", classic_ct
        start = parse_dt(g.get("StartDate") or g.get("StartDateEst") or g.get("MinStartTime"))
        if not start or not (start_win <= start < end_win):
            continue
        suffix = (g.get("ContestStartTimeSuffix") or "").strip(" ()")
        name = suffix or ("Main" if g.get("DraftGroupTag") == "Featured" else "Classic")
        if int(g.get("GameCount") or 0) == 1 and " vs " not in name and "@" not in name:
            name = name if name not in ("Classic", "Main") else "Single game"
        out.append(Slate(
            draft_group_id=int(g["DraftGroupId"]),
            name=name,
            start_utc=start,
            game_count=int(g.get("GameCount") or 0),
            featured=g.get("DraftGroupTag") == "Featured",
            kind=kind,
            contest_type=contest_type,
        ))
    # de-duplicate (the lobby lists one row per draft group, but be safe)
    uniq = {s.draft_group_id: s for s in out}
    return sort_slates(list(uniq.values()))


def sort_slates(slates: list[Slate]) -> list[Slate]:
    return sorted(slates, key=lambda s: (s.kind != "classic", not s.featured, -s.game_count, s.start_utc))


def slate_header(s: Slate) -> dict:
    return {"draft_group_id": s.draft_group_id, "name": s.name, "start_utc": s.start_utc.isoformat(),
            "game_count": s.game_count, "featured": s.featured, "kind": s.kind,
            "contest_type": s.contest_type}


def slate_from_header(h: dict) -> Slate:
    return Slate(draft_group_id=int(h["draft_group_id"]), name=h["name"], start_utc=parse_dt(h["start_utc"]),
                 game_count=int(h.get("game_count") or 0), featured=bool(h.get("featured")),
                 kind=h.get("kind", "classic"), contest_type=int(h.get("contest_type") or 70))


def dfs_day(now: datetime):
    """The ET calendar date whose slate we are building (rolls over at 6am ET)."""
    local = now.astimezone(ET)
    if local.hour < 6:
        local -= timedelta(days=1)
    return local.date()


def _salary_cache(dg: int, sport: str = "NBA"):
    if sport == "NBA":
        from ..config import STATE_DIR
        return STATE_DIR / "salaries" / f"{dg}.csv"
    from ..sports.paths import paths
    return paths(sport).state / "salaries" / f"{dg}.csv"


def load_draftables(slate: Slate, sport: str = "NBA", ttl_s: float | None = None) -> Slate:
    """Players for a draft group from DraftKings' salary CSV.

    A draft group's salaries never change once released, so the last good copy is
    kept; if DraftKings can't be reached for a moment (network drop, timeout), that
    copy is used instead of dropping the slate from the site. With `ttl_s` (non-NBA
    sports), a cached copy younger than ttl_s is used without fetching. Outside
    NBA, a header-only file (an unpopulated group) raises ValueError, so the
    caller skips the slate."""
    cache = _salary_cache(slate.draft_group_id, sport)
    if ttl_s is not None and cache.exists() and time.time() - cache.stat().st_mtime < ttl_s:
        text = cache.read_text()
        if sport == "NBA" or len(text.encode()) >= MIN_CSV_BYTES:
            slate.players = []
            return load_salary_csv(slate, text, sport)
    try:
        text = get_text(SALARY_CSV_URL.format(ct=slate.contest_type, dg=slate.draft_group_id))
        if sport != "NBA" and len(text.encode()) < MIN_CSV_BYTES:
            raise ValueError(f"group {slate.draft_group_id} has no players yet ({len(text)} bytes)")
        load_salary_csv(slate, text, sport)
        health.ok("DraftKings salaries", f"{len(slate.players)} players in group {slate.draft_group_id}")
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text)
        except OSError:
            pass
    except Exception as e:  # noqa: BLE001
        health.fail("DraftKings salaries", e)
        if not cache.exists() or (sport != "NBA" and cache.stat().st_size < MIN_CSV_BYTES):
            raise
        log.warning("salaries for %s unavailable (%s); using the saved copy", slate.draft_group_id, e)
        slate.players = []
        load_salary_csv(slate, cache.read_text(), sport)
    return slate


_GAME_INFO = re.compile(r"([A-Z]{2,4})@([A-Z]{2,4})\s+(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)


def load_salary_csv(slate: Slate, text: str, sport: str = "NBA") -> Slate:
    """DKSalaries CSV: Position, Name + ID, Name, ID, Roster Position, Salary,
    Game Info, TeamAbbrev, AvgPointsPerGame. Showdown lists every player twice
    (Roster Position CPT and UTIL, with different IDs)."""
    if _cfg(sport).get("position_source", "position") == "roster":
        with health.default_scope(sport):          # never into NBA's source table
            return _load_salary_csv_roster(slate, text, sport)
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    head = next((i for i, r in enumerate(rows) if "ID" in r and "Salary" in r and "Name" in r), None)
    if head is None:
        raise ValueError("salary file has no header row")
    h = [c.strip() for c in rows[head]]
    col = {k: h.index(k) for k in ("Position", "Name", "ID", "Roster Position", "Salary", "Game Info", "TeamAbbrev") if k in h}
    games: dict[str, dict] = {}
    by_player: dict[tuple, dict] = {}
    for r in rows[head + 1:]:
        if len(r) <= max(col.values()) or not r[col["ID"]].strip().isdigit():
            continue
        name, team = r[col["Name"]].strip(), r[col["TeamAbbrev"]].strip()
        m = _GAME_INFO.search(r[col["Game Info"]]) if "Game Info" in col else None
        g = {}
        if m:
            away, home, day, tm = m.groups()
            key = f"{away}@{home}"
            if key not in games:
                start = datetime.strptime(f"{day} {tm.replace(' ', '').upper()}", "%m/%d/%Y %I:%M%p").replace(tzinfo=ET)
                games[key] = {"competition_id": key, "name": f"{away} @ {home}", "home": home, "away": away,
                              "start_utc": start.astimezone(timezone.utc)}
            g = games[key]
        p = by_player.setdefault((name, team), {"position": r[col["Position"]].strip(), "game": g, "entries": []})
        p["entries"].append({"id": int(r[col["ID"]]), "salary": int(float(r[col["Salary"]] or 0)),
                             "roster": r[col["Roster Position"]].strip().upper() if "Roster Position" in col else ""})
    slate.kind = "showdown" if any(e["roster"] == "CPT" for p in by_player.values() for e in p["entries"]) else "classic"
    players = []
    for (name, team), p in by_player.items():
        ents = sorted(p["entries"], key=lambda e: e["salary"])
        util = next((e for e in ents if e["roster"] != "CPT"), ents[0])
        cpt = next((e for e in ents if e["roster"] == "CPT"), None)
        g = p["game"]
        opp = g.get("home") if team == g.get("away") else g.get("away")
        players.append({
            "draftable_id": util["id"], "dk_player_id": f"{name}|{team}", "name": name,
            "position": p["position"], "salary": util["salary"],
            "cpt_id": cpt["id"] if cpt else None, "cpt_salary": cpt["salary"] if cpt else None,
            "team_dk": team, "team": team_code(team), "opp_dk": opp or "", "opp": team_code(opp),
            "home": team == g.get("home"), "competition_id": g.get("competition_id"),
            "start_utc": g.get("start_utc"), "dk_status": "", "disabled": False,
        })
    slate.players = [p for p in players if p["salary"] > 0]
    slate.games = sorted(games.values(), key=lambda g: g["start_utc"])
    slate.game_count = len(slate.games) or slate.game_count
    return slate


def _load_salary_csv_roster(slate: Slate, text: str, sport: str) -> Slate:
    """Registry sports: slot eligibility from Roster Position (fail closed),
    Status through the sport's status_map, AvgPointsPerGame as dk_avg. Players
    whose eligibility doesn't match the registry, or whose Game Info can't be
    read, are left out and counted in slate.excluded."""
    from ..sports import slots as S
    cfg = _cfg(sport)
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    head = next((i for i, r in enumerate(rows) if "ID" in r and "Salary" in r and "Name" in r), None)
    if head is None:
        raise ValueError("salary file has no header row")
    h = [c.strip() for c in rows[head]]
    col = {k: h.index(k) for k in ("Position", "Name", "ID", "Roster Position", "Salary", "Game Info", "TeamAbbrev",
                                   "AvgPointsPerGame", "Status") if k in h}
    need = [col[k] for k in ("Position", "Name", "ID", "Roster Position", "Salary", "TeamAbbrev") if k in col]
    status_map = {k.upper(): v for k, v in (cfg.get("status_map") or {}).items()}
    games: dict[str, dict] = {}
    by_player: dict[tuple, dict] = {}
    excluded: dict[str, int] = {}
    for r in rows[head + 1:]:
        if len(r) <= max(need) or not r[col["ID"]].strip().isdigit():
            continue
        name, team = r[col["Name"]].strip(), r[col["TeamAbbrev"]].strip().upper()
        m = _GAME_INFO.search(r[col["Game Info"]]) if "Game Info" in col and len(r) > col["Game Info"] else None
        if not m and cfg.get("team_sport", True):
            excluded["game_info"] = excluded.get("game_info", 0) + 1
            continue
        g = {}
        if m:
            away, home, day, tm = m.groups()
            away, home = away.upper(), home.upper()
            key = f"{away}@{home}"
            if key not in games:
                start = datetime.strptime(f"{day} {tm.replace(' ', '').upper()}", "%m/%d/%Y %I:%M%p").replace(tzinfo=ET)
                games[key] = {"competition_id": key, "name": f"{away} @ {home}", "home": home, "away": away,
                              "start_utc": start.astimezone(timezone.utc)}
            g = games[key]

        def cell(k):
            return r[col[k]].strip() if k in col and len(r) > col[k] else ""
        p = by_player.setdefault((name, team), {"position": cell("Position").upper(), "game": g, "entries": [],
                                                "status": cell("Status").upper(), "avg": cell("AvgPointsPerGame")})
        p["entries"].append({"id": int(r[col["ID"]]), "salary": int(float(cell("Salary") or 0)),
                             "roster": cell("Roster Position").upper()})
    csv_kind = "showdown" if any(e["roster"] == "CPT" for p in by_player.values() for e in p["entries"]) else "classic"
    reg_kind = slate.kind
    if by_player and reg_kind in ("classic", "showdown") and csv_kind != reg_kind:
        health.fail("DraftKings slate kind", f"group {slate.draft_group_id}: lobby says {reg_kind}, CSV is {csv_kind}")
    slate.kind = csv_kind if by_player else reg_kind
    spec = S.spec(sport, slate.kind)
    players = []
    for (name, team), p in by_player.items():
        ents = sorted(p["entries"], key=lambda e: e["salary"])
        util = next((e for e in ents if e["roster"] != "CPT"), ents[0])
        cpt = next((e for e in ents if e["roster"] == "CPT"), None)
        el = S.eligibility(util["roster"], p["position"], spec)
        if el is None:
            excluded["eligibility"] = excluded.get("eligibility", 0) + 1
            continue
        if spec.showdown and cpt is None:
            excluded["no_captain_row"] = excluded.get("no_captain_row", 0) + 1
            continue
        g = p["game"]
        opp = g.get("home") if team == g.get("away") else g.get("away")
        try:
            avg = float(p["avg"]) if p["avg"] else None
        except ValueError:
            avg = None
        players.append({
            "draftable_id": util["id"], "dk_player_id": f"{name}|{team}", "name": name,
            "position": "/".join(el["base"]), "salary": util["salary"],
            "cpt_id": cpt["id"] if cpt else None, "cpt_salary": cpt["salary"] if cpt else None,
            "team_dk": team, "team": team, "opp_dk": opp or "", "opp": opp or "",
            "home": team == g.get("home"), "competition_id": g.get("competition_id"),
            "start_utc": g.get("start_utc"),
            "dk_status": status_map.get(p["status"], "") if cfg.get("use_csv_status") else "",
            "disabled": False, "eligible": el["slots"], "pos_code": el["code"], "dk_avg": avg,
        })
    slate.players = [p for p in players if p["salary"] > 0]
    slate.games = sorted(games.values(), key=lambda g: g["start_utc"])
    slate.game_count = len(slate.games) or slate.game_count
    slate.excluded = excluded
    if excluded:
        health.fail("DraftKings player eligibility",
                    f"group {slate.draft_group_id}: excluded " + ", ".join(f"{v} ({k})" for k, v in sorted(excluded.items())))
    return slate


def load_draftables_json(slate: Slate, data: dict) -> Slate:
    """Parse the draftables JSON format (kept for files saved in that format)."""
    comps = {}
    for c in data.get("competitions") or []:
        cid = c.get("competitionId")
        home = (c.get("homeTeam") or {}).get("abbreviation") or (c.get("homeTeam") or {}).get("teamAbbreviation")
        away = (c.get("awayTeam") or {}).get("abbreviation") or (c.get("awayTeam") or {}).get("teamAbbreviation")
        comps[cid] = {
            "competition_id": cid,
            "name": c.get("name") or f"{away} @ {home}",
            "home": home, "away": away,
            "start_utc": parse_dt(c.get("startTime")),
        }
    by_player: dict[int, list[dict]] = {}
    for d in data.get("draftables") or []:
        by_player.setdefault(d.get("playerId"), []).append(d)
    # showdown groups list every player twice: captain (1.5x salary) and UTIL
    doubled = sum(1 for v in by_player.values() if len(v) > 1)
    slate.kind = "showdown" if by_player and doubled > len(by_player) / 2 else "classic"
    players: dict[int, dict] = {}
    for pid, entries in by_player.items():
        entries = sorted(entries, key=lambda d: int(d.get("salary") or 0))
        d = entries[0]                      # UTIL (or the only) entry
        cpt = entries[-1] if slate.kind == "showdown" and len(entries) > 1 else None
        comp = d.get("competition") or {}
        cid = comp.get("competitionId")
        if cid not in comps and cid is not None:
            name = comp.get("name") or ""
            away, _, home = name.partition(" @ ")
            comps[cid] = {"competition_id": cid, "name": name, "home": home.strip(),
                          "away": away.strip(), "start_utc": parse_dt(comp.get("startTime"))}
        g = comps.get(cid, {})
        team = d.get("teamAbbreviation") or ""
        opp = g.get("home") if team == g.get("away") else g.get("away")
        players[pid] = {
            "draftable_id": d.get("draftableId"),
            "dk_player_id": pid,
            "name": d.get("displayName") or f"{d.get('firstName','')} {d.get('lastName','')}".strip(),
            "position": d.get("position") or "",
            "salary": int(d.get("salary") or 0),
            "cpt_id": cpt.get("draftableId") if cpt else None,
            "cpt_salary": int(cpt.get("salary") or 0) if cpt else None,
            "team_dk": team,
            "team": team_code(team),
            "opp_dk": opp or "",
            "opp": team_code(opp),
            "home": team == g.get("home"),
            "competition_id": cid,
            "start_utc": g.get("start_utc"),
            "dk_status": _dk_status(d.get("status")),
            "disabled": bool(d.get("isDisabled")),
        }
    slate.players = [p for p in players.values() if p["salary"] > 0]
    slate.games = sorted(comps.values(), key=lambda g: (g["start_utc"] or datetime.max.replace(tzinfo=timezone.utc)))
    slate.game_count = len(slate.games) or slate.game_count
    return slate


def _dk_status(raw) -> str:
    s = (raw or "").strip().upper()
    return {
        "O": "OUT", "OUT": "OUT", "D": "DOUBTFUL", "DOUBTFUL": "DOUBTFUL",
        "Q": "QUESTIONABLE", "GTD": "QUESTIONABLE", "QUESTIONABLE": "QUESTIONABLE",
        "P": "PROBABLE", "PROBABLE": "PROBABLE", "OFS": "OUT", "INJ": "OUT",
    }.get(s, "")
