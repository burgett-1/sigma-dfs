import numpy as np
import pandas as pd

from pipeline import benchmark, distribution, model


def _rows(nights, per_night, ratio, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for d in range(nights):
        for i in range(per_night):
            proj = rng.uniform(41, 60)
            out.append({"day": f"2026-12-{d + 1:02d}", "name": f"P{d}_{i}", "team": "AAA", "proj": proj,
                        "act": proj * ratio * rng.lognormal(0, 0.25)})
    return pd.DataFrame(out)


def test_no_correction_without_enough_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "DIR", tmp_path)
    assert benchmark.evaluate(_rows(7, 10, 0.9))["level_correction"] is None          # 7 nights: too few
    assert benchmark.evaluate(_rows(12, 20, 1.0))["level_correction"] is None         # no real gap


def test_correction_when_data_shows_it(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "DIR", tmp_path)
    rep = benchmark.evaluate(_rows(12, 20, 0.88))
    top = [c for c in rep["level_correction"] if c["lo"] == 40][0]
    assert 0.83 < top["ratio"] < 0.93
    # applied to a player's whole outcome curve, median and all
    r = {"name": "X", "team": "AAA", "proj": 45.0,
         "q": np.array(distribution.U_SAMPLE) * 90.0}
    ctx = model.Context(today=None, statuses={}, odds={}, benchmark=rep)
    model.apply_benchmark(r, ctx)
    assert abs(r["proj"] - 45.0 * top["ratio"]) < 0.01 and r["proj_base"] == 45.0
