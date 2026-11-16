"""Phone push notifications through ntfy (free app for iOS and Android).

Set the NTFY_TOPIC secret to a long random topic name and subscribe to that
topic in the ntfy app. NTFY_SERVER / NTFY_TOKEN are optional (self-hosted or
reserved topics).
"""
from __future__ import annotations

import logging
import os
import time

from .http import session

log = logging.getLogger(__name__)

PRETTY = {"OUT": "OUT", "DOUBTFUL": "Doubtful", "QUESTIONABLE": "Questionable",
          "PROBABLE": "Probable", "": "Available"}


def _label(s: str) -> str:
    return PRETTY.get(s, s.title())


def send(title: str, message: str, priority: int = 3, tags: list[str] | None = None,
         click: str | None = None) -> bool:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log.info("[notify disabled] %s | %s", title, message.replace("\n", " / "))
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    headers = {}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    try:
        r = session().post(server, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("notification failed: %s", e)
        return False


def injury_messages(changes: list[dict], impacts: dict[str, list[tuple[str, float, float]]]) -> list[dict]:
    """One push per team per run, listing each player whose status changed and
    the teammates whose projections moved the most."""
    by_team: dict[str, list[dict]] = {}
    for c in changes:
        by_team.setdefault(c["team"], []).append(c)
    msgs = []
    for team, cs in by_team.items():
        lines = []
        worst = 3
        for c in cs:
            line = f"{c['name']}: {_label(c['from'])} → {_label(c['to'])}"
            if c.get("reason"):
                line += f" ({c['reason'][:70]})"
            lines.append(line)
            if c["to"] in ("OUT", "DOUBTFUL"):
                worst = 5          # newly ruled out: urgent
            elif c["to"] in ("", "PROBABLE") and c["from"] in ("OUT", "DOUBTFUL", "QUESTIONABLE"):
                worst = max(worst, 4)  # cleared to play
            elif c["to"] == "QUESTIONABLE":
                worst = max(worst, 4)
        imp = impacts.get(team) or []
        if imp:
            lines.append("Proj moves: " + ", ".join(f"{n} {a:.1f}→{b:.1f}" for n, a, b in imp[:4]))
        if len(cs) == 1:
            c = cs[0]
            title = f"🏀 {c['name']} ({team}) → {_label(c['to'])}"
        else:
            title = f"🏀 {team}: {len(cs)} injury updates"
        msgs.append({"title": title, "message": "\n".join(lines), "priority": worst,
                     "tags": ["basketball", "rotating_light" if worst >= 4 else "warning"]})
    return msgs


def send_all(msgs: list[dict], click: str | None = None) -> int:
    sent = 0
    for m in msgs:
        if send(m["title"], m["message"], m["priority"], m["tags"], click=click):
            sent += 1
        time.sleep(0.5)
    return sent


def lineup_changes(old: dict[str, dict], new: dict[str, dict], locked_teams: set[str]) -> list[dict]:
    """Starting-lineup events: first posted, confirmed, or any change."""
    events = []
    for team, cur in new.items():
        if team in locked_teams:
            continue
        prev = old.get(team) or {}
        before, after = list(prev.get("starters") or []), list(cur.get("starters") or [])
        if not after:
            continue
        if not before:
            kind = "confirmed" if cur["status"] == "confirmed" else "expected"
        elif set(before) != set(after):
            kind = "changed"
        elif prev.get("status") != "confirmed" and cur["status"] == "confirmed":
            kind = "confirmed"
        else:
            continue
        events.append({"team": team, "kind": kind, "status": cur["status"], "starters": after,
                       "in": [n for n in after if n not in before] if before else [],
                       "out": [n for n in before if n not in after]})
    return events


def lineup_messages(events: list[dict], unexpected: dict[str, list[str]],
                    impacts: dict[str, list[tuple[str, float, float]]]) -> list[dict]:
    msgs = []
    for e in events:
        team = e["team"]
        if e["kind"] == "changed":
            title = f"🔁 {team} starting lineup CHANGED"
            lines = [f"In: {', '.join(e['in'])}", f"Out: {', '.join(e['out'])}",
                     f"Now ({e['status']}): {', '.join(e['starters'])}"]
            prio = 5
        elif e["kind"] == "confirmed":
            title = f"✅ {team} starters confirmed"
            lines = [", ".join(e["starters"])]
            prio = 4
        else:
            title = f"📋 {team} expected starters posted"
            lines = [", ".join(e["starters"])]
            prio = 3
        for note in unexpected.get(team, []):
            lines.append(f"⚠️ {note}")
            prio = max(prio, 5)
        imp = impacts.get(team) or []
        if imp:
            lines.append("Proj moves: " + ", ".join(f"{n} {a:.1f}→{b:.1f}" for n, a, b in imp[:4]))
        msgs.append({"title": title, "message": "\n".join(lines), "priority": prio,
                     "tags": ["basketball", "clipboard"]})
    return msgs
