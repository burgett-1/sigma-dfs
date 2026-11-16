"""Starting lineups.

Primary source: NBA.com's own "Today's Lineups" data feed (the file behind
nba.com/players/todays-lineups). For each team it lists the five starters and
whether they are "Expected" or "Confirmed" by the team.

Before a team's lineup is posted, Sigma projects the starters itself
(`project_starters`): recent starters who are available, with replacements for
injured starters chosen by position and role.

Verification: after tip-off, the official box score lists the actual starters;
`run.py` compares them with what was announced and logs any mismatch.
"""
from __future__ import annotations

import logging

from ..http import get_json
from ..names import norm_name, team_code

log = logging.getLogger(__name__)

DAILY_URL = "https://stats.nba.com/js/data/leaders/00_daily_lineups_{ymd}.json"
_STARTER_POS = {"PG", "SG", "SF", "PF", "C", "G", "F", "G-F", "F-C", "F-G", "C-F"}


def fetch(day) -> dict[str, dict]:
    data = get_json(DAILY_URL.format(ymd=day.strftime("%Y%m%d")),
                    headers={"Referer": "https://www.nba.com/", "Origin": "https://www.nba.com"})
    return parse(data)


def parse(data: dict) -> dict[str, dict]:
    """{team: {"status": "confirmed"|"expected", "starters": [names], "game_id"}}"""
    out: dict[str, dict] = {}
    for g in data.get("games") or []:
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side) or {}
            team = team_code(t.get("teamAbbreviation") or t.get("teamTricode"))
            if not team:
                continue
            starters, statuses = [], set()
            for p in t.get("players") or []:
                pos = (p.get("position") or "").upper().strip()
                if pos not in _STARTER_POS:
                    continue
                if (p.get("rosterStatus") or "Active").lower() not in ("active", ""):
                    continue
                name = p.get("playerName") or f"{p.get('firstName','')} {p.get('lastName','')}".strip()
                starters.append({"name": name, "position": pos})
                statuses.add((p.get("lineupStatus") or "").lower())
            if len(starters) < 5:
                continue
            status = "confirmed" if statuses == {"confirmed"} else "expected"
            out[team] = {"status": status, "starters": [s["name"] for s in starters[:5]],
                         "positions": [s["position"] for s in starters[:5]],
                         "game_id": g.get("gameId"), "source": "NBA.com"}
    return out


def project_starters(team_rows: list[dict]) -> list[str]:
    """Sigma's projected five when the team hasn't posted one: recent starters
    who are available; each missing starter is replaced by the available player
    with the most recent starts / minutes at the same position group."""
    avail = [r for r in team_rows if r.get("status") not in ("OUT", "DOUBTFUL")]
    regulars = [r for r in team_rows if r.get("start_rate", 0) >= 0.5]
    regulars.sort(key=lambda r: (-r.get("start_rate", 0), -r.get("minutes", 0)))
    chosen = [r for r in regulars if r in avail][:5]
    missing_groups = [r.get("grp", "") for r in regulars[:5] if r not in avail]
    pool = [r for r in avail if r not in chosen]
    pool.sort(key=lambda r: (-r.get("start_rate", 0), -r.get("minutes", 0)))
    for grp in missing_groups:
        if len(chosen) >= 5:
            break
        same = [r for r in pool if r.get("grp") == grp] or pool
        if same:
            pick = same[0]
            chosen.append(pick)
            pool.remove(pick)
    while len(chosen) < 5 and pool:
        chosen.append(pool.pop(0))
    return [r["name"] for r in chosen[:5]]


def starters_by_key(info: dict[str, dict]) -> dict[str, set]:
    return {team: {norm_name(n) for n in v["starters"]} for team, v in info.items()}
