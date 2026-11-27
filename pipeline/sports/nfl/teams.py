"""The 32 NFL teams and their codes at each source: DraftKings (canonical in the
pipeline), nflverse, ESPN, plus the DK DST nickname and full name."""
from __future__ import annotations

# (dk, nflverse, espn, nickname, full name)
_TEAMS = [
    ("ARI", "ARI", "ARI", "Cardinals", "Arizona Cardinals"), ("ATL", "ATL", "ATL", "Falcons", "Atlanta Falcons"),
    ("BAL", "BAL", "BAL", "Ravens", "Baltimore Ravens"), ("BUF", "BUF", "BUF", "Bills", "Buffalo Bills"),
    ("CAR", "CAR", "CAR", "Panthers", "Carolina Panthers"), ("CHI", "CHI", "CHI", "Bears", "Chicago Bears"),
    ("CIN", "CIN", "CIN", "Bengals", "Cincinnati Bengals"), ("CLE", "CLE", "CLE", "Browns", "Cleveland Browns"),
    ("DAL", "DAL", "DAL", "Cowboys", "Dallas Cowboys"), ("DEN", "DEN", "DEN", "Broncos", "Denver Broncos"),
    ("DET", "DET", "DET", "Lions", "Detroit Lions"), ("GB", "GB", "GB", "Packers", "Green Bay Packers"),
    ("HOU", "HOU", "HOU", "Texans", "Houston Texans"), ("IND", "IND", "IND", "Colts", "Indianapolis Colts"),
    ("JAX", "JAX", "JAX", "Jaguars", "Jacksonville Jaguars"), ("KC", "KC", "KC", "Chiefs", "Kansas City Chiefs"),
    ("LV", "LV", "LV", "Raiders", "Las Vegas Raiders"), ("LAC", "LAC", "LAC", "Chargers", "Los Angeles Chargers"),
    ("LAR", "LA", "LAR", "Rams", "Los Angeles Rams"), ("MIA", "MIA", "MIA", "Dolphins", "Miami Dolphins"),
    ("MIN", "MIN", "MIN", "Vikings", "Minnesota Vikings"), ("NE", "NE", "NE", "Patriots", "New England Patriots"),
    ("NO", "NO", "NO", "Saints", "New Orleans Saints"), ("NYG", "NYG", "NYG", "Giants", "New York Giants"),
    ("NYJ", "NYJ", "NYJ", "Jets", "New York Jets"), ("PHI", "PHI", "PHI", "Eagles", "Philadelphia Eagles"),
    ("PIT", "PIT", "PIT", "Steelers", "Pittsburgh Steelers"), ("SF", "SF", "SF", "49ers", "San Francisco 49ers"),
    ("SEA", "SEA", "SEA", "Seahawks", "Seattle Seahawks"), ("TB", "TB", "TB", "Buccaneers", "Tampa Bay Buccaneers"),
    ("TEN", "TEN", "TEN", "Titans", "Tennessee Titans"), ("WAS", "WAS", "WSH", "Commanders", "Washington Commanders"),
]
TEAMS = [{"dk": d, "nflverse": n, "espn": e, "nickname": k, "name": f} for d, n, e, k, f in _TEAMS]
DK = {t["dk"] for t in TEAMS}
_BY = {src: {t[src]: t for t in TEAMS} for src in ("dk", "nflverse", "espn", "nickname", "name")}
# historic / alternate spellings seen across sources
_ALT = {"LA": "LAR", "WSH": "WAS", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LAR", "LVR": "LV", "GNB": "GB",
        "KAN": "KC", "NWE": "NE", "NOR": "NO", "SFO": "SF", "TAM": "TB"}


def dk(code: str | None, source: str = "dk") -> str:
    """Canonical (DraftKings) code for a team code from `source`; '' if unknown."""
    c = (code or "").strip()
    if not c:
        return ""
    t = _BY.get(source, {}).get(c if source in ("nickname", "name") else c.upper())
    if t:
        return t["dk"]
    c = c.upper()
    if c in DK:
        return c
    return _ALT.get(c, "")


def to(source: str, dk_code: str) -> str:
    t = _BY["dk"].get((dk_code or "").upper())
    return t[source] if t else ""


def nickname(dk_code: str) -> str:
    return to("nickname", dk_code)


def from_name(name: str) -> str:
    """'Buffalo Bills' or 'Bills' -> 'BUF'."""
    n = (name or "").strip()
    return dk(n, "name") or dk(n, "nickname")
