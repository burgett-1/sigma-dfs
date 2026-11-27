import json
import os
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
    monkeypatch.setattr(contests, "PUBLIC_OUT", tmp_path / "public" / "contest_history.json")
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("SIGMA_CONTEST_IMPORT", str(dl))
    monkeypatch.delenv("SIGMA_DK_USERNAME", raising=False)
    monkeypatch.setattr(contests, "SETTLE_SECONDS", 0)     # files written by a test are complete at once
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


# ── Analyst: slot summing, sport detection, NBA-only analysis, public output ──
from pathlib import Path  # noqa: E402

REAL_DIR = Path(os.environ.get("SIGMA_TEST_REAL_CONTESTS", Path.home() / "Downloads" / "DK DFS Post Contest Results"))


def _slot_export(player_rows, lineup_slots=("PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"), n_entries=12, tag="u"):
    lines = [HEADER]
    for i in range(max(n_entries, len(player_rows))):
        if i < n_entries:
            lu = " ".join(f"{s} {PLAYERS[(i + k) % 10]}" for k, s in enumerate(lineup_slots))
            left = f"{i + 1},{5000 + i},{tag}{i} (1/3),0,{250 - i},{lu}"
        else:
            left = ",,,,,"
        right = ",".join(player_rows[i]) if i < len(player_rows) else ",,,"
        lines.append(f"{left},,{right}\n")
    return "".join(lines)


def test_parse_sums_ownership_across_slots():
    rows = [("Immanuel Quickley", "PG", "28.73%", "38.75"), ("Immanuel Quickley", "G", "19.73%", "38.75"),
            ("Immanuel Quickley", "UTIL", "4.17%", "38.75"), ("Alpha One", "C", "10%", "20")]
    res = contests.parse(_slot_export(rows))
    k = contests.norm_name("Immanuel Quickley")
    assert res["own"][k] == pytest.approx(52.63, abs=0.01)
    assert res["own_slots"][k] == {"PG": 28.73, "G": 19.73, "UTIL": 4.17}
    assert res["fpts"][k] == 38.75
    assert res["own_total"] == pytest.approx(62.63, abs=0.01)
    assert res["sport"] == "NBA" and res["kind"] == "classic"
    assert res["names"][k] == "Immanuel Quickley"


def test_parse_ignores_bad_percentages_and_short_rows():
    rows = [("Alpha One", "PG", "abc", "1"), ("Bravo Two", "SG", "nan%", "1"), ("Charlie Three", "SF", "inf", "1"),
            ("Delta Four", "PF", "12.5%", "x")]
    text = _slot_export(rows) + "13,9,short\n"
    res = contests.parse(text)
    assert res["own"] == {contests.norm_name("Delta Four"): 12.5}
    assert len(res["entries"]) == 12


@pytest.mark.parametrize("slots,expected", [
    ({"PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"}, "NBA"),
    ({"PG", "SG", "SF", "PF", "C"}, "NBA"),
    ({"QB", "RB", "WR", "TE", "FLEX", "DST"}, "NFL"),
    ({"P", "C", "1B", "2B", "3B", "SS", "OF"}, "MLB"),
    ({"G"}, "OTHER"),
    ({"F"}, "OTHER"),
    ({"CPT", "UTIL"}, "UNKNOWN"),
    (set(), "UNKNOWN"),
])
def test_detect_sport(slots, expected):
    assert contests.detect_sport(slots) == expected


def test_detect_sport_showdown_with_nba_names():
    names = ["Alpha One", "Bravo Two", "Charlie Three", "Delta Four"]
    nba = {contests.norm_name(n) for n in names[:2]}
    assert contests.detect_sport({"CPT", "UTIL"}, names=names, nba_names=nba) == "NBA"        # 50%
    assert contests.detect_sport({"CPT", "UTIL"}, names=names, nba_names=nba - {contests.norm_name("Alpha One")}) == "UNKNOWN"


def test_nba_only_stats_and_calibration(private):
    rng = np.random.default_rng(1)
    names = [f"Player {chr(65 + i)}{chr(65 + j)}" for i in range(8) for j in range(8)]
    proj = rng.uniform(1, 40, len(names))
    contests.snapshot(7, "2026-01-02", [{"name": n, "proj": 20, "own": o, "salary": 5000} for n, o in zip(names, proj)])
    own = {contests.norm_name(n): float(o) * 1.2 for n, o in zip(names, proj)}
    nba = {"id": "1", "sport": "NBA", "own": own, "file_date": "2026-01-02", "kind": "classic",
           "entries": [{"rank": 1, "points": 300.0, "user": "a", "entry": "a", "players": []}], "fpts": {}}
    nfl = dict(nba, id="2", sport="NFL")
    one = contests.fit_own_calibration([nba])
    both = contests.fit_own_calibration([nba, nfl])
    assert both["n"] == one["n"] == len(names) and both["contests_matched"] == one["contests_matched"] == 1
    assert contests.contest_stats(nfl, None) is None
    assert contests.contest_stats(nba, None)["id"] == "1"
    assert contests.is_nba({"own": {}})                 # legacy dicts without "sport" count as NBA


def test_refresh_writes_public_out_and_skips_nfl(private):
    own = [60, 45, 30, 8, 5, 3, 25, 12, 9, 2]
    (private / "contest-standings-111.csv").write_text(_export(["me (1/2)", "shark", "a", "b", "c", "d", "e", "f", "g", "h"], own))
    nfl_rows = [(PLAYERS[i], ["QB", "RB", "WR", "TE", "FLEX", "DST"][i % 6], "50%", "10") for i in range(10)]
    # Phase 2: import_new() no longer copies non-NBA files, so the NFL export is one that was
    # imported before (like the MLB files already in the real private folder)
    contests.CONTESTS.mkdir(parents=True, exist_ok=True)
    (contests.CONTESTS / "contest-standings-333.csv").write_text(_slot_export(nfl_rows, lineup_slots=("QB", "RB", "WR", "TE", "FLEX", "DST")))
    res = contests.refresh()
    assert res["contests"] == 2
    assert contests.PUBLIC_OUT.exists() and str(contests.PUBLIC_OUT).startswith(str(private.parent))
    doc = json.loads(contests.PUBLIC_OUT.read_text())
    assert doc["version"] == 1 and doc["samples"]["contests"] == 1
    assert doc["skipped"] == [{"reason": "not_nba", "detected": "NFL", "count": 1}]
    text = contests.PUBLIC_OUT.read_text()
    assert "shark" not in text and '"111"' not in text and "111" not in text
    rep = json.loads((contests.PRIVATE / "report.json").read_text())
    assert [c["id"] for c in rep["contests"]] == ["111"] and rep["skipped_non_nba"] == 1
    assert "Skipped 1 non-NBA" in (contests.PRIVATE / "report.html").read_text()


def test_refresh_survives_write_public_failure(private, monkeypatch):
    from pipeline import contest_history
    (private / "contest-standings-111.csv").write_text(_export(["me (1/2)", "x", "a", "b", "c", "d", "e", "f", "g", "h"], [5] * 10))

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(contest_history, "write_public", boom)
    assert contests.refresh()["contests"] == 1          # no exception
    assert (contests.PRIVATE / "report.json").exists() and not contests.PUBLIC_OUT.exists()


def test_refresh_survives_unwritable_public_path(private, monkeypatch):
    blocker = private.parent / "blocker"
    blocker.write_text("a file where a folder should be")
    monkeypatch.setattr(contests, "PUBLIC_OUT", blocker / "contest_history.json")
    (private / "contest-standings-111.csv").write_text(_export(["me (1/2)", "x", "a", "b", "c", "d", "e", "f", "g", "h"], [5] * 10))
    assert contests.refresh()["contests"] == 1          # real OSError inside write_public is swallowed


def test_load_all_reads_bom_file_in_private_folder(private):
    # a raw export dropped straight into the private contests folder keeps its BOM
    contests.CONTESTS.mkdir(parents=True, exist_ok=True)
    text = "﻿" + _export(["me (1/2)", "x", "a", "b", "c", "d", "e", "f", "g", "h"], [5] * 10)
    (contests.CONTESTS / "contest-standings-444.csv").write_text(text, encoding="utf-8")
    assert [c["id"] for c in contests.load_all()] == ["444"]


def test_parse_accepts_bom_prefixed_text():
    text = "﻿" + _export(["me (1/2)", "x", "a", "b", "c", "d", "e", "f", "g", "h"], [5] * 10)
    assert contests.parse(text) is not None


def test_real_quickley_52_63():
    from tests.test_contest_history import quickley_file
    f = quickley_file(REAL_DIR) if REAL_DIR.exists() else None
    if f is None:
        pytest.skip("no real export with the Quickley slot rows")
    res = contests.parse(f.read_text(encoding="utf-8-sig"))
    k = contests.norm_name("Immanuel Quickley")
    assert res["own"][k] == pytest.approx(52.63, abs=0.01)
    assert res["own_slots"][k] == {"PG": 28.73, "G": 19.73, "UTIL": 4.17}
    assert res["sport"] == "NBA" and 770 <= res["own_total"] <= 805


@pytest.mark.skipif(not REAL_DIR.exists(), reason="real exports absent")
def test_real_sport_split():
    sports = {}
    for f in sorted(REAL_DIR.glob("contest-standings-*.csv")):
        res = contests.parse(f.read_text(encoding="utf-8-sig"))
        sports[res["sport"]] = sports.get(res["sport"], 0) + 1
    assert sports == {"NBA": 8, "NFL": 4}


def test_refresh_twice_unchanged_keeps_public_file(private):
    import os
    import time
    (private / "contest-standings-111.csv").write_text(_export(["me (1/2)", "x", "a", "b", "c", "d", "e", "f", "g", "h"], [5] * 10))
    contests.refresh()
    out = contests.PUBLIC_OUT
    old = 1_600_000_000_000_000_000
    os.utime(out, ns=(old, old))
    before = (out.read_bytes(), out.stat().st_mtime_ns)
    time.sleep(1.1)
    contests.refresh()
    assert (out.read_bytes(), out.stat().st_mtime_ns) == before
    (private / "contest-standings-222.csv").write_text(_export(["me (1/1)", "y", "a", "b", "c", "d", "e", "f", "g", "h"], [7] * 10))
    contests.refresh()
    assert out.stat().st_mtime_ns != old and json.loads(out.read_text())["samples"]["contests"] == 2


# ── Phase 2: Downloads watcher ────────────────────────────────────────────────
def _nba_text(users=("u1", "u2", "u3", "u4", "u5", "u6", "u7", "u8", "u9", "u10")):
    return _export(list(users), [30, 20, 10, 8, 5, 3, 25, 12, 9, 2])


def _old(path, age=3600):
    t = path.stat().st_mtime - age
    os.utime(path, (t, t))


def test_import_new_accepts_nba_and_rejects_the_rest(private, monkeypatch):
    monkeypatch.setattr(contests, "SETTLE_SECONDS", 60)
    f_nba = private / "contest-standings-501.csv"
    f_nba.write_text(_nba_text())
    with zipfile.ZipFile(private / "contest-standings-502.zip", "w") as z:
        z.writestr("contest-standings-502.csv", _nba_text())
    (private / "contest-standings-503.csv").write_text("")                                     # 0-byte
    (private / "contest-standings-504.csv.crdownload").write_text(_nba_text())                 # partial
    (private / "contest-standings-505.csv").write_text(_nba_text())                            # still writing
    with zipfile.ZipFile(private / "contest-standings-506.zip", "w") as z:
        z.writestr("readme.txt", "no csv")
    nfl = [(PLAYERS[i], ["QB", "RB", "WR", "TE", "FLEX", "DST"][i % 6], "50%", "10") for i in range(10)]
    (private / "contest-standings-507.csv").write_text(_slot_export(nfl, lineup_slots=("QB", "RB", "WR", "TE", "FLEX", "DST")))
    mlb = [(PLAYERS[i], ["P", "C", "1B", "2B", "3B", "SS", "OF"][i % 7], "50%", "10") for i in range(10)]
    (private / "contest-standings-508.csv").write_text(_slot_export(mlb, lineup_slots=("P", "C", "1B", "2B", "3B", "SS", "OF")))
    fonly = [(PLAYERS[i], "F", "50%", "10") for i in range(10)]
    (private / "contest-standings-509.csv").write_text(_slot_export(fonly, lineup_slots=("F",)))
    (private / "contest-standings-510.csv").write_text("Name,Points\na,1\n")                  # malformed header
    dup = private.parent / "Downloads2"
    dup.mkdir()
    (dup / "contest-standings-501.csv").write_text(_nba_text())
    monkeypatch.setenv("SIGMA_CONTEST_IMPORT", str(private) + os.pathsep + str(dup))
    for f in list(private.iterdir()) + list(dup.iterdir()):
        if f.name != "contest-standings-505.csv":
            _old(f)
    before = {f: (f.stat().st_size, f.stat().st_mtime) for f in list(private.iterdir()) + list(dup.iterdir())}
    new = contests.import_new()
    assert sorted(new) == ["501", "502"]
    rej = json.loads((contests.CONTESTS / "index.json").read_text())["rejected"]
    assert rej["503"]["reason"] == "empty" and rej["504"]["reason"] == "partial" and rej["506"]["reason"] == "no_csv"
    assert rej["507"]["reason"] == "not_nba:NFL" and rej["508"]["reason"] == "not_nba:MLB"
    assert rej["509"]["reason"] == "not_nba:OTHER" and rej["510"]["reason"] == "malformed"
    assert "505" not in rej                                            # settling files are simply retried later
    assert {f: (f.stat().st_size, f.stat().st_mtime) for f in before} == before    # originals untouched
    calls = []
    real = contests.parse
    monkeypatch.setattr(contests, "parse", lambda t: calls.append(1) or real(t))
    assert contests.import_new() == []                                 # cached rejects aren't re-parsed
    assert calls == []
    _old(private / "contest-standings-505.csv")
    assert contests.import_new() == ["505"]


def test_rejected_file_retried_when_it_changes(private):
    f = private / "contest-standings-601.csv"
    f.write_text("")
    _old(f)
    assert contests.import_new() == []
    f.write_text(_nba_text())
    _old(f, 100)
    assert contests.import_new() == ["601"]
    assert "601" not in json.loads((contests.CONTESTS / "index.json").read_text()).get("rejected", {})


def test_entry_history_files_are_not_standings(private):
    (private / "draftkings-contest-entry-history.csv").write_text("Sport,Contest_Key\nNBA,123\n")
    assert contests.import_new() == []


def test_watch_refreshes_once_and_is_rate_limited(private, monkeypatch):
    calls = []
    real = contests.refresh
    monkeypatch.setattr(contests, "refresh", lambda: calls.append(1) or real())
    monkeypatch.setattr(contests, "_WATCH", {"at": 0.0})
    assert contests.watch(now=1000.0) == [] and calls == []            # nothing new: no refresh
    (private / "contest-standings-701.csv").write_text(_nba_text(("zz_user_one", "b", "c", "d", "e", "f", "g", "h", "i", "j")))
    _old(private / "contest-standings-701.csv")
    assert contests.watch(now=1030.0) == []                            # < 60 s since the last scan
    assert contests.watch(now=1061.0) == ["701"] and calls == [1]
    text = contests.PUBLIC_OUT.read_text()
    assert "701" not in text and "zz_user_one" not in text and "EntryId" not in text
    assert json.loads(text)["samples"]["contests"] == 1
    assert contests.watch(now=1200.0) == [] and calls == [1]
