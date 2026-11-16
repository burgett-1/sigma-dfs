"""NBA.com CDN: season schedule and box scores (the historical data source).

The CDN (cdn.nba.com) serves static JSON and, unlike stats.nba.com, does not
block cloud/CI IP ranges.
"""
from __future__ import annotations

import logging
import re
from ..config import ET
from ..http import get_json
from .draftkings import parse_dt

log = logging.getLogger(__name__)

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
BOXSCORE_URL = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json"

_ISO_MIN = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?")


def season_start_year(d) -> int:
    """2026-11-15 -> 2026 (the 2026-27 season). Seasons roll over in August."""
    return d.year if d.month >= 8 else d.year - 1


def schedule() -> list[dict]:
    """Every game of the current season: id, date (ET), teams, status, start time."""
    data = get_json(SCHEDULE_URL)
    out = []
    for gd in data.get("leagueSchedule", {}).get("gameDates", []):
        for g in gd.get("games", []):
            gid = g.get("gameId", "")
            if not gid.startswith(("002", "004", "006")):  # regular season, playoffs, cup final
                continue
            start = parse_dt(g.get("gameDateTimeUTC"))
            out.append({
                "game_id": gid,
                "date": start.astimezone(ET).date().isoformat() if start else None,
                "start_utc": start,
                "home": (g.get("homeTeam") or {}).get("teamTricode"),
                "away": (g.get("awayTeam") or {}).get("teamTricode"),
                "status": int(g.get("gameStatus") or 0),  # 1 scheduled, 2 live, 3 final
            })
    return out


def parse_minutes(raw) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw)
    m = _ISO_MIN.fullmatch(s)
    if m:
        h, mi, se = m.groups()
        return (int(h or 0) * 60) + int(mi or 0) + float(se or 0) / 60.0
    if ":" in s:
        mi, se = s.split(":", 1)
        try:
            return int(mi) + float(se) / 60.0
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def boxscore_rows(game_id: str) -> list[dict]:
    """One row per rostered player for a completed game."""
    data = get_json(BOXSCORE_URL.format(gid=game_id))
    return parse_boxscore(data)


def parse_boxscore(data: dict) -> list[dict]:
    g = data.get("game") or {}
    start = parse_dt(g.get("gameTimeUTC") or g.get("gameEt"))
    date = start.astimezone(ET).date().isoformat() if start else ""
    officials = "|".join(sorted(o.get("name") or "" for o in (g.get("officials") or []) if o.get("name"))) or "-"
    rows = []
    for side, other in (("homeTeam", "awayTeam"), ("awayTeam", "homeTeam")):
        team = g.get(side) or {}
        opp = g.get(other) or {}
        for p in team.get("players") or []:
            st = p.get("statistics") or {}
            mins = parse_minutes(st.get("minutes") or st.get("minutesCalculated"))
            rows.append({
                "game_id": g.get("gameId"),
                "date": date,
                "team": team.get("teamTricode"),
                "opp": opp.get("teamTricode"),
                "home": side == "homeTeam",
                "person_id": p.get("personId"),
                "name": p.get("name") or f"{p.get('firstName','')} {p.get('familyName','')}".strip(),
                "position": p.get("position") or "",
                "starter": str(p.get("starter")) == "1",
                "status": (p.get("status") or "").upper(),
                "not_playing_reason": (p.get("notPlayingReason") or "").upper(),
                "minutes": round(mins, 2),
                "pts": int(st.get("points") or 0),
                "reb": int(st.get("reboundsTotal") or 0),
                "ast": int(st.get("assists") or 0),
                "stl": int(st.get("steals") or 0),
                "blk": int(st.get("blocks") or 0),
                "tov": int(st.get("turnovers") or 0),
                "fg3m": int(st.get("threePointersMade") or 0),
                "fga": int(st.get("fieldGoalsAttempted") or 0),
                "fta": int(st.get("freeThrowsAttempted") or 0),
                "team_score": int(team.get("score") or 0),
                "opp_score": int(opp.get("score") or 0),
                "officials": officials,
                "arena_team": (g.get("homeTeam") or {}).get("teamTricode"),
            })
    return rows


def game_ids_for_season(start_year: int, max_games: int = 1230) -> list[str]:
    """Regular-season ids, e.g. 2025 -> 0022500001 .. 0022501230."""
    yy = start_year % 100
    return [f"002{yy:02d}{n:05d}" for n in range(1, max_games + 1)]
