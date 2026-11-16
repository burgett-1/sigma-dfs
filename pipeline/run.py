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
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from . import history, injuries, model, notify, ownership
from .config import DATA_DIR, ET, SLATES_DIR, STATE_DIR
from .names import norm_name, player_key
from .sources import draftkings, espn, nba_cdn

log = logging.getLogger("pipeline")

CSV_HEADER = ["Name", "Salary", "Position", "Team", "Opp", "Minutes", "FptsPerMin", "Proj",
              "Value", "Own", "OptPct", "Leverage", "Unused", "StdDev", "Boom", "Bust",
              "Ceiling", "Floor", "Injury", "Starting", "BatOrder"]


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
        f"{r.get('lev', 0):.1f}", "", f"{r['std_dev']:.2f}", f"{r['boom']:.2f}",
        f"{r['bust']:.2f}", f"{r['ceiling']:.2f}", f"{r['floor']:.2f}", injury,
        "true" if r.get("starting") else "false", 0,
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
    "has_history", "absence_streak",
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
def run(now: datetime, do_notify: bool = True, force: bool = False) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / "run_state.json"
    state = _read_json(state_path, {})
    today = draftkings.dfs_day(now)
    today_iso = today.isoformat()

    # 1) history (new box scores since the last run)
    logs = history.load()
    season = nba_cdn.season_start_year(today)
    try:
        have_prev = (not logs.empty) and (logs["date"].astype(str) < f"{season}-08-01").any()
        if not have_prev and state.get("backfilled") != season - 1:
            state["backfilled"] = season - 1  # attempt once; rerun with --backfill if needed
            logs = history.backfill_season(logs, season - 1)
        logs = history.update_current_season(logs, now)
        history.save(logs)
    except Exception as e:  # noqa: BLE001
        log.warning("history update failed, using stored logs: %s", e)

    # 2) slates
    try:
        slates = draftkings.list_classic_draft_groups(now)
    except Exception as e:  # noqa: BLE001
        log.error("DraftKings lobby unavailable: %s", e)
        return 1
    loaded = []
    for s in slates:
        try:
            loaded.append(draftkings.load_draftables(s))
        except Exception as e:  # noqa: BLE001
            log.info("skipping draft group %s: %s", s.draft_group_id, e)
    slates = [s for s in loaded if s.players]
    if not slates:
        log.info("no DraftKings classic NBA slate for %s", today_iso)
        _write_json(DATA_DIR / "meta.json", {"updated_at": _iso(now), "date": today_iso,
                                             "slates": [], "message": "No NBA slate open on DraftKings",
                                             "dk_groups_seen": draftkings.seen[:40],
                                             "history_games": int(logs["game_id"].nunique()) if not logs.empty else 0})
        _write_json(state_path, state)
        return 0

    players: dict[int, dict] = {}
    for s in slates:
        for p in s.players:
            p["key"] = player_key(p["name"], p["team"])
            players.setdefault(p["dk_player_id"], p)
    all_players = list(players.values())
    starts = [p["start_utc"] for p in all_players if p["start_utc"]]
    if starts and max(starts) <= now and not force and state.get("final_run_for") == today_iso:
        log.info("every game has locked and the final projections are saved; nothing to do")
        return 0

    # 3) injuries, odds, positions
    src = injuries.fetch_sources(now, state)
    statuses = injuries.merge(all_players, src, today_iso)
    try:
        odds = espn.odds_for_date(today)
    except Exception as e:  # noqa: BLE001
        log.warning("odds unavailable: %s", e)
        odds = state.get("odds", {}).get(today_iso, {})
    state["odds"] = {today_iso: odds}

    positions = _read_json(STATE_DIR / "positions.json", {})
    for p in all_players:
        positions[norm_name(p["name"])] = p["position"]
    _write_json(STATE_DIR / "positions.json", positions)

    played_yday = set()
    if not logs.empty:
        yday = (today - timedelta(days=1)).isoformat()
        played_yday = set(logs.loc[logs["date"].astype(str) == yday, "team"])

    ctx = model.Context(today=today, statuses=statuses, odds=odds, positions=positions,
                        teams_played_yesterday=played_yday)

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
                      "bust", "value", "starting", "status", "reason", "bump", "env", "dvp"):
                if k in old and old[k] is not None:
                    r[k] = old[k]
            frozen += 1

    # 5) injury notifications (before writing, so we can show projection moves)
    old_status = _read_json(STATE_DIR / "injuries.json", {})
    first_ever = not old_status
    meta = {r["key"]: {"name": r["name"], "team_dk": r["team_dk"], "locked": r["locked"]} for r in rows}
    changes = [] if first_ever else injuries.diff(old_status, statuses, meta)
    impacts: dict[str, list] = {}
    if changes and prev:
        prev_by_key = {player_key(p["name"], p["team_dk"]): p for p in prev.values()}
        for team in {c["team"] for c in changes}:
            moves = []
            for r in rows:
                if r["team_dk"] != team or r["locked"]:
                    continue
                o = prev_by_key.get(r["key"])
                if o and abs(r["proj"] - (o.get("proj") or 0)) >= 1.0:
                    moves.append((r["name"], o.get("proj") or 0.0, r["proj"]))
            impacts[team] = sorted(moves, key=lambda m: -abs(m[2] - m[1]))
    if do_notify:
        site = os.environ.get("SITE_URL") or None
        if first_ever:
            listed = sum(1 for v in statuses.values() if v["status"])
            notify.send("🏀 Sigma DFS injury alerts are live",
                        f"Tracking {len(statuses)} players on {len(slates)} slate(s); "
                        f"{listed} currently on the injury report.", 3, ["basketball"], click=site)
        elif changes:
            notify.send_all(notify.injury_messages(changes, impacts), click=site)
    # keep statuses for players we didn't see this run (e.g. other days)
    merged_status = {**old_status, **statuses}
    _write_json(STATE_DIR / "injuries.json", merged_status)
    log_path = DATA_DIR / "injury_log.json"
    hist_log = _read_json(log_path, [])
    if changes or not log_path.exists():
        stamp = _iso(now)
        hist_log = [dict(c, at=stamp) for c in changes] + hist_log
        _write_json(log_path, hist_log[:300])

    # 6) slate outputs
    by_id = {r["dk_player_id"]: r for r in rows}
    slate_meta = []
    main_id = slates[0].draft_group_id
    for s in slates:
        srows = [dict(by_id[p["dk_player_id"]]) for p in s.players if p["dk_player_id"] in by_id]
        ownership.estimate_ownership(srows)
        ownership.optimal_rates(srows)
        path = SLATES_DIR / f"{s.draft_group_id}.csv"
        write_csv(path, srows)
        if s.draft_group_id == main_id:
            write_csv(DATA_DIR / "projections.csv", srows)
            for r in srows:  # carry slate-specific numbers into the JSON for the main slate
                by_id[r["dk_player_id"]].update(own=r["own"], opt_pct=r["opt_pct"], lev=r["lev"])
        games = []
        for g in s.games:
            st = g.get("start_utc")
            games.append({"name": g.get("name"), "home": g.get("home"), "away": g.get("away"),
                          "start_utc": _iso(st),
                          "start_et": st.astimezone(ET).strftime("%-I:%M %p") if st else None,
                          "start_minutes": (st.astimezone(ET).hour * 60 + st.astimezone(ET).minute) if st else None,
                          "locked": bool(st and st <= now)})
        slate_meta.append({"id": s.draft_group_id, "name": s.name, "main": s.draft_group_id == main_id,
                           "start_utc": _iso(s.start_utc), "game_count": len(s.games),
                           "file": f"slates/{s.draft_group_id}.csv", "games": games})

    for s in slate_meta:
        s["locked"] = all(g["locked"] for g in s["games"]) if s["games"] else False
    _write_json(DATA_DIR / "slates.json", slate_meta)
    _write_json(DATA_DIR / "projections.json", {
        "date": today_iso, "updated_at": _iso(now),
        "players": [public(r) | {"own": by_id[r["dk_player_id"]].get("own"),
                                 "opt_pct": by_id[r["dk_player_id"]].get("opt_pct")} for r in rows],
    })
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
        "odds_games": len(odds) // 2,
        "history_games": int(logs["game_id"].nunique()) if not logs.empty else 0,
        "changes_this_run": len(changes),
    })
    if not unlocked:
        state["final_run_for"] = today_iso
    state["last_run"] = _iso(now)
    _write_json(state_path, state)
    log.info("projected %d players (%d frozen), %d injury changes, %d slates",
             len(rows), frozen, len(changes), len(slate_meta))
    return 0


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
    return run(datetime.now(timezone.utc), do_notify=not args.no_notify, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
