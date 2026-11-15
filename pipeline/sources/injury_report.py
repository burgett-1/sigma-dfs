"""Official NBA injury report (PDF published by the league throughout the day).

The league posts a new PDF at a predictable URL every 15 minutes on game days:
  https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-15_05_15PM.pdf
Older seasons used an hourly pattern without minutes (..._05PM.pdf); both are tried.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timedelta

from ..config import ET
from ..http import get_bytes, session, HTTP_TIMEOUT
from ..names import flip_last_first, full_team_names, team_code

log = logging.getLogger(__name__)

BASE = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{stamp}.pdf"
INDEX_PAGES = [
    "https://official.nba.com/nba-injury-report-{y1}-{y2}-season/",
]
STATUSES = ("Out", "Doubtful", "Questionable", "Probable", "Available")
_STATUS_RE = re.compile(r"\b(" + "|".join(STATUSES) + r")\b")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_MATCHUP_RE = re.compile(r"^[A-Z]{2,4}@[A-Z]{2,4}$")


def candidate_urls(now: datetime, hours_back: float = 30.0) -> list[tuple[datetime, str]]:
    """Newest-first list of (report time, url) to probe."""
    local = now.astimezone(ET)
    t = local.replace(minute=(local.minute // 15) * 15, second=0, microsecond=0)
    out = []
    steps = int(hours_back * 4)
    for i in range(steps + 1):
        ts = t - timedelta(minutes=15 * i)
        hour12 = ts.strftime("%I")
        ampm = ts.strftime("%p")
        day = ts.strftime("%Y-%m-%d")
        out.append((ts, BASE.format(stamp=f"{day}_{hour12}_{ts.strftime('%M')}{ampm}")))
        if ts.minute == 0:
            out.append((ts, BASE.format(stamp=f"{day}_{hour12}{ampm}")))
    return out


def find_latest(now: datetime, last_known_url: str | None = None) -> tuple[str, bytes] | None:
    """Return (url, pdf_bytes) for the newest report available."""
    # 1) the league's index page links the latest PDFs directly
    try:
        y1 = now.astimezone(ET).year if now.astimezone(ET).month >= 8 else now.astimezone(ET).year - 1
        for page in INDEX_PAGES:
            r = session().get(page.format(y1=y1, y2=y1 + 1), timeout=HTTP_TIMEOUT)
            if r.ok:
                links = re.findall(r"https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_[\w-]+\.pdf", r.text)
                links = sorted(set(links), key=_url_time, reverse=True)
                for link in links[:3]:
                    body = get_bytes(link)
                    if body and body[:4] == b"%PDF":
                        return link, body
    except Exception as e:  # noqa: BLE001 - index page is best-effort
        log.info("injury index page unavailable: %s", e)
    # 2) probe the predictable URL pattern, newest first
    last_t = _url_time(last_known_url) if last_known_url else None
    for ts, url in candidate_urls(now):
        if last_t and ts < last_t - timedelta(hours=1):
            # nothing newer than what we already have; reuse it
            body = get_bytes(last_known_url)
            return (last_known_url, body) if body else None
        body = get_bytes(url)
        if body and body[:4] == b"%PDF":
            return url, body
    return None


def _url_time(url: str | None) -> datetime:
    if not url:
        return datetime.min.replace(tzinfo=ET)
    m = re.search(r"Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})(?:_(\d{2}))?(AM|PM)", url)
    if not m:
        return datetime.min.replace(tzinfo=ET)
    day, hh, mm, ampm = m.groups()
    dt = datetime.strptime(f"{day} {hh}:{mm or '00'} {ampm}", "%Y-%m-%d %I:%M %p")
    return dt.replace(tzinfo=ET)


def parse_pdf(pdf_bytes: bytes) -> list[dict]:
    import pdfplumber

    rows: list[dict] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        lines: list[str] = []
        for page in pdf.pages:
            text = page.extract_text(layout=False) or ""
            lines.extend(text.splitlines())
    return parse_lines(lines)


def parse_lines(lines: list[str]) -> list[dict]:
    """Parse report text lines. Game/team cells are blank when repeated, so
    the last seen value is carried forward."""
    teams = full_team_names()
    cur = {"date": "", "time": "", "matchup": "", "team": ""}
    out: list[dict] = []
    pending: dict | None = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("Injury Report:", "Page ", "Game Date")):
            continue
        tokens = line.split()
        # leading date / time / matchup
        if tokens and _DATE_RE.match(tokens[0]):
            cur["date"] = tokens.pop(0)
        if len(tokens) >= 2 and _TIME_RE.match(tokens[0]) and tokens[1].startswith("("):
            cur["time"] = f"{tokens[0]} {tokens[1]}"
            tokens = tokens[2:]
        if tokens and _MATCHUP_RE.match(tokens[0]):
            cur["matchup"] = tokens.pop(0)
        rest = " ".join(tokens)
        for t in teams:
            if rest.startswith(t):
                cur["team"] = team_code(t)
                rest = rest[len(t):].strip()
                break
        if not rest:
            continue
        if rest.upper().startswith("NOT YET SUBMITTED"):
            pending = None
            out.append({"team": cur["team"], "not_submitted": True, "matchup": cur["matchup"]})
            continue
        m = _STATUS_RE.search(rest)
        if m and "," in rest[: m.start()]:
            name = rest[: m.start()].strip()
            reason = rest[m.end():].strip()
            pending = {
                "name": flip_last_first(name),
                "team": cur["team"],
                "status": m.group(1).upper(),
                "reason": reason,
                "matchup": cur["matchup"],
                "game_date": cur["date"],
                "game_time": cur["time"],
                "source": "NBA",
            }
            out.append(pending)
        elif pending is not None:
            # wrapped reason text from the previous row
            pending["reason"] = (pending["reason"] + " " + rest).strip()
    return out
