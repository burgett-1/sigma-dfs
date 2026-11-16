import itertools

import numpy as np

from pipeline import sim
from pipeline.config import DK_SALARY_CAP


def _rows(n_games=4, seed=3):
    rng = np.random.default_rng(seed)
    pos = ["PG", "SG", "SF", "PF", "C", "PG/SG", "SF/PF", "PF/C", "SG/SF", "C"]
    rows = []
    for g in range(n_games):
        for side in (0, 1):
            t, o = f"T{2*g+side}", f"T{2*g+1-side}"
            for k in range(10):
                sal = int(rng.choice([3000, 3800, 4500, 5200, 6000, 7000, 8200, 9500, 10800]))
                proj = sal / 1000 * rng.uniform(3.8, 5.4)
                rows.append({"name": f"{t}-{k}", "draftable_id": len(rows) + 100, "position": pos[k],
                             "salary": sal, "proj": proj, "std_dev": proj * 0.33, "team_dk": t,
                             "opp_dk": o, "competition_id": g, "own": float(rng.uniform(1, 30)), "status": ""})
    return rows


def test_hall_condition_matches_matching():
    codes = [1, 2, 4, 8, 16, 3, 12, 24, 6, 5, 17]
    for cs in itertools.combinations_with_replacement(codes, 8):
        hall = all(sum(1 for c in cs if c & U) >= need for U, need in sim.HALL_NEED.items())
        assert hall == sim.can_fill(tuple(sorted(cs)))


def test_lineups_are_legal_and_exact_beats_heuristic():
    pool = sim.build_pool(_rows())
    rng = np.random.default_rng(1)
    rules = {"max_per_team": 4, "min_salary": 45000}
    for x in sim.sample_scores(pool, 20, rng, {"team": 0.02, "opp": 0.05}).astype(float):
        h = sim.best_lineup(x, pool, {**rules, "min_salary": 0})
        e = sim.exact_lineup(x, pool, rules, h)
        for lu in (h, e):
            assert len(set(lu)) == 8
            assert pool.sal[list(lu)].sum() <= DK_SALARY_CAP
            assert sim.can_fill(tuple(sorted(int(pool.code[i]) for i in lu)))
            assert max(np.bincount(pool.team[list(lu)])) <= 4
        assert pool.sal[list(e)].sum() >= 45000
        if pool.sal[list(h)].sum() >= 45000:
            assert x[list(e)].sum() >= x[list(h)].sum() - 1e-9


def test_correlated_sims_have_right_mean_and_correlation():
    pool = sim.build_pool(_rows())
    X = sim.sample_scores(pool, 60000, np.random.default_rng(2), {"team": 0.10, "opp": 0.06})
    assert np.allclose(X.mean(0), pool.mu, rtol=0.03)
    c = np.corrcoef(X[:, :40].T)
    same_team = [c[i, j] for i in range(10) for j in range(10) if i < j]
    opp = [c[i, j] for i in range(10) for j in range(10, 20)]
    other_game = [c[i, j] for i in range(10) for j in range(20, 30)]
    assert 0.06 < np.mean(same_team) < 0.14
    assert 0.03 < np.mean(opp) < 0.09
    assert abs(np.mean(other_game)) < 0.02


def test_full_build_ranks_by_win_probability():
    out = sim.build_slate_lineups(_rows(), {"max_per_team": 4, "min_salary": 45000},
                                  {"team": 0.02, "opp": 0.05}, n_lineups=300, sims=20000,
                                  field_size=500, exact=False)
    lus = out["lineups"]
    assert len(lus) >= 250
    wins = [l["win"] for l in lus]
    assert wins == sorted(wins, reverse=True)
    assert abs(sum(wins) + out["field_win_pct"] - 100) < 0.01
    assert len({tuple(sorted(l["ids"])) for l in lus}) == len(lus)
    assert all(len(l["ids"]) == 8 and l["salary"] <= DK_SALARY_CAP for l in lus)
