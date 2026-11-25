"""Team and player name normalisation so DraftKings, NBA.com and ESPN agree."""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache

from .config import ROOT

# canonical key = NBA.com tricode
TEAMS = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}

_ALIASES = {
    "GS": "GSW", "GOS": "GSW", "NO": "NOP", "NOR": "NOP", "NY": "NYK", "SA": "SAS",
    "PHO": "PHX", "UTAH": "UTA", "UTH": "UTA", "WSH": "WAS", "BRK": "BKN", "BKLYN": "BKN", "BK": "BKN",
    "CHO": "CHA", "LA": "LAC", "NJN": "BKN",
}

_FULLNAME_TO_TRI = {v.lower(): k for k, v in TEAMS.items()}
_FULLNAME_TO_TRI.update({
    "los angeles clippers": "LAC", "la lakers": "LAL", "philadelphia sixers": "PHI",
})
# nickname only, e.g. "Celtics"
_NICK_TO_TRI = {v.split()[-1].lower(): k for k, v in TEAMS.items()}
_NICK_TO_TRI["blazers"] = "POR"
_NICK_TO_TRI["76ers"] = "PHI"


def team_code(raw: str | None) -> str:
    """Return the NBA.com tricode for any abbreviation or full team name."""
    if not isinstance(raw, str) or not raw:
        return ""
    s = raw.strip()
    up = s.upper()
    if up in TEAMS:
        return up
    if up in _ALIASES:
        return _ALIASES[up]
    low = s.lower()
    if low in _FULLNAME_TO_TRI:
        return _FULLNAME_TO_TRI[low]
    last = low.split()[-1] if low.split() else ""
    if last in _NICK_TO_TRI:
        return _NICK_TO_TRI[last]
    return up


def full_team_names() -> list[str]:
    names = list(TEAMS.values()) + ["Los Angeles Clippers"]
    return sorted(names, key=len, reverse=True)


_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.I)


def norm_name(name: str | None) -> str:
    """Lowercase ascii name without punctuation or Jr./III suffixes."""
    if not isinstance(name, str) or not name:     # blank cells arrive as NaN floats
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("’", "'")
    s = _SUFFIX.sub(" ", s)
    s = re.sub(r"[^a-z ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    alias = _aliases().get(s)
    return alias or s


def flip_last_first(name: str) -> str:
    """'James, LeBron' -> 'LeBron James' (official injury report format)."""
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name.strip()


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    """Optional manual fixes in name_aliases.json: {"variant": "canonical"}."""
    path = ROOT / "name_aliases.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in raw.items():
        k2 = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", k.lower())).strip()
        v2 = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", v.lower())).strip()
        out[k2] = v2
    return out


def player_key(name: str, team: str) -> str:
    return f"{norm_name(name)}|{team_code(team)}"
