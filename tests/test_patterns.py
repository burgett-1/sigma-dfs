from datetime import date

import numpy as np
import pandas as pd

from pipeline import factors, model, patterns
from tests import synthetic as syn

DAY = date(2026, 11, 15)


def _rotoguru_page(logs, day, corrupt=False):
    tonight = logs[logs["date"] == day.isoformat()]
    sal = {0: 10400, 1: 8200, 2: 8800, 3: 7000, 4: 8000, 5: 5200, 6: 4600, 7: 3900, 8: 3400, 9: 3000}
    lines = ["Date;GID;Pos;Name;Starter;DK Pts;DK Salary;Team;H/A;Oppt;Team Score;Oppt Score;Minutes;Stat line"]
    for _, r in tonight.iterrows():
        first, last = r["name"].rsplit(" ", 1)
        slot = "ABCDEFGHJK".index(last)
        pts = r["dk_fp"] + (7 if corrupt else 0)
        lines.append(f"{day:%Y%m%d};1;{syn.POS[slot]};{last}, {first};1;{pts};${sal[slot]:,};{r['team']};H;{r['opp']};1;1;30;x")
    return "<html><pre>" + "\n".join(lines) + "</pre></html>"


def test_rotoguru_parse_verify_and_optimal():
    logs = syn.season_logs(date(2026, 11, 20), n_games=8)
    day = date.fromisoformat(sorted(logs["date"].unique())[-1])
    sal = patterns.parse_rotoguru(_rotoguru_page(logs, day))
    assert len(sal) == 40 and sal["salary"].max() == 10400 and (sal["name"].str.contains(",")).sum() == 0
    prep = model.prepare_history(logs, date(2027, 1, 1))
    joined, rate = patterns.verify_night(sal, prep[prep["d"] == day])
    assert rate == 1.0 and len(joined) == 40
    bad, rate_bad = patterns.verify_night(patterns.parse_rotoguru(_rotoguru_page(logs, day, corrupt=True)),
                                          prep[prep["d"] == day])
    assert rate_bad == 0.0    # wrong archive data is rejected
    base = factors.baseline_predictions(prep)
    ctx = patterns.night_context(day, prep, base)
    res = patterns.analyse_night(day, joined, ctx)
    lus = res["lineups"]
    assert len(lus) == patterns.TOP_K
    scores = [l["score"] for l in lus]
    assert scores == sorted(scores, reverse=True)          # exact top-K in order
    assert all(l["salary"] <= 50000 for l in lus)
    assert len({tuple(sorted(p["name"] for p in l["players"])) for l in lus}) == len(lus)
    # brute-force check of the optimum on this small night
    df = joined[joined["dk_fp"] > 0].reset_index(drop=True)
    best = patterns.top_lineups(df, 1)[0]
    assert abs(df.loc[best, "dk_fp"].sum() - scores[0]) < 1e-6


def test_summary_and_rules():
    lineup = {"score": 300, "salary": 49800, "studs": 2, "mid": 3, "value": 3, "punts": 1, "max_team": 3,
              "games": 4, "max_game": 4, "bring_back": True,
              "players": [{"teammate_out": i < 3, "surprise_start": i == 0, "minutes_spike": i < 2,
                           "overtime": False, "blowout": False, "high_total": i < 4, "beat_proj": 1.3,
                           "proj_value_rank": 10 + 5 * i} for i in range(8)]}
    s = patterns.summarise([{"date": "2025-01-01", "lineups": [lineup] * 10}], [])
    assert s["structure"]["salary_used_median"] == 49800
    assert s["why_players_hit"]["teammate_out_pct"] == 37.5
    assert s["rules"]["max_per_team"] >= 3 and s["rules"]["min_salary"] <= 49800


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
