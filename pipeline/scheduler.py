"""Cooperative scheduler for the non-NBA sports (P3-2.5).

The live loop runs the NBA pass first, exactly as before, then hands the
scheduler whatever is left of the 60 s interval:

    scheduler.run_due(now, t_left)

Jobs are (sport, job) pairs: lobby -> salaries -> context -> project -> opt
(one slate per job) -> history -> audit. Each has a cadence from the sport's
registry `cadence` table (chosen by time to that sport's next lock) and an EWMA
of its own duration. Jobs run in order of (minutes to the sport's next lock,
job order) while their expected duration fits in the time left. A job overdue by
more than 3x its cadence may run over budget, but never within 20 minutes of
an NBA lock. Season gating: outside its season windows, with no lobby group in
7 days, a sport polls its lobby once a day; in season after 3 empty polls, every
6 hours. The scheduler also writes data/sports.json (the site's sport switcher)
and data/state/dg_index.json (which sport a draft group belongs to, for owner
sim requests), each only when its content changes.

`SIGMA_SPORTS=NBA` (sports.enabled() == ["NBA"]) means no scheduler at all.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import sports
from .config import ET

log = logging.getLogger(__name__)

JOB_ORDER = ("lobby", "salaries", "context", "project", "opt", "history", "audit")
DEFAULT_DURATION = {"lobby": 3.0, "salaries": 6.0, "context": 4.0, "project": 6.0, "opt": 10.0, "history": 15.0,
                    "audit": 120.0, "sim": 120.0}
EWMA_ALPHA = 0.3
OVERDUE_FACTOR = 3.0
NBA_LOCK_GUARD = timedelta(minutes=20)
SPORT_LOCK_GUARD = timedelta(hours=3)
AUDIT_RETRY = timedelta(hours=1)           # backoff after a failed / skipped audit      # background audit: never this close to the sport's own lock
RECENT_GROUP = timedelta(days=7)
EMPTY_POLLS_BACKOFF = 3
DG_INDEX_DAYS = 14


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _parse(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def make_runner(code: str, data_dir=None):
    if code == "NFL":
        from .sports.nfl.runner import NFLRunner
        return NFLRunner(data_dir)
    return None


@dataclass
class Job:
    sport: str
    name: str
    cadence_min: float | None          # None: not scheduled in this state
    lock_min: float                    # minutes to the sport's next lock (priority)
    arg: int | None = None             # opt: draft group

    @property
    def key(self) -> str:
        return f"{self.sport}:{self.name}" + (f":{self.arg}" if self.arg is not None else "")


def cadence_row(cfg: dict, status: dict, now: datetime, in_season: bool) -> dict:
    """The first matching row of the sport's cadence table."""
    rows = {r["state"]: r for r in (cfg.get("cadence") or {}).get("rows") or []}
    if not in_season:
        return rows.get("off_season", {"lobby": 1440, "project": 1440})
    nxt = status.get("next_lock")
    if status.get("groups") and nxt is not None:
        h = (nxt - now).total_seconds() / 3600.0
        if h < 3:
            return rows["lock_lt_3h"]
        if h < 24:
            return rows["lock_3_24h"]
        if h < 144:
            return rows["lock_24_144h"]
    if status.get("groups") and status.get("in_progress"):
        return rows["in_progress"]
    if status.get("groups"):
        return rows["lock_24_144h"]
    if int(status.get("empty_polls", 0)) >= EMPTY_POLLS_BACKOFF:
        return rows["no_group"]
    return rows["lock_24_144h"]


def in_season(code: str, status: dict, now: datetime) -> bool:
    """In the season windows, or the lobby listed a qualifying group in the last 7 days."""
    if sports.in_season(code, now):
        return True
    seen = _parse(status.get("last_group_seen_at"))
    return bool(seen and now - seen <= RECENT_GROUP)


class Scheduler:
    def __init__(self, codes: list[str] | None = None, data_dir=None, clock=time.monotonic, runners: dict | None = None):
        self.codes = [c for c in (codes if codes is not None else sports.enabled()) if c != "NBA"]
        self.data_dir = Path(data_dir) if data_dir else None
        self.clock = clock
        self.runners = runners if runners is not None else {c: r for c in self.codes if (r := make_runner(c, data_dir))}
        self.codes = [c for c in self.codes if c in self.runners]

    # ── paths ────────────────────────────────────────────────────────────────
    @property
    def root(self) -> Path:
        from . import config
        return self.data_dir or Path(config.DATA_DIR)

    def _state_path(self) -> Path:
        return self.root / "state" / "scheduler.json"

    def state(self) -> dict:
        return _read(self._state_path(), {"jobs": {}})

    # ── planning ─────────────────────────────────────────────────────────────
    def plan(self, now: datetime, st: dict | None = None) -> list[Job]:
        """Every job that is due now, in priority order."""
        st = st if st is not None else self.state()
        due = []
        for code in self.codes:
            cfg = sports.get(code)
            runner = self.runners[code]
            status = runner.status(now)
            season = in_season(code, status, now)
            row = cadence_row(cfg, status, now, season)
            nxt = status.get("next_lock")
            lock_min = (nxt - now).total_seconds() / 60.0 if nxt else 1e9
            cad = cfg.get("cadence") or {}
            jobs = [Job(code, "lobby", row.get("lobby"), lock_min)]
            if season and status.get("groups"):
                jobs += [Job(code, "salaries", row.get("salaries"), lock_min),
                         Job(code, "context", row.get("context"), lock_min)]
            jobs.append(Job(code, "project", row.get("project"), lock_min))
            if season and status.get("groups") and row.get("opt") is not None:
                jobs += [Job(code, "opt", row.get("opt"), lock_min, dg) for dg in runner.opt_candidates(now)]
            if season:
                jobs.append(Job(code, "history", cad.get("history", 360), lock_min))
            # the weekly audit (a ~10-60 s backtest) never fits a loop's leftover time:
            # it runs from live.py's 600 s background block instead (run_background)
            for j in jobs:
                if j.cadence_min is None:
                    continue
                last = _parse((st["jobs"].get(j.key) or {}).get("last"))
                if j.name == "opt":
                    if not runner.opt_needed(j.arg):
                        continue
                if last is None or (now - last).total_seconds() >= j.cadence_min * 60.0:
                    due.append(j)
        due.sort(key=lambda j: (j.lock_min, JOB_ORDER.index(j.name)))
        return due

    def _audit_due(self, st: dict, code: str, now: datetime, cfg: dict) -> bool:
        """At most weekly: never run yet (seeds dist.json / corr.json), or more than
        6 days since the last run and the weekly window is open (default Tuesday
        from 10:00 ET), or more than 8 days (the window was missed)."""
        rec = st["jobs"].get(f"{code}:audit") or {}
        retry = _parse(rec.get("retry_after"))
        if retry is not None and now < retry:             # a failed or skipped attempt backs off
            return False
        last = _parse(rec.get("last"))
        if last is None:
            return True
        age = now - last
        if age <= timedelta(days=6):
            return False
        local = now.astimezone(ET)
        window = local.weekday() == int(cfg.get("weekday", 1)) and local.hour >= int(cfg.get("hour_et", 10))
        return window or age > timedelta(days=8)

    def run_background(self, now: datetime, nba_next_lock: datetime | None = None) -> dict:
        """Long, rare jobs (the weekly backtest audit), called from live.py's 600 s
        background block like NBA's calibration. Skipped within 20 min of an NBA
        lock or 3 h of the sport's own next lock. Records the real duration."""
        st = self.state()
        ran, changed = [], False
        if nba_next_lock is not None and nba_next_lock - now <= NBA_LOCK_GUARD:
            return {"changed": False, "ran": []}
        for code in self.codes:
            runner = self.runners[code]
            if not hasattr(runner, "audit"):
                continue
            cad = sports.get(code).get("cadence") or {}
            if not in_season(code, runner.status(now), now) or not self._audit_due(st, code, now, cad.get("audit") or {}):
                continue
            nxt = runner.status(now).get("next_lock")
            if nxt is not None and nxt - now <= SPORT_LOCK_GUARD:
                continue
            key = f"{code}:audit"
            start = self.clock()
            try:
                res = runner.audit(now)
            except Exception as e:  # noqa: BLE001
                log.warning("%s failed: %s", key, e)
                res = {"changed": False, "error": str(e)}
            dur = self.clock() - start
            rec = st["jobs"].setdefault(key, {})
            rec["ewma"] = round(rec["ewma"] + EWMA_ALPHA * (dur - rec["ewma"]), 3) if "ewma" in rec else round(dur, 3)
            rec["last_seconds"] = round(dur, 3)
            if res.get("error") or res.get("skipped"):
                # not this week's run: try again after a short backoff
                rec["error"] = str(res.get("error") or res.get("skipped"))[:200]
                rec["retry_after"] = _iso(now + AUDIT_RETRY)
            else:
                rec["last"] = _iso(now)
                rec.pop("error", None)
                rec.pop("retry_after", None)
            ran.append(key)
            changed = changed or bool(res.get("changed"))
        if ran:
            _write(self._state_path(), st)
        return {"changed": changed, "ran": ran}

    def expected(self, st: dict, job: Job) -> float:
        rec = st["jobs"].get(job.key) or st["jobs"].get(f"{job.sport}:{job.name}") or {}
        return float(rec.get("ewma", DEFAULT_DURATION.get(job.name, 10.0)))

    def overdue(self, st: dict, job: Job, now: datetime) -> bool:
        last = _parse((st["jobs"].get(job.key) or {}).get("last"))
        if last is None or not job.cadence_min:
            return False
        return (now - last).total_seconds() > OVERDUE_FACTOR * job.cadence_min * 60.0

    # ── running ──────────────────────────────────────────────────────────────
    def run_due(self, now: datetime, t_left: float, nba_next_lock: datetime | None = None,
                stop=None) -> dict:
        """Run due jobs that fit in t_left seconds. `stop()` (optional) is checked
        between jobs, e.g. for a pending owner request. Returns {changed, ran}."""
        t0 = self.clock()
        st = self.state()
        ran, changed = [], False
        nba_soon = nba_next_lock is not None and nba_next_lock - now <= NBA_LOCK_GUARD
        skipped: set[str] = set()
        while True:
            # re-plan after every job: a lobby poll can make salaries/context/opt due
            job = None
            for cand in self.plan(now, st):
                if cand.key in skipped or cand.key in ran:
                    continue
                exp = self.expected(st, cand)
                if exp <= t_left - (self.clock() - t0) or (self.overdue(st, cand, now) and not nba_soon):
                    job = cand
                    break
                skipped.add(cand.key)
            if job is None or (stop is not None and stop()):
                break
            exp = self.expected(st, job)
            start = self.clock()
            try:
                res = self._call(job, now)
            except Exception as e:  # noqa: BLE001 - one sport's failure must not stop the loop
                log.warning("%s failed: %s", job.key, e)
                res = {"changed": False, "error": str(e)}
            dur = self.clock() - start
            rec = st["jobs"].setdefault(job.key, {})
            rec["ewma"] = round(exp + EWMA_ALPHA * (dur - exp), 3) if "ewma" in rec else round(dur, 3)
            rec["last"] = _iso(now)
            if job.arg is not None:      # per-slate jobs: a sport-level EWMA seeds slates not seen yet
                base = st["jobs"].setdefault(f"{job.sport}:{job.name}", {})
                base["ewma"] = round(base["ewma"] + EWMA_ALPHA * (dur - base["ewma"]), 3) if "ewma" in base else round(dur, 3)
            if res.get("error"):
                rec["error"] = str(res["error"])[:200]
            else:
                rec.pop("error", None)
            changed = changed or bool(res.get("changed"))
            ran.append(job.key)
        _write(self._state_path(), st)
        changed = self.write_index(now) or changed
        return {"changed": changed, "ran": ran}

    def _call(self, job: Job, now: datetime) -> dict:
        r = self.runners[job.sport]
        if job.name == "opt":
            return r.opt(now, job.arg)
        if job.name == "salaries":
            return r.salaries(now, ttl_s=(job.cadence_min or 15) * 60.0)
        return getattr(r, job.name)(now)

    # ── owner requests ───────────────────────────────────────────────────────
    def sport_for(self, slate_id) -> str | None:
        idx = _read(self.root / "state" / "dg_index.json", {})
        code = (idx.get("groups") or {}).get(str(slate_id))
        return code if code in self.runners else None

    def run_sim(self, code: str, now: datetime, request: dict) -> dict:
        return self.runners[code].sim(now, request)

    # ── published indexes ────────────────────────────────────────────────────
    def write_index(self, now: datetime) -> bool:
        """data/sports.json and data/state/dg_index.json, each written only on change."""
        changed = False
        idx_path = self.root / "state" / "dg_index.json"
        old_idx = _read(idx_path, {})
        groups = dict(old_idx.get("groups") or {})
        seen = dict(old_idx.get("seen") or {})
        for code in self.codes:
            for dg, sport in self.runners[code].dg_index().items():
                groups[dg] = sport
                seen[dg] = now.date().isoformat()
        cutoff = (now - timedelta(days=DG_INDEX_DAYS)).date().isoformat()
        for dg in [d for d, day in seen.items() if day < cutoff]:
            groups.pop(dg, None)
            seen.pop(dg, None)
        new_idx = {"version": 1, "groups": groups, "seen": seen}
        if new_idx != old_idx:
            _write(idx_path, new_idx)
            changed = True
        doc = self.sports_doc(now)
        path = self.root / "sports.json"
        old = _read(path, {})
        if {k: v for k, v in old.items() if k != "updated_at"} != {k: v for k, v in doc.items() if k != "updated_at"}:
            _write(path, doc)
            changed = True
        return changed

    def sports_doc(self, now: datetime) -> dict:
        out = [self._nba_entry(now)]
        for code in self.codes:
            cfg = sports.get(code)
            status = self.runners[code].status(now)
            season = in_season(code, status, now)
            state = "live" if status.get("groups") else ("no_slate" if season else "off_season")
            nxt = status.get("next_lock")
            out.append({"code": code, "label": cfg.get("label", code), "base": cfg.get("data_prefix", ""),
                        "enabled": True, "in_season": season, "status": state, "slates": status.get("slates", 0),
                        "next_lock_utc": _iso(nxt), "season_note": "" if season else (cfg.get("season") or {}).get("note_off", ""),
                        "scoring_verified": bool((cfg.get("scoring") or {}).get("verified"))})
        return {"version": 1, "updated_at": _iso(now), "sports": out}

    def _nba_entry(self, now: datetime) -> dict:
        meta = _read(self.root / "meta.json", {})
        n = len(meta.get("slates") or [])
        return {"code": "NBA", "label": "NBA", "base": "", "enabled": True,
                "in_season": sports.in_season("NBA", now), "status": "live" if n else "no_slate", "slates": n,
                "next_lock_utc": meta.get("next_lock_utc"), "season_note": "", "scoring_verified": True}


def nba_next_lock(data_dir: Path) -> datetime | None:
    return _parse(_read(Path(data_dir) / "meta.json", {}).get("next_lock_utc"))
