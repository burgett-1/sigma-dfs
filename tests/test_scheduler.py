"""Scheduler: cadence, time budget, overdue rule, season gating, the kill switch."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import config, live, scheduler as S

NOW = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class StubRunner:
    """Jobs that only advance a fake clock; status is set by the test."""

    def __init__(self, clock, durations=None, status=None, opt=()):
        self.clock, self.durations = clock, durations or {}
        self.st = status or {"groups": 0, "next_lock": None, "in_progress": False, "empty_polls": 0,
                             "last_group_seen_at": None, "slates": 0}
        self.calls, self.opts = [], list(opt)

    def status(self, now):
        return dict(self.st)

    def opt_candidates(self, now):
        return self.opts

    def opt_needed(self, dg):
        return True

    def dg_index(self):
        return {}

    def _job(self, name, now):
        self.calls.append((name, now))
        self.clock.t += self.durations.get(name, 1.0)
        return {"changed": name == "project"}

    def __getattr__(self, name):
        if name in S.JOB_ORDER or name == "sim":
            return lambda now, *a, **k: self._job(name, now)
        raise AttributeError(name)


def _sched(tmp_path, runner, clock):
    return S.Scheduler(["NFL"], data_dir=tmp_path, clock=clock, runners={"NFL": runner})


def test_jobs_only_start_when_they_fit(tmp_path):
    clock = Clock()
    r = StubRunner(clock, {"lobby": 2, "salaries": 3, "context": 3, "project": 4, "opt": 30, "history": 6},
                   {"groups": 2, "next_lock": NOW + timedelta(hours=2), "in_progress": False, "empty_polls": 0,
                    "slates": 2}, opt=[1])
    sch = _sched(tmp_path, r, clock)
    st = {"jobs": {k: {"ewma": v} for k, v in (("NFL:lobby", 2), ("NFL:salaries", 3), ("NFL:context", 3),
                                                ("NFL:project", 4), ("NFL:opt:1", 30), ("NFL:history", 6))}}
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "scheduler.json").write_text(json.dumps(st))
    res = sch.run_due(NOW, 15.0)
    assert [c[0] for c in r.calls] == ["lobby", "salaries", "context", "project"]   # opt (30 s) doesn't fit
    assert clock.t <= 15.0 and "NFL:opt:1" not in res["ran"]
    clock.t = 0
    res = sch.run_due(NOW + timedelta(seconds=60), 40.0)
    assert "NFL:opt:1" in res["ran"]


def test_nba_interval_is_never_stretched(tmp_path):
    """Simulated live loop: NBA pass, then the scheduler in what's left; every
    NBA start is <= interval + 5 s after the previous one."""
    clock = Clock()
    r = StubRunner(clock, {"lobby": 3, "salaries": 4, "context": 5, "project": 6, "opt": 9, "history": 12},
                   {"groups": 3, "next_lock": NOW + timedelta(hours=1), "in_progress": False, "empty_polls": 0,
                    "slates": 3}, opt=[1, 2, 3])
    sch = _sched(tmp_path, r, clock)
    interval, nba = 60.0, [41, 22, 50, 35, 58, 30, 44, 20, 47, 39]
    starts, now = [], NOW
    for d in nba * 3:
        t0 = clock.t
        starts.append(t0)
        clock.t += d                                     # the NBA pass
        sch.run_due(now, interval - (clock.t - t0) - 5.0)
        clock.t = max(clock.t, t0 + interval)            # wait for the next pass
        now += timedelta(seconds=interval)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert max(gaps) <= interval + 5.0 and r.calls


def test_overdue_job_runs_over_budget_but_not_near_an_nba_lock(tmp_path):
    clock = Clock()
    r = StubRunner(clock, {"project": 20}, {"groups": 1, "next_lock": NOW + timedelta(hours=10), "in_progress": False,
                                             "empty_polls": 0, "slates": 1})
    sch = _sched(tmp_path, r, clock)
    old = (NOW - timedelta(hours=2)).isoformat()
    st = {"jobs": {k: {"ewma": 20.0, "last": old} for k in ("NFL:lobby", "NFL:salaries", "NFL:context", "NFL:project",
                                                            "NFL:history")}}
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "scheduler.json").write_text(json.dumps(st))
    sch.run_due(NOW, 1.0, nba_next_lock=NOW + timedelta(minutes=10))
    assert r.calls == []                                 # NBA locks within 20 minutes: no overruns
    sch.run_due(NOW, 1.0, nba_next_lock=NOW + timedelta(hours=2))
    assert "project" in [c[0] for c in r.calls]          # 5-min cadence, 2 h overdue (> 3x): may run


def _drive(tmp_path, runner, clock, start, hours, step_min=10):
    sch = _sched(tmp_path, runner, clock)
    now = start
    while now < start + timedelta(hours=hours):
        sch.run_due(now, 1000.0)
        now += timedelta(minutes=step_min)
    return sch


def test_off_season_polls_lobby_once_a_day(tmp_path):
    clock = Clock()
    r = StubRunner(clock)
    start = datetime(2027, 6, 1, 12, tzinfo=timezone.utc)
    sch = _drive(tmp_path, r, clock, start, 48)
    lobby = [c[1] for c in r.calls if c[0] == "lobby"]
    assert len(lobby) == 2 and lobby[1] - lobby[0] >= timedelta(hours=24)
    assert {c[0] for c in r.calls} <= {"lobby", "project"}
    nfl = sch.sports_doc(start)["sports"][1]
    assert nfl["status"] == "off_season" and nfl["season_note"] == "Returns in September"


def test_in_season_backs_off_after_three_empty_polls(tmp_path):
    clock = Clock()
    r = StubRunner(clock, status={"groups": 0, "next_lock": None, "in_progress": False, "empty_polls": 3, "slates": 0})
    _drive(tmp_path, r, clock, NOW, 24)
    lobby = [c[1] for c in r.calls if c[0] == "lobby"]
    assert len(lobby) == 4 and all(b - a >= timedelta(hours=6) for a, b in zip(lobby, lobby[1:]))
    sch = _sched(tmp_path, r, clock)
    assert sch.sports_doc(NOW)["sports"][1]["status"] == "no_slate"


def test_cadence_rows():
    from pipeline import sports
    cfg = sports.get("NFL")
    st = {"groups": 1, "next_lock": NOW + timedelta(hours=2)}
    assert S.cadence_row(cfg, st, NOW, True)["lobby"] == 5
    st["next_lock"] = NOW + timedelta(hours=10)
    assert S.cadence_row(cfg, st, NOW, True)["project"] == 5
    st["next_lock"] = NOW + timedelta(hours=100)
    assert S.cadence_row(cfg, st, NOW, True)["salaries"] == 60
    assert S.cadence_row(cfg, {"groups": 1, "next_lock": None, "in_progress": True}, NOW, True)["project"] == 3
    assert S.cadence_row(cfg, {"groups": 0}, NOW, False)["lobby"] == 1440


def test_kill_switch_live_loop_makes_no_nfl_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGMA_SPORTS", "NBA")
    assert live.make_scheduler() is None
    requests = []
    monkeypatch.setattr(live, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(live, "make_control", lambda: None)
    monkeypatch.setattr(live.runner, "run", lambda *a, **k: {"status": 0, "changed": False})
    monkeypatch.setattr(live.patterns, "run", lambda **k: None)
    monkeypatch.setattr(live, "publish", lambda: False)
    monkeypatch.setattr(live.contests, "watch", lambda *a, **k: [])
    monkeypatch.setattr(live.contests, "refresh", lambda: {})
    monkeypatch.setattr(live.contest_reminders, "check", lambda *a, **k: [])
    monkeypatch.setattr(live.runner, "snapshot_industry", lambda now: 0)
    monkeypatch.setattr(live.runner, "refresh_benchmark", lambda now: False)
    monkeypatch.setattr(live.runner, "refresh_calibration", lambda now: False)
    monkeypatch.setattr(live.NewsWatch, "changed", lambda self: True)
    from pipeline.sources import draftkings
    monkeypatch.setattr(draftkings, "get_json", lambda url: requests.append(url) or {})
    monkeypatch.setattr(draftkings, "get_text", lambda url: requests.append(url) or "")
    assert live.main(["--minutes", "0.02", "--interval", "1", "--no-notify"]) == 0
    assert not any("sport=NFL" in u for u in requests)
    assert not (tmp_path / "nfl").exists() and not (tmp_path / "sports.json").exists()


def test_nfl_enabled_builds_a_scheduler(monkeypatch):
    monkeypatch.setenv("SIGMA_SPORTS", "NBA,NFL")
    sch = live.make_scheduler()
    assert sch is not None and sch.codes == ["NFL"]


def test_sim_request_routed_by_draft_group(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "DATA_DIR", tmp_path)
    statuses = []
    monkeypatch.setattr(live, "publish", lambda: statuses.append(json.loads((tmp_path / "run_status.json").read_text())))
    monkeypatch.setattr(live.runner, "run", lambda *a, **k: pytest.fail("NBA runner must not run for an NFL slate"))

    class Sched:
        def sport_for(self, sid):
            return "NFL" if sid == 900001 else None

        def run_sim(self, code, now, req):
            return {"sims_ran": [(req["slate_id"], req["phase"])], "sim_errors": []}
    live.handle_request({"slate_id": 900001, "req": "r1"}, do_notify=False, scheduler=Sched())
    assert [s["state"] for s in statuses] == ["running", "running", "done"] and all(s["sport"] == "NFL" for s in statuses)


# ── F6: the weekly audit runs from the 600 s background block ───────────────
TUE_9ET = datetime(2026, 12, 1, 14, 0, tzinfo=timezone.utc)      # Tuesday 09:00 ET


def _week_of_blocks(tmp_path, nba_lock=None, last=None, t_left=50.0):
    """A week of production loops: run_due every minute with t_left, the background
    block every 10 minutes. Returns the audit call times."""
    clock = Clock()
    r = StubRunner(clock, {"audit": 45.0}, {"groups": 0, "next_lock": None, "in_progress": False, "empty_polls": 3,
                                             "slates": 0})
    sch = _sched(tmp_path, r, clock)
    if last is not None:
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state" / "scheduler.json").write_text(json.dumps({"jobs": {"NFL:audit": {"last": last.isoformat(),
                                                                                              "ewma": 45.0}}}))
    now = TUE_9ET
    for minute in range(7 * 24 * 60):
        sch.run_due(now, t_left)
        if minute % 10 == 0:
            sch.run_background(now, nba_lock(now) if nba_lock else None)
        now += timedelta(minutes=1)
    return [c[1] for c in r.calls if c[0] == "audit"], sch


def test_weekly_audit_runs_once_in_the_window(tmp_path):
    runs, sch = _week_of_blocks(tmp_path, last=TUE_9ET - timedelta(days=7))
    assert len(runs) == 1 and runs[0].astimezone(S.ET).hour >= 10 and runs[0].astimezone(S.ET).weekday() == 1
    rec = sch.state()["jobs"]["NFL:audit"]
    assert rec["last_seconds"] == 45.0                       # the measured duration is recorded


def test_audit_never_from_run_due_and_never_near_an_nba_lock(tmp_path):
    runs, _ = _week_of_blocks(tmp_path, nba_lock=lambda now: now + timedelta(minutes=10),
                              last=TUE_9ET - timedelta(days=7), t_left=10_000.0)
    assert runs == []


def test_first_audit_seeds_immediately(tmp_path):
    runs, _ = _week_of_blocks(tmp_path)
    assert len(runs) == 1 and runs[0] == TUE_9ET


def test_audit_waits_for_the_sports_own_lock(tmp_path):
    clock = Clock()
    r = StubRunner(clock, {}, {"groups": 1, "next_lock": NOW + timedelta(hours=2), "in_progress": False,
                               "empty_polls": 0, "slates": 1})
    sch = _sched(tmp_path, r, clock)
    assert sch.run_background(NOW)["ran"] == []
    r.st["next_lock"] = NOW + timedelta(hours=4)
    assert sch.run_background(NOW)["ran"] == ["NFL:audit"]


def test_new_opt_slates_inherit_the_sport_level_ewma(tmp_path):
    clock = Clock()
    r = StubRunner(clock, {"opt": 30.0}, {"groups": 2, "next_lock": NOW + timedelta(hours=2), "in_progress": False,
                                           "empty_polls": 0, "slates": 2}, opt=[1])
    sch = _sched(tmp_path, r, clock)
    sch.run_due(NOW, 10_000.0)
    st = sch.state()
    assert st["jobs"]["NFL:opt"]["ewma"] == 30.0
    assert sch.expected(st, S.Job("NFL", "opt", 5, 0, 2)) == 30.0      # slate 2 never ran: seeded, not 10 s


def test_failed_or_skipped_audit_retries_after_an_hour_and_success_stays_weekly(tmp_path):
    """R1-B1: an error (or 'no history yet') doesn't count as this week's audit."""
    clock = Clock()
    results = [{"changed": False, "error": "KeyError: season"}, {"changed": False, "skipped": "no NFL history yet"},
               {"changed": True}]

    class R(StubRunner):
        def audit(self, now):
            self.calls.append(("audit", now))
            return results[min(len([c for c in self.calls if c[0] == "audit"]) - 1, 2)]
    r = R(clock)
    sch = _sched(tmp_path, r, clock)
    now = TUE_9ET
    for minute in range(0, 7 * 24 * 60, 10):
        sch.run_background(now + timedelta(minutes=minute))
    runs = [c[1] for c in r.calls if c[0] == "audit"]
    assert len(runs) == 3                                   # fail, skip, then success; then nothing all week
    assert runs[1] - runs[0] >= S.AUDIT_RETRY and runs[2] - runs[1] >= S.AUDIT_RETRY
    assert runs[2] - runs[0] < timedelta(hours=3)
    rec = sch.state()["jobs"]["NFL:audit"]
    assert rec["last"] == runs[2].isoformat() and "retry_after" not in rec and "error" not in rec
