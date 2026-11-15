"""ESPN public site API: Vegas lines (spread / total) and a backup injury feed."""
from __future__ import annotations

import logging
import re

from ..http import get_json
from ..names import team_code

log = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={ymd}"
INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"


def odds_for_date(day) -> dict[str, dict]:
    """{team_tricode: {"total": 226.5, "spread": -4.5, "implied": 115.5}} for a date.

    spread is from that team's perspective (negative = favourite).
    """
    data = get_json(SCOREBOARD_URL.format(ymd=day.strftime("%Y%m%d")))
    return parse_scoreboard_odds(data)


def parse_scoreboard_odds(data: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ev in data.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        teams = {}
        for c in comp.get("competitors") or []:
            teams[c.get("homeAway")] = team_code((c.get("team") or {}).get("abbreviation"))
        odds_list = comp.get("odds") or []
        if not odds_list or "home" not in teams or "away" not in teams:
            continue
        o = odds_list[0]
        total = _num(o.get("overUnder"))
        fav, line = _parse_details(o.get("details"))
        home_spread = None
        if fav is not None and line is not None:
            fav = team_code(fav)
            if fav == teams["home"]:
                home_spread = -abs(line)
            elif fav == teams["away"]:
                home_spread = abs(line)
        elif (o.get("details") or "").strip().upper() in ("EVEN", "PK", "PICK"):
            home_spread = 0.0
        if home_spread is None:
            sp = _num(o.get("spread"))
            if sp is not None:
                # ESPN's numeric 'spread' is the home line
                home_spread = sp
        for side, sign in (("home", 1), ("away", -1)):
            team = teams[side]
            spread = None if home_spread is None else sign * home_spread
            implied = None
            if total is not None and spread is not None:
                implied = total / 2.0 - spread / 2.0
            elif total is not None:
                implied = total / 2.0
            out[team] = {"total": total, "spread": spread, "implied": implied,
                         "opp": teams["away" if side == "home" else "home"]}
    return out


def _parse_details(details):
    """'BOS -6.5' -> ('BOS', 6.5)."""
    if not details:
        return None, None
    m = re.match(r"\s*([A-Za-z]{2,4})\s*([-+]?\d+(?:\.\d+)?)", details)
    if not m:
        return None, None
    return m.group(1), abs(float(m.group(2)))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def injuries() -> list[dict]:
    data = get_json(INJURIES_URL)
    return parse_injuries(data)


def parse_injuries(data: dict) -> list[dict]:
    out = []
    for team_block in data.get("injuries") or []:
        team = team_code((team_block.get("team") or {}).get("abbreviation")
                         or team_block.get("displayName"))
        for inj in team_block.get("injuries") or []:
            ath = inj.get("athlete") or {}
            t = team
            if ath.get("team"):
                t = team_code(ath["team"].get("abbreviation")) or team
            out.append({
                "name": ath.get("displayName") or "",
                "team": t,
                "status": normalize_status(inj.get("status")),
                "reason": inj.get("shortComment") or (inj.get("details") or {}).get("type") or "",
                "source": "ESPN",
            })
    return out


def normalize_status(raw) -> str:
    s = (raw or "").strip().lower()
    if s.startswith("out") or "suspen" in s or "inactive" in s:
        return "OUT"
    if s.startswith("doubt"):
        return "DOUBTFUL"
    if s.startswith("question") or "day-to-day" in s or s == "gtd":
        return "QUESTIONABLE"
    if s.startswith("prob"):
        return "PROBABLE"
    if s.startswith("avail") or s == "active":
        return "AVAILABLE"
    return ""
