from datetime import date, timedelta

import numpy as np
import pandas as pd

from pipeline import factors, model
from pipeline.history import COLUMNS, dk_points
from pipeline.sources import referees

TEAMS = ["BOS", "DEN", "LAL", "NYK", "MIA", "UTA", "CHI", "PHX"]


def league(n_days=170, seed=4, b2b_effect=-0.06, alt_effect=-0.05, ww_boost=0.25):
    """Synthetic seasons with known situational effects baked in."""
    rng = np.random.default_rng(seed)
    talent = {(t, i): (rng.uniform(0.8, 1.3), [34, 32, 30, 28, 26, 20, 16, 12][i]) for t in TEAMS for i in range(8)}
    last_played = {}
    rows, gid = [], 0
    start = date(2025, 10, 22)
    for day in range(n_days):
        d = start + timedelta(days=day)
        order = list(rng.permutation(TEAMS))
        for k in range(0, 8, 2):
            if rng.random() < 0.35:
                continue
            home, away = order[k], order[k + 1]
            gid += 1
            ref = "Ref Fast" if gid % 3 == 0 else "Ref Slow"
            for team, opp, is_home in ((home, away, True), (away, home, False)):
                b2b = last_played.get(team) == d - timedelta(days=1)
                star_out = rng.random() < 0.15
                for i in range(8):
                    rate, mins = talent[(team, i)]
                    if i == 0 and star_out:
                        rows.append(dict(game_id=f"g{gid}", date=d.isoformat(), team=team, opp=opp, home=is_home,
                                         person_id=hash((team, i)) % 10**6, name=f"{team} P{i}", position="",
                                         starter=True, status="INACTIVE", not_playing_reason="INACTIVE_INJURY",
                                         minutes=0, pts=0, reb=0, ast=0, stl=0, blk=0, tov=0, fg3m=0, fga=0, fta=0,
                                         team_score=0, opp_score=0, officials=ref, arena_team=home))
                        continue
                    eff = 1.0
                    if b2b:
                        eff *= 1 + b2b_effect
                    if home in ("DEN", "UTA") and team != home:
                        eff *= 1 + alt_effect
                    if ref == "Ref Fast":
                        eff *= 1.04
                    if i == 1 and star_out:
                        eff *= 1 + ww_boost
                    m = max(0, rng.normal(mins, 2.5))
                    fp = max(0, rng.normal(m * rate * eff, 0.25 * m * rate))
                    rows.append(dict(game_id=f"g{gid}", date=d.isoformat(), team=team, opp=opp, home=is_home,
                                     person_id=hash((team, i)) % 10**6, name=f"{team} P{i}", position="",
                                     starter=i < 5, status="ACTIVE", not_playing_reason="", minutes=round(m, 1),
                                     pts=int(fp * 0.5), reb=int(fp * 0.2), ast=int(fp * 0.12), stl=1, blk=0, tov=2,
                                     fg3m=1, fga=10, fta=3, team_score=110 + (6 if ref == "Ref Fast" else 0),
                                     opp_score=108 + (6 if ref == "Ref Fast" else 0), officials=ref, arena_team=home))
                last_played[team] = d
    df = pd.DataFrame(rows)
    df["dk_fp"] = dk_points(df)
    # make the synthetic fp exact (dk_points rounds via integer stats): rescale to the drawn value
    return df[COLUMNS]


def _hist():
    return model.prepare_history(league(), date(2026, 6, 1))


def test_fit_recovers_known_effects():
    fit = factors.fit_factors(_hist())
    assert fit["used"], fit
    c = fit["coef"]
    assert c["b2b"] < -0.02            # true effect -6%
    assert c["altitude"] < -0.015      # true effect -5%
    assert 0.5 < c["ref"] < 1.6        # true: +4% in games where the crew adds ~4% scoring
    bt = fit["backtest"]
    assert bt["mae_with_factors"] < bt["mae_baseline"]
    assert "Ref Fast" in fit["refs"] and fit["refs"]["Ref Fast"] > fit["refs"]["Ref Slow"]


def test_situational_multiplier_is_capped():
    fit = {"used": True, "coef": {f: 5.0 for f in factors.FEATURES}, "center": {}}
    assert factors.situational_multiplier({"b2b": 1, "travel": 4}, fit) <= np.exp(factors.EFFECT_CAP) + 1e-9


def test_with_without_finds_usage_boost():
    h = _hist()
    star = h.loc[h["name"] == "BOS P0", "person_id"].iloc[0]
    second = h.loc[h["name"] == "BOS P1", "person_id"].iloc[0]
    third = h.loc[h["name"] == "BOS P2", "person_id"].iloc[0]
    ww = factors.with_without(h, "BOS", {"p1": second, "p2": third}, [star], season=2025)
    assert ww["p1"][0] > 1.1           # true boost +25%
    assert abs(ww.get("p2", (1.0, 0))[0] - 1.0) < 0.08


def test_travel_and_context():
    f = factors.context_features("BOS", "LAL", date(2026, 1, 1), "BOS", date(2026, 1, 2))
    assert f["b2b"] == 1 and f["tz_west"] == 3 and 4000 < f["travel"] * 1000 < 4300 and f["home"] == 0
    g = factors.context_features("LAL", "DEN", date(2026, 1, 1), "LAL", date(2026, 1, 5))
    assert g["altitude"] == 1 and g["rest3"] == 1 and g["tz_east"] == 1


def test_minutes_restriction_words():
    assert factors.minutes_limited("Injury/Illness - Left Knee; Return to Competition Reconditioning")
    assert not factors.minutes_limited("Injury/Illness - Left Ankle; Sprain")


def test_referee_page_parsing():
    page = """<table><tr><th>Game</th><th>Crew Chief</th></tr>
    <tr><td>Boston @ L.A. Lakers</td><td>Scott Foster (#48)</td><td>Tony Brothers (#25)</td>
    <td>Josh Tiven (#58)</td><td>Alt Person (#99)</td></tr></table>"""
    out = referees.parse(page)
    assert out["LAL"] == ["Scott Foster", "Tony Brothers", "Josh Tiven"] and out["BOS"] == out["LAL"]


def test_residual_correlation_runs():
    fit = factors.fit_factors(_hist())
    assert set(fit["corr"]) >= {"team", "opp"}
