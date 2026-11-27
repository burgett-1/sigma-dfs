"""NFL projection model: bonus expectation, DST, injuries and vacated shares,
fallbacks, team codes, ESPN adapters."""
import json
from pathlib import Path

import numpy as np
import pytest

from pipeline.sports import scoring
from pipeline.sports.nfl import data as D
from pipeline.sports.nfl import model as M
from pipeline.sports.nfl import teams as T
from tests.fixtures.nfl import synthetic_nfl as N

FIX = Path(__file__).parent / "fixtures" / "nfl"


@pytest.fixture(scope="module")
def hist():
    return N.history()


def _players(statuses=None):
    statuses = statuses or {}
    out = []
    for team in N.TEAMS:
        for pos, n in N.ROSTER:
            for i in range(n):
                nm = N.name(team, pos, i)
                out.append({"dk_player_id": f"{nm}|{team}", "name": nm, "team": team, "opp": N.opp_of(team),
                            "position": pos, "status": statuses.get(nm, ""), "dk_avg": 10.0,
                            "player_id": N.pid(team, pos, i), "salary": N.SALARY[pos][i]})
        out.append({"dk_player_id": f"{N.NICK[team]}|{team}", "name": N.NICK[team], "team": team, "opp": N.opp_of(team),
                    "position": "DST", "status": "", "dk_avg": 7.0, "player_id": f"DST-{team}", "salary": 3200})
    return out


def test_bonus_expectation():
    assert M.bonus_expectation(300, "pass_yds", 300) == pytest.approx(1.5, abs=0.05)
    assert M.bonus_expectation(100, "rec_yds", 100) == pytest.approx(1.5, abs=0.05)
    # AC25 as restated by the Critic (P3-5 Wave 1): 0.22 +/- 0.08 at mu = 200
    assert 0.14 <= M.bonus_expectation(200, "pass_yds", 300) <= 0.30
    assert M.bonus_expectation(120, "pass_yds", 300) < 0.1
    assert M.bonus_expectation(0, "rush_yds", 100) == 0


def test_dst_positive_and_decreasing_in_opponent_total():
    t = scoring.table_for("NFL", "DST")
    rates = {"sack": 2.4, "int": 0.8, "fum_rec": 0.6}
    vals = [M.dst_mean(x, rates, t)[0] for x in range(14, 31)]
    assert all(v > 0 for v in vals) and all(a > b for a, b in zip(vals, vals[1:]))


def test_projection_rows_and_curve(hist):
    rows = M.project(_players(), hist, N.context(), N.SEASON, N.WEEK)
    by = {r["name"]: r for r in rows}
    qb = by["Buf Qb A"]
    assert qb["has_history"] and qb["env_source"] == "vegas" and 10 < qb["mean"] < 35
    assert len(qb["q"]) == 25 and qb["q"] == sorted(qb["q"]) and qb["floor"] < qb["proj"] < qb["ceiling"]
    assert by["Buf Wr A"]["mean"] > by["Buf Wr E"]["mean"]
    assert by["Bills"]["mean"] > 0 and by["Bills"]["position"] == "DST"
    assert all(r["proj"] >= 0 for r in rows)


def test_out_ir_doubtful_project_zero(hist):
    rows = {r["name"]: r for r in M.project(_players({"Buf Wr A": "OUT", "Buf Rb A": "DOUBTFUL"}), hist,
                                            N.context(), N.SEASON, N.WEEK)}
    assert rows["Buf Wr A"]["proj"] == 0 and rows["Buf Wr A"]["mean"] == 0
    assert rows["Buf Rb A"]["proj"] == 0


def test_vacated_targets_redistributed(hist):
    base = {r["name"]: r for r in M.project(_players(), hist, N.context(), N.SEASON, N.WEEK)}
    out = {r["name"]: r for r in M.project(_players({"Buf Wr A": "OUT"}), hist, N.context(), N.SEASON, N.WEEK)}
    mates = [n for n, r in base.items() if r["team"] == "BUF" and r["position"] in ("WR", "TE", "RB") and n != "Buf Wr A"]
    gain = sum(out[n]["targets_share"] - base[n]["targets_share"] for n in mates)
    assert gain == pytest.approx(0.85 * base["Buf Wr A"]["targets_share"], rel=0.01)


def test_fallbacks(hist):
    p = _players()
    p.append({"dk_player_id": "Rookie|BUF", "name": "Rookie", "team": "BUF", "opp": "MIA", "position": "WR",
              "status": "", "dk_avg": 8.0, "player_id": None, "salary": 3000})
    rows = {r["name"]: r for r in M.project(p, hist, {}, N.SEASON, N.WEEK)}
    assert rows["Rookie"]["mean"] == pytest.approx(7.2) and not rows["Rookie"]["has_history"]
    assert rows["Buf Qb A"]["env_source"] == "default" and rows["Buf Qb A"]["implied_total"] == 21.5


def test_wind_cuts_passing(hist):
    calm = {r["name"]: r for r in M.project(_players(), hist, N.context(), N.SEASON, N.WEEK)}
    ctx = N.context()
    ctx["BUF"]["wind"] = 25.0
    windy = {r["name"]: r for r in M.project(_players(), hist, ctx, N.SEASON, N.WEEK)}
    assert windy["Buf Qb A"]["components"]["pass_yds"] == pytest.approx(
        calm["Buf Qb A"]["components"]["pass_yds"] * 0.9, abs=0.01)
    ctx["BUF"]["indoor"] = True
    dome = {r["name"]: r for r in M.project(_players(), hist, ctx, N.SEASON, N.WEEK)}
    assert dome["Buf Qb A"]["components"]["pass_yds"] == pytest.approx(calm["Buf Qb A"]["components"]["pass_yds"], abs=0.01)


def test_team_codes_cover_fixture_and_sources():
    for line in (FIX / "dk_classic_4g.csv").read_text().splitlines()[1:]:
        assert line.split(",")[7] in T.DK
    assert len(T.TEAMS) == 32 and T.dk("LA", "nflverse") == "LAR" and T.dk("WSH", "espn") == "WAS"
    assert T.from_name("Buffalo Bills") == "BUF" and T.nickname("SF") == "49ers"


def test_espn_scoreboard_and_injuries_adapters():
    sb = json.loads((FIX / "espn_scoreboard_sample.json").read_text())
    ctx = D.scoreboard(["20261129"], lambda url: sb)
    assert ctx and all(t in T.DK for t in ctx)
    with_lines = [c for c in ctx.values() if c["implied"] is not None]
    for c in with_lines:
        o = ctx[c["opp"]]
        assert c["implied"] + o["implied"] == pytest.approx(c["total"]) and c["spread"] == -o["spread"]
    inj = D.injuries(lambda url: json.loads((FIX / "espn_injuries_sample.json").read_text()))
    assert inj and all(i["status"] in ("OUT", "DOUBTFUL", "QUESTIONABLE", "PROBABLE") and i["team"] in T.DK for i in inj)


def test_default_ratio_curves_are_mean_one():
    for pos in M.DEFAULT_CV:
        r = np.array(M.default_ratios(pos))
        u = np.linspace(0.0005, 0.9995, 4000)
        from pipeline.distribution import U_SAMPLE
        assert np.interp(u, U_SAMPLE, r).mean() == pytest.approx(1.0, abs=0.06)
