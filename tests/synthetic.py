"""Synthetic league used by the offline tests (no network access needed)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from pipeline.history import COLUMNS, dk_points

TEAMS = ["BOS", "LAL", "GSW", "NYK"]
POS = ["PG", "SG", "SF", "PF", "C", "PG/SG", "SF/PF", "PF/C", "SG/SF", "C"]
BASE_MIN = [34, 32, 33, 30, 31, 22, 20, 16, 14, 8]
BASE_RATE = [1.25, 1.0, 1.05, 0.95, 1.15, 0.9, 0.85, 0.9, 0.8, 0.85]


def name(team: str, i: int) -> str:
    return f"{team.title()} Player {'ABCDEFGHJK'[i]}"


def roster():
    out = []
    pid = 1000
    for t in TEAMS:
        for i in range(10):
            pid += 1
            out.append({"person_id": pid, "name": name(t, i), "team": t, "slot": i,
                        "position": POS[i]})
    return out


def season_logs(today: date, n_games: int = 18, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ros = roster()
    rows = []
    gid = 0
    for g in range(n_games):
        d = today - timedelta(days=2 * (n_games - g))
        for a, b in ((0, 1), (2, 3)) if g % 2 == 0 else ((0, 2), (1, 3)):
            gid += 1
            game_id = f"00226{gid:05d}"
            for team, opp, home in ((TEAMS[a], TEAMS[b], True), (TEAMS[b], TEAMS[a], False)):
                for p in [r for r in ros if r["team"] == team]:
                    i = p["slot"]
                    m = max(0.0, rng.normal(BASE_MIN[i], 3.0))
                    fp = max(0.0, rng.normal(m * BASE_RATE[i], m * BASE_RATE[i] * 0.3))
                    pts = int(fp * 0.5)
                    reb = int(fp * 0.2)
                    ast = int(fp * 0.12)
                    rows.append({
                        "game_id": game_id, "date": d.isoformat(), "team": team, "opp": opp,
                        "home": home, "person_id": p["person_id"], "name": p["name"],
                        "position": p["position"].split("/")[0] if i < 5 else "",
                        "starter": i < 5, "status": "ACTIVE", "not_playing_reason": "",
                        "minutes": round(m, 2), "pts": pts, "reb": reb, "ast": ast,
                        "stl": 1, "blk": 0, "tov": 2, "fg3m": 1, "fga": 10, "fta": 3,
                        "team_score": 112, "opp_score": 110,
                        "officials": "Ref A|Ref B|Ref C" if gid % 2 else "Ref D|Ref E|Ref F",
                        "arena_team": TEAMS[a],
                    })
    df = pd.DataFrame(rows)
    df["dk_fp"] = dk_points(df)
    return df[COLUMNS]


def draftables(start: datetime) -> dict:
    """DraftKings draftables JSON for BOS@LAL and GSW@NYK."""
    ros = roster()
    comps = [
        {"competitionId": 1, "name": "BOS @ LAL", "startTime": start.isoformat().replace("+00:00", "Z"),
         "homeTeam": {"abbreviation": "LAL"}, "awayTeam": {"abbreviation": "BOS"}},
        {"competitionId": 2, "name": "GS @ NY",
         "startTime": (start + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
         "homeTeam": {"abbreviation": "NY"}, "awayTeam": {"abbreviation": "GS"}},
    ]
    dk_abbr = {"BOS": "BOS", "LAL": "LAL", "GSW": "GS", "NYK": "NY"}
    sal = [10400, 8200, 8800, 7000, 8000, 5200, 4600, 3900, 3400, 3000]
    d = []
    for p in ros:
        comp = comps[0] if p["team"] in ("BOS", "LAL") else comps[1]
        d.append({"draftableId": 90000 + p["person_id"], "playerId": p["person_id"],
                  "displayName": p["name"], "position": p["position"], "salary": sal[p["slot"]],
                  "teamAbbreviation": dk_abbr[p["team"]], "status": "None",
                  "competition": {"competitionId": comp["competitionId"], "name": comp["name"],
                                  "startTime": comp["startTime"]}})
    return {"draftables": d, "competitions": comps}


def lobby(start: datetime) -> dict:
    return {"DraftGroups": [
        {"DraftGroupId": 555, "ContestTypeId": 70, "DraftGroupTag": "Featured",
         "StartDate": start.isoformat().replace("+00:00", "Z"), "GameCount": 2,
         "ContestStartTimeSuffix": None},
        {"DraftGroupId": 556, "ContestTypeId": 81, "DraftGroupTag": "",
         "StartDate": start.isoformat().replace("+00:00", "Z"), "GameCount": 1,
         "ContestStartTimeSuffix": " (BOS vs LAL)"},
    ]}


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)
