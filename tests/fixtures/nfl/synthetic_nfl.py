"""Synthetic NFL league for offline tests: 8 teams in 4 games, two seasons of
weekly stats in the nflverse-derived history format, and DraftKings salary CSVs
in the real format (fake names "Buf WR A", fake ids). Deterministic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
GAMES = [("MIA", "BUF"), ("LV", "KC"), ("PHI", "DAL"), ("SEA", "SF")]      # away @ home
TEAMS = [t for g in GAMES for t in g]
NICK = {"BUF": "Bills", "MIA": "Dolphins", "KC": "Chiefs", "LV": "Raiders", "DAL": "Cowboys", "PHI": "Eagles",
        "SF": "49ers", "SEA": "Seahawks"}
ROSTER = [("QB", 2), ("RB", 3), ("WR", 5), ("TE", 2), ("K", 1)]
# per depth slot: target share, carry share, attempt share
SHARE = {"QB": [(0, 0.08, 0.97), (0, 0.01, 0.03)], "RB": [(0.10, 0.50, 0), (0.05, 0.28, 0), (0.02, 0.08, 0)],
         "WR": [(0.24, 0.01, 0), (0.18, 0.0, 0), (0.12, 0.0, 0), (0.05, 0, 0), (0.02, 0, 0)],
         "TE": [(0.15, 0, 0), (0.05, 0, 0)], "K": [(0, 0, 0)]}
SALARY = {"QB": [7400, 4800], "RB": [8200, 5600, 4200], "WR": [8600, 6400, 5000, 3800, 3000], "TE": [6000, 3200],
          "K": [4800]}
DST_SAL = 3200
KICKOFF = [datetime(2026, 11, 29, 13, 0, tzinfo=ET)] * 2 + [datetime(2026, 11, 29, 16, 25, tzinfo=ET)] * 2
NOW = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
SEASON, WEEK = 2026, 13


def name(team: str, pos: str, i: int) -> str:
    return f"{team.title()} {pos.title()} {'ABCDE'[i]}"   # 'Kc Wr A': no slot token inside a name


def pid(team: str, pos: str, i: int) -> str:
    return f"00-{TEAMS.index(team):02d}{pos}{i}"


def opp_of(team: str) -> str:
    for a, h in GAMES:
        if team == a:
            return h
        if team == h:
            return a
    return ""


def history(seed: int = 3, seasons=(2025, 2026), weeks_2026: int = 12) -> pd.DataFrame:
    """Player-week history (pipeline.sports.nfl.data.HISTORY_COLUMNS) with DK points."""
    from pipeline.sports.nfl.data import HISTORY_COLUMNS, dk_points
    rng = np.random.default_rng(seed)
    quality = {t: rng.normal(0, 1) for t in TEAMS}
    rows = []
    for season in seasons:
        for week in range(1, (18 if season < 2026 else weeks_2026) + 1):
            order = TEAMS[week % len(TEAMS):] + TEAMS[:week % len(TEAMS)]
            pairs = [(order[i], order[i + 1]) for i in range(0, len(order), 2)]
            for a, h in pairs:
                gid = f"{season}_{week:02d}_{a}_{h}"
                total = 44 + 3 * (quality[a] + quality[h]) + rng.normal(0, 2)
                spread_home = 2.5 * (quality[h] - quality[a]) + 1.5
                pts = {h: max(0, int(rng.normal(total / 2 + spread_home / 2, 9))),
                       a: max(0, int(rng.normal(total / 2 - spread_home / 2, 9)))}
                for team, opp, home in ((h, a, True), (a, h, False)):
                    g = rng.normal(0, 1)                         # shared team-game factor
                    pass_att = max(20, int(rng.normal(34 + 2 * g, 4)))
                    rush_att = max(15, int(rng.normal(26 - g, 4)))
                    targets_team = int(pass_att * 0.94)
                    spread = -spread_home if home else spread_home
                    base = dict(season=season, week=week, game_id=gid, team=team, opp=opp, home=home,
                                team_pass_att=pass_att, team_rush_att=rush_att, team_targets=targets_team,
                                team_points=pts[team], opp_points=pts[opp], spread=round(spread * 2) / 2,
                                total=round(total * 2) / 2, roof="outdoors", wind=8.0)
                    for pos, n in ROSTER:
                        for i in range(n):
                            ts, cs, as_ = SHARE[pos][i]
                            r = {**base, "player_id": pid(team, pos, i), "name": name(team, pos, i), "position": pos}
                            tg = rng.binomial(targets_team, ts) if ts else 0
                            ca = rng.binomial(rush_att, cs) if cs else 0
                            r.update(targets=tg, carries=ca, rec=rng.binomial(tg, 0.66) if tg else 0)
                            r["rec_yds"] = max(0.0, r["rec"] * rng.normal(11 + 1.5 * g, 3)) if r["rec"] else 0.0
                            r["rec_td"] = rng.poisson(0.045 * tg * (1 + 0.2 * g))
                            r["rush_yds"] = max(0.0, ca * rng.normal(4.3, 1.2)) if ca else 0.0
                            r["rush_td"] = rng.poisson(0.03 * ca)
                            if pos == "QB":
                                att = int(pass_att * as_)
                                r.update(attempts=att, completions=int(att * 0.65),
                                         pass_yds=max(0.0, att * rng.normal(7.0 + 0.6 * g, 1.2)),
                                         pass_td=rng.poisson(0.048 * att * (1 + 0.25 * g)), int=rng.poisson(0.022 * att))
                            if pos == "K":
                                r.update(xp=rng.poisson(2.2), fg_0_39=rng.poisson(0.9), fg_40_49=rng.poisson(0.5),
                                         fg_50p=rng.poisson(0.2))
                            r["fum_lost"] = rng.binomial(1, 0.03) if (ca + tg) else 0
                            rows.append(r)
                    rows.append({**base, "player_id": f"DST-{team}", "name": NICK[team], "position": "DST",
                                 "sack": rng.poisson(2.5), "int": rng.poisson(0.8), "fum_rec": rng.poisson(0.6),
                                 "safety": rng.binomial(1, 0.03), "blocked_kick": rng.binomial(1, 0.05),
                                 "def_st_td": rng.binomial(1, 0.12), "xp_return": 0, "points_allowed": pts[opp],
                                 "sacks_taken": rng.poisson(2.5), "fum_lost_team": rng.poisson(0.6)})
    df = pd.DataFrame(rows)
    for c in HISTORY_COLUMNS:
        if c not in df:
            df[c] = 0.0
    num = [c for c in HISTORY_COLUMNS if c not in ("game_id", "team", "opp", "home", "player_id", "name", "position",
                                                     "roof")]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    df["dk_fp"] = dk_points(df)
    return df[HISTORY_COLUMNS]


def _info(away: str, home: str, kick: datetime) -> str:
    return f"{away}@{home} {kick:%m/%d/%Y %I:%M%p} ET"


def salary_csv(kind: str = "classic", games=None, statuses: dict | None = None, id_base: int = 47_000_000) -> str:
    """DraftKings salary CSV. classic: QB/RB/WR/TE/DST with Roster Position
    ("WR/FLEX", "DST"); showdown: CPT (1.5x) + FLEX rows, K and DST included."""
    games = games if games is not None else (GAMES if kind == "classic" else GAMES[:1])
    statuses = statuses or {}
    head = "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame,Status"
    lines = [head]
    nid = id_base
    for gi, (away, home) in enumerate(games):
        kick = KICKOFF[GAMES.index((away, home))]
        for team in (away, home):
            entries = [(pos, i) for pos, n in ROSTER for i in range(n)]
            for pos, i in entries + [("DST", 0)]:
                if kind == "classic" and pos == "K":
                    continue
                nm = NICK[team] if pos == "DST" else name(team, pos, i)
                sal = DST_SAL if pos == "DST" else SALARY[pos][i]
                avg = round(sal / 1000 * 2.4, 1)
                st = statuses.get(nm, "")
                if kind == "classic":
                    rp = pos if pos in ("QB", "DST") else f"{pos}/FLEX"
                    nid += 1
                    lines.append(f"{pos},{nm} ({nid}),{nm},{nid},{rp},{sal},{_info(away, home, kick)},{team},{avg},{st}")
                else:
                    nid += 1
                    lines.append(f"{pos},{nm} ({nid}),{nm},{nid},CPT,{int(sal * 1.5)},{_info(away, home, kick)},{team},{avg},{st}")
                    nid += 1
                    lines.append(f"{pos},{nm} ({nid}),{nm},{nid},FLEX,{sal},{_info(away, home, kick)},{team},{avg},{st}")
    return "\n".join(lines) + "\n"


def context(spread_home: float = -3.0, total: float = 46.0) -> dict:
    """ESPN-scoreboard-shaped context {team: {implied, spread, opp, opp_implied, wind, indoor}}."""
    out = {}
    for away, home in GAMES:
        for team, opp, s in ((home, away, spread_home), (away, home, -spread_home)):
            out[team] = {"opp": opp, "spread": s, "total": total, "implied": (total - s) / 2, "wind": 6.0,
                         "indoor": False}
    for t, c in out.items():
        c["opp_implied"] = out[c["opp"]]["implied"]
    return out


def lobby(now: datetime = NOW) -> dict:
    """A public-lobby-shaped NFL response: classic ct 21 / gt 1, a showdown, a far-future group."""
    sunday = datetime(2026, 11, 29, 18, 0, tzinfo=timezone.utc)
    rows = [
        {"DraftGroupId": 900001, "ContestTypeId": 21, "GameTypeId": 1, "DraftGroupTag": "Featured",
         "StartDate": sunday.isoformat().replace("+00:00", "Z"), "GameCount": 4, "ContestStartTimeSuffix": None},
        {"DraftGroupId": 900002, "ContestTypeId": 96, "GameTypeId": 96, "DraftGroupTag": "",
         "StartDate": sunday.isoformat().replace("+00:00", "Z"), "GameCount": 1,
         "ContestStartTimeSuffix": " (MIA @ BUF)"},
        {"DraftGroupId": 900003, "ContestTypeId": 21, "GameTypeId": 1, "DraftGroupTag": "Featured",
         "StartDate": (now + timedelta(days=38)).isoformat().replace("+00:00", "Z"), "GameCount": 13,
         "ContestStartTimeSuffix": " (Week 18)"},
        {"DraftGroupId": 900004, "ContestTypeId": 51, "GameTypeId": 51, "DraftGroupTag": "",
         "StartDate": sunday.isoformat().replace("+00:00", "Z"), "GameCount": 4, "ContestStartTimeSuffix": " (Tiers)"},
    ]
    return {"DraftGroups": rows}


def fake_dk_text(url: str) -> str:
    if "draftGroupId=900002" in url:
        return salary_csv("showdown")
    if "draftGroupId=900001" in url:
        return salary_csv("classic")
    return "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"


def contest_export(n_entries: int = 60, seed: int = 5) -> str:
    """Synthetic NFL classic contest-standings export (fake usernames)."""
    rng = np.random.default_rng(seed)
    head = "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS"
    lines = [head]
    pool = {pos: [(name(t, pos, i), t) for t in TEAMS for i in range(n)] for pos, n in ROSTER if pos != "K"}
    rows_p = []
    for pos in ("QB", "RB", "WR", "TE"):
        for nm, t in pool[pos]:
            rows_p.append((nm, pos if pos == "QB" else (pos if rng.random() < 0.8 else "FLEX"), round(rng.random() * 30, 2)))
    for t in TEAMS:
        rows_p.append((NICK[t], "DST", round(rng.random() * 25, 2)))
    for k in range(n_entries):
        qb = pool["QB"][int(rng.integers(len(pool["QB"])))]
        mate = next(nm for nm, t in pool["WR"] if t == qb[1])
        rbs = [pool["RB"][int(j)][0] for j in rng.choice(len(pool["RB"]), 3, replace=False)]
        wrs = [mate] + [pool["WR"][int(j)][0] for j in rng.choice(len(pool["WR"]), 3, replace=False)]
        te = pool["TE"][int(rng.integers(len(pool["TE"])))][0]
        dst = NICK[TEAMS[int(rng.integers(len(TEAMS)))]]
        lu = f"QB {qb[0]} RB {rbs[0]} RB {rbs[1]} WR {wrs[0]} WR {wrs[1]} WR {wrs[2]} TE {te} FLEX {rbs[2]} DST {dst}"
        pts = round(200 - k * 1.7, 2)
        extra = rows_p[k] if k < len(rows_p) else ("", "", "")
        lines.append(f"{k + 1},{9_000_000 + k},fakeuser{k} (1/150),0,{pts},{lu},," +
                     (f"{extra[0]},{extra[1]},{extra[2]}%,{round(rng.random() * 30, 2)}" if extra[0] else ",,,"))
    return "\n".join(lines) + "\n"
