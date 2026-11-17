"""DraftKings public lobby + draftables endpoints (salaries, positions, start times)."""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import ET
from .. import health
from ..http import get_json, get_text
from ..names import team_code

log = logging.getLogger(__name__)

LOBBY_URL = "https://www.draftkings.com/lobby/getcontests?sport=NBA"
DRAFTABLES_URL = "https://api.draftkings.com/draftgroups/v1/draftgroups/{dg}/draftables"
# The salary file DraftKings offers on every contest page ("Export to CSV"): names,
# positions, salaries, games and the player IDs DraftKings uses for lineup uploads.
SALARY_CSV_URL = "https://www.draftkings.com/lineup/getavailableplayerscsv?contestTypeId={ct}&draftGroupId={dg}"
# DraftKings contest types we build for. Others in the NBA lobby (Tiers, "Points"
# scoring, second-half, in-game, pick'em…) use different rules and are skipped.
CONTEST_TYPES = {70: "classic", 81: "showdown"}

seen: list[dict] = []  # draft groups the lobby listed on the last call (for meta.json)
# contest styles we don't build for (showdown IS supported; it's detected from the draftables)
_SKIP_WORDS = ("tiers", "snake", "pick6", "in-game", "best ball", "arcade")


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


def list_draft_groups(now: datetime) -> list[Slate]:
    """NBA draft groups (classic + showdown) whose first game is on the current ET 'DFS day'.
    Note: DraftKings removes a group from the lobby once it locks; run.py remembers the
    groups it has seen today so late swap keeps working after lock."""
    try:
        data = get_json(LOBBY_URL)
        health.ok("DraftKings lobby")
    except Exception as e:  # noqa: BLE001
        health.fail("DraftKings lobby", e)
        raise
    groups = data.get("DraftGroups") or data.get("draftGroups") or []
    day = dfs_day(now)
    start_win = datetime.combine(day, datetime.min.time(), ET) + timedelta(hours=6)
    end_win = start_win + timedelta(hours=24)
    out: list[Slate] = []
    seen.clear()
    for g in groups:
        seen.append({k: g.get(k) for k in ("DraftGroupId", "ContestTypeId", "GameTypeId", "GameCount",
                                           "DraftGroupTag", "ContestStartTimeSuffix", "StartDate")})
        tag = " ".join(str(g.get(k) or "") for k in (
            "DraftGroupTag", "ContestStartTimeSuffix", "GameType", "ContestTypeName")).lower()
        if any(w in tag for w in _SKIP_WORDS):
            continue
        ctype = g.get("ContestTypeId") or g.get("GameTypeId")
        if ctype is not None and int(ctype) not in CONTEST_TYPES:
            continue
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
            kind=CONTEST_TYPES.get(int(ctype), "classic") if ctype is not None else "classic",
            contest_type=int(ctype) if ctype is not None else 70,
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


def load_draftables(slate: Slate) -> Slate:
    """Players for a draft group from DraftKings' salary CSV."""
    try:
        text = get_text(SALARY_CSV_URL.format(ct=slate.contest_type, dg=slate.draft_group_id))
        load_salary_csv(slate, text)
        health.ok("DraftKings salaries", f"{len(slate.players)} players in group {slate.draft_group_id}")
    except Exception as e:  # noqa: BLE001
        health.fail("DraftKings salaries", e)
        raise
    return slate


_GAME_INFO = re.compile(r"([A-Z]{2,4})@([A-Z]{2,4})\s+(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)


def load_salary_csv(slate: Slate, text: str) -> Slate:
    """DKSalaries CSV: Position, Name + ID, Name, ID, Roster Position, Salary,
    Game Info, TeamAbbrev, AvgPointsPerGame. Showdown lists every player twice
    (Roster Position CPT and UTIL, with different IDs)."""
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
