"""Merge injury sources into one status per player and detect changes.

Priority: official NBA report (for teams that have submitted) > ESPN > DraftKings.
If a source fails to download, its last good copy is reused so a temporary
outage never looks like a wave of status changes.
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import health
from .names import norm_name, player_key, team_code
from .sources import espn, injury_report

log = logging.getLogger(__name__)


def fetch_sources(now: datetime, state: dict) -> dict:
    src = state.setdefault("injury_sources", {})
    # official report
    try:
        found = injury_report.find_latest(now, src.get("nba_url"))
        if found:
            url, body = found
            rows = injury_report.parse_pdf(body)
            if rows:
                src["nba_url"] = url
                src["nba_rows"] = rows
                src["nba_fetched"] = now.isoformat()
                health.ok("NBA injury report", url.rsplit("/", 1)[-1])
        else:
            health.fail("NBA injury report", "no report found")
    except Exception as e:  # noqa: BLE001
        log.warning("official injury report failed: %s", e)
        health.fail("NBA injury report", e)
    # ESPN
    try:
        rows = espn.injuries()
        if rows:
            src["espn_rows"] = rows
            src["espn_fetched"] = now.isoformat()
            health.ok("ESPN injuries", f"{len(rows)} listed")
    except Exception as e:  # noqa: BLE001
        log.warning("ESPN injuries failed: %s", e)
        health.fail("ESPN injuries", e)
    return src


def merge(players: list[dict], src: dict, today_iso: str | None = None) -> dict[str, dict]:
    """player_key -> {status, reason, source} for every slate player."""
    nba_rows = src.get("nba_rows") or []
    # only use the official report if it is for today's games
    if today_iso and nba_rows:
        mmddyyyy = _to_mmddyyyy(today_iso)
        dated = [r for r in nba_rows if r.get("game_date")]
        if dated and not any(r["game_date"] == mmddyyyy for r in dated):
            nba_rows = []
    submitted = {r["team"] for r in nba_rows if r.get("team") and not r.get("not_submitted")}
    not_submitted = {r["team"] for r in nba_rows if r.get("not_submitted")}
    submitted -= not_submitted
    official = {player_key(r["name"], r["team"]): r for r in nba_rows if r.get("name")}
    official_by_name = {norm_name(r["name"]): r for r in nba_rows if r.get("name")}
    espn_rows = {player_key(r["name"], r["team"]): r for r in (src.get("espn_rows") or [])}
    espn_by_name = {norm_name(r["name"]): r for r in (src.get("espn_rows") or [])}

    out: dict[str, dict] = {}
    for p in players:
        k = p["key"]
        nk = norm_name(p["name"])
        team = team_code(p["team"])
        rec = None
        if team in submitted:
            o = official.get(k) or official_by_name.get(nk)
            if o and team_code(o["team"]) == team:
                rec = {"status": o["status"], "reason": o.get("reason", ""), "source": "NBA"}
            else:
                rec = {"status": "", "reason": "", "source": "NBA"}
        if rec is None:
            e = espn_rows.get(k) or espn_by_name.get(nk)
            if e and e.get("status"):
                rec = {"status": e["status"], "reason": e.get("reason", ""), "source": "ESPN"}
        if rec is None and p.get("dk_status"):
            rec = {"status": p["dk_status"], "reason": "", "source": "DK"}
        if rec is None:
            rec = {"status": "", "reason": "", "source": ""}
        if rec["status"] == "AVAILABLE":
            rec["status"] = ""
        out[k] = rec
    return out


def diff(old: dict[str, dict], new: dict[str, dict], meta: dict[str, dict]) -> list[dict]:
    """Status changes between runs. meta: player_key -> {name, team, locked}."""
    changes = []
    for k, rec in new.items():
        info = meta.get(k, {})
        if info.get("locked"):
            continue
        before = (old.get(k) or {}).get("status", "")
        after = rec.get("status", "")
        if before == after:
            continue
        changes.append({
            "key": k, "name": info.get("name", k), "team": info.get("team_dk", ""),
            "from": before, "to": after, "reason": rec.get("reason", ""),
            "source": rec.get("source", ""),
        })
    sev = {"OUT": 0, "DOUBTFUL": 1, "QUESTIONABLE": 2, "PROBABLE": 3, "": 4}
    return sorted(changes, key=lambda c: (c["team"], sev.get(c["to"], 5), c["name"]))


def _to_mmddyyyy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"
