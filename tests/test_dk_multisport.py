"""DraftKings lobby / salary CSV per sport: gt/ct split, windows, Roster Position
eligibility (fail closed), Status, header-only files, the per-sport cache."""
import copy
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import health, sports
from pipeline.sources import draftkings as dk
from tests.fixtures.nfl import synthetic_nfl as N

FIX = Path(__file__).parent / "fixtures"


def _lobby(name):
    return json.loads((FIX / "dk" / name).read_text())


def _legacy_nba_filter(data, now):
    """main's list_draft_groups filter, copied verbatim, to prove NBA output is unchanged."""
    from pipeline.config import ET
    day = dk.dfs_day(now)
    start_win = datetime.combine(day, datetime.min.time(), ET) + timedelta(hours=6)
    end_win = start_win + timedelta(hours=24)
    out = []
    for g in data.get("DraftGroups") or []:
        tag = " ".join(str(g.get(k) or "") for k in ("DraftGroupTag", "ContestStartTimeSuffix", "GameType",
                                                      "ContestTypeName")).lower()
        if any(w in tag for w in ("tiers", "snake", "pick6", "in-game", "best ball", "arcade")):
            continue
        ctype = g.get("ContestTypeId") or g.get("GameTypeId")
        if ctype is not None and int(ctype) not in {70: "classic", 81: "showdown"}:
            continue
        start = dk.parse_dt(g.get("StartDate") or g.get("StartDateEst") or g.get("MinStartTime"))
        if not start or not (start_win <= start < end_win):
            continue
        out.append((int(g["DraftGroupId"]), {70: "classic", 81: "showdown"}[int(ctype)], int(ctype), start,
                    g.get("DraftGroupTag") == "Featured"))
    return sorted(out)


@pytest.mark.parametrize("now", [datetime(2026, 11, 26, 15, tzinfo=timezone.utc),
                                 datetime(2026, 11, 27, 15, tzinfo=timezone.utc)])
def test_nba_lobby_fixture_unchanged(monkeypatch, now):
    data = _lobby("lobby_nba_min.json")
    urls = []
    monkeypatch.setattr(dk, "get_json", lambda url: urls.append(url) or data)
    got = sorted((s.draft_group_id, s.kind, s.contest_type, s.start_utc, s.featured) for s in dk.list_draft_groups(now))
    assert got == _legacy_nba_filter(data, now)
    assert urls == ["https://www.draftkings.com/lobby/getcontests?sport=NBA"]


def test_nfl_lobby_uses_game_type_and_keeps_contest_type(monkeypatch):
    data = _lobby("lobby_nfl_min.json")
    urls = []
    monkeypatch.setattr(dk, "get_json", lambda url: urls.append(url) or data)
    now = datetime(2026, 11, 26, 15, tzinfo=timezone.utc)
    got = dk.list_draft_groups(now, "NFL")
    assert urls == ["https://www.draftkings.com/lobby/getcontests?sport=NFL"]
    raw = {g["DraftGroupId"]: g for g in data["DraftGroups"]}
    assert got
    for s in got:
        g = raw[s.draft_group_id]
        if g["GameTypeId"] == 1:
            assert s.kind == "classic" and s.contest_type == 21 and g["ContestTypeId"] == 21
        else:
            assert g["GameTypeId"] == 96 and s.kind == "showdown" and s.contest_type == 96
        assert s.start_utc < now + timedelta(hours=144)
        assert dk.SALARY_CSV_URL.format(ct=s.contest_type, dg=s.draft_group_id).count("contestTypeId=") == 1
    classic = [s for s in got if s.kind == "classic"]
    assert classic and all(f"contestTypeId=21&draftGroupId={s.draft_group_id}" in
                           dk.SALARY_CSV_URL.format(ct=s.contest_type, dg=s.draft_group_id) for s in classic)


def test_lookahead_excludes_a_group_38_days_out(monkeypatch):
    monkeypatch.setattr(dk, "get_json", lambda url: N.lobby())
    got = {s.draft_group_id: s for s in dk.list_draft_groups(N.NOW, "NFL")}
    assert set(got) == {900001, 900002}            # week-18 group (38 days) and Tiers are left out
    assert got[900001].kind == "classic" and got[900001].contest_type == 21
    assert got[900002].kind == "showdown"


def test_golf_classic_recognised_when_enabled(monkeypatch):
    reg = copy.deepcopy(sports.REGISTRY)
    reg["sports"]["GOLF"]["enabled"] = True
    monkeypatch.setattr(sports, "REGISTRY", reg)
    monkeypatch.setattr(dk, "_cfg", lambda sport: reg["sports"][sport])
    monkeypatch.setattr(dk, "get_json", lambda url: _lobby("lobby_golf_min.json"))
    got = dk.list_draft_groups(datetime(2026, 12, 1, 15, tzinfo=timezone.utc), "GOLF")
    classic = [s for s in got if s.kind == "classic"]
    assert classic and all(s.contest_type == 29 for s in classic)


def _slate(kind="classic", dg=900001):
    return dk.Slate(draft_group_id=dg, name="x", start_utc=N.NOW, game_count=4, featured=True, kind=kind,
                    contest_type=21 if kind == "classic" else 96)


def test_nfl_classic_csv_parse():
    s = dk.load_salary_csv(_slate(), (FIX / "nfl" / "dk_classic_4g.csv").read_text(), "NFL")
    assert s.kind == "classic" and s.game_count == 4 and not s.excluded
    by = {p["name"]: p for p in s.players}
    wr = by["Buf Wr A"]
    assert wr["position"] == "WR" and wr["eligible"] == ["WR", "FLEX"] and wr["pos_code"] == 4
    assert wr["team"] == "BUF" and wr["opp"] == "MIA" and wr["dk_avg"] > 0
    dst = by["Bills"]
    assert dst["position"] == "DST" and dst["team"] == "BUF" and dst["eligible"] == ["DST"]
    assert not any(p["position"] == "K" for p in s.players)


def test_status_column_mapped():
    text = N.salary_csv("classic", statuses={"Buf Wr A": "Q", "Buf Rb A": "IR", "Mia Te A": "D", "Kc Qb A": "O"})
    by = {p["name"]: p for p in dk.load_salary_csv(_slate(), text, "NFL").players}
    assert by["Buf Wr A"]["dk_status"] == "QUESTIONABLE" and by["Buf Rb A"]["dk_status"] == "OUT"
    assert by["Mia Te A"]["dk_status"] == "DOUBTFUL" and by["Kc Qb A"]["dk_status"] == "OUT"
    assert by["Buf Wr B"]["dk_status"] == ""


def test_fail_closed_exclusions_are_counted():
    text = N.salary_csv("classic")
    lines = text.splitlines()
    k_row = "K,Buf K A (47999001),Buf K A,47999001,K,4800,MIA@BUF 11/29/2026 01:00PM ET,BUF,8.0,"
    bad_info = lines[1].replace("MIA@BUF 11/29/2026 01:00PM ET", "Postponed")
    text = "\n".join(lines[:1] + [k_row, bad_info] + lines[2:]) + "\n"
    health._scoped.clear()
    with health.scope("NFL"):
        s = dk.load_salary_csv(_slate(), text, "NFL")
    assert s.excluded == {"eligibility": 1, "game_info": 1}
    assert all(p["name"] not in ("Buf K A", "Mia Qb A") for p in s.players) and len(s.players) == 104 - 1
    assert "DraftKings player eligibility" in health.snapshot("NFL")
    assert "DraftKings player eligibility" not in health.snapshot()


def test_nfl_showdown_csv_parse():
    s = dk.load_salary_csv(_slate("showdown", 900002), (FIX / "nfl" / "dk_showdown.csv").read_text(), "NFL")
    assert s.kind == "showdown"
    by = {p["name"]: p for p in s.players}
    assert {"K", "DST"} <= {p["position"] for p in s.players}
    for p in s.players:
        assert p["cpt_salary"] == int(p["salary"] * 1.5) and p["cpt_id"] and p["eligible"] == ["CPT", "FLEX"]
    assert by["Buf K A"]["position"] == "K"


def test_kind_conflict_csv_wins_with_warning():
    health._scoped.clear()
    with health.scope("NFL"):
        s = dk.load_salary_csv(_slate("classic", 900002), (FIX / "nfl" / "dk_showdown.csv").read_text(), "NFL")
    assert s.kind == "showdown" and not health.snapshot("NFL")["DraftKings slate kind"]["ok"]


def test_unpopulated_group_is_skipped(monkeypatch):
    monkeypatch.setattr(dk, "get_text", lambda url: (FIX / "nfl" / "dk_unpopulated.csv").read_text())
    with pytest.raises(ValueError):
        dk.load_draftables(_slate(dg=900009), "NFL", ttl_s=60)


def test_ttl_cache_is_per_sport(monkeypatch):
    calls = []
    monkeypatch.setattr(dk, "get_text", lambda url: calls.append(url) or N.salary_csv("classic"))
    s = dk.load_draftables(_slate(), "NFL", ttl_s=600)
    cache = dk._salary_cache(900001, "NFL")
    assert cache.exists() and "nfl" in cache.parts and len(calls) == 1 and s.players
    dk.load_draftables(_slate(), "NFL", ttl_s=600)
    assert len(calls) == 1                                     # fresh cache: no refetch
    old = time.time() - 3600
    os.utime(cache, (old, old))
    dk.load_draftables(_slate(), "NFL", ttl_s=600)
    assert len(calls) == 2


def test_nba_salary_path_unchanged_on_synthetic_csv():
    from tests import synthetic as syn
    s = dk.load_salary_csv(dk.Slate(555, "x", syn.utc(2026, 11, 16, 0), 2, True), syn.salary_csv(syn.utc(2026, 11, 16, 0, 30)))
    keys = {"draftable_id", "dk_player_id", "name", "position", "salary", "cpt_id", "cpt_salary", "team_dk", "team",
            "opp_dk", "opp", "home", "competition_id", "start_utc", "dk_status", "disabled"}
    assert all(set(p) == keys for p in s.players) and all(p["dk_status"] == "" for p in s.players)
    assert s.excluded == {}


@pytest.mark.skipif(not (Path(__file__).parent.parent / "data" / "state" / "salaries").exists(),
                    reason="no local NBA salary cache")
def test_nba_local_cache_csvs_parse_without_exclusions():
    folder = Path(__file__).parent.parent / "data" / "state" / "salaries"
    n = 0
    for f in sorted(folder.glob("*.csv")):
        s = dk.load_salary_csv(dk.Slate(int(f.stem), "x", N.NOW, 0, False), f.read_text(encoding="utf-8-sig"))
        assert not s.excluded and all("eligible" not in p for p in s.players)
        n += 1
    assert n
