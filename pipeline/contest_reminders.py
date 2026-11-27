"""Reminders to export your DraftKings NBA contest results (private, owner only).

DraftKings offers each contest's standings CSV for 10 days after it ends, and
only to the logged-in player, so nothing here downloads anything: the Mac sends
you a push (through notify.send, your own NTFY_TOPIC) with links you open
yourself, and the Downloads watcher (contests.watch) imports the files.

* The morning after an NBA slate (first check at or after 09:00 ET): one push
  listing that night's contests you haven't exported yet.
* On day 7 after a contest that still isn't imported: one follow-up, before the
  export window closes. Nothing older than 10 days is reminded.

Contest ids come from DraftKings' "Download Entry History" file
(draftkings-contest-entry-history*.csv) if you drop one into Downloads or the
private drop folder; without it the push links to My Contests. State lives in
the private folder (entry_history.json, reminders.json). This module makes no
HTTP requests of its own.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from . import contests, history, notify
from .config import ET

log = logging.getLogger(__name__)

EXPORT_URL = "https://www.draftkings.com/contest/exportfullstandingscsv/{id}"   # unofficial; may change
GAMECENTER_URL = "https://www.draftkings.com/contest/gamecenter/{id}"
MY_CONTESTS_URL = "https://www.draftkings.com/mycontests"
ENTRY_GLOB = "draftkings-contest-entry-history*.csv"
REMIND_HOUR = 9
FOLLOWUP_DAY = 7
WINDOW_DAYS = 10

_KEY = ("contestkey", "contestid", "contest")
_SPORT = ("sport",)
_DATE = ("contestdateest", "contestdate", "date", "contestdatetime")
_NAME = ("entry", "contestname", "entryname")
_DATE_RES = (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"))


def _priv() -> Path:
    return Path(contests.PRIVATE)      # resolved at call time (tests redirect it)


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _parse_date(s: str) -> str | None:
    s = (s or "").strip()
    m = _DATE_RES[0].search(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = _DATE_RES[1].search(s)
        if not m:
            return None
        mo, d, y = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def parse_entry_history(text: str) -> dict[str, dict]:
    """{contest_id: {"date", "name"}} for NBA rows of a DraftKings entry-history CSV
    (header names matched case- and space-insensitively)."""
    rows = list(csv.reader(io.StringIO((text or "").lstrip("﻿"))))
    if not rows:
        return {}
    head = [_norm(h) for h in rows[0]]

    def col(names):
        return next((head.index(n) for n in names if n in head), None)
    ik, isp, idt, inm = col(_KEY), col(_SPORT), col(_DATE), col(_NAME)
    if ik is None or isp is None:
        return {}
    out: dict[str, dict] = {}
    for r in rows[1:]:
        if len(r) <= max(ik, isp):
            continue
        if r[isp].strip().upper() != "NBA":
            continue
        cid = re.sub(r"\D", "", r[ik])
        if not cid:
            continue
        d = _parse_date(r[idt]) if idt is not None and idt < len(r) else None
        name = r[inm].strip() if inm is not None and inm < len(r) else ""
        out.setdefault(cid, {"date": d, "name": name[:80]})
    return out


def _json(path: Path, default):
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def update_entry_history() -> dict[str, dict]:
    """Merge every entry-history file in the import folders into the private
    entry_history.json (raw files are left where they are)."""
    path = _priv() / "entry_history.json"
    known = _json(path, {})
    before = len(known)
    for d in contests._import_dirs():
        try:
            files = sorted(d.glob(ENTRY_GLOB))
        except OSError:
            continue
        for f in files:
            try:
                rows = parse_entry_history(f.read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                continue
            for cid, v in rows.items():
                known.setdefault(cid, v)
    if len(known) != before:
        _save(path, known)
    return known


def imported_ids() -> set[str]:
    m = _json(Path(contests.CONTESTS) / "index.json", {})
    have = {k for k in m if k.isdigit()}
    try:
        have |= {mm.group(1) for f in Path(contests.CONTESTS).glob("*.csv") if (mm := contests.FILE_RE.search(f.name))}
    except OSError:
        pass
    return have


def _slate_ran(day: str, known: dict) -> bool:
    if any(v.get("date") == day for v in known.values()):
        return True
    f = Path(history.HISTORY_DIR) / f"{day}.csv.gz"
    try:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            next(fh)                     # header
            return next(fh, None) is not None
    except (OSError, StopIteration, EOFError):
        return False


def _md(s: str) -> str:
    return re.sub(r"([\[\]()])", r"\\\1", s)


def build_message(items: list[tuple[str, dict]], followup: bool = False) -> dict:
    """notify.send kwargs. items = [(contest_id, {"date","name"})]; empty = generic."""
    if not items:
        return {"title": "Export yesterday's NBA contest results",
                "message": (f"Open [My Contests]({MY_CONTESTS_URL}) → History, export each NBA contest's standings "
                            "(Export Lineups to CSV) and leave the files in Downloads: they're imported automatically. "
                            "Optionally download Entry History too, so future reminders link each contest. "
                            "Exports are available for 10 days after a contest ends."),
                "click": MY_CONTESTS_URL,
                "actions": [{"action": "view", "label": "My Contests", "url": MY_CONTESTS_URL}]}
    lines = []
    for cid, v in items:
        label = _md(v.get("name") or f"Contest {cid}")
        lines.append(f"- [{label}]({EXPORT_URL.format(id=cid)}) · [GameCenter]({GAMECENTER_URL.format(id=cid)})")
    lines.append("")
    lines.append("Exports expire 10 days after the contest. Leave the downloaded files in Downloads and "
                 "they're imported automatically.")
    n = len(items)
    title = (f"Last chance: export {n} NBA contest result{'s' if n != 1 else ''}" if followup
             else f"Export yesterday's NBA contest results ({n})")
    actions = [{"action": "view", "label": ("Export " + (v.get("name") or cid))[:40], "url": EXPORT_URL.format(id=cid)}
               for cid, v in items[:3]]
    return {"title": title, "message": "\n".join(lines), "click": EXPORT_URL.format(id=items[0][0]),
            "actions": actions[:3]}


def _send(msg: dict) -> bool:
    return bool(notify.send(msg["title"], msg["message"], 3, ["inbox_tray"], click=msg.get("click"),
                            actions=msg.get("actions"), markdown=True))


def check(now: datetime | None = None) -> list[str]:
    """Send any due reminder. Returns the kinds sent ("morning:<day>", "followup:<n>")."""
    now = now or datetime.now(ET)
    local = now.astimezone(ET)
    if local.hour < REMIND_HOUR:
        return []
    today = local.date()
    state_path = _priv() / "reminders.json"
    state = _json(state_path, {})
    state.setdefault("days", {})
    state.setdefault("followups", {})
    known = update_entry_history()
    done = imported_ids()
    sent: list[str] = []
    changed = False

    d1 = (today - timedelta(days=1)).isoformat()
    if d1 not in state["days"] and _slate_ran(d1, known):
        items = sorted(((c, v) for c, v in known.items() if v.get("date") == d1 and c not in done), key=lambda x: x[0])
        if items or not any(v.get("date") == d1 for v in known.values()):
            if _send(build_message(items)):
                state["days"][d1] = now.isoformat()
                sent.append("morning:" + d1)
                changed = True
        else:                                   # every contest of that night is already imported
            state["days"][d1] = now.isoformat()
            changed = True

    due = []
    for cid, v in known.items():
        if cid in done or cid in state["followups"] or not v.get("date"):
            continue
        try:
            age = (today - date.fromisoformat(v["date"])).days
        except ValueError:
            continue
        if FOLLOWUP_DAY <= age <= WINDOW_DAYS:
            due.append((cid, v))
    if due:
        due.sort(key=lambda x: x[0])
        if _send(build_message(due, followup=True)):
            for cid, _ in due:
                state["followups"][cid] = now.isoformat()
            sent.append(f"followup:{len(due)}")
            changed = True

    if changed:
        # keep the state small: forget days and contests older than the export window
        cut = (today - timedelta(days=WINDOW_DAYS + 5)).isoformat()
        state["days"] = {k: v for k, v in state["days"].items() if k >= cut}
        state["followups"] = {k: v for k, v in state["followups"].items()
                              if (known.get(k, {}).get("date") or today.isoformat()) >= cut}
        _save(state_path, state)
    return sent
