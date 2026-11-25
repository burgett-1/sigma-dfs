from datetime import date

import numpy as np
import pandas as pd

from pipeline import factors, model, patterns
from tests import synthetic as syn

DAY = date(2026, 11, 15)


def _salaries(logs, day, corrupt=False):
    tonight = logs[logs["date"] == day.isoformat()]
    sal = {0: 10400, 1: 8200, 2: 8800, 3: 7000, 4: 8000, 5: 5200, 6: 4600, 7: 3900, 8: 3400, 9: 3000}
    rows = []
    for _, r in tonight.iterrows():
        slot = "ABCDEFGHJK".index(r["name"].rsplit(" ", 1)[1])
        if corrupt and slot % 3 == 0:
            continue                      # a partial / wrong file: players missing
        rows.append({"name": r["name"], "position": syn.POS[slot], "salary": sal[slot], "dk_pts": np.nan,
                     "team": r["team"], "opp": r["opp"], "minutes": ""})
    return pd.DataFrame(rows)


def test_archive_salaries_verify_and_optimal():
    logs = syn.season_logs(date(2026, 11, 20), n_games=8)
    day = date.fromisoformat(sorted(logs["date"].unique())[-1])
    sal = _salaries(logs, day)
    assert len(sal) == 40 and sal["salary"].max() == 10400
    prep = model.prepare_history(logs, date(2027, 1, 1))
    joined, rate = patterns.verify_night(sal, prep[prep["d"] == day])
    assert rate == 1.0 and len(joined) == 40
    bad, rate_bad = patterns.verify_night(_salaries(logs, day, corrupt=True), prep[prep["d"] == day])
    assert rate_bad < 0.95    # a partial file is rejected
    base = factors.baseline_predictions(prep)
    ctx = patterns.night_context(day, prep, base)
    res = patterns.analyse_night(day, joined, ctx)
    full = res["slates"][0]
    assert full["type"] == "all" and full["games"] == 2
    lus = full["lineups"]
    exps = [p["exp"] for p in full["players"]]
    assert abs(sum(exps) - 8.0) < 1e-6        # 8 roster spots: exposures sum to 8 per lineup
    assert all(0 <= e <= 1 for e in exps)
    assert len(lus) == patterns.TOP_K
    scores = [l["score"] for l in lus]
    assert scores == sorted(scores, reverse=True)          # exact top-K in order
    assert all(l["salary"] <= 50000 for l in lus)
    assert len({tuple(sorted(p["name"] for p in l["players"])) for l in lus}) == len(lus)
    # brute-force check of the optimum on this small night
    df = joined[joined["dk_fp"] > 0].reset_index(drop=True)
    best = patterns.top_lineups(df, 1)[0]
    assert abs(df.loc[best, "dk_fp"].sum() - scores[0]) < 1e-6


def test_summary_buckets_rules_and_exposure_model():
    lineup = {"score": 300, "salary": 49800, "studs": 2, "mid": 3, "value": 3, "punts": 1, "max_team": 3,
              "games": 4, "max_game": 4, "bring_back": True,
              "players": [{"teammate_out": i < 3, "surprise_start": i == 0, "minutes_spike": i < 2,
                           "overtime": False, "blowout": False, "high_total": i < 4, "beat_proj": 1.3,
                           "proj_value_rank": 10 + 5 * i} for i in range(8)]}
    players = ([{"value_rank": 1, "teammate_out": True, "exp": 0.8, "salary": 5000, "proj_rank": 3, "starter": True}] * 40
               + [{"value_rank": 30, "teammate_out": False, "exp": 0.0, "salary": 4000, "proj_rank": 60, "starter": False}] * 400)
    nights = [{"date": f"2025-01-{d:02d}", "slates": [
        {"type": "all", "games": 9, "lineups": [lineup] * 10, "players": players},
        {"type": "late", "games": 3, "lineups": [dict(lineup, salary=48000)] * 10, "players": players}]}
        for d in range(1, 11)]
    s = patterns.summarise(nights, [])
    assert s["structure"]["salary_used_median"] == 49800           # full-night view
    assert s["buckets"]["small"]["structure"]["salary_used_median"] == 48000
    assert s["buckets"]["large"]["slates"] == 10 and "medium" not in s["buckets"]
    assert s["why_players_hit"]["teammate_out_pct"] == 37.5
    m = s["exposure_model"]
    hot = patterns.suggested_exposure(m, 9, 1, True)
    cold = patterns.suggested_exposure(m, 9, 30, False)
    assert hot > 60 and cold < 2                                    # shrunk toward the average
    patterns.OUT.parent.mkdir(parents=True, exist_ok=True)


def test_sub_slates():
    df = pd.DataFrame({"game_id": ["a", "a", "b", "c", "d"],
                       "start_utc": ["2025-01-02T00:00:00+00:00", "2025-01-02T00:00:00+00:00",
                                     "2025-01-02T03:00:00+00:00", "2025-01-02T03:00:00+00:00",
                                     "2025-01-01T20:00:00+00:00"]})
    kinds = {k: g["game_id"].nunique() for k, g in patterns.sub_slates(df)}
    assert kinds == {"all": 4, "main": 3, "late": 2}       # 3pm game out of main; 10pm games = late


def test_contest_standings_import():
    rows = ["Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS"]
    lu = "C Nikola Jokic F LeBron James G Stephen Curry PF Anthony Davis PG Luka Doncic SF Jayson Tatum SG Devin Booker UTIL Joel Embiid"
    rows.append(f"1,111,winner,0,350.5,{lu},,Nikola Jokic,C,35.5%,70")
    rows.append(f"2,112,second,0,340.0,{lu},,LeBron James,F,8.2%,50")
    for i in range(3, 2001):
        rows.append(f"{i},{i},x,0,{300 - i * 0.01},{lu},,,,,")
    res = patterns.parse_contest_standings("\n".join(rows))
    assert res["entries"] == 2000 and res["winning_score"] == 350.5
    w = res["top_0_1pct"][0]
    assert w["players"][0] == "Nikola Jokic" and len(w["players"]) == 8
    assert w["sub10_owned"] == 1 and w["total_own"] == 43.7
