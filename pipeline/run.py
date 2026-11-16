"""Pipeline entry point: `python -m pipeline.run`.

Each run (every 15 minutes on game days):
  1. adds any newly finished box scores to the history store
  2. loads today's DraftKings classic slates (salaries, positions, lock times)
  3. pulls the official NBA injury report, ESPN injuries and Vegas lines
  4. projects every player; players in games that have started are frozen
  5. writes data/projections.csv (main slate) + data/slates/<id>.csv per slate
  6. pushes a phone notification for every injury status change
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from . import factors, health, history, injuries, model, notify, ownership, patterns, sim, spotlight
from .config import DATA_DIR, ET, SIM, SLATES_DIR, STATE_DIR
from .names import norm_name, player_key
from .sources import draftkings, espn, nba_cdn, referees
from .sources import lineups as lineups_src

log = logging.getLogger("pipeline")

CSV_HEADER = ["Name", "Salary", "Position", "Team", "Opp", "Minutes", "FptsPerMin", "Proj",
              "Value", "Own", "OptPct", "Leverage", "DKID", "StdDev", "Boom", "Bust",
              "Ceiling", "Floor", "Injury", "Starting", "BatOrder", "StarterStatus", "Unexpected",
              "GameStartUTC", "MinutesRestricted", "CptID", "CptSalary", "LiveFP", "GameProgress",
              "OptExposure", "CorrGame"]


# ── state helpers ────────────────────────────────────────────────────────────
def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str, sort_keys=False))
    tmp.replace(path)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


# ── output ───────────────────────────────────────────────────────────────────
def csv_row(r: dict) -> list:
    status = r.get("status", "")
    injury = {"OUT": "Out", "DOUBTFUL": "Doubtful", "QUESTIONABLE": "Questionable",
              "PROBABLE": "Probable"}.get(status, "")
    return [
        r["name"], r["salary"], r["position"], r["team_dk"], r["opp_dk"],
        f"{r.get('proj_minutes', 0):.1f}", f"{r.get('fpts_min', 0):.3f}", f"{r['proj']:.2f}",
        f"{r['value']:.2f}", f"{r.get('own', 0):.1f}", f"{r.get('opt_pct', 0):.1f}",
        f"{r.get('lev', 0):.1f}", r.get("draftable_id") or "", f"{r['std_dev']:.2f}", f"{r['boom']:.2f}",
        f"{r['bust']:.2f}", f"{r['ceiling']:.2f}", f"{r['floor']:.2f}", injury,
        "true" if r.get("starting") else "false", 0,
        r.get("starter_status", ""), r.get("unexpected", ""), _iso(r.get("start_utc")) or "",
        "true" if r.get("restricted") else "false",
        r.get("cpt_id") or "", r.get("cpt_salary") or "",
        "" if r.get("live_fp") is None else f"{r['live_fp']:.2f}",
        "" if r.get("progress") is None else f"{r['progress']:.3f}",
        "" if r.get("opt_exposure") is None else f"{r['opt_exposure']:.1f}",
        r.get("competition_id") or "",
    ]


def write_csv(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: -r["proj"])
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for r in rows:
            w.writerow(csv_row(r))
    tmp.replace(path)


PUBLIC_FIELDS = [
    "name", "dk_player_id", "draftable_id", "position", "salary", "team_dk", "opp_dk", "home",
    "start_utc", "locked", "status", "reason", "status_source", "proj", "proj_minutes", "rate",
    "fpts_min", "bump", "env", "env_source", "implied_total", "spread", "dvp", "std_dev",
    "ceiling", "floor", "boom", "bust", "value", "starting", "games_cur", "games_prev",
    "has_history", "absence_streak", "is_starter", "starter_status", "unexpected", "start_rate",
    "restricted", "situational", "ww_evidence",
]


def public(r: dict) -> dict:
    out = {}
    for k in PUBLIC_FIELDS:
        v = r.get(k)
        if isinstance(v, float):
            v = round(v, 3)
        elif isinstance(v, datetime):
            v = _iso(v)
        out[k] = v
    return out


# ── main ─────────────────────────────────────────────────────────────────────
_MEM: dict = {}   # in-process cache shared by consecutive runs of the live loop


def _due(state: dict, key: str, now: datetime, minutes: float) -> bool:
    last = state.get(key)
    if not last:
        return True
    try:
        return (now - datetime.fromisoformat(last)).total_seconds() >= minutes * 60
    except ValueError:
        return True


def _load_logs(state: dict, now: datetime, today, force_refresh: bool):
    season = nba_cdn.season_start_year(today)
    logs = _MEM.get("logs")
    if logs is None:
        logs = history.load()
    if force_refresh or _due(state, "history_at", now, 20):
        try:
            if state.get("backfill_v") != 2:
                state["backfill_v"] = 2   # once: last season incl. referee crews
                logs = history.backfill_season(logs, season - 1)
            logs = history.update_current_season(logs, now)
            history.save(logs)
            state["history_at"] = now.isoformat()
            health.ok("NBA.com box scores", f"{logs['game_id'].nunique() if not logs.empty else 0} games stored")
        except Exception as e:  # noqa: BLE001
            log.warning("history update failed, using stored logs: %s", e)
            health.fail("NBA.com box scores", e)
    _MEM["logs"] = logs
    return logs


def _fit(state: dict, logs, today) -> dict:
    path = STATE_DIR / "factors.json"
    fit = _MEM.get("fit") or _read_json(path, {})
    if fit.get("date") != today.isoformat():
        try:
            fit = factors.fit_factors(model.prepare_history(logs, today))
        except Exception as e:  # noqa: BLE001
            log.warning("factor fit failed: %s", e)
            fit = {"used": False, "note": f"fit failed: {e}"}
        fit["date"] = today.isoformat()
        _write_json(path, fit)
    _MEM["fit"] = fit
    return fit


def _game_context(all_players, logs, refs_today: dict, fit: dict, today) -> dict[str, dict]:
    ctx = {}
    for p in all_players:
        team = p["team"]
        if team in ctx:
            continue
        arena = p["team"] if p["home"] else p["opp"]
        prev_d, prev_arena = None, None
        if not logs.empty:
            tl = logs[(logs["team"] == team) & (logs["date"].astype(str) < today.isoformat())]
            if not tl.empty:
                last = tl.sort_values("date").iloc[-1]
                prev_d = datetime.fromisoformat(str(last["date"])).date()
                prev_arena = last["team"] if bool(last["home"]) else last["opp"]
        crew = refs_today.get(team) or []
        ref_f = factors.crew_factor(crew, {k: (v, 99) for k, v in (fit.get("refs") or {}).items()})
        f = factors.context_features(team, arena, prev_d, prev_arena, today, ref_f)
        f["referees"] = crew
        ctx[team] = f
    return ctx


def _live_scoring(rows: list[dict], state: dict, now: datetime) -> None:
    """For games that have tipped: DraftKings points so far + share of the game played
    (official NBA live box score), refreshed every 3 minutes. Feeds the late swap tool."""
    started = {}
    for r in rows:
        if r["start_utc"] and r["start_utc"] <= now:
            started.setdefault((r["team"], r["opp"]), r["start_utc"])
    if not started:
        return
    cache = state.setdefault("live", {})
    if _due(state, "live_at", now, 3):
        sched = _MEM.get("schedule")
        if sched is None or _MEM.get("schedule_day") != now.date():
            try:
                sched = nba_cdn.schedule()
                _MEM["schedule"], _MEM["schedule_day"] = sched, now.date()
            except Exception as e:  # noqa: BLE001
                log.info("schedule unavailable for live scoring: %s", e)
                sched = []
        for (team, opp), start in started.items():
            g = next((g for g in sched if {g["home"], g["away"]} == {team, opp}
                      and g["start_utc"] and abs((g["start_utc"] - start).total_seconds()) < 6 * 3600), None)
            if not g:
                continue
            key = g["game_id"]
            if cache.get(key, {}).get("status") == 3:
                continue
            try:
                progress, status, players = nba_cdn.live_scores(key)
                cache[key] = {"progress": progress, "status": status,
                              "fp": {norm_name(p["name"]) + "|" + p["team"]: p["dk_fp"] for p in players}}
            except Exception as e:  # noqa: BLE001
                log.info("live box score %s unavailable: %s", key, e)
        state["live_at"] = now.isoformat()
    for r in rows:
        if not (r["start_utc"] and r["start_utc"] <= now):
            continue
        for g in cache.values():
            k = norm_name(r["name"]) + "|" + r["team"]
            if k in g.get("fp", {}):
                r["live_fp"] = g["fp"][k]
                r["progress"] = g["progress"]
                break
        else:
            r["live_fp"], r["progress"] = None, None


def _slate_hash(rows: list[dict]) -> str:
    key = sorted((r.get("draftable_id"), round(r["proj"] * 2) / 2, r.get("status", ""),
                  bool(r.get("is_starter")), round(r.get("own", 0))) for r in rows)
    return hashlib.sha1(json.dumps(key, default=str).encode()).hexdigest()[:16]


def run(now: datetime, do_notify: bool = True, force: bool = False, sims: bool = True) -> dict:
    """One pass. Returns {"status": exit code, "changed": outputs changed}."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / "run_state.json"
    state = _read_json(state_path, {})
    today = draftkings.dfs_day(now)
    today_iso = today.isoformat()
    site = os.environ.get("SITE_URL") or None

    # 1) history + fitted factors
    logs = _load_logs(state, now, today, force)
    fit = _fit(state, logs, today)

    # 2) slates: the lobby, plus groups seen earlier today (DraftKings drops a group from
    # the lobby once it locks, but late swap needs it until its last game tips)
    remembered = (state.get("dk_groups") or {}).get(today_iso, [])
    try:
        slates = draftkings.list_draft_groups(now)
    except Exception as e:  # noqa: BLE001
        log.error("DraftKings lobby unavailable: %s", e)
        slates = []
        if not remembered:
            _write_json(state_path, state)
            return {"status": 1, "changed": False}
    have = {s.draft_group_id for s in slates}
    slates += [draftkings.slate_from_header(h) for h in remembered if int(h["draft_group_id"]) not in have]
    state["dk_groups"] = {today_iso: [draftkings.slate_header(s) for s in slates]}
    if state.get("live_day") != today_iso:
        state["live"], state["live_day"] = {}, today_iso
    loaded = []
    for s in slates:
        try:
            loaded.append(draftkings.load_draftables(s))
        except Exception as e:  # noqa: BLE001
            log.info("skipping draft group %s: %s", s.draft_group_id, e)
    slates = draftkings.sort_slates([s for s in loaded if s.players])
    for s in slates:   # keep the detected kind (classic/showdown) for next time
        for h in state["dk_groups"][today_iso]:
            if int(h["draft_group_id"]) == s.draft_group_id:
                h["kind"], h["game_count"] = s.kind, s.game_count
    if not slates:
        log.info("no DraftKings classic NBA slate for %s", today_iso)
        before = (DATA_DIR / "meta.json").read_text() if (DATA_DIR / "meta.json").exists() else ""
        msg = {"updated_at": _iso(now), "date": today_iso, "slates": [],
               "message": "No NBA slate open on DraftKings",
               "dk_groups_seen": draftkings.seen[:40],
               "history_games": int(logs["game_id"].nunique()) if not logs.empty else 0,
               "factors": {"used": fit.get("used"), "backtest": fit.get("backtest"), "note": fit.get("note")}}
        _write_json(DATA_DIR / "meta.json", msg)
        _write_json(DATA_DIR / "slates.json", [])
        if not (DATA_DIR / "injury_log.json").exists():
            _write_json(DATA_DIR / "injury_log.json", [])
        _write_json(state_path, state)
        msg["sources"] = health.snapshot()
        _write_json(DATA_DIR / "meta.json", msg)
        changed = json.loads(before or "{}").get("message") != msg["message"] or _due(state, "published_at", now, 60)
        if changed:
            state["published_at"] = now.isoformat()
        _write_json(state_path, state)
        return {"status": 0, "changed": changed}

    players: dict[int, dict] = {}
    for s in slates:
        for p in s.players:
            p["key"] = player_key(p["name"], p["team"])
            q = players.setdefault(p["dk_player_id"], dict(p))
            # the same player appears on several slates; draftable ids differ per slate
            q.setdefault("slate_ids", {})[s.draft_group_id] = {
                "draftable_id": p["draftable_id"], "cpt_id": p.get("cpt_id"),
                "salary": p["salary"], "cpt_salary": p.get("cpt_salary")}
    all_players = list(players.values())
    starts = [p["start_utc"] for p in all_players if p["start_utc"]]
    if starts and max(starts) <= now and not force and state.get("final_run_for") == today_iso:
        log.info("every game has locked and the final projections are saved; nothing to do")
        return {"status": 0, "changed": False}

    # 3) injuries, odds, starting lineups, referees
    src = injuries.fetch_sources(now, state)
    statuses = injuries.merge(all_players, src, today_iso)
    odds = (state.get("odds") or {}).get(today_iso, {})
    if not odds or _due(state, "odds_at", now, 10):
        try:
            odds = espn.odds_for_date(today) or odds
            state["odds_at"] = now.isoformat()
            health.ok("ESPN odds", f"{len(odds) // 2} games")
        except Exception as e:  # noqa: BLE001
            log.warning("odds unavailable: %s", e)
            health.fail("ESPN odds", e)
    state["odds"] = {today_iso: odds}
    nba_lineups = (state.get("nba_lineups") or {}).get(today_iso, {})
    try:
        nba_lineups = lineups_src.fetch(today) or nba_lineups
        state["lineups_ok_at"] = now.isoformat()
        health.ok("NBA.com starting lineups", f"{len(nba_lineups)} teams posted")
    except Exception as e:  # noqa: BLE001
        log.info("NBA lineups feed unavailable: %s", e)
        health.fail("NBA.com starting lineups", e)
    state["nba_lineups"] = {today_iso: nba_lineups}
    refs_today = (state.get("refs") or {}).get(today_iso, {})
    if not refs_today and _due(state, "refs_at", now, 30):
        try:
            refs_today = referees.fetch()
            health.ok("NBA referee assignments", f"{len(refs_today) // 2} games")
        except Exception as e:  # noqa: BLE001
            log.info("referee assignments unavailable: %s", e)
            health.fail("NBA referee assignments", e)
        state["refs_at"] = now.isoformat()
    state["refs"] = {today_iso: refs_today}

    positions = _read_json(STATE_DIR / "positions.json", {})
    for p in all_players:
        positions[norm_name(p["name"])] = p["position"]
    _write_json(STATE_DIR / "positions.json", positions)

    played_yday = set()
    if not logs.empty:
        yday = (today - timedelta(days=1)).isoformat()
        played_yday = set(logs.loc[logs["date"].astype(str) == yday, "team"])

    game_ctx = _game_context(all_players, logs, refs_today, fit, today)
    ctx = model.Context(today=today, statuses=statuses, odds=odds, positions=positions,
                        teams_played_yesterday=played_yday, lineups=nba_lineups, factors=fit,
                        game_context=game_ctx)

    # 4) projections, freezing anyone whose game has started
    rows = model.project(all_players, logs, ctx)
    prev_doc = _read_json(DATA_DIR / "projections.json", {})
    prev = {}
    if prev_doc.get("date") == today_iso:
        prev = {p["dk_player_id"]: p for p in prev_doc.get("players", [])}
    frozen = 0
    for r in rows:
        r["locked"] = bool(r["start_utc"] and r["start_utc"] <= now)
        if r["locked"] and r["dk_player_id"] in prev:
            old = prev[r["dk_player_id"]]
            for k in ("proj", "proj_minutes", "fpts_min", "std_dev", "ceiling", "floor", "boom",
                      "bust", "value", "starting", "status", "reason", "bump", "env", "dvp",
                      "is_starter", "starter_status", "unexpected"):
                if k in old and old[k] is not None:
                    r[k] = old[k]
            frozen += 1
    locked_teams = {r["team"] for r in rows if r["locked"]}
    _live_scoring(rows, state, now)

    def impacts_for(teams_dk: set[str]) -> dict[str, list]:
        out: dict[str, list] = {}
        if not prev:
            return out
        for team in teams_dk:
            moves = []
            for r in rows:
                if r["team_dk"] != team or r["locked"]:
                    continue
                o = prev.get(r["dk_player_id"])
                if o and abs(r["proj"] - (o.get("proj") or 0)) >= 1.0:
                    moves.append((r["name"], o.get("proj") or 0.0, r["proj"]))
            out[team] = sorted(moves, key=lambda m: -abs(m[2] - m[1]))
        return out

    # 5) notifications: injuries
    old_status = _read_json(STATE_DIR / "injuries.json", {})
    first_ever = not old_status
    meta_by_key = {r["key"]: {"name": r["name"], "team_dk": r["team_dk"], "locked": r["locked"]} for r in rows}
    changes = [] if first_ever else injuries.diff(old_status, statuses, meta_by_key)
    if do_notify:
        if first_ever:
            listed = sum(1 for v in statuses.values() if v["status"])
            notify.send("🏀 Sigma DFS alerts are live",
                        f"Tracking {len(statuses)} players on {len(slates)} slate(s); "
                        f"{listed} on the injury report. You'll get injury and starting-lineup alerts here.",
                        3, ["basketball"], click=site)
        elif changes:
            notify.send_all(notify.injury_messages(changes, impacts_for({c["team"] for c in changes})), click=site)
    _write_json(STATE_DIR / "injuries.json", {**old_status, **statuses})
    log_path = DATA_DIR / "injury_log.json"
    hist_log = _read_json(log_path, [])
    if changes or not log_path.exists():
        hist_log = [dict(c, at=_iso(now)) for c in changes] + hist_log
        _write_json(log_path, hist_log[:300])

    # 5b) notifications: starting lineups (announced / confirmed / every change)
    dk_abbr = {r["team"]: r["team_dk"] for r in rows}
    old_lineups = _read_json(STATE_DIR / "lineups_seen.json", {})
    if old_lineups.get("date") != today_iso:
        old_lineups = {"date": today_iso, "teams": {}}
    lu_events = notify.lineup_changes(old_lineups["teams"], nba_lineups, locked_teams)
    if lu_events:
        unexpected: dict[str, list[str]] = {}
        for r in rows:
            if r.get("unexpected") == "starting":
                unexpected.setdefault(r["team"], []).append(
                    f"{r['name']} STARTING (started {round(r['start_rate']*10)} of last 10)")
            elif r.get("unexpected") == "benched":
                unexpected.setdefault(r["team"], []).append(f"{r['name']} NOT starting (usual starter)")
        for e in lu_events:
            e["team_dk"] = dk_abbr.get(e["team"], e["team"])
        if do_notify:
            msgs = notify.lineup_messages(
                [dict(e, team=e["team_dk"]) for e in lu_events],
                {dk_abbr.get(t, t): v for t, v in unexpected.items()},
                impacts_for({e["team_dk"] for e in lu_events}))
            notify.send_all(msgs, click=site)
        lu_log = _read_json(DATA_DIR / "lineup_log.json", [])
        lu_log = [dict(e, at=_iso(now)) for e in lu_events] + lu_log
        _write_json(DATA_DIR / "lineup_log.json", lu_log[:300])
    old_lineups["teams"].update(nba_lineups)
    _write_json(STATE_DIR / "lineups_seen.json", old_lineups)

    # 6) slate outputs
    by_id = {r["dk_player_id"]: r for r in rows}
    slate_meta = []
    classic = [s for s in slates if s.kind == "classic"]
    main_id = next((s.draft_group_id for s in classic if s.featured), (classic or slates)[0].draft_group_id)
    exp_model = patterns.load_exposure_model()
    key = (today_iso, len(logs))
    if _MEM.get("hist_key") != key:
        _MEM["hist_prep"], _MEM["hist_key"] = model.prepare_history(logs, today), key
    hist_prep = _MEM["hist_prep"]
    season = nba_cdn.season_start_year(today)
    corr = fit.get("corr") if (fit.get("corr") or {}).get("team_games", 0) >= 500 else SIM["default_corr"]
    sim_state = state.setdefault("sims", {})
    sims_ran = []
    for s in slates:
        srows = []
        for p in s.players:
            if p["dk_player_id"] not in by_id:
                continue
            r = dict(by_id[p["dk_player_id"]])
            r.update(draftable_id=p["draftable_id"], cpt_id=p.get("cpt_id"), cpt_salary=p.get("cpt_salary"))
            if r["salary"] != p["salary"]:      # value/boom/bust depend on this slate's salary
                r["salary"] = p["salary"]
                model.finish(r)
            srows.append(r)
        ownership.estimate_ownership(srows, 6 if s.kind == "showdown" else 8)
        if s.kind == "classic":
            ownership.optimal_rates(srows)
        rules = patterns.load_rules(s.game_count)
        spotlight.tag_exposure(srows, s.game_count, exp_model)
        try:
            _write_json(DATA_DIR / "spotlight" / f"{s.draft_group_id}.json",
                        {"slate_id": s.draft_group_id, "updated_at": _iso(now), "kind": s.kind,
                         "games": s.game_count, **spotlight.build(srows, hist_prep, season, s.game_count)})
        except Exception as e:  # noqa: BLE001
            log.warning("spotlight failed for %s: %s", s.draft_group_id, e)
        write_csv(SLATES_DIR / f"{s.draft_group_id}.csv", srows)
        if s.draft_group_id == main_id:
            write_csv(DATA_DIR / "projections.csv", srows)
            for r in srows:
                by_id[r["dk_player_id"]].update(own=r["own"], opt_pct=r["opt_pct"], lev=r["lev"])
        games = []
        for g in s.games:
            st = g.get("start_utc")
            games.append({"name": g.get("name"), "home": g.get("home"), "away": g.get("away"),
                          "start_utc": _iso(st),
                          "start_et": st.astimezone(ET).strftime("%-I:%M %p") if st else None,
                          "start_minutes": (st.astimezone(ET).hour * 60 + st.astimezone(ET).minute) if st else None,
                          "locked": bool(st and st <= now)})
        all_locked = all(g["locked"] for g in games) if games else False
        contest_locked = any(g["locked"] for g in games)
        lfile = f"lineups/{s.draft_group_id}.json"
        # 10,000 lineups x 1,000,000 simulated contests, re-run whenever the inputs move.
        # Once the first game tips no new entries are possible: the late swap tool takes over.
        if sims and not contest_locked:
            h = _slate_hash(srows)
            st = sim_state.get(str(s.draft_group_id), {})
            phase = None
            if st.get("hash") != h:
                phase = "fast"     # news: publish a fresh pool within about a minute
            elif st.get("phase") == "fast" and (s.draft_group_id == main_id or not _MEM.get("busy")):
                phase = "exact"    # then upgrade to exactly-solved lineups
            if phase:
                try:
                    res = sim.build_slate_lineups(srows, rules, corr, exact=(phase == "exact"),
                                                  seed=int(now.timestamp()) % 100000, kind=s.kind)
                    res.update({"generated_at": _iso(now), "slate_id": s.draft_group_id,
                                "phase": phase, "inputs": h})
                    _write_json(DATA_DIR / lfile, res)
                    sim_state[str(s.draft_group_id)] = {"hash": h, "phase": phase, "at": now.isoformat()}
                    sims_ran.append((s.draft_group_id, phase))
                except Exception as e:  # noqa: BLE001
                    log.exception("simulation failed for %s: %s", s.draft_group_id, e)
        slate_meta.append({"id": s.draft_group_id, "name": s.name, "main": s.draft_group_id == main_id,
                           "kind": s.kind, "contest_locked": contest_locked,
                           "start_utc": _iso(s.start_utc), "game_count": len(s.games),
                           "file": f"slates/{s.draft_group_id}.csv",
                           "lineups_file": lfile if (DATA_DIR / lfile).exists() else None,
                           "spotlight_file": f"spotlight/{s.draft_group_id}.json",
                           "size_bucket": patterns.size_bucket(s.game_count),
                           "lineups_phase": sim_state.get(str(s.draft_group_id), {}).get("phase"),
                           "games": games, "locked": all_locked})

    # starting lineups page: every team, announced or projected
    teams_out = {}
    for r in rows:
        t = teams_out.setdefault(r["team_dk"], {"team": r["team_dk"], "opp": r["opp_dk"], "home": r["home"],
                                               "start_utc": _iso(r["start_utc"]), "status": "projected",
                                               "starters": [], "out": [], "questionable": []})
        if r.get("is_starter"):
            t["starters"].append({"name": r["name"], "position": r["position"], "proj": round(r["proj"], 1),
                                  "unexpected": r.get("unexpected", "")})
            t["status"] = r.get("starter_status") or t["status"]
        elif r.get("unexpected") == "benched":
            t.setdefault("benched", []).append(r["name"])
        if r.get("status") in ("OUT", "DOUBTFUL"):
            t["out"].append(r["name"])
        elif r.get("status") == "QUESTIONABLE":
            t["questionable"].append(r["name"])
        t["referees"] = game_ctx.get(r["team"], {}).get("referees", [])
    _write_json(DATA_DIR / "starting_lineups.json", {
        "date": today_iso, "updated_at": _iso(now),
        "source": "NBA.com Today's Lineups (confirmed/expected); otherwise Sigma projection",
        "feed_ok_at": state.get("lineups_ok_at"),
        "teams": sorted(teams_out.values(), key=lambda t: (t["start_utc"] or "", t["team"]))})
    _write_json(DATA_DIR / "slates.json", slate_meta)
    _write_json(DATA_DIR / "projections.json", {
        "date": today_iso, "updated_at": _iso(now),
        "players": [public(r) | {"own": by_id[r["dk_player_id"]].get("own"),
                                 "opt_pct": by_id[r["dk_player_id"]].get("opt_pct"),
                                 "team": r["team"]} for r in rows],
    })
    if not (DATA_DIR / "patterns.json").exists():
        _write_json(DATA_DIR / "patterns.json", {"nights": 0, "rules": patterns.default_rules(),
                                                 "note": "Historical analysis runs in the background."})
    unlocked = [p["start_utc"] for p in all_players if p["start_utc"] and p["start_utc"] > now]
    _write_json(DATA_DIR / "meta.json", {
        "updated_at": _iso(now),
        "date": today_iso,
        "slates": [{"id": s["id"], "name": s["name"], "main": s["main"]} for s in slate_meta],
        "players": len(rows),
        "frozen_players": frozen,
        "next_lock_utc": _iso(min(unlocked)) if unlocked else None,
        "injury_report_url": src.get("nba_url"),
        "injury_report_fetched": src.get("nba_fetched"),
        "espn_injuries_fetched": src.get("espn_fetched"),
        "lineups_feed_ok_at": state.get("lineups_ok_at"),
        "teams_confirmed": sorted(t for t, v in nba_lineups.items() if v.get("status") == "confirmed"),
        "referee_crews": len(refs_today) // 2,
        "odds_games": len(odds) // 2,
        "history_games": int(logs["game_id"].nunique()) if not logs.empty else 0,
        "factors": {"used": fit.get("used"), "backtest": fit.get("backtest"), "coef": fit.get("coef"),
                    "note": fit.get("note")},
        "correlation": corr,
        "rules": patterns.load_rules(),
        "changes_this_run": len(changes),
        "lineup_events_this_run": len(lu_events),
        "sims_this_run": sims_ran,
        "sources": health.snapshot(),
    })
    if not unlocked:
        state["final_run_for"] = today_iso
    content = _slate_hash(rows) + json.dumps(sorted(nba_lineups.items()), default=str)
    changed = bool(changes or lu_events or sims_ran or state.get("content") != content
                   or _due(state, "published_at", now, 10))
    if changed:
        state["published_at"] = now.isoformat()
    state["content"] = content
    state["last_run"] = _iso(now)
    _write_json(state_path, state)
    log.info("projected %d players (%d frozen), %d injury changes, %d lineup events, sims %s",
             len(rows), frozen, len(changes), len(lu_events), sims_ran)
    return {"status": 0, "changed": changed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sigma DFS NBA projection pipeline")
    ap.add_argument("--no-notify", action="store_true", help="don't send phone notifications")
    ap.add_argument("--force", action="store_true", help="run even if every game has locked")
    ap.add_argument("--backfill", type=int, metavar="YEAR",
                    help="download a full past season (start year, e.g. 2024) and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.backfill:
        logs = history.backfill_season(history.load(), args.backfill)
        history.save(logs)
        return 0
    res = run(datetime.now(timezone.utc), do_notify=not args.no_notify, force=args.force)
    return res["status"]


if __name__ == "__main__":
    sys.exit(main())
