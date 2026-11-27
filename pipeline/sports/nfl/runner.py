"""NFL job runner: the scheduler (pipeline/scheduler.py) calls these jobs.

    lobby -> salaries -> context -> project -> opt (one slate per job) -> history -> audit
    sim (owner request, fast then exact)

Each job is idempotent and resumable: what it needs from earlier jobs is kept
in nfl/state/run_state.json and the cached files, so a restarted loop picks up
where it stopped. Everything runs inside health.scope("NFL") (NFL sources never
reach NBA's meta.json) and writes only under data/nfl/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ... import contests, health
from ...config import ET, SIM
from ...sources import draftkings
from .. import get, slots
from ..paths import paths
from . import backtest, data as D
from . import model as M
from . import ownership as O

log = logging.getLogger(__name__)
SPORT = "NFL"
KEEP_AFTER_LAST_START = timedelta(hours=4)
LINEUPS_KEEP_DAYS = 14
OPT_OTHER_CLASSICS = 3
SHOWDOWN_OPT_WITHIN = timedelta(hours=24)
NO_SLATE_MESSAGE = "No NFL slate open on DraftKings in the next 6 days"
# CSV columns that are basketball-only: left empty for NFL
_BLANK_COLUMNS = ("Minutes", "FptsPerMin", "Starting", "BatOrder", "StarterStatus", "Unexpected",
                  "MinutesRestricted", "LiveFP", "GameProgress")
RULES = {"max_per_team": 8, "max_per_game": 9}


def nfl_season(now: datetime) -> int:
    d = now.astimezone(ET)
    return d.year if d.month >= 3 else d.year - 1


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _write_json(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


class NFLRunner:
    sport = SPORT

    def __init__(self, data_dir=None, get_json=None, get_text=None):
        self.data_dir = data_dir
        self.get_json = get_json            # injectable for tests (fixtures, no network)
        self.get_text = get_text
        self.slates: dict[int, draftkings.Slate] = {}
        self.rows: dict[int, list[dict]] = {}
        self.ctx: dict[str, dict] = {}
        self.injuries: list[dict] = []
        self.hist: pd.DataFrame | None = None
        self.games: pd.DataFrame | None = None

    # ── paths / state ────────────────────────────────────────────────────────
    @property
    def P(self):
        return paths(SPORT, self.data_dir)

    def _state_path(self):
        return self.P.state / "run_state.json"

    def state(self) -> dict:
        return _read_json(self._state_path(), {})

    def _save(self, st: dict) -> None:
        _write_json(self._state_path(), st)

    def _http(self):
        """Point the shared DK module at the injected getters for the duration of a job."""
        return _Patch(draftkings, get_json=self.get_json, get_text=self.get_text)

    # ── scheduler interface ─────────────────────────────────────────────────
    def status(self, now: datetime) -> dict:
        st = self.state()
        groups = list((st.get("groups") or {}).values())
        starts = []
        for g in groups:
            for s in g.get("game_starts") or [g.get("start_utc")]:
                dt = draftkings.parse_dt(s)
                if dt:
                    starts.append(dt)
        upcoming = [s for s in starts if s > now]
        started = [s for s in starts if s <= now]
        last = max(starts) if starts else None
        return {"groups": len(groups), "next_lock": min(upcoming) if upcoming else None,
                "in_progress": bool(started and last and now < last + timedelta(hours=4)),
                "last_group_seen_at": st.get("last_group_seen_at"), "empty_polls": int(st.get("empty_polls", 0)),
                "slates": len(groups)}

    def opt_candidates(self, now: datetime) -> list[int]:
        """Slates that get opt%: featured classics, the 3 largest other classics and
        showdowns locking within 24 h, all unlocked."""
        st = self.state()
        groups = [g for g in (st.get("groups") or {}).values()
                  if (draftkings.parse_dt(g.get("start_utc")) or now) > now]
        classic = [g for g in groups if g.get("kind") == "classic"]
        feat = [g for g in classic if g.get("featured")]
        others = sorted([g for g in classic if not g.get("featured")], key=lambda g: -int(g.get("game_count") or 0))
        sd = [g for g in groups if g.get("kind") == "showdown"
              and draftkings.parse_dt(g.get("start_utc")) - now <= SHOWDOWN_OPT_WITHIN]
        return [int(g["draft_group_id"]) for g in feat + others[:OPT_OTHER_CLASSICS] + sd]

    def opt_needed(self, dg: int) -> bool:
        rows = self.rows.get(dg)
        if not rows:
            return False
        cached = (self.state().get("opt") or {}).get(str(dg)) or {}
        return cached.get("hash") != _opt_hash(rows)

    def dg_index(self) -> dict[str, str]:
        return {str(k): SPORT for k in (self.state().get("groups") or {})}

    # ── jobs ────────────────────────────────────────────────────────────────
    def lobby(self, now: datetime) -> dict:
        st = self.state()
        groups = st.get("groups") or {}
        with health.scope(SPORT), self._http():
            try:
                listed = draftkings.list_draft_groups(now, SPORT)
            except Exception as e:  # noqa: BLE001
                log.info("NFL lobby unavailable: %s", e)
                return {"changed": False, "error": str(e)}
        before = json.dumps(sorted(groups), default=str)
        for s in listed:
            h = draftkings.slate_header(s)
            old = groups.get(str(s.draft_group_id)) or {}
            groups[str(s.draft_group_id)] = {**old, **h, "seen_at": _iso(now)}
        listed_ids = {str(s.draft_group_id) for s in listed}
        for dg in list(groups):
            if dg in listed_ids:
                continue
            g = groups[dg]
            last = draftkings.parse_dt(g.get("last_start")) or draftkings.parse_dt(g.get("start_utc"))
            if last is None or now > last + KEEP_AFTER_LAST_START:
                groups.pop(dg)
                self.slates.pop(int(dg), None)
                self.rows.pop(int(dg), None)
        st["groups"] = groups
        st["empty_polls"] = 0 if listed else int(st.get("empty_polls", 0)) + 1
        if listed:
            st["last_group_seen_at"] = _iso(now)
        st["lobby_at"] = _iso(now)
        self._save(st)
        return {"changed": json.dumps(sorted(groups), default=str) != before}

    def salaries(self, now: datetime, ttl_s: float | None = 900.0) -> dict:
        st = self.state()
        groups = st.get("groups") or {}
        changed = False
        with health.scope(SPORT), self._http():
            for dg, h in groups.items():
                s = draftkings.slate_from_header(h)
                try:
                    draftkings.load_draftables(s, SPORT, ttl_s=ttl_s)
                except Exception as e:  # noqa: BLE001
                    log.info("NFL group %s skipped: %s", dg, e)
                    h["skipped"] = str(e)[:120]
                    continue
                if not s.players:
                    h["skipped"] = "no players"
                    continue
                h.pop("skipped", None)
                old = self.slates.get(int(dg))
                if old is None or _players_sig(old) != _players_sig(s):
                    changed = True
                self.slates[int(dg)] = s
                starts = sorted(_iso(g["start_utc"]) for g in s.games if g.get("start_utc"))
                h.update(kind=s.kind, game_count=s.game_count, game_starts=starts,
                         last_start=starts[-1] if starts else h.get("start_utc"),
                         excluded=dict(s.excluded or {}))
        st["groups"] = groups
        st["salaries_at"] = _iso(now)
        self._save(st)
        return {"changed": changed}

    def history(self, now: datetime, max_age_s: float = 6 * 3600) -> dict:
        season = nfl_season(now)
        with health.scope(SPORT):
            try:
                # three seasons: projections use the last two; the weekly backtest of
                # season - 1 needs season - 2 as its prior-season context
                self.hist = D.refresh_history([season - 2, season - 1, season], season, max_age_s, self.get_text)
                self.games = D.games(max_age_s, self.get_text)
            except Exception as e:  # noqa: BLE001
                health.fail("nflverse", e)
                self.hist = D.load_history(self.P.history)
                return {"changed": False, "error": str(e)}
        st = self.state()
        st["history_at"] = _iso(now)
        self._save(st)
        return {"changed": True}

    def context(self, now: datetime) -> dict:
        self._ensure_slates(now)
        days = sorted({g["start_utc"].astimezone(ET).date() for s in self.slates.values() for g in s.games
                       if g.get("start_utc")})
        changed = False
        with health.scope(SPORT):
            if days:
                try:
                    ctx = D.scoreboard(days, self.get_json)
                    for t, c in ctx.items():
                        c["opp_implied"] = (ctx.get(c["opp"]) or {}).get("implied")
                    changed = ctx != self.ctx
                    self.ctx = ctx
                    health.ok("ESPN NFL scoreboard", f"{len(ctx) // 2} games, "
                              f"{sum(1 for c in ctx.values() if c.get('implied') is not None) // 2} with lines")
                except Exception as e:  # noqa: BLE001
                    health.fail("ESPN NFL scoreboard", e)
            try:
                inj = D.injuries(self.get_json)
                changed = changed or inj != self.injuries
                self.injuries = inj
                health.ok("ESPN NFL injuries", f"{len(inj)} players listed")
            except Exception as e:  # noqa: BLE001
                health.fail("ESPN NFL injuries", e)
        st = self.state()
        st["context_at"] = _iso(now)
        st["context"] = self.ctx
        self._save(st)
        return {"changed": changed}

    def project(self, now: datetime) -> dict:
        P = self.P
        st = self.state()
        cfg = get(SPORT)
        with health.scope(SPORT):
            if not (st.get("groups") or {}):
                return self._no_slate(now, st)
            self._ensure_slates(now)
            slates = draftkings.sort_slates([s for s in self.slates.values() if s.players])
            if not slates:
                return self._no_slate(now, st)
            hist = self._ensure_history()
            if not self.ctx:
                self.ctx = st.get("context") or {}
            season, week = self._cutoff(now)
            dist = _read_json(P.state / "dist.json", None)
            loadings, corr_fit = self._loadings()
            injuries = {(i["team"], _key(i["name"])): i["status"] for i in self.injuries}
            players: dict[str, dict] = {}
            for s in slates:
                for p in s.players:
                    q = players.setdefault(p["dk_player_id"], dict(p, status=""))
                    # the most severe of every slate's DK Status and the ESPN report
                    q["status"] = _worse(q["status"], _worse(p.get("dk_status", ""),
                                                             injuries.get((p["team"], _key(p["name"])), "")))
            plist = list(players.values())
            join = D.join_ids(plist, hist)
            for p in plist:
                p["player_id"] = join["ids"].get(p["dk_player_id"])
            base_rows = {r["dk_player_id"]: r for r in M.project(plist, hist, self.ctx, season, week, dist)}
            own_cal = O.load_calibration(contests.PRIVATE / "own_calibration_nfl.json")
            opt_cache = st.get("opt") or {}
            main_id = _main_id(slates)
            slate_meta = []
            P.slates.mkdir(parents=True, exist_ok=True)
            for s in slates:
                srows = []
                for p in s.players:
                    r = dict(base_rows[p["dk_player_id"]])
                    r.update(draftable_id=p["draftable_id"], cpt_id=p.get("cpt_id"), cpt_salary=p.get("cpt_salary"),
                             salary=p["salary"], position=p["position"])
                    r["value"] = round(r["proj"] / (r["salary"] / 1000.0), 3) if r["salary"] else 0.0
                    srows.append(r)
                O.estimate(srows, s.kind, own_cal)
                cached = opt_cache.get(str(s.draft_group_id)) or {}
                rates = cached.get("rates") if cached.get("hash") == _opt_hash(srows) else None
                for r in srows:
                    o, lev = (rates or {}).get(str(r.get("draftable_id")), (0.0, -r.get("own", 0.0)))
                    r["opt_pct"], r["lev"] = o, lev
                self.rows[s.draft_group_id] = srows
                _write_csv(P.slates / f"{s.draft_group_id}.csv", srows)
                if s.draft_group_id == main_id:
                    _write_csv(P.root / "projections.csv", srows)
                if not any(g.get("start_utc") and g["start_utc"] <= now for g in s.games):
                    try:
                        contests.snapshot(s.draft_group_id, now.astimezone(ET).date().isoformat(), srows, sport=SPORT)
                    except OSError as e:
                        log.debug("private NFL snapshot failed: %s", e)
                slate_meta.append(self._slate_meta(s, srows, now, main_id))
            all_rows = list(base_rows.values())
            changes = self._injury_log(all_rows, now)
            _write_json(P.root / "slates.json", slate_meta)
            main_rows = {r["dk_player_id"]: r for r in self.rows.get(main_id, [])}
            from ...run import public
            _write_json(P.root / "projections.json", {
                "date": now.astimezone(ET).date().isoformat(), "updated_at": _iso(now), "sport": SPORT,
                "players": [public(r) | {"own": (main_rows.get(r["dk_player_id"]) or {}).get("own"),
                                         "opt_pct": (main_rows.get(r["dk_player_id"]) or {}).get("opt_pct"),
                                         "team": r["team"], "implied_total": r.get("implied_total"),
                                         "spread": r.get("spread"), "wind": r.get("wind"),
                                         "targets_share": r.get("targets_share"),
                                         "carries_share": r.get("carries_share"), "dk_avg": r.get("dk_avg")}
                            for r in all_rows]})
            unlocked = [g["start_utc"] for s in slates for g in s.games if g.get("start_utc") and g["start_utc"] > now]
            excluded = {}
            for h in (st.get("groups") or {}).values():
                for k, v in (h.get("excluded") or {}).items():
                    excluded[k] = excluded.get(k, 0) + int(v)
            meta = {
                "updated_at": _iso(now), "date": now.astimezone(ET).date().isoformat(), "sport": SPORT,
                "week": self._week_label(now),
                "slates": [{"id": m["id"], "name": m["name"], "main": m["main"]} for m in slate_meta],
                "players": len(all_rows), "next_lock_utc": _iso(min(unlocked)) if unlocked else None,
                "scoring": {k: cfg["scoring"].get(k) for k in ("id", "verified", "source", "as_of")},
                "correlation": {"model": "factor", "loadings": loadings,
                                "implied": _corr_summary(loadings), "fit": corr_fit},
                "ownership_cal": {"used": bool(own_cal.get("used")), "contests": own_cal.get("contests", 0),
                                  "tau": own_cal.get("tau")},
                "excluded_players": excluded, "join_rate": round(join["rate"], 3),
                "history_rows": int(len(hist)), "rules": self.rules(),
                "changes_this_run": len(changes), "attribution": D.ATTRIBUTION,
                "sources": health.snapshot(SPORT), "control": {"auth": "hmac-v1"},
            }
            _write_json(P.root / "meta.json", meta)
            if not (P.root / "audit.json").exists():
                _write_json(P.root / "audit.json", {"version": 1, "sport": SPORT, "note": "Backtest pending (weekly)",
                                                    "scoring": meta["scoring"]})
            content = hashlib.sha1(json.dumps([[(r["dk_player_id"], r["proj"], r.get("own"), r.get("status"))
                                                for r in self.rows[s.draft_group_id]] for s in slates],
                                              default=str).encode()).hexdigest()
            changed = content != st.get("content")
            st = self.state()
            st["content"], st["project_at"], st["last_run"] = content, _iso(now), _iso(now)
            self._save(st)
            return {"changed": changed}

    def opt(self, now: datetime, dg: int) -> dict:
        rows = self.rows.get(dg)
        s = self.slates.get(dg)
        if not rows or s is None:
            return {"changed": False}
        from ... import ownership
        spec = slots.spec(SPORT, s.kind)
        loadings, _ = self._loadings()
        proj = get(SPORT).get("projection") or {}
        work = [dict(r) for r in rows]
        with health.scope(SPORT):
            ownership.optimal_rates(work, s.kind, self.rules(), None,
                                    sims=int(proj.get("opt_sims_showdown" if s.kind == "showdown" else "opt_sims", 800)),
                                    spec=spec, loadings=loadings)
        rates = {str(r.get("draftable_id")): (round(r["opt_pct"], 3), round(r["lev"], 3)) for r in work}
        st = self.state()
        st.setdefault("opt", {})[str(dg)] = {"hash": _opt_hash(rows), "rates": rates, "at": _iso(now)}
        self._save(st)
        for r in rows:
            r["opt_pct"], r["lev"] = rates.get(str(r.get("draftable_id")), (0.0, -r.get("own", 0.0)))
        _write_csv(self.P.slates / f"{dg}.csv", rows)
        if dg == _main_id(list(self.slates.values())):
            _write_csv(self.P.root / "projections.csv", rows)
        return {"changed": True}

    def sim(self, now: datetime, request: dict) -> dict:
        """One phase of an owner simulation request ({slate_id, phase, req})."""
        from ... import sim
        dg = int(request["slate_id"])
        if dg not in self.rows:
            self.project(now)
        rows, s = self.rows.get(dg), self.slates.get(dg)
        if not rows or s is None:
            return {"sims_ran": [], "sim_errors": []}
        if any(g.get("start_utc") and g["start_utc"] <= now for g in s.games):
            return {"sims_ran": [], "sim_errors": []}          # contest locked: late swap takes over
        phase = request.get("phase", "fast")
        loadings, _ = self._loadings()
        try:
            res = sim.build_slate_lineups([dict(r) for r in rows], self.rules(), {}, exact=(phase == "exact"),
                                          seed=int(now.timestamp()) % 100000, kind=s.kind,
                                          spec=slots.spec(SPORT, s.kind), loadings=loadings)
        except Exception as e:  # noqa: BLE001
            log.exception("NFL simulation failed for %s: %s", dg, e)
            return {"sims_ran": [], "sim_errors": [str(e)]}
        res.update({"generated_at": _iso(now), "slate_id": dg, "phase": phase, "inputs": _slate_hash(rows),
                    "request": request.get("req"), "sport": SPORT})
        _write_json(self.P.lineups / f"{dg}.json", res)
        st = self.state()
        st.setdefault("sims", {})[str(dg)] = {"hash": _slate_hash(rows), "phase": phase, "at": _iso(now)}
        self._save(st)
        self._prune_lineups(now)
        slates_doc = _read_json(self.P.root / "slates.json", [])
        for m in slates_doc:
            if m.get("id") == dg:
                m["lineups_file"], m["lineups_phase"] = f"lineups/{dg}.json", phase
        _write_json(self.P.root / "slates.json", slates_doc)
        return {"sims_ran": [(dg, phase)], "sim_errors": []}

    def audit(self, now: datetime) -> dict:
        season = nfl_season(now)
        hist = self._ensure_history()
        if hist is None or hist.empty or not (hist["season"] == season - 1).any():
            return {"changed": False, "skipped": "no NFL history yet"}     # retried once history lands
        with health.scope(SPORT):
            try:
                backtest.run_all(hist, [season - 1, season])
                health.ok("NFL backtest", "audit.json refreshed")
            except Exception as e:  # noqa: BLE001
                health.fail("NFL backtest", e)
                return {"changed": False, "error": str(e)}
        st = self.state()
        st["audit_at"] = _iso(now)
        self._save(st)
        return {"changed": True}

    # ── helpers ─────────────────────────────────────────────────────────────
    def rules(self) -> dict:
        cl = get(SPORT)["kinds"]["classic"]
        sd = get(SPORT)["kinds"]["showdown"]
        return {**RULES, "min_salary": cl["pool_min_salary"], "min_salary_showdown": sd["pool_min_salary"],
                "stack_rules": cl.get("stack_rules"), "field_min_salary": cl["field_min_salary"]}

    def _ensure_slates(self, now: datetime) -> None:
        groups = (self.state().get("groups") or {})
        if groups and any(int(dg) not in self.slates for dg in groups):
            self.salaries(now, ttl_s=10 * 365 * 86400.0)      # cached copies only, unless missing

    def _ensure_history(self) -> pd.DataFrame:
        if self.hist is None:
            self.hist = D.load_history(self.P.history)
        return self.hist

    def _cutoff(self, now: datetime) -> tuple[int, int]:
        lab = self._week_label(now)
        if lab:
            season, week = lab.split(" W")
            return int(season), int(week)
        hist = self._ensure_history()
        if hist.empty:
            return nfl_season(now), 1
        last = hist.sort_values(["season", "week"]).iloc[-1]
        return int(last["season"]), int(last["week"]) + 1

    def _week_label(self, now: datetime) -> str | None:
        if self.games is None:
            try:
                self.games = D.games(None, self.get_text) if (self.P.cache / "games.csv").exists() else None
            except Exception:  # noqa: BLE001
                self.games = None
        if self.games is None or self.games.empty:
            return None
        try:
            return D.week_label(self.games, now)
        except (KeyError, IndexError, ValueError):
            return None

    def _loadings(self) -> tuple[dict, dict]:
        fit = _read_json(self.P.state / "corr.json", {})
        base = get(SPORT)["corr"]["loadings"]
        if fit.get("used") and fit.get("loadings"):
            return fit["loadings"], {"used": True, "n": fit.get("n"), "reason": fit.get("reason")}
        return base, {"used": False, "n": fit.get("n", 0), "reason": fit.get("reason") or "registry defaults"}

    def _slate_meta(self, s, srows, now, main_id) -> dict:
        games = []
        for g in s.games:
            st = g.get("start_utc")
            games.append({"name": g.get("name"), "home": g.get("home"), "away": g.get("away"), "start_utc": _iso(st),
                          "start_et": st.astimezone(ET).strftime("%a %-I:%M %p") if st else None,
                          "start_minutes": (st.astimezone(ET).hour * 60 + st.astimezone(ET).minute) if st else None,
                          "locked": bool(st and st <= now)})
        n = len(s.games)
        lfile = f"lineups/{s.draft_group_id}.json"
        sims = (self.state().get("sims") or {}).get(str(s.draft_group_id), {})
        return {"id": s.draft_group_id, "name": s.name, "main": s.draft_group_id == main_id, "kind": s.kind,
                "contest_locked": any(g["locked"] for g in games), "start_utc": _iso(s.start_utc), "game_count": n,
                "file": f"slates/{s.draft_group_id}.csv",
                "lineups_file": lfile if (self.P.root / lfile).exists() else None, "spotlight_file": None,
                "size_bucket": "small" if n <= 3 else "medium" if n <= 7 else "large",
                "lineups_phase": sims.get("phase"), "inputs": _slate_hash(srows), "games": games,
                "locked": all(g["locked"] for g in games) if games else False, "sport": SPORT}

    def _injury_log(self, rows: list[dict], now: datetime) -> list[dict]:
        P = self.P
        old = _read_json(P.state / "injuries.json", {})
        new = {f"{r['name']}|{r['team']}": r.get("status") or "" for r in rows}
        changes = [{"name": k.split("|")[0], "team": k.split("|")[1], "from": old.get(k, ""), "to": v,
                    "at": _iso(now)} for k, v in new.items() if k in old and old[k] != v]
        log_path = P.root / "injury_log.json"
        hist = _read_json(log_path, [])
        if changes or not log_path.exists():
            _write_json(log_path, (changes + hist)[:300])
        _write_json(P.state / "injuries.json", {**old, **new})
        return changes

    def _no_slate(self, now: datetime, st: dict) -> dict:
        from .. import in_season
        P = self.P
        msg = NO_SLATE_MESSAGE if in_season(SPORT, now) else get(SPORT)["season"].get("note_off", NO_SLATE_MESSAGE)
        meta = {"updated_at": _iso(now), "date": now.astimezone(ET).date().isoformat(), "sport": SPORT,
                "slates": [], "message": msg, "scoring": {k: get(SPORT)["scoring"].get(k) for k in ("id", "verified")},
                "sources": health.snapshot(SPORT), "control": {"auth": "hmac-v1"}}
        before = _read_json(P.root / "meta.json", {})
        _write_json(P.root / "meta.json", meta)
        _write_json(P.root / "slates.json", [])
        if not (P.root / "injury_log.json").exists():
            _write_json(P.root / "injury_log.json", [])
        self.rows.clear()
        st["project_at"] = _iso(now)
        self._save(st)
        return {"changed": before.get("message") != msg or before.get("slates") != []}

    def _prune_lineups(self, now: datetime) -> None:
        cutoff = time.time() - LINEUPS_KEEP_DAYS * 86400
        for f in self.P.lineups.glob("*.json") if self.P.lineups.exists() else []:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


class _Patch:
    """Temporarily replace module attributes (only those given non-None)."""

    def __init__(self, mod, **attrs):
        self.mod, self.attrs, self.old = mod, {k: v for k, v in attrs.items() if v is not None}, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.mod, k, v)
        return False


STATUS_RANK = {"OUT": 4, "DOUBTFUL": 3, "QUESTIONABLE": 2, "PROBABLE": 1, "": 0}


def _worse(a: str, b: str) -> str:
    return a if STATUS_RANK.get(a or "", 0) >= STATUS_RANK.get(b or "", 0) else b


def _key(name: str) -> str:
    from ...names import norm_name
    return norm_name(name)


def _players_sig(s) -> str:
    return hashlib.sha1(json.dumps([(p["draftable_id"], p["salary"], p.get("dk_status"), _iso(p.get("start_utc")))
                                    for p in s.players]).encode()).hexdigest()


def _main_id(slates) -> int:
    classic = [s for s in slates if s.kind == "classic"]
    pool = classic or list(slates)
    feat = [s for s in pool if s.featured]
    return (feat or sorted(pool, key=lambda s: -s.game_count))[0].draft_group_id


def _opt_hash(rows: list[dict]) -> str:
    return hashlib.sha1(json.dumps([(r.get("draftable_id"), round(r.get("mean", r["proj"]), 2),
                                     round(r.get("own", 0), 1), r.get("status", "")) for r in rows],
                                   sort_keys=True).encode()).hexdigest()


def _slate_hash(rows: list[dict]) -> str:
    return hashlib.sha1(json.dumps(sorted((r["dk_player_id"], round(r["proj"], 2), r.get("status", ""))
                                          for r in rows)).encode()).hexdigest()[:12]


def _corr_summary(loadings: dict) -> dict:
    from .. import corr as C
    return C.summary(SPORT, loadings=loadings)


def _write_csv(path, rows: list[dict]) -> None:
    """Same header and format as NBA (run.csv_row); basketball-only columns empty."""
    import csv

    from ...run import CSV_HEADER, csv_row
    blank = [CSV_HEADER.index(c) for c in _BLANK_COLUMNS]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for r in sorted(rows, key=lambda r: -r["proj"]):
            row = csv_row({**r, "proj_minutes": 0.0, "fpts_min": 0.0})
            for i in blank:
                row[i] = ""
            w.writerow(row)
    tmp.replace(path)
