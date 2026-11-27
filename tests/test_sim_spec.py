"""The shared simulator on the NFL spec: DraftKings legality checked by an
independent validator, the factor copula, stack rules. NBA defaults are
covered by tests/test_sim.py and the golden replay."""
import csv
import dataclasses
import io
from pathlib import Path

import numpy as np
import pytest

from pipeline import ownership, sim
from pipeline.config import SIM
from pipeline.sports import corr as C
from pipeline.sports import slots
from tests.fixtures.nfl import synthetic_nfl as N

FIX = Path(__file__).parent / "fixtures" / "nfl"
CLASSIC = slots.spec("NFL", "classic")
SHOWDOWN = slots.spec("NFL", "showdown")
RULES = {"max_per_team": 8, "min_salary": 0, "min_salary_showdown": 0}


def _csv_index(path):
    """draftable id -> (position, salary, game, team) read straight from the DK file."""
    out = {}
    for r in csv.DictReader(io.StringIO(path.read_text())):
        out[int(r["ID"])] = (r["Position"], int(r["Salary"]), r["Game Info"].split(" ")[0], r["TeamAbbrev"],
                             r["Roster Position"])
    return out


CLASSIC_IDX = _csv_index(FIX / "dk_classic_4g.csv")
SD_IDX = _csv_index(FIX / "dk_showdown.csv")
ORDER = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]


def dk_classic_legal(ids) -> bool:
    """Written from DraftKings' NFL classic rules, not from the code under test."""
    if len(ids) != 9 or len(set(ids)) != 9 or any(i not in CLASSIC_IDX for i in ids):
        return False
    pos = [CLASSIC_IDX[i][0] for i in ids]
    for p, slot in zip(pos, ORDER):
        if slot == "FLEX":
            if p not in ("RB", "WR", "TE"):
                return False
        elif p != slot:
            return False
    if pos.count("QB") != 1 or pos.count("DST") != 1:
        return False
    if sum(CLASSIC_IDX[i][1] for i in ids) > 50000:
        return False
    return len({CLASSIC_IDX[i][2] for i in ids}) >= 2


def has_qb_stack(ids) -> bool:
    qb = next(i for i in ids if CLASSIC_IDX[i][0] == "QB")
    return any(CLASSIC_IDX[i][0] in ("WR", "TE") and CLASSIC_IDX[i][3] == CLASSIC_IDX[qb][3] for i in ids if i != qb)


def dst_faces_qb(ids) -> bool:
    qb = next(i for i in ids if CLASSIC_IDX[i][0] == "QB")
    dst = next(i for i in ids if CLASSIC_IDX[i][0] == "DST")
    away, home = CLASSIC_IDX[qb][2].split("@")
    opp = home if CLASSIC_IDX[qb][3] == away else away
    return CLASSIC_IDX[dst][3] == opp


@pytest.fixture(scope="module")
def projected():
    """Projected NFL rows for the synthetic 4-game classic and 1-game showdown."""
    import os
    import tempfile

    from pipeline import config
    from pipeline.sports.nfl import data as D
    from pipeline.sports.nfl import runner as R
    tmp = Path(tempfile.mkdtemp(prefix="nfl-sim-"))
    old = config.DATA_DIR
    config.DATA_DIR = tmp
    try:
        D.save_history(N.history())
        r = R.NFLRunner(get_json=lambda url: N.lobby() if "lobby" in url else {"events": [], "injuries": []},
                        get_text=N.fake_dk_text)
        r.lobby(N.NOW)
        r.salaries(N.NOW)
        r.ctx = N.context()
        r.project(N.NOW)
        return {k: [dict(x) for x in v] for k, v in r.rows.items()}
    finally:
        config.DATA_DIR = old
        os.environ.pop("_unused", None)


def test_build_slate_lineups_fast_is_legal_and_stacked(projected, monkeypatch):
    monkeypatch.setitem(SIM, "portfolio_sims", 2000)
    res = sim.build_slate_lineups(projected[900001], RULES, {}, n_lineups=150, sims=4000, field_size=200,
                                  exact=False, kind="classic", spec=CLASSIC)
    assert res["slots"] == ORDER and len(res["lineups"]) >= 50
    for lu in res["lineups"]:
        assert dk_classic_legal(lu["ids"]), lu["ids"]
        assert has_qb_stack(lu["ids"]) and not dst_faces_qb(lu["ids"])
        assert lu["salary"] == sum(CLASSIC_IDX[i][1] for i in lu["ids"])


def test_field_and_opt_lineups_are_legal(projected):
    rows = [dict(r) for r in projected[900001]]
    pool = sim.build_pool(rows, "classic", CLASSIC)
    assert pool.load is not None and pool.spec is CLASSIC
    field = sim.sample_field(pool, 300, np.random.default_rng(3), RULES)
    assert len(field) >= 100
    for lu in field:
        order = sim.slot_order(lu, pool)
        ids = [int(pool.ids[i]) for i in order]
        assert dk_classic_legal(ids) and not dst_faces_qb(ids)
    rng = np.random.default_rng(4)
    for s in sim.sample_scores(pool, 60, rng, {}).astype(float):
        lu = sim.best_lineup(s, pool, RULES)
        assert lu is not None
        ids = [int(pool.ids[i]) for i in sim.slot_order(lu, pool)]
        assert dk_classic_legal(ids) and has_qb_stack(ids)
    ownership.optimal_rates(rows, "classic", RULES, None, sims=120, spec=CLASSIC)
    assert abs(sum(r["opt_pct"] for r in rows) - 900.0) < 1e-6


def test_kicker_never_in_classic(projected):
    assert not any(r["position"] == "K" for r in projected[900001])
    assert sim.pos_code("K", CLASSIC) == 0


def test_bring_back_rule(projected):
    sp = dataclasses.replace(CLASSIC, stack_rules={**CLASSIC.stack_rules, "bring_back_min": 1})
    pool = sim.build_pool([dict(r) for r in projected[900001]], "classic", sp)
    rng = np.random.default_rng(9)
    for s in sim.sample_scores(pool, 40, rng, {}).astype(float):
        lu = sim.best_lineup(s, pool, RULES)
        assert lu is not None
        qb = next(i for i in lu if pool.code[i] & sp.bit("QB"))
        assert any(pool.team[j] == pool.opp[qb] and pool.code[j] & (sp.bit("RB") | sp.bit("WR") | sp.bit("TE"))
                   for j in lu)


def test_correlation_alone_builds_stacks(projected):
    """With the stack rules off, the copula alone still stacks most optimal lineups."""
    sp = dataclasses.replace(CLASSIC, stack_rules={})
    pool = sim.build_pool([dict(r) for r in projected[900001]], "classic", sp)
    rng = np.random.default_rng(11)
    lus = [sim.best_lineup(s, pool, RULES) for s in sim.sample_scores(pool, 300, rng, {}).astype(float)]
    ids = [[int(pool.ids[i]) for i in lu] for lu in lus if lu]
    rate = np.mean([has_qb_stack(x) for x in ids])
    assert rate >= 0.6, rate


def test_factor_copula_matches_implied_correlation(projected):
    rows = [dict(r) for r in projected[900001]]
    pool = sim.build_pool(rows, "classic", CLASSIC)
    z = sim._factor_z(pool, 60_000, np.random.default_rng(1)).astype(np.float64)
    implied = C.implied_corr([{"position": r["position"], "team": r["team_dk"], "opp": r["opp_dk"],
                               "game": r["competition_id"]} for r in rows if r.get("mean", 0) >= CLASSIC.min_proj], "NFL")
    emp = np.corrcoef(z.T)
    assert np.abs(emp - implied).max() <= 0.04
    q = pool.q
    pool.q = None                                         # gamma marginals: means match the projection
    x = sim.sample_scores(pool, 60_000, np.random.default_rng(2), {})
    pool.q = q
    live = pool.p_play == 1
    assert np.all(np.abs(x.mean(axis=0)[live] / pool.mu[live] - 1) < 0.03)


def test_registry_default_implied_pairs():
    s = C.summary("NFL")
    assert 0.25 <= s["QB-own WR"] <= 0.50 and 0.20 <= s["QB-own TE"] <= 0.45 and 0.0 <= s["QB-own RB"] <= 0.25
    assert 0.03 <= s["QB-opp WR"] <= 0.20 and s["DST-opp QB"] <= -0.10 and s["DST-own RB"] >= 0.05
    assert s["different games"] == 0


def test_nba_pool_has_no_factor_path():
    from tests import synthetic as syn
    rows = [{"name": "a", "position": "PG", "salary": 5000, "proj": 20, "team_dk": "BOS", "opp_dk": "LAL"}]
    pool = sim.build_pool(rows)
    assert pool.load is None and pool.spec is sim.NBA_CLASSIC and syn is not None


def test_showdown_legal_and_k_dst_used(projected):
    pytest.importorskip("highspy")
    rows = [dict(r) for r in projected[900002]]
    for r in rows:                                        # make a K and a DST the best values
        if r["name"] in ("Buf K A", "Mia Dolphins", "Dolphins"):
            r["mean"] = r["proj"] = 30.0
    res = sim.build_slate_lineups(rows, RULES, {}, n_lineups=100, sims=4000, field_size=200, exact=True,
                                  kind="showdown", spec=SHOWDOWN)
    used = set()
    for lu in res["lineups"]:
        cpt, *flex = lu["ids"]
        assert SD_IDX[cpt][4] == "CPT" and all(SD_IDX[i][4] == "FLEX" for i in flex)
        names = [SD_IDX[i] for i in lu["ids"]]
        assert lu["salary"] <= 50000 and len({n[3] for n in names}) == 2
        used |= {n[0] for n in names}
    assert {"K", "DST"} <= used


def test_showdown_field_fills_and_spends_like_the_public(projected):
    """F2: the NFL showdown field reaches the requested size, legal, median salary >= $47,000."""
    pool = sim.build_pool([dict(r) for r in projected[900002]], "showdown", SHOWDOWN)
    field = sim.sample_field(pool, 500, np.random.default_rng(2), RULES)
    assert len(field) >= 475
    sal = []
    for lu in field:
        cpt, *flex = lu
        ids = [int(pool.cpt_ids[cpt])] + [int(pool.ids[i]) for i in flex]
        assert SD_IDX[ids[0]][4] == "CPT" and all(SD_IDX[i][4] == "FLEX" for i in ids[1:])
        s = sum(SD_IDX[i][1] for i in ids)
        assert s <= 50000 and len({SD_IDX[i][3] for i in ids}) == 2 and len(set(lu)) == 6
        sal.append(s)
    assert np.median(sal) >= 47000 and min(sal) >= SHOWDOWN.field_min_salary


def test_underfilled_field_is_flagged(projected, monkeypatch):
    from pipeline import health
    monkeypatch.setitem(SIM, "portfolio_sims", 500)
    monkeypatch.setattr(sim, "sample_field", lambda pool, n, rng, rules: [])
    health._scoped.clear()
    with health.scope("NFL"):
        res = sim.build_slate_lineups(projected[900001], RULES, {}, n_lineups=30, sims=500, field_size=200,
                                      exact=False, kind="classic", spec=CLASSIC)
    assert res["field_filled"] == 0 and not health.snapshot("NFL")["Simulation field"]["ok"]
    assert "Simulation field" not in health.snapshot()
