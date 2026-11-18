import json
import zipfile

import numpy as np
import pytest

from pipeline import contests, ownership, sim

HEADER = "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS\n"
PLAYERS = ["Alpha One", "Bravo Two", "Charlie Three", "Delta Four", "Echo Five", "Fox Six", "Golf Seven",
           "Hotel Eight", "India Nine", "Juliet Ten"]


def _export(users, own):
    lines = [HEADER]
    for i, u in enumerate(users):
        lu = " ".join(f"{s} {PLAYERS[(i + k) % 10]}" for k, s in enumerate(["C", "F", "G", "PF", "PG", "SF", "SG", "UTIL"]))
        extra = f"{PLAYERS[i]},PG,{own[i]}%,{40 - i}" if i < len(PLAYERS) else ",,,"
        lines.append(f"{i + 1},{1000 + i},{u},0,{300 - 5 * i},{lu},,{extra}\n")
    return "".join(lines)


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setattr(contests, "PRIVATE", tmp_path / "priv")
    monkeypatch.setattr(contests, "CONTESTS", tmp_path / "priv" / "contests")
    monkeypatch.setattr(contests, "SNAPSHOTS", tmp_path / "priv" / "projections")
    monkeypatch.setattr(contests, "OWN_CAL", tmp_path / "priv" / "own_calibration.json")
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("SIGMA_CONTEST_IMPORT", str(dl))
    monkeypatch.delenv("SIGMA_DK_USERNAME", raising=False)
    return dl


def test_split_lineup_classic_and_showdown():
    assert contests.split_lineup("C Nikola Jokic F LeBron James UTIL Jalen Williams") == [
        ("C", "Nikola Jokic"), ("F", "LeBron James"), ("UTIL", "Jalen Williams")]
    assert contests.split_lineup("CPT Luka Doncic UTIL Kyrie Irving")[0] == ("CPT", "Luka Doncic")
    # a name containing a slot word as part of a longer word is not split
    assert contests.split_lineup("PG Cade Cunningham C Clint Capela") == [("PG", "Cade Cunningham"), ("C", "Clint Capela")]


def test_import_report_and_username(private):
    own = [60, 45, 30, 8, 5, 3, 25, 12, 9, 2]
    users = ["me (1/2)", "shark", "me (2/2)", "fish", "whale", "x", "y", "z", "w", "v"]
    (private / "contest-standings-111.csv").write_text(_export(users, own))
    with zipfile.ZipFile(private / "contest-standings-222.zip", "w") as z:
        z.writestr("contest-standings-222.csv", _export(["me (1/1)", "other", "a", "b", "c", "d", "e", "f", "g", "h"], own))
    res = contests.refresh()
    assert res["contests"] == 2 and sorted(res["new"]) == ["111", "222"]
    rep = json.loads((contests.PRIVATE / "report.json").read_text())
    assert rep["me"] == "me"                       # the only user in every contest
    c111 = next(c for c in rep["contests"] if c["id"] == "111")
    assert c111["entries"] == 10 and c111["mine"]["entries"] == 2 and c111["mine"]["best_rank"] == 1
    assert c111["winner_total_own"] == pytest.approx(sum(own[:8]))
    assert (contests.PRIVATE / "report.html").read_text().startswith("<!doctype html>")
    # importing again adds nothing; originals are left where they were
    assert contests.refresh()["new"] == []
    assert (private / "contest-standings-111.csv").exists()


def test_ownership_correction_is_shrunk(private):
    rng = np.random.default_rng(0)
    names = [f"Player {chr(65 + i)}{chr(65 + j)}" for i in range(10) for j in range(10)]
    proj_own = rng.uniform(1, 40, len(names))
    real = np.clip(proj_own ** 1.3 / 3, 0.1, 90)        # real ownership more concentrated
    snap_rows = [{"name": n, "proj": 20, "own": o, "salary": 5000} for n, o in zip(names, proj_own)]
    contests.snapshot(1, "2026-01-01", snap_rows)
    c = {"own": {contests.norm_name(n): float(r) for n, r in zip(names, real)}, "file_date": "2026-01-01"}
    cal = contests.fit_own_calibration([c])
    assert cal["used"] and cal["slope_raw"] > 1.1
    assert 1.0 < cal["slope"] < cal["slope_raw"]          # shrunk toward no change
    assert cal["weight"] <= 0.5
    rows = [{"proj": 20 + i, "value": 3 + i / 25, "status": "", "bump": 0} for i in range(120)]
    ownership.estimate_ownership(rows, 8, own_cal={})
    base = [r["own"] for r in rows]
    ownership.estimate_ownership(rows, 8, own_cal=cal)
    assert sum(r["own"] for r in rows) == pytest.approx(800, rel=1e-6)
    assert max(r["own"] for r in rows) > max(base)       # sharper, as the data said


def test_portfolio_greedy_coverage():
    beats = np.zeros((16, 4), dtype=bool)
    beats[:8, 0] = True            # lineup 0 wins sims 0-7
    beats[:7, 1] = True            # lineup 1 mostly overlaps lineup 0
    beats[8:12, 2] = True          # lineup 2 wins different sims
    picks, cum = sim.portfolio(np.packbits(beats, axis=0), 16, 3)
    assert picks[:2] == [0, 2]     # the diversifying lineup beats the overlapping one
    assert cum[1] == pytest.approx(75.0)
