"""Incremental store of every player game log (box score row) used by the model."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pandas as pd

from .config import DK_DOUBLE_DOUBLE, DK_SCORING, DK_TRIPLE_DOUBLE, HISTORY_DIR
from .sources import espn_box, nba_cdn

log = logging.getLogger(__name__)

COLUMNS = [
    "game_id", "date", "team", "opp", "home", "person_id", "name", "position", "starter",
    "status", "not_playing_reason", "minutes", "pts", "reb", "ast", "stl", "blk", "tov",
    "fg3m", "fga", "fta", "team_score", "opp_score", "dk_fp", "officials", "arena_team", "start_utc", "spread", "total",
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
    # rows without a player name or team can't be used (occasional gaps in box scores)
    df = df[df["name"].apply(lambda x: isinstance(x, str) and x.strip() != "")
            & df["team"].apply(lambda x: isinstance(x, str) and x.strip() != "")].reset_index(drop=True)
    # columns added later: older days are re-downloaded once to fill them
    if "officials" not in df.columns:
        df["officials"] = ""
    if "arena_team" not in df.columns:
        df["arena_team"] = ""
    if "start_utc" not in df.columns:
        df["start_utc"] = ""
    for c in ("spread", "total"):          # closing betting lines (added later: refetched once)
        if c not in df.columns:
            df[c] = ""
    df["start_utc"] = df["start_utc"].fillna("")
    df["officials"] = df["officials"].fillna("")
    df["arena_team"] = df["arena_team"].fillna("")
    return df


def complete_game_ids(df: pd.DataFrame) -> set[str]:
    """Games stored with every column (older rows lack referee data)."""
    if df.empty:
        return set()
    ok = df[(df["officials"].astype(str) != "") & (df["start_utc"].astype(str) != "")
            & (df["spread"].astype(str) != "") & (df["spread"].astype(str) != "nan")]
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
                if "spread" in old.columns and len(pd.read_csv(path, usecols=["game_id"])) == len(g) \
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


DATES_FILE = "espn_dates.json"


def _dates_done() -> dict:
    try:
        return json.loads((HISTORY_DIR / DATES_FILE).read_text())
    except (OSError, ValueError):
        return {}


def _sync_dates(df: pd.DataFrame, days: list, now: datetime, recheck_days: int = 2) -> pd.DataFrame:
    """Fetch ESPN box scores for every finished game on these dates not yet stored."""
    done = _dates_done()
    today = espn_box.now_et_date(now)
    # days whose stored games are missing newer columns (e.g. closing lines) are redone once
    if not df.empty:
        complete = complete_game_ids(df)
        stale = set(df.loc[~df["game_id"].astype(str).isin(complete), "date"].astype(str))
        for d in stale:
            done.pop(d, None)
    todo_days = [d for d in days if d < today and (d.isoformat() not in done or (today - d).days <= recheck_days)]
    if not todo_days:
        return df
    have = complete_game_ids(df)
    events: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for d, games in zip(todo_days, ex.map(_games_on, todo_days)):
            if games is None:
                continue
            fin = [g for g in games if g["completed"]]
            events += [g for g in fin if g["game_id"] not in have]
            if len(fin) == len(games):
                done[d.isoformat()] = len(fin)
    if events:
        log.info("fetching %d box scores", len(events))
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(_fetch_espn, [g["event_id"] for g in events]):
                rows.extend(r)
        df = _append(df, rows)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    (HISTORY_DIR / DATES_FILE).write_text(json.dumps(done))
    return df


def _games_on(d):
    try:
        return espn_box.games_on(d)
    except Exception as e:  # noqa: BLE001
        log.debug("scoreboard %s unavailable: %s", d, e)
        return None


def _fetch_espn(event_id: str) -> list[dict]:
    try:
        return espn_box.parse_summary(espn_box.summary(event_id), event_id)
    except Exception as e:  # noqa: BLE001
        log.debug("box score %s unavailable: %s", event_id, e)
        return []


def update_current_season(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Add box scores for finished games this season (ESPN)."""
    today = espn_box.now_et_date(now)
    season = nba_cdn.season_start_year(today)
    return _sync_dates(df, espn_box.season_dates(season, today), now)


def backfill_season(df: pd.DataFrame, start_year: int) -> pd.DataFrame:
    """Pull a full past season (ESPN), date by date."""
    log.info("backfilling the %d-%02d season", start_year, (start_year + 1) % 100)
    return _sync_dates(df, espn_box.season_dates(start_year, date(start_year + 1, 6, 30)), datetime.now().astimezone())
