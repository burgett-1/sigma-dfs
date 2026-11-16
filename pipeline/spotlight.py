"""Tonight's spotlight: connections from the historical data to the slate being played.

Card types (strongest first):
  * same situation before: how a player produced in past games without the
    teammates who are out tonight (official box scores), incl. how often he hit
    6x tonight's salary
  * winning profile: players whose pre-game profile (value rank on a slate of
    this size, teammate out) historically filled a large share of the winning
    lineups, but whose projected ownership is much lower
  * surprise starters: confirmed starters who rarely start, with their new minutes
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import patterns
from .config import NOT_PLAYING

Z_SHOW = 2.5     # show a "same situation" card only above this signal-to-noise level
Z_STRONG = 3.0


def with_without_cards(srows: list[dict], hist: pd.DataFrame, season: int) -> list[dict]:
    cards = []
    if hist.empty:
        return cards
    recent = hist[hist["season"] >= season - 1]
    teams = {r["team"] for r in srows}
    for team in teams:
        tr = [r for r in srows if r["team"] == team]
        outs = [r for r in tr if r.get("status") in NOT_PLAYING and r.get("fresh", 0) > 0
                and r.get("person_id") is not None and r.get("minutes", 0) >= 18]
        if not outs:
            continue
        th = recent[recent["team"] == team]
        if th.empty:
            continue
        team_games = set(th["game_id"])
        played = {o["person_id"]: set(th.loc[(th["person_id"] == o["person_id"]) & th["played"], "game_id"])
                  for o in outs}
        without_all = team_games - set().union(*played.values())
        with_all = set.intersection(*played.values()) if played else set()
        names = ", ".join(o["name"] for o in outs)
        for r in tr:
            if r.get("status") in NOT_PLAYING or r.get("person_id") is None or r.get("proj", 0) <= 0:
                continue
            mine = th[(th["person_id"] == r["person_id"]) & th["played"]]
            wo = mine[mine["game_id"].isin(without_all)]
            wi = mine[mine["game_id"].isin(with_all)]
            if len(wo) < 2 or len(wi) < 3:
                continue
            avg_wo, avg_wi = float(wo["dk_fp"].mean()), float(wi["dk_fp"].mean())
            lift = avg_wo / avg_wi - 1 if avg_wi > 0 else 0.0
            target = 6.0 * r["salary"] / 1000
            hits = int((wo["dk_fp"] >= target).sum())
            # Welch's t: is the difference bigger than game-to-game noise? Small
            # "without" samples are noisy, so only clear signals become cards.
            se = math.sqrt(wo["dk_fp"].var(ddof=1) / len(wo) + wi["dk_fp"].var(ddof=1) / len(wi))
            z = (avg_wo - avg_wi) / se if se > 0 else 0.0
            if z < Z_SHOW or lift < 0.08:
                continue
            strength = z
            cards.append({
                "type": "same_situation", "player": r["name"], "team": r["team_dk"], "salary": r["salary"],
                "proj": round(r["proj"], 1), "own": round(r.get("own", 0), 1),
                "headline": f"{r['name']} without {names}",
                "detail": (f"Averaged {avg_wo:.1f} DK pts in {len(wo)} games without them "
                           f"(vs {avg_wi:.1f} with). Hit 6x tonight's salary ({target:.0f}+ pts) "
                           f"in {hits} of {len(wo)}."),
                "evidence": {"games_without": len(wo), "avg_without": round(avg_wo, 1), "z": round(z, 2),
                             "avg_with": round(avg_wi, 1), "hits_6x": hits,
                             "last_without": str(wo["date"].max())},
                "strength": round(strength, 3),
                "strong": len(wo) >= 5 and z >= Z_STRONG,
            })
    return cards


def profile_cards(srows: list[dict], n_games: int, model: dict, doc: dict) -> list[dict]:
    cards = []
    bucket = doc.get("buckets", {}).get(patterns.size_bucket(n_games), {})
    n_slates = bucket.get("slates") or (model.get(patterns.size_bucket(n_games)) or {}).get("slates", 0)
    for r in srows:
        se = r.get("opt_exposure")
        if se is None or r.get("proj", 0) <= 0:
            continue
        gap = se - r.get("own", 0)
        if gap < 8:
            continue
        rb = patterns.rank_bucket(r["value_rank"]).replace("-", "–")
        why = f"ranked {rb} in projected value" + (" with a rotation teammate out" if r.get("teammate_out") else "")
        cards.append({
            "type": "winning_profile", "player": r["name"], "team": r["team_dk"], "salary": r["salary"],
            "proj": round(r["proj"], 1), "own": round(r.get("own", 0), 1),
            "headline": f"{r['name']} fits the winning profile, and the field is light on him",
            "detail": (f"On {patterns.size_bucket(n_games)} slates ({n_slates} in the history), players "
                       f"{why} appeared in {se:.0f}% of the winning lineups. His projected ownership: "
                       f"{r.get('own', 0):.0f}%."),
            "evidence": {"suggested_exposure": se, "own": round(r.get("own", 0), 1)},
            "strength": round(gap / 8.0, 3),
            "strong": gap >= 20,
        })
    return cards


def surprise_cards(srows: list[dict], doc: dict) -> list[dict]:
    cards = []
    pct = (doc.get("why_players_hit") or {}).get("surprise_start_pct")
    for r in srows:
        if r.get("unexpected") != "starting" or r.get("proj", 0) <= 0:
            continue
        cards.append({
            "type": "surprise_start", "player": r["name"], "team": r["team_dk"], "salary": r["salary"],
            "proj": round(r["proj"], 1), "own": round(r.get("own", 0), 1),
            "headline": f"{r['name']} is starting ({round(r.get('start_rate', 0) * 10)} starts in his last 10)",
            "detail": (f"Projected {r.get('proj_minutes', 0):.0f} minutes now."
                       + (f" Historically {pct}% of players in winning lineups were surprise starters." if pct else "")),
            "evidence": {"start_rate": r.get("start_rate")},
            "strength": 1.5,
            "strong": r["salary"] <= 5000,
        })
    return cards


def build(srows: list[dict], hist: pd.DataFrame, season: int, n_games: int) -> dict:
    doc = patterns._load()
    model = doc.get("exposure_model") or {}
    cards = (with_without_cards(srows, hist, season) + profile_cards(srows, n_games, model, doc)
             + surprise_cards(srows, doc))
    # one card per player: keep the strongest
    best: dict[str, dict] = {}
    for c in cards:
        if c["player"] not in best or c["strength"] > best[c["player"]]["strength"]:
            best[c["player"]] = c
    # only the very best profile gaps stand out; same-situation cards earn "strong" statistically
    prof = sorted((c for c in best.values() if c["type"] == "winning_profile"), key=lambda c: -c["strength"])
    for i, c in enumerate(prof):
        c["strong"] = c["strong"] and i < 3
        if i >= 8:                       # keep the page about the best connections
            best.pop(c["player"], None)
    ordered = sorted(best.values(), key=lambda c: (not c["strong"], -c["strength"]))
    return {"cards": ordered[:25], "size_bucket": patterns.size_bucket(n_games),
            "history_slates": (doc.get("buckets", {}).get(patterns.size_bucket(n_games)) or {}).get("slates", 0)}


def tag_exposure(srows: list[dict], n_games: int, model: dict) -> None:
    """Suggested exposure for every player on the slate (share of historical winning
    lineups that contained players with the same pre-game profile)."""
    live = [r for r in srows if r.get("proj", 0) > 0]
    order = sorted(live, key=lambda r: -(r["proj"] / max(r["salary"], 1)))
    team_out = {r["team"] for r in srows if r.get("status") in NOT_PLAYING and r.get("fresh", 0) > 0
                and r.get("minutes", 0) >= 18}
    for i, r in enumerate(order, 1):
        r["value_rank"] = i
        r["teammate_out"] = r["team"] in team_out
        r["opt_exposure"] = patterns.suggested_exposure(model, n_games, i, r["teammate_out"]) if model else None
    for r in srows:
        if r.get("proj", 0) <= 0:
            r["opt_exposure"] = 0.0 if model else None
