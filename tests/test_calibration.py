import numpy as np
import pandas as pd

from pipeline import calibration, distribution


def _rows(n=6000, seed=1):
    """Synthetic backtest: real outcomes skewed, 8% above the raw projection, and wider
    when a fact (f_bump) is on; another fact (f_total) is pure noise."""
    rng = np.random.default_rng(seed)
    proj = rng.uniform(3, 55, n)
    bump = (rng.random(n) < 0.3).astype(float)
    true = proj * 1.08
    cv = 1.6 * true ** -0.5 * (1 + 0.6 * bump)
    k = 1 / cv ** 2
    act = rng.gamma(k, true / k)
    days = np.array([f"2026-01-{1 + i % 28:02d}" for i in range(n)])
    return pd.DataFrame({"proj": proj, "act_fp": act, "date": days, "f_bump": bump,
                         "f_total": rng.normal(114, 5, n), "minutes": proj, "act_min": proj * 0.95})


def test_distribution_learns_median_and_useful_facts_only():
    df = _rows()
    m = distribution.fit(df)
    assert m["used"]
    assert "f_bump" in m["feats"] and "f_total" not in m["feats"]
    # median of the curve tracks the real median (gamma median of 1.08 * proj)
    q_off = distribution.curve(30.0, {"f_bump": 0.0}, m)
    q_on = distribution.curve(30.0, {"f_bump": 1.0}, m)
    us = distribution.U_SAMPLE
    med = np.interp(0.5, us, q_off)
    sub = df[(df.proj > 27) & (df.proj < 33) & (df.f_bump == 0)]
    assert abs(med - sub.act_fp.median()) < 1.5
    # the fact widens the distribution: higher ceiling, lower floor
    assert np.interp(0.9, us, q_on) > np.interp(0.9, us, q_off)
    assert np.interp(0.1, us, q_on) < np.interp(0.1, us, q_off)
    # held-out: each percentile is where it should be
    for u, share in m["held_out"]["share_below"].items():
        assert abs(share - float(u)) < 0.025


def test_summary_consistent_with_curve():
    m = distribution.fit(_rows())
    q = distribution.curve(25.0, {"f_bump": 0.0}, m)
    s = distribution.summary(q, 6000, 6, 4)
    assert s["floor"] < s["median"] < s["ceiling"]
    assert s["mean"] > s["median"]                      # right-skewed outcomes
    # boom = P(X >= 36): consistent with the curve
    assert abs(s["boom"] / 100 - (1 - np.interp(36.0, q, us := distribution.U_SAMPLE))) < 1e-6


def test_calibration_fit_and_small_sample():
    cal = calibration.fit(_rows())
    assert cal["used"] and cal["dist"]["used"] and cal["minutes"]["x"]
    assert not calibration.fit(_rows(500))["used"]
