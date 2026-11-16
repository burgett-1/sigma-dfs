"""Today's referee crews from the NBA's official assignments page
(official.nba.com/referee-assignments, posted each morning ~9am ET)."""
from __future__ import annotations

import html
import logging
import re

from ..http import session, HTTP_TIMEOUT
from ..names import team_code

log = logging.getLogger(__name__)

URL = "https://official.nba.com/referee-assignments/"

# how the assignments page names teams (city or nickname)
CITY = {
    "atlanta": "ATL", "boston": "BOS", "brooklyn": "BKN", "charlotte": "CHA", "chicago": "CHI",
    "cleveland": "CLE", "dallas": "DAL", "denver": "DEN", "detroit": "DET", "golden state": "GSW",
    "houston": "HOU", "indiana": "IND", "l.a. clippers": "LAC", "la clippers": "LAC",
    "los angeles clippers": "LAC", "l.a. lakers": "LAL", "la lakers": "LAL", "los angeles lakers": "LAL",
    "memphis": "MEM", "miami": "MIA", "milwaukee": "MIL", "minnesota": "MIN", "new orleans": "NOP",
    "new york": "NYK", "oklahoma city": "OKC", "orlando": "ORL", "philadelphia": "PHI",
    "phoenix": "PHX", "portland": "POR", "sacramento": "SAC", "san antonio": "SAS", "toronto": "TOR",
    "utah": "UTA", "washington": "WAS",
}


def _team(s: str) -> str:
    k = s.strip().lower()
    return CITY.get(k) or team_code(s)


def fetch() -> dict[str, list[str]]:
    r = session().get(URL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return parse(r.text)


def parse(page: str) -> dict[str, list[str]]:
    """{team: [official names]} for both teams in each game."""
    out: dict[str, list[str]] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)]
        cells = [re.sub(r"\s+", " ", c) for c in cells]
        if len(cells) < 4 or "@" not in cells[0]:
            continue
        away, home = (x.strip() for x in cells[0].split("@", 1))
        refs = []
        for c in cells[1:4]:  # crew chief, referee, umpire (alternate excluded)
            name = re.sub(r"\(#?\d+\)", "", c).strip()
            if name:
                refs.append(name)
        a, h = _team(away), _team(home)
        if a and h and refs:
            out[a] = refs
            out[h] = refs
    return out
