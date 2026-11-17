"""Betting markets: sharp lines and props for today's games, how they moved, how
the model compares, and each team's history against the closing lines.

Feeds the projections (team implied totals, player props) and writes
markets.json for the site's Betting Markets tab.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from statistics import NormalDist

import pandas as pd

from . import health
from .config import DATA_DIR
from .names import norm_name, team_code
from .sources import odds_api

log = logging.getLogger(__name__)
_N = NormalDist()


def _minutes_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def refresh(state: dict, now: datetime, teams: set[str], due) -> dict:
    """Update cached lines/props when their timers are due. Returns the cache:
    {"games": [...], "props": {norm name: {...}}, "props_team": {norm name: team}}."""
    key = os.environ.get("ODDS_API_KEY", "").strip()
    cache = state.setdefault("markets", {})
    if cache.get("day") != now.date().isoformat():
        cache.clear()
        cache["day"] = now.date().isoformat()
    if not key:
        return cache
    if due(state, "odds_lines_at", now, _minutes_env("ODDS_LINES_MINUTES", 2)):
        try:
            games = [g for g in odds_api.game_lines(key) if g["home"] in teams or g["away"] in teams]
            cache["games"] = games
            state["odds_lines_at"] = now.isoformat()
            health.ok("Sharp lines (The Odds API)", f"{len(games)} games; credits left {odds_api.last_usage.get('remaining')}")
        except Exception as e:  # noqa: BLE001
            health.fail("Sharp lines (The Odds API)", e)
    if cache.get("games") and due(state, "odds_props_at", now, _minutes_env("ODDS_PROPS_MINUTES", 10)):
        props, n = {}, 0
        for g in cache["games"]:
            try:
                ev = odds_api.player_props(key, g["event_id"])
                for nk, v in odds_api.parse_props(ev).items():
                    props[nk] = v
                n += 1
            except Exception as e:  # noqa: BLE001
                log.info("props for %s unavailable: %s", g.get("event_id"), e)
        if n:
            cache["props"] = props
            state["odds_props_at"] = now.isoformat()
            health.ok("Sharp player props (The Odds API)",
                      f"{len(props)} players in {n} games; credits left {odds_api.last_usage.get('remaining')}")
        else:
            health.fail("Sharp player props (The Odds API)", "no props returned")
    return cache


def snapshot(cache: dict, now: datetime) -> None:
    """Append the sharp consensus to today's movement log when it changed."""
    if not cache.get("games"):
        return
    path = DATA_DIR / "odds" / f"{now.date().isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"games": {f"{g['away']}@{g['home']}": [_r(g.get("home_margin")), _r(g.get("total")), _r(g.get("home_win"), 3)]
                     for g in cache["games"]},
           "props": {nk: {s: [v["line"], v["over_fair"]] for s, v in p.items() if s != "name"}
                     for nk, p in (cache.get("props") or {}).items()}}
    last = cache.get("_last_snapshot")
    if last == rec:
        return
    cache["_last_snapshot"] = rec
    with path.open("a") as f:
        f.write(json.dumps({"t": now.isoformat(), **rec}) + "\n")


def _r(x, d=1):
    return None if x is None else round(float(x), d)


def movement(now: datetime) -> list[dict]:
    path = DATA_DIR / "odds" / f"{now.date().isoformat()}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def team_history(logs: pd.DataFrame, season: int) -> dict[str, dict]:
    """Each team's record against the closing spread and total (ESPN box scores)."""
    if logs.empty or "spread" not in logs.columns:
        return {}
    g = logs.drop_duplicates(["game_id", "team"])[["game_id", "date", "team", "opp", "home", "team_score", "opp_score",
                                                    "spread", "total"]].copy()
    g["spread"] = pd.to_numeric(g["spread"], errors="coerce")
    g["total"] = pd.to_numeric(g["total"], errors="coerce")
    g = g[(g["team_score"] > 0) & g["spread"].notna()]
    g["season"] = pd.to_datetime(g["date"]).map(lambda d: d.year if d.month >= 8 else d.year - 1)
    g["margin"] = g["team_score"] - g["opp_score"]
    g["ats"] = (g["margin"] + g["spread"]).map(lambda x: "W" if x > 0 else "L" if x < 0 else "P")
    pts = g["team_score"] + g["opp_score"]
    g["ou"] = [("O" if p > t else "U" if p < t else "P") if not math.isnan(t) else None for p, t in zip(pts, g["total"])]
    out = {}
    for team, tg in g.groupby("team"):
        tg = tg.sort_values("date")
        cur = tg[tg["season"] == season]

        def rec(df, col, a, b):
            return f"{(df[col] == a).sum()}-{(df[col] == b).sum()}" + (f"-{(df[col] == 'P').sum()}" if (df[col] == "P").any() else "")
        out[team] = {
            "games": int(len(cur)),
            "ats": rec(cur, "ats", "W", "L"), "ou": rec(cur, "ou", "O", "U"),
            "ats_last10": rec(cur.tail(10), "ats", "W", "L"), "ou_last10": rec(cur.tail(10), "ou", "O", "U"),
            "ats_home": rec(cur[cur["home"].astype(bool)], "ats", "W", "L"),
            "ats_away": rec(cur[~cur["home"].astype(bool)], "ats", "W", "L"),
            "avg_margin_vs_spread": round(float((cur["margin"] + cur["spread"]).mean()), 2) if len(cur) else None,
            "last_season_ats": rec(tg[tg["season"] == season - 1], "ats", "W", "L"),
            "last_season_ou": rec(tg[tg["season"] == season - 1], "ou", "O", "U"),
            "recent": [{"date": str(r.date), "opp": r.opp, "home": bool(r.home), "score": f"{int(r.team_score)}-{int(r.opp_score)}",
                        "spread": float(r.spread), "total": None if math.isnan(r.total) else float(r.total), "ats": r.ats, "ou": r.ou}
                       for r in tg.tail(10).itertuples()],
        }
    return out


def prop_rows(rows: list[dict], props: dict) -> list[dict]:
    """Every priced prop next to the model's own number (before any market blend)."""
    from .model import STAT_KEYS
    out = []
    for r in rows:
        p = props.get(norm_name(r["name"]))
        if not p or r.get("proj_model") is None:
            continue
        rates = r.get("stat_rates") or {}
        m = r.get("proj_minutes") or 0
        from .config import DK_SCORING
        raw = sum(DK_SCORING[s] * rates.get(s, 0) * m for s in STAT_KEYS)
        scale = r["proj_model"] / raw if raw > 0 else 1.0
        for s in STAT_KEYS:
            mk = p.get(s)
            if not mk:
                continue
            model_mean = rates.get(s, 0) * m * scale
            sd = odds_api.STAT_SPREAD[s] * math.sqrt(max(mk["line"], 0.5))
            p_over = 1 - _N.cdf((mk["line"] - model_mean) / sd) if sd > 0 else 0.5
            out.append({"player": r["name"], "team": r.get("team_dk"), "stat": s, "line": mk["line"],
                        "book": mk["book"], "books": mk["books"], "market_mean": mk["mean"],
                        "over_fair": mk["over_fair"], "model_mean": round(model_mean, 2),
                        "model_over": round(p_over, 4), "edge_over": round(p_over - mk["over_fair"], 4),
                        "best_over": mk["best_over"], "best_under": mk["best_under"],
                        "status": r.get("status", ""), "starter": r.get("starter_status", "")})
    return sorted(out, key=lambda x: -abs(x["edge_over"]))


def write(cache: dict, rows: list[dict], logs: pd.DataFrame, season: int, now: datetime, espn_odds: dict) -> None:
    games = []
    hist = movement(now)
    for g in cache.get("games") or []:
        k = f"{g['away']}@{g['home']}"
        moves = [{"t": h["t"], "home_margin": h["games"][k][0], "total": h["games"][k][1], "home_win": h["games"][k][2]}
                 for h in hist if k in h.get("games", {})]
        games.append({**{x: g.get(x) for x in ("home", "away", "commence", "home_margin", "total", "home_win",
                                                  "home_implied", "away_implied", "sharpest")},
                      "books": g.get("books"), "open": moves[0] if moves else None, "moves": moves[-60:]})
    if not games and espn_odds:       # no odds key: ESPN's line, no props
        seen = set()
        for t, o in espn_odds.items():
            pair = tuple(sorted((t, o.get("opp", ""))))
            if pair in seen:
                continue
            seen.add(pair)
            games.append({"home": None, "away": None, "teams": list(pair), "espn": {t: o, o.get("opp"): espn_odds.get(o.get("opp"))}})
    teams = {team_code(r["team"]) for r in rows}
    th = team_history(logs, season)
    doc = {"updated_at": now.isoformat(), "source": "The Odds API (sharp books first)" if cache.get("games") else "ESPN (no odds key set)",
           "credits_left": odds_api.last_usage.get("remaining"),
           "games": games, "props": prop_rows(rows, cache.get("props") or {}),
           "teams": {t: th[t] for t in teams if t in th}}
    path = DATA_DIR / "markets.json"
    path.write_text(json.dumps(doc, default=str))
