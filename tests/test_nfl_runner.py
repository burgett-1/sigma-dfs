"""NFL runner jobs on fixtures: data layout, empty and edge slates, health
isolation from NBA, owner sims, privacy, runtime budget."""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import config, health
from pipeline.config import SIM
from pipeline.scheduler import Scheduler
from pipeline.sports.nfl import data as D
from pipeline.sports.nfl import runner as R
from pipeline.sports.nfl import teams as T
from tests.fixtures.nfl import synthetic_nfl as N


def _get_json(lobby=None):
    def g(url):
        if "lobby" in url:
            return lobby if lobby is not None else N.lobby()
        if "scoreboard" in url:
            return {"events": []}
        if "injuries" in url:
            return {"injuries": []}
        raise AssertionError(f"unexpected request {url}")
    return g


@pytest.fixture
def runner():
    D.save_history(N.history())
    return R.NFLRunner(get_json=_get_json(), get_text=N.fake_dk_text)


def _pass(r, now=N.NOW):
    for job in ("lobby", "salaries", "context"):
        getattr(r, job)(now)
    r.ctx = N.context()
    return r.project(now)


def _hashes(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha1(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


def test_pass_writes_only_nfl_and_scheduler_files(runner):
    root = Path(config.DATA_DIR)
    (root / "meta.json").write_text(json.dumps({"slates": [{"id": 1}], "sources": {}}))
    (root / "slates.json").write_text("[]")
    before = _hashes(root)
    sched = Scheduler(["NFL"], data_dir=root, runners={"NFL": runner})
    res = sched.run_due(N.NOW, 10_000.0)
    assert {"NFL:lobby", "NFL:salaries", "NFL:context", "NFL:project", "NFL:history"} <= set(res["ran"])
    after = _hashes(root)
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed and all(k.startswith("nfl/") or k in ("sports.json", "state/dg_index.json", "state/scheduler.json")
                           for k in changed), changed
    meta = json.loads((root / "nfl" / "meta.json").read_text())
    assert meta["sport"] == "NFL" and meta["scoring"]["verified"] is False and meta["join_rate"] == 1.0
    assert meta["correlation"]["model"] == "factor" and meta["rules"]["stack_rules"]["qb_stack_min"] == 1
    doc = json.loads((root / "sports.json").read_text())
    assert [s["code"] for s in doc["sports"]] == ["NBA", "NFL"]
    nfl = doc["sports"][1]
    assert nfl["base"] == "nfl/" and nfl["status"] == "live" and nfl["scoring_verified"] is False
    assert set(nfl) == {"code", "label", "base", "enabled", "in_season", "status", "slates", "next_lock_utc",
                        "season_note", "scoring_verified"}
    idx = json.loads((root / "state" / "dg_index.json").read_text())
    assert idx["groups"] == {"900001": "NFL", "900002": "NFL"} and sched.sport_for(900001) == "NFL"
    assert sched.sport_for(555) is None


def test_outputs_match_nba_schemas(runner):
    _pass(runner)
    root = Path(config.DATA_DIR) / "nfl"
    from pipeline.run import CSV_HEADER
    lines = (root / "slates" / "900001.csv").read_text().splitlines()
    assert lines[0].split(",") == CSV_HEADER
    first = dict(zip(CSV_HEADER, lines[1].split(",")))
    assert first["Minutes"] == first["FptsPerMin"] == first["Starting"] == first["BatOrder"] == ""
    assert first["DKID"].isdigit() and len(first["Quantiles"].split(";")) == 25
    slates = json.loads((root / "slates.json").read_text())
    s = next(x for x in slates if x["id"] == 900001)
    assert s["main"] and s["kind"] == "classic" and s["size_bucket"] == "medium" and len(s["games"]) == 4
    assert next(x for x in slates if x["id"] == 900002)["kind"] == "showdown"
    proj = json.loads((root / "projections.json").read_text())
    p = proj["players"][0]
    assert {"implied_total", "spread", "wind", "targets_share", "carries_share", "dk_avg", "own", "q"} <= set(p)


def test_nba_health_never_sees_nfl_sources(runner):
    health._status.clear()
    health.ok("DraftKings lobby", "nba")
    _pass(runner)
    assert set(health.snapshot()) == {"DraftKings lobby"} and health.snapshot()["DraftKings lobby"]["detail"] == "nba"
    assert "DraftKings salaries" in health.snapshot("NFL")


def test_no_slate_in_window(runner):
    runner.get_json = _get_json({"DraftGroups": []})
    runner.lobby(N.NOW)
    res = runner.project(N.NOW)
    root = Path(config.DATA_DIR) / "nfl"
    meta = json.loads((root / "meta.json").read_text())
    assert res["changed"] and meta["message"] == R.NO_SLATE_MESSAGE and meta["slates"] == []
    assert json.loads((root / "slates.json").read_text()) == []


def test_started_group_dropped_by_dk_is_kept_until_last_start_plus_4h(runner):
    _pass(runner)
    runner.get_json = _get_json({"DraftGroups": []})
    kick = datetime(2026, 11, 29, 18, 5, tzinfo=timezone.utc)
    runner.lobby(kick)
    assert "900001" in runner.state()["groups"]
    last = datetime(2026, 11, 29, 21, 25, tzinfo=timezone.utc)
    runner.lobby(last + timedelta(hours=3, minutes=59))
    assert "900001" in runner.state()["groups"]
    runner.lobby(last + timedelta(hours=4, minutes=1))
    assert "900001" not in runner.state()["groups"]


def test_unpopulated_group_skipped_and_bad_game_info_excluded(runner, monkeypatch):
    lob = N.lobby()
    lob["DraftGroups"].append(dict(lob["DraftGroups"][0], DraftGroupId=900005))
    runner.get_json = _get_json(lob)
    bad = N.salary_csv("classic").replace("MIA@BUF 11/29/2026 01:00PM ET,MIA,17.8", "Postponed,MIA,17.8", 1)

    def text(url):
        return bad if "draftGroupId=900001" in url else N.fake_dk_text(url)
    runner.get_text = text
    _pass(runner)
    st = runner.state()["groups"]
    assert "900005" in st and st["900005"].get("skipped")
    meta = json.loads((Path(config.DATA_DIR) / "nfl" / "meta.json").read_text())
    assert meta["excluded_players"] == {"game_info": 1}
    assert 900005 not in [s["id"] for s in meta["slates"]] and 900001 in [s["id"] for s in meta["slates"]]


def test_injury_statuses_flow_into_pools(runner):
    runner.get_text = lambda url: N.salary_csv("classic", statuses={"Buf Wr A": "IR", "Mia Rb A": "Q"}) \
        if "900001" in url else N.fake_dk_text(url)
    _pass(runner)
    rows = {r["name"]: r for r in runner.rows[900001]}
    assert rows["Buf Wr A"]["proj"] == 0 and rows["Buf Wr A"]["own"] == 0
    from pipeline import sim
    from pipeline.sports import slots
    pool = sim.build_pool(runner.rows[900001], "classic", slots.spec("NFL", "classic"))
    assert "Buf Wr A" not in pool.names
    assert pool.p_play[pool.names.index("Mia Rb A")] == pytest.approx(0.85)


def test_owner_sim_request_fast(runner, monkeypatch):
    monkeypatch.setitem(SIM, "lineups", 120)
    monkeypatch.setitem(SIM, "sims", 3000)
    monkeypatch.setitem(SIM, "field_lineups", 150)
    monkeypatch.setitem(SIM, "portfolio_sims", 1000)
    _pass(runner)
    res = runner.sim(N.NOW, {"slate_id": 900001, "phase": "fast", "req": "r1"})
    assert res == {"sims_ran": [(900001, "fast")], "sim_errors": []}
    root = Path(config.DATA_DIR) / "nfl"
    lu = json.loads((root / "lineups" / "900001.json").read_text())
    assert lu["sport"] == "NFL" and lu["slots"] == ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]
    assert next(s for s in json.loads((root / "slates.json").read_text()) if s["id"] == 900001)["lineups_file"]
    locked = runner.sim(datetime(2026, 11, 29, 18, 30, tzinfo=timezone.utc), {"slate_id": 900001, "phase": "fast"})
    assert locked["sims_ran"] == []


def test_published_files_carry_no_private_identifiers(runner):
    _pass(runner)
    blob = "".join(p.read_text(errors="ignore") for p in (Path(config.DATA_DIR) / "nfl").rglob("*")
                   if p.is_file() and p.suffix in (".json", ".csv"))
    assert "fakeuser" not in blob and "EntryId" not in blob


def _big_csv(n_teams=16, per_team=31):
    teams = [t["dk"] for t in T.TEAMS][:n_teams]
    lines = ["Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame,Status"]
    nid = 50_000_000
    mix = ["QB"] * 3 + ["RB"] * 7 + ["WR"] * 12 + ["TE"] * 5 + ["DST"] + ["WR"] * 3
    for g in range(0, n_teams, 2):
        a, h = teams[g], teams[g + 1]
        info = f"{a}@{h} 11/29/2026 01:00PM ET"
        for team in (a, h):
            for i, pos in enumerate(mix[:per_team]):
                nid += 1
                nm = T.nickname(team) if pos == "DST" else f"{team.title()} Player {i}"
                rp = pos if pos in ("QB", "DST") else f"{pos}/FLEX"
                sal = 3000 + (i * 977) % 6000
                lines.append(f"{pos},{nm} ({nid}),{nm},{nid},{rp},{sal},{info},{team},{sal / 900:.1f},")
    return "\n".join(lines) + "\n"


def test_runtime_budget_project_10_groups_and_one_opt():
    D.save_history(N.history())
    rows = [{"DraftGroupId": 910000 + k, "ContestTypeId": 21, "GameTypeId": 1, "DraftGroupTag": "Featured" if k == 0 else "",
             "StartDate": "2026-11-29T18:00:00Z", "GameCount": 8, "ContestStartTimeSuffix": f" (S{k})"} for k in range(10)]
    big = _big_csv()
    r = R.NFLRunner(get_json=_get_json({"DraftGroups": rows}), get_text=lambda url: big)
    r.lobby(N.NOW)
    r.salaries(N.NOW)
    assert all(len(s.players) >= 480 for s in r.slates.values())
    t0 = time.time()
    r.project(N.NOW)
    assert time.time() - t0 < 20.0
    t0 = time.time()
    r.opt(N.NOW, 910000)
    assert time.time() - t0 < 12.0
    assert sum(x["opt_pct"] for x in r.rows[910000]) > 0


def test_audit_skips_cleanly_without_history():
    r = R.NFLRunner(get_json=_get_json(), get_text=N.fake_dk_text)
    assert r.audit(N.NOW) == {"changed": False, "skipped": "no NFL history yet"}
    assert not (Path(config.DATA_DIR) / "nfl" / "audit.json").exists()
