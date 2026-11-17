"""ESPN box scores: the historical and live game data source.

ESPN's public site API serves the scoreboard for any date and a full box score
for every game (minutes, points, rebounds, assists, steals, blocks, turnovers,
threes, starters, did-not-play reasons, referees, tip time, score and status).
Rows come out in the same format as the rest of the pipeline uses.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from ..config import ET
from ..http import get_json
from ..names import team_code
from .draftkings import parse_dt

log = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={ymd}&limit=50"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={eid}"
SEASON_TYPES = {2, 3}          # regular season, playoffs (no preseason / All-Star)


def games_on(day: date) -> list[dict]:
    """[{event_id, game_id, start_utc, home, away, state, completed, season_type}]"""
    data = get_json(SCOREBOARD_URL.format(ymd=day.strftime("%Y%m%d")))
    return parse_scoreboard(data)


def parse_scoreboard(data: dict) -> list[dict]:
    out = []
    for ev in data.get("events") or []:
        stype = (ev.get("season") or {}).get("type")
        if stype is not None and int(stype) not in SEASON_TYPES:
            continue
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): team_code((c.get("team") or {}).get("abbreviation")) for c in comp.get("competitors") or []}
        st = (ev.get("status") or comp.get("status") or {}).get("type") or {}
        start = parse_dt(ev.get("date") or comp.get("date"))
        out.append({
            "event_id": str(ev.get("id")), "game_id": f"E{ev.get('id')}", "start_utc": start,
            "home": teams.get("home"), "away": teams.get("away"),
            "state": st.get("state", ""), "completed": bool(st.get("completed")),
        })
    return out


def summary(event_id: str) -> dict:
    return get_json(SUMMARY_URL.format(eid=event_id))


def _num(v) -> float:
    try:
        return float(str(v).replace("+", ""))
    except (TypeError, ValueError):
        return 0.0


def _made(v) -> int:
    """'7-15' -> 7"""
    s = str(v or "")
    return int(_num(s.split("-")[0])) if s else 0


def _attempts(v) -> int:
    s = str(v or "")
    return int(_num(s.split("-")[1])) if "-" in s else 0


def progress(data: dict) -> tuple[float, str]:
    """(share of regulation played 0..1, state 'pre'|'in'|'post')."""
    comp = ((data.get("header") or {}).get("competitions") or [{}])[0]
    st = comp.get("status") or {}
    state = (st.get("type") or {}).get("state", "")
    if state == "post":
        return 1.0, state
    if state != "in":
        return 0.0, state
    period = int(_num(st.get("period")))
    clock = str(st.get("displayClock") or "0:00")
    if ":" in clock:
        m, s = clock.split(":", 1)
        left = _num(m) + _num(s) / 60
    else:
        left = _num(clock) / 60          # final minute shows seconds only
    played = 48.0 if period > 4 else (period - 1) * 12 + (12 - left)
    return max(0.0, min(1.0, played / 48.0)), state


def parse_summary(data: dict, event_id: str) -> list[dict]:
    """One row per listed player, in the pipeline's game-log format."""
    header = data.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    start = parse_dt(comp.get("date"))
    day = start.astimezone(ET).date().isoformat() if start else ""
    teams = {}
    for c in comp.get("competitors") or []:
        teams[team_code((c.get("team") or {}).get("abbreviation"))] = {
            "home": c.get("homeAway") == "home", "score": int(_num(c.get("score")))}
    officials = "|".join(sorted(o.get("displayName", "") for o in (data.get("gameInfo") or {}).get("officials") or []
                                if o.get("displayName"))) or "-"
    home_team = next((t for t, v in teams.items() if v["home"]), "")
    rows = []
    for block in (data.get("boxscore") or {}).get("players") or []:
        team = team_code((block.get("team") or {}).get("abbreviation"))
        opp = next((t for t in teams if t != team), "")
        for stat in block.get("statistics") or []:
            labels = [str(x).upper() for x in (stat.get("labels") or stat.get("names") or [])]
            idx = {k: i for i, k in enumerate(labels)}
            for a in stat.get("athletes") or []:
                ath = a.get("athlete") or {}
                vals = a.get("stats") or []
                dnp = bool(a.get("didNotPlay")) or not vals
                get = (lambda k: vals[idx[k]] if k in idx and idx[k] < len(vals) else "")
                minutes = 0.0 if dnp else _num(get("MIN"))
                reason = str(a.get("reason") or "").upper()
                # coach's decision counts as a 0-minute game; any other DNP (injury, illness,
                # rest, personal, suspension, not with team) is an absence
                npr = ("DNP_COACH" if "COACH" in reason else "INACTIVE_INJURY") if dnp else ""
                rows.append({
                    "game_id": f"E{event_id}", "date": day, "team": team, "opp": opp,
                    "home": teams.get(team, {}).get("home", False),
                    "person_id": f"E{ath.get('id')}", "name": ath.get("displayName") or "",
                    "position": (ath.get("position") or {}).get("abbreviation", "") if a.get("starter") else "",
                    "starter": bool(a.get("starter")),
                    "status": "INACTIVE" if dnp and npr != "DNP_COACH" else "ACTIVE",
                    "not_playing_reason": npr,
                    "minutes": round(minutes, 2),
                    "pts": int(_num(get("PTS"))), "reb": int(_num(get("REB"))), "ast": int(_num(get("AST"))),
                    "stl": int(_num(get("STL"))), "blk": int(_num(get("BLK"))), "tov": int(_num(get("TO"))),
                    "fg3m": _made(get("3PT")), "fga": _attempts(get("FG")), "fta": _attempts(get("FT")),
                    "team_score": teams.get(team, {}).get("score", 0), "opp_score": teams.get(opp, {}).get("score", 0),
                    "officials": officials, "arena_team": home_team,
                    "start_utc": start.isoformat() if start else "",
                })
    return rows


def season_dates(start_year: int, until: date) -> list[date]:
    """Every date from mid-October of the season to `until` (capped at the season end)."""
    d, end = date(start_year, 10, 15), min(until, date(start_year + 1, 6, 30))
    out = []
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def now_et_date(now: datetime) -> date:
    return now.astimezone(ET).date()
