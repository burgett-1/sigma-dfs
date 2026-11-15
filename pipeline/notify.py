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
