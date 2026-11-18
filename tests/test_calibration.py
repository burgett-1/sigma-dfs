import numpy as np
import pandas as pd

from pipeline import calibration


def _rows(n=4000, seed=1):
    rng = np.random.default_rng(seed)
    proj = rng.uniform(3, 55, n)
    true = proj * 1.08                                    # model 8% low
    cv = 1.6 * true ** -0.5
    k = 1 / cv ** 2
    act = rng.gamma(k, true / k)
    mins = proj / 1.0
    return pd.DataFrame({"proj": proj, "act_fp": act, "cv_player": 0.3, "minutes": mins, "act_min": mins * 0.95})


def test_fit_recovers_level_and_spread():
    cal = calibration.fit(_rows())
    assert cal["used"]
    mid = calibration.apply_level(np.array([30.0]), cal)[0]
    assert 30 * 1.03 < mid < 30 * 1.13
    assert -0.7 < cal["cv_b"] < -0.3                       # relative spread shrinks with projection
    assert calibration.apply_level(np.array([30.0]), {"proj_x": cal["min_x"], "proj_y": cal["min_y"]})[0] < 30


def test_quantiles_are_consistent():
    m, cv = 30.0, 0.4
    lo, hi = calibration.quantile(m, cv, 0.1), calibration.quantile(m, cv, 0.9)
    assert lo < m < hi
    assert abs(calibration.prob_at_least(m, cv, hi) - 0.10) < 0.01
    assert abs(calibration.prob_at_least(m, cv, lo) - 0.90) < 0.01
    rng = np.random.default_rng(0)
    k = 1 / cv ** 2
    x = rng.gamma(k, m / k, 200_000)
    assert abs((x > hi).mean() - 0.10) < 0.01             # matches a real gamma


def test_small_sample_not_used():
    assert not calibration.fit(_rows(200))["used"]
