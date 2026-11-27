"""NFL data adapters: nflverse weekly stats and schedule (free CSV, CC-BY), the
ESPN scoreboard (Vegas lines, weather) and injury report.

Raw downloads are cached under SIGMA_CACHE_DIR/nfl (never published). The
derived player-week history (DK points plus the components the model uses) is
written to data/nfl/history/{season}.csv.gz. Every fetcher takes its HTTP
getter as a parameter so tests run on saved fixtures with no network.
"""
from __future__ import annotations

import io
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ... import health
from ...names import norm_name
from .. import scoring
from ..paths import paths
from . import teams as T

log = logging.getLogger(__name__)

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
PLAYER_WEEK_URL = NFLVERSE + "/stats_player/stats_player_week_{season}.csv"
TEAM_WEEK_URL = NFLVERSE + "/stats_team/stats_team_week_{season}.csv"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={ymd}"
ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
ATTRIBUTION = "nflverse (CC-BY 4.0), ESPN"

OFFENSE = ("QB", "RB", "WR", "TE")
HISTORY_COLUMNS = [
    "season", "week", "game_id", "team", "opp", "home", "player_id", "name", "position",
    "attempts", "completions", "pass_yds", "pass_td", "int", "carries", "rush_yds", "rush_td", "targets", "rec",
    "rec_yds", "rec_td", "fum_lost", "two_pt", "ret_td", "fum_rec_td",
    "sack", "fum_rec", "safety", "blocked_kick", "def_st_td", "xp_return", "points_allowed",
    "xp", "fg_0_39", "fg_40_49", "fg_50p",
    "sacks_taken", "fum_lost_team",
    "dk_fp", "team_pass_att", "team_rush_att", "team_targets", "team_points", "opp_points",
    "spread", "total", "roof", "wind",
]


# ── raw downloads ────────────────────────────────────────────────────────────
def _default_get(url: str) -> str:
    from ...http import get_text
    return get_text(url)


def csv_validator(*columns: str):
    """A check that a body is a complete CSV: its header has `columns`, it has >= 1
    data row, it ends with a newline, and every row has the header's field count
    (a download cut short mid-file fails the last two)."""
    import csv

    def ok(text: str) -> bool:
        body = (text or "").lstrip("\ufeff")
        if not body.endswith("\n"):
            return False
        rows = [r for r in csv.reader(io.StringIO(body)) if r and any(c.strip() for c in r)]
        if len(rows) < 2:
            return False
        head = [c.strip() for c in rows[0]]
        if not all(c in head for c in columns):
            return False
        return all(len(r) == len(head) for r in rows[1:])
    return ok


PLAYER_WEEK_OK = csv_validator("player_id", "season", "week")
TEAM_WEEK_OK = csv_validator("team", "season")
GAMES_OK = csv_validator("game_id")


def cached_text(url: str, name: str, max_age_s: float | None, get=None, validate=None) -> str:
    """Body of `url`, from SIGMA_CACHE_DIR/nfl/<name> when younger than max_age_s
    (None = forever). A failed download falls back to any cached copy.

    With `validate`, nothing invalid is ever cached or returned: an invalid
    cached file (e.g. written before validation existed) is discarded and
    refetched, and an invalid response falls back to a valid cached copy or
    raises (with a health note), so one bad 200 can't poison the cache."""
    cache = paths("NFL").cache / name
    cached = None
    if cache.exists():
        cached = cache.read_text(encoding="utf-8")
        if validate is not None and not validate(cached):
            log.warning("cached %s is not valid nflverse data; discarding it", name)
            try:
                cache.unlink()
            except OSError:
                pass
            cached = None
        elif max_age_s is None or time.time() - cache.stat().st_mtime < max_age_s:
            return cached
    try:
        text = (get or _default_get)(url)
    except Exception as e:  # noqa: BLE001
        if cached is not None:
            log.warning("%s unavailable (%s); using the cached copy", name, e)
            return cached
        raise
    if validate is not None and not validate(text):
        health.fail("nflverse", f"{name}: response is not valid nflverse data ({len(text or '')} bytes)")
        if cached is not None:
            return cached
        raise ValueError(f"{name}: invalid nflverse response")
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cache)
    return text


def read_csv_text(text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(text), low_memory=False)


def player_week(season: int, max_age_s: float | None, get=None) -> pd.DataFrame:
    return read_csv_text(cached_text(PLAYER_WEEK_URL.format(season=season), f"stats_player_week_{season}.csv",
                                     max_age_s, get, PLAYER_WEEK_OK))


def team_week(season: int, max_age_s: float | None, get=None) -> pd.DataFrame:
    return read_csv_text(cached_text(TEAM_WEEK_URL.format(season=season), f"stats_team_week_{season}.csv",
                                     max_age_s, get, TEAM_WEEK_OK))


def games(max_age_s: float | None, get=None) -> pd.DataFrame:
    return read_csv_text(cached_text(GAMES_URL, "games.csv", max_age_s, get, GAMES_OK))


# ── history (nflverse -> DK points) ─────────────────────────────────────────
def _n(df: pd.DataFrame, *cols) -> pd.Series:
    out = pd.Series(0.0, index=df.index)
    for c in cols:
        if c in df:
            out = out + pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return out


def game_lines(g: pd.DataFrame) -> pd.DataFrame:
    """games.csv -> one row per team-game: team (DK code), opp, home, points, opp
    points, spread from the team's view (negative = favourite), total, roof, wind."""
    g = g[g["game_type"].fillna("REG").isin(["REG", "WC", "DIV", "CON", "SB"])].copy()
    out = []
    for side, other, sign in (("home", "away", -1.0), ("away", "home", 1.0)):
        d = pd.DataFrame({
            "game_id": g["game_id"], "season": g["season"], "week": g["week"],
            "team": g[f"{side}_team"].map(lambda c: T.dk(c, "nflverse")),
            "opp": g[f"{other}_team"].map(lambda c: T.dk(c, "nflverse")),
            "home": side == "home",
            "team_points": pd.to_numeric(g[f"{side}_score"], errors="coerce"),
            "opp_points": pd.to_numeric(g[f"{other}_score"], errors="coerce"),
            # nflverse spread_line = expected home margin (positive = home favoured)
            "spread": sign * pd.to_numeric(g["spread_line"], errors="coerce"),
            "total": pd.to_numeric(g["total_line"], errors="coerce"),
            "roof": g.get("roof", pd.Series("", index=g.index)).fillna(""),
            "wind": pd.to_numeric(g.get("wind", pd.Series(np.nan, index=g.index)), errors="coerce"),
            "gameday": g.get("gameday", pd.Series("", index=g.index)),
        })
        out.append(d)
    return pd.concat(out, ignore_index=True)


def _fumbles_lost(p: pd.DataFrame) -> pd.Series:
    """Every fumble lost, including on returns and laterals: nflverse's
    fumbles_lost_total where present, else the rushing + receiving + sack sum."""
    parts = _n(p, "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost")
    if "fumbles_lost_total" not in p:
        return parts
    tot = pd.to_numeric(p["fumbles_lost_total"], errors="coerce")
    return tot.where(tot.notna(), parts)


def build_history(pw: pd.DataFrame, tw: pd.DataFrame, gm: pd.DataFrame) -> pd.DataFrame:
    """nflverse player-week + team-week + games -> the history frame
    (HISTORY_COLUMNS): QB/RB/WR/TE/K rows plus one DST row per team-game."""
    pw = pw[pw.get("season_type", "REG").isin(["REG", "POST"])] if "season_type" in pw else pw
    tw = tw[tw.get("season_type", "REG").isin(["REG", "POST"])] if "season_type" in tw else tw
    lines = game_lines(gm)
    pos = pw["position"].replace({"FB": "RB", "HB": "RB"})
    keep = pos.isin(OFFENSE + ("K",))
    p = pw[keep].copy()
    p["position"] = pos[keep]
    h = pd.DataFrame({
        "season": p["season"].astype(int), "week": p["week"].astype(int), "game_id": p["game_id"],
        "team": p["team"].map(lambda c: T.dk(c, "nflverse")), "player_id": p["player_id"].astype(str),
        "name": p.get("player_display_name", p.get("player_name")), "position": p["position"],
        "attempts": _n(p, "attempts"), "completions": _n(p, "completions"),
        "pass_yds": _n(p, "passing_yards"), "pass_td": _n(p, "passing_tds"), "int": _n(p, "passing_interceptions"),
        "carries": _n(p, "carries"), "rush_yds": _n(p, "rushing_yards"), "rush_td": _n(p, "rushing_tds"),
        "targets": _n(p, "targets"), "rec": _n(p, "receptions"), "rec_yds": _n(p, "receiving_yards"),
        "rec_td": _n(p, "receiving_tds"),
        "fum_lost": _fumbles_lost(p),
        "two_pt": _n(p, "passing_2pt_conversions", "rushing_2pt_conversions", "receiving_2pt_conversions"),
        "ret_td": _n(p, "special_teams_tds"), "fum_rec_td": _n(p, "fumble_recovery_tds"),
        "xp": _n(p, "pat_made"), "fg_0_39": _n(p, "fg_made_0_19", "fg_made_20_29", "fg_made_30_39"),
        "fg_40_49": _n(p, "fg_made_40_49"), "fg_50p": _n(p, "fg_made_50_59", "fg_made_60_"),
    })
    t = pd.DataFrame({
        "season": tw["season"].astype(int), "week": tw["week"].astype(int), "game_id": tw["game_id"],
        "team": tw["team"].map(lambda c: T.dk(c, "nflverse")),
        "team_pass_att": _n(tw, "attempts"), "team_rush_att": _n(tw, "carries"), "team_targets": _n(tw, "targets"),
        "sack": _n(tw, "def_sacks"), "int_def": _n(tw, "def_interceptions"), "fum_rec": _n(tw, "fumble_recovery_opp"),
        "safety": _n(tw, "def_safeties"), "blocked_kick": _n(tw, "def_punt_blocks", "def_fg_blocks", "def_pat_blocks"),
        "def_st_td": _n(tw, "def_tds", "special_teams_tds"),
        "sacks_taken": _n(tw, "sacks_suffered"), "int_thrown": _n(tw, "passing_interceptions"),
        "fum_lost_team": _n(tw, "fumbles_lost_total") if "fumbles_lost_total" in tw else
        _n(tw, "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"),
    })
    t = t.merge(lines.drop(columns=["season", "week"]), on=["game_id", "team"], how="left")
    team_ctx = t[["game_id", "team", "opp", "home", "team_pass_att", "team_rush_att", "team_targets", "team_points",
                  "opp_points", "spread", "total", "roof", "wind"]]
    h = h.merge(team_ctx, on=["game_id", "team"], how="left")
    # DST: one row per team-game
    d = pd.DataFrame({
        "season": t["season"], "week": t["week"], "game_id": t["game_id"], "team": t["team"], "opp": t["opp"],
        "home": t["home"], "player_id": "DST-" + t["team"], "name": t["team"].map(T.nickname), "position": "DST",
        "sack": t["sack"], "int": t["int_def"], "fum_rec": t["fum_rec"], "safety": t["safety"],
        "blocked_kick": t["blocked_kick"], "def_st_td": t["def_st_td"], "xp_return": 0.0,
        "points_allowed": t["opp_points"], "team_pass_att": t["team_pass_att"], "team_rush_att": t["team_rush_att"],
        "team_targets": t["team_targets"], "team_points": t["team_points"], "opp_points": t["opp_points"],
        "spread": t["spread"], "total": t["total"], "roof": t["roof"], "wind": t["wind"],
        "sacks_taken": t["sacks_taken"], "fum_lost_team": t["fum_lost_team"],
    })
    # the opponent's offensive giveaways (what this DST faces): kept on the DST row
    opp_off = t[["game_id", "team", "sacks_taken", "int_thrown", "fum_lost_team"]].rename(
        columns={"team": "opp", "sacks_taken": "opp_sacks_taken", "int_thrown": "opp_int_thrown",
                 "fum_lost_team": "opp_fum_lost"})
    d = d.merge(opp_off, on=["game_id", "opp"], how="left")
    hist = pd.concat([h, d], ignore_index=True)
    for c in HISTORY_COLUMNS:
        if c not in hist:
            hist[c] = 0.0 if c not in ("roof",) else ""
    num = [c for c in HISTORY_COLUMNS if c not in ("game_id", "team", "opp", "home", "player_id", "name", "position",
                                                     "roof")]
    for c in num:
        hist[c] = pd.to_numeric(hist[c], errors="coerce")
    stat_cols = [c for c in num if c not in ("team_points", "opp_points", "spread", "total", "wind", "points_allowed")]
    hist[stat_cols] = hist[stat_cols].fillna(0.0)
    hist["dk_fp"] = dk_points(hist)
    extra = [c for c in ("opp_sacks_taken", "opp_int_thrown", "opp_fum_lost") if c in hist]
    return hist[HISTORY_COLUMNS + extra].sort_values(["season", "week", "team", "position", "name"]).reset_index(drop=True)


def dk_points(df: pd.DataFrame) -> pd.Series:
    """DK points per row with the registry's NFL table for its position."""
    out = pd.Series(0.0, index=df.index)
    for pos, table in (("DST", scoring.table_for("NFL", "DST")), ("K", scoring.table_for("NFL", "K"))):
        sel = df["position"] == pos
        if sel.any():
            sub = df[sel]
            if pos == "DST":
                sub = sub[sub["points_allowed"].notna()]
            out.loc[sub.index] = scoring.score(sub, table)
    off = ~df["position"].isin(["DST", "K"])
    if off.any():
        out.loc[off] = scoring.score(df[off], scoring.table_for("NFL", "QB"))
    return out.round(2)


def history_path(season: int, root: Path | None = None) -> Path:
    return (root or paths("NFL").history) / f"{season}.csv.gz"


def save_history(df: pd.DataFrame, root: Path | None = None) -> list[Path]:
    out = []
    for season, part in df.groupby("season"):
        p = history_path(int(season), root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        part.to_csv(tmp, index=False, compression="gzip")
        tmp.replace(p)
        out.append(p)
    return out


def load_history(root: Path | None = None, seasons: list[int] | None = None) -> pd.DataFrame:
    root = root or paths("NFL").history
    files = sorted(root.glob("*.csv.gz")) if root.exists() else []
    if seasons is not None:
        files = [f for f in files if f.name.split(".")[0].isdigit() and int(f.name.split(".")[0]) in seasons]
    if not files:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    df = pd.concat([pd.read_csv(f, dtype={"player_id": str, "game_id": str}) for f in files], ignore_index=True)
    df["roof"] = df["roof"].fillna("")
    return df


def refresh_history(seasons: list[int], current: int, max_age_s: float, get=None) -> pd.DataFrame:
    with health.default_scope("NFL"):
        return _refresh_history(seasons, current, max_age_s, get)


def _refresh_history(seasons: list[int], current: int, max_age_s: float, get=None) -> pd.DataFrame:
    """Download (or reuse cached) nflverse files for `seasons`, rebuild and save the
    history. Past seasons are cached forever; the current one for max_age_s."""
    pw, tw = [], []
    for s in seasons:
        age = max_age_s if s == current else None
        try:
            p, t = player_week(s, age, get), team_week(s, age, get)
        except Exception as e:  # noqa: BLE001 - one season unavailable: build from the rest, retry next time
            health.fail("nflverse", f"season {s}: {e}")
            if s == current:
                raise
            continue
        pw.append(p)
        tw.append(t)
    gm = games(max_age_s, get)
    hist = build_history(pd.concat(pw, ignore_index=True), pd.concat(tw, ignore_index=True), gm)
    save_history(hist)
    if len(pw) == len(seasons):
        health.ok("nflverse", f"{len(hist)} player-weeks, seasons {min(seasons)}-{max(seasons)}")
    return hist


# ── ESPN: lines, weather, injuries ──────────────────────────────────────────
def scoreboard(dates: list, get_json=None) -> dict[str, dict]:
    """{team (DK code): {spread, total, implied, opp, start_utc, indoor, wind, temp,
    condition, game_id}} for every game on the given dates (ET dates)."""
    if get_json is None:
        from ...http import get_json
    out: dict[str, dict] = {}
    for d in dates:
        ymd = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d)
        data = get_json(ESPN_SCOREBOARD_URL.format(ymd=ymd))
        for ev in data.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
            if "home" not in sides or "away" not in sides:
                continue
            home = T.dk((sides["home"].get("team") or {}).get("abbreviation"), "espn")
            away = T.dk((sides["away"].get("team") or {}).get("abbreviation"), "espn")
            if not home or not away:
                continue
            odds = (comp.get("odds") or [None])[0] or {}
            spread = odds.get("spread")               # home perspective: negative = home favoured
            total = odds.get("overUnder")
            w = ev.get("weather") or comp.get("weather") or {}
            indoor = bool((comp.get("venue") or {}).get("indoor"))
            wind = _wind(w)
            start = ev.get("date")
            for team, opp, sign in ((home, away, 1.0), (away, home, -1.0)):
                row = {"opp": opp, "start_utc": start, "indoor": indoor, "wind": wind,
                       "temp": w.get("temperature"), "condition": w.get("displayValue"), "game_id": ev.get("id"),
                       "spread": None, "total": None, "implied": None}
                if spread is not None and total is not None:
                    s = sign * float(spread)
                    row.update(spread=s, total=float(total), implied=(float(total) - s) / 2.0)
                out[team] = row
    return out


def _wind(w: dict) -> float | None:
    for k in ("windSpeed", "wind"):
        v = w.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            num = "".join(ch for ch in v if ch.isdigit() or ch == ".")
            if num:
                try:
                    return float(num)
                except ValueError:
                    pass
    return None


ESPN_STATUS = {"OUT": "OUT", "INJURED RESERVE": "OUT", "DOUBTFUL": "DOUBTFUL", "QUESTIONABLE": "QUESTIONABLE",
               "PHYSICALLY UNABLE TO PERFORM": "OUT", "SUSPENSION": "OUT", "DAY-TO-DAY": "QUESTIONABLE",
               "PROBABLE": "PROBABLE"}


def injuries(get_json=None) -> list[dict]:
    """ESPN NFL injury report -> [{name, team (DK), status}] (statuses normalised)."""
    if get_json is None:
        from ...http import get_json
    data = get_json(ESPN_INJURIES_URL)
    out = []
    for team in data.get("injuries") or []:
        code = T.from_name(team.get("displayName", ""))
        for inj in team.get("injuries") or []:
            st = ESPN_STATUS.get(str(inj.get("status") or "").upper(), "")
            name = (inj.get("athlete") or {}).get("displayName")
            if name and st:
                out.append({"name": name, "team": code, "status": st})
    return out


# ── joining DK players to history ───────────────────────────────────────────
def load_aliases() -> dict[str, str]:
    try:
        raw = json.loads((Path(__file__).with_name("aliases.json")).read_text())
    except (OSError, ValueError):
        return {}
    return {norm_name(k): norm_name(v) for k, v in raw.items() if not k.startswith("_")}


def join_ids(players: list[dict], hist: pd.DataFrame) -> dict:
    """DK player -> nflverse player_id, by normalised name + team (then name alone
    when unique). DST joins by team. Returns {dk_player_id: player_id} and the rate."""
    aliases = load_aliases()
    if hist.empty:
        return {"ids": {}, "rate": 0.0}
    last = hist.sort_values(["season", "week"]).drop_duplicates("player_id", keep="last")
    last = last[last["position"] != "DST"]
    by_nt, by_n = {}, {}
    for _, r in last.iterrows():
        k = norm_name(str(r["name"]))
        by_nt[(k, r["team"])] = r["player_id"]
        by_n.setdefault(k, set()).add(r["player_id"])
    ids, hit, total = {}, 0, 0
    for p in players:
        total += 1
        if p.get("position") == "DST":
            ids[p["dk_player_id"]] = f"DST-{p['team']}"
            hit += 1
            continue
        k = norm_name(p["name"])
        k = aliases.get(k, k)
        pid = by_nt.get((k, p["team"]))
        if pid is None and len(by_n.get(k, ())) == 1:
            pid = next(iter(by_n[k]))
        if pid is not None:
            ids[p["dk_player_id"]] = pid
            hit += 1
    return {"ids": ids, "rate": hit / total if total else 0.0}


def week_label(gm: pd.DataFrame, now: datetime) -> str:
    """'2026 W13': the first scheduled week whose games are on or after today."""
    g = gm[gm["game_type"].fillna("REG") == "REG"]
    day = (now - timedelta(hours=10)).astimezone(timezone.utc).date().isoformat()
    upcoming = g[g["gameday"].astype(str) >= day]
    if upcoming.empty:
        upcoming = g.tail(1)
    r = upcoming.sort_values("gameday").iloc[0]
    return f"{int(r['season'])} W{int(r['week'])}"
