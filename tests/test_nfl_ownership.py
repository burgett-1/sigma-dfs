"""NFL ownership: slot totals, caps, OUT players, shape calibration privacy."""
import json

import pytest

from pipeline import contests
from pipeline.sports.nfl import ownership as O
from tests.fixtures.nfl import synthetic_nfl as N


def _rows(kind="classic"):
    rows = []
    for team in N.TEAMS:
        for pos, n in N.ROSTER:
            if pos == "K" and kind == "classic":
                continue
            for i in range(n):
                proj = 18.0 / (i + 1)
                rows.append({"name": N.name(team, pos, i), "position": pos, "proj": proj,
                             "value": proj / (N.SALARY[pos][i] / 1000), "implied_total": 22.0, "status": ""})
        rows.append({"name": N.NICK[team], "position": "DST", "proj": 7.0, "value": 2.2, "implied_total": 22.0,
                     "status": ""})
    return rows


def test_classic_totals_by_group():
    rows = _rows()
    rows[0]["proj"] = 0.0                      # an OUT player
    O.estimate(rows)
    assert sum(r["own"] for r in rows) == pytest.approx(900, abs=0.5)
    tot = O.group_totals()
    for pos in ("QB", "RB", "WR", "TE", "DST"):
        assert sum(r["own"] for r in rows if r["position"] == pos) == pytest.approx(tot[pos], abs=0.5)
    assert tot["QB"] == 100 and tot["DST"] == 100
    assert rows[0]["own"] == 0 and max(r["own"] for r in rows) <= 80 + 1e-9


def test_cap_respread_inside_group():
    rows = _rows()
    for r in rows:
        if r["position"] == "QB" and r["name"] != "Buf Qb A":
            r["proj"], r["value"] = 1.0, 0.1
    O.estimate(rows)
    assert max(r["own"] for r in rows) <= 80 + 1e-9
    assert sum(r["own"] for r in rows if r["position"] == "QB") == pytest.approx(100, abs=0.5)


def test_showdown_total():
    rows = [r for r in _rows("showdown") if r["name"].startswith(("Mia", "Buf")) or r["name"] in ("Bills", "Dolphins")]
    O.estimate(rows, "showdown")
    assert sum(r["own"] for r in rows) == pytest.approx(600, abs=0.5)


def test_shape_calibration_is_aggregate_only(tmp_path):
    text = N.contest_export(120)
    c = contests.parse(text)
    c["file_date"] = "2026-11-30"
    import pandas as pd
    games = pd.DataFrame([{"season": 2026, "week": 12, "gameday": "2026-11-29"}])
    hist = N.history()
    cal = O.calibrate([c], hist, games)
    assert cal["used"] and cal["contests"] == 1 and set(cal["tau"]) == {"QB", "RB", "WR", "TE", "DST"}
    assert 0 < cal["weight"] < 1 and abs(sum(cal["flex_split"].values()) - 1) < 0.01
    blob = json.dumps(cal)
    assert "fakeuser" not in blob and "Buf" not in blob and "Bills" not in blob
