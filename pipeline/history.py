"""Incremental store of every player game log (box score row) used by the model."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd

from .config import DK_DOUBLE_DOUBLE, DK_SCORING, DK_TRIPLE_DOUBLE, HISTORY_DIR
from .sources import nba_cdn

log = logging.getLogger(__name__)

COLUMNS = [
    "game_id", "date", "team", "opp", "home", "person_id", "name", "position", "starter",
    "status", "not_playing_reason", "minutes", "pts", "reb", "ast", "stl", "blk", "tov",
    "fg3m", "fga", "fta", "team_score", "opp_score", "dk_fp", "officials", "arena_team",
]


def dk_points(df: pd.DataFrame) -> pd.Series:
    fp = sum(df[k] * w for k, w in DK_SCORING.items())
    cats = (df[["pts", "reb", "ast", "stl", "blk"]] >= 10).sum(axis=1)
    bonus = (cats >= 2) * DK_DOUBLE_DOUBLE + (cats >= 3) * DK_TRIPLE_DOUBLE
    return (fp + bonus).round(2)


def _day_path(day: str):
    return HISTORY_DIR / f"{day}.csv.gz"


def load() -> pd.DataFrame:
    """All stored game logs (one gzip CSV per game date in data/history/)."""
    files = sorted(HISTORY_DIR.glob("*.csv.gz")) if HISTORY_DIR.exists() else []
    if not files:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.concat([pd.read_csv(f, dtype={"game_id": str, "not_playing_reason": str,
                                           "position": str, "status": str}) for f in files],
                   ignore_index=True)
    for c in ("not_playing_reason", "position", "status"):
        df[c] = df[c].fillna("")
    # columns added later: older days are re-downloaded once to fill them
    if "officials" not in df.columns:
        df["officials"] = ""
    if "arena_team" not in df.columns:
        df["arena_team"] = ""
    df["officials"] = df["officials"].fillna("")
    df["arena_team"] = df["arena_team"].fillna("")
    return df


def complete_game_ids(df: pd.DataFrame) -> set[str]:
    """Games stored with every column (older rows lack referee data)."""
    if df.empty:
        return set()
    ok = df[df["officials"].astype(str) != ""]
    return set(ok["game_id"].astype(str))


def save(df: pd.DataFrame) -> None:
    """Write only the dates whose rows changed, so the data branch stays small."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    df = df[COLUMNS].sort_values(["date", "game_id", "team", "person_id"])
    for day, g in df.groupby("date"):
        path = _day_path(str(day))
        if path.exists():
            try:
                old = pd.read_csv(path, nrows=1)
                if "officials" in old.columns and len(pd.read_csv(path, usecols=["game_id"])) == len(g) \
                        and not (g["officials"].astype(str) == "").any():
                    continue
            except (OSError, ValueError):
                pass
        g.to_csv(path, index=False, compression={"method": "gzip", "mtime": 0})


def _fetch(gid: str) -> list[dict]:
    try:
        return nba_cdn.boxscore_rows(gid)
    except Exception as e:  # noqa: BLE001
        log.debug("boxscore %s unavailable: %s", gid, e)
        return []


def _append(df: pd.DataFrame, rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return df
    new = pd.DataFrame(rows)
    new["dk_fp"] = dk_points(new)
    new = new[COLUMNS]
    if df.empty:
        return new
    return pd.concat([df, new], ignore_index=True).drop_duplicates(
        ["game_id", "person_id"], keep="last")


def update_current_season(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Download box scores for any finished games not yet stored."""
    sched = nba_cdn.schedule()
    have = complete_game_ids(df)
    todo = [g["game_id"] for g in sched if g["status"] == 3 and g["game_id"] not in have
            and g["start_utc"] and g["start_utc"] < now]
    if not todo:
        return df
    log.info("fetching %d new box scores", len(todo))
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_fetch, todo):
            rows.extend(r)
    return _append(df, rows)


def backfill_season(df: pd.DataFrame, start_year: int) -> pd.DataFrame:
    """One-off: pull a full prior regular season by enumerating game ids."""
    have = complete_game_ids(df)
    ids = [g for g in nba_cdn.game_ids_for_season(start_year) if g not in have]
    log.info("backfilling %d games for %d-%02d", len(ids), start_year, (start_year + 1) % 100)
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_fetch, ids):
            rows.extend(r)
    return _append(df, rows)
