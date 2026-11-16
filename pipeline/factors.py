"""Situational factors, measured from the historical box scores rather than assumed.

Each night's game context (rest, back-to-back, travel distance, time-zone
shift, altitude, home court, referee crew) is turned into features. A ridge
regression on every past player-game ("how far did the player land above or
below his pre-game baseline?") estimates how much each factor matters. The fit
is checked out-of-sample on the most recent 20% of games; if it does not lower
the projection error, it is not used.

Also here:
  - referee crew tendencies (total points in games they work, shrunk)
  - teammate with/without splits (how a player produces when a teammate sits)
  - the correlation structure used by the contest simulator
"""
from __future__ import annotations

import logging
import math
from datetime import date

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# arena latitude, longitude, UTC offset (standard time), altitude in metres
ARENAS = {
    "ATL": (33.757, -84.396, -5, 320), "BOS": (42.366, -71.062, -5, 5), "BKN": (40.683, -73.975, -5, 10),
    "CHA": (35.225, -80.839, -5, 230), "CHI": (41.881, -87.674, -6, 180), "CLE": (41.496, -81.688, -5, 200),
    "DAL": (32.790, -96.810, -6, 130), "DEN": (39.749, -105.008, -7, 1609), "DET": (42.341, -83.055, -5, 180),
    "GSW": (37.768, -122.388, -8, 5), "HOU": (29.751, -95.362, -6, 15), "IND": (39.764, -86.156, -5, 220),
    "LAC": (33.945, -118.341, -8, 30), "LAL": (34.043, -118.267, -8, 90), "MEM": (35.138, -90.051, -6, 80),
    "MIA": (25.781, -80.188, -5, 2), "MIL": (43.045, -87.917, -6, 190), "MIN": (44.979, -93.276, -6, 260),
    "NOP": (29.949, -90.082, -6, 2), "NYK": (40.751, -73.993, -5, 10), "OKC": (35.463, -97.515, -6, 370),
    "ORL": (28.539, -81.384, -5, 30), "PHI": (39.901, -75.172, -5, 10), "PHX": (33.446, -112.071, -7, 330),
    "POR": (45.532, -122.667, -8, 15), "SAC": (38.580, -121.500, -8, 8), "SAS": (29.427, -98.437, -6, 200),
    "TOR": (43.643, -79.379, -5, 80), "UTA": (40.768, -111.901, -7, 1288), "WAS": (38.898, -77.021, -5, 10),
}
FEATURES = ["b2b", "rest3", "travel", "tz_east", "tz_west", "altitude", "home", "ref"]
EFFECT_CAP = 0.08   # no single situational factor may move a projection more than 8%


def haversine_km(a: str, b: str) -> float:
    if a not in ARENAS or b not in ARENAS:
        return 0.0
    la1, lo1 = map(math.radians, ARENAS[a][:2])
    la2, lo2 = map(math.radians, ARENAS[b][:2])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def context_features(team: str, arena: str, prev_date, prev_arena: str | None, game_date,
                     ref_factor: float = 1.0) -> dict:
    rest = (game_date - prev_date).days if prev_date else 4
    tz = 0
    if prev_arena in ARENAS and arena in ARENAS:
        tz = ARENAS[arena][2] - ARENAS[prev_arena][2]
    return {
        "b2b": 1.0 if rest == 1 else 0.0,
        "rest3": 1.0 if rest >= 3 else 0.0,
        "travel": haversine_km(prev_arena, arena) / 1000.0 if prev_arena else 0.0,
        "tz_east": float(max(tz, 0)),
        "tz_west": float(max(-tz, 0)),
        "altitude": 1.0 if arena in ("DEN", "UTA") and team != arena else 0.0,
        "home": 1.0 if team == arena else 0.0,
        "ref": math.log(max(ref_factor, 0.5)),
        "rest_days": rest,
    }


# ── referees ─────────────────────────────────────────────────────────────────
def official_factors(hist: pd.DataFrame, shrink: float = 30.0) -> dict[str, tuple[float, int]]:
    """official -> (shrunk ratio of game total points to the league average, games)."""
    g = hist.drop_duplicates("game_id")[["game_id", "season", "team_score", "opp_score", "officials"]].copy()
    g = g[(g["team_score"] > 0) & g["officials"].astype(str).str.len().gt(1)]
    if g.empty:
        return {}
    g["total"] = g["team_score"] + g["opp_score"]
    g["ratio"] = g["total"] / g.groupby("season")["total"].transform("mean")
    rows = g.assign(official=g["officials"].astype(str).str.split("|")).explode("official")
    rows = rows[rows["official"].str.len() > 1]
    out = {}
    for name, grp in rows.groupby("official"):
        n = len(grp)
        out[name] = (1.0 + (grp["ratio"].mean() - 1.0) * n / (n + shrink), n)
    return out


def crew_factor(names, table: dict) -> float:
    vals = [table[n][0] for n in names if n in table]
    return float(np.mean(vals)) if vals else 1.0


# ── historical training set ──────────────────────────────────────────────────
def team_game_context(hist: pd.DataFrame, refs: dict) -> pd.DataFrame:
    """One row per (team, game) with the situational features."""
    tg = hist.drop_duplicates(["game_id", "team"])[["game_id", "d", "team", "opp", "home", "officials"]].copy()
    tg["arena"] = np.where(tg["home"].astype(bool), tg["team"], tg["opp"])
    tg = tg.sort_values(["team", "d"])
    tg["prev_d"] = tg.groupby("team")["d"].shift(1)
    tg["prev_arena"] = tg.groupby("team")["arena"].shift(1)
    feats = []
    for r in tg.itertuples(index=False):
        names = str(r.officials).split("|") if isinstance(r.officials, str) else []
        f = context_features(r.team, r.arena, r.prev_d if isinstance(r.prev_d, date) else None,
                             r.prev_arena if isinstance(r.prev_arena, str) else None, r.d,
                             crew_factor(names, refs))
        feats.append(f)
    tg = tg.drop(columns=["home"]).reset_index(drop=True)   # re-derived below as a feature
    return pd.concat([tg, pd.DataFrame(feats)], axis=1)


def baseline_predictions(hist: pd.DataFrame) -> pd.DataFrame:
    """Pre-game baseline for every historical player-game using only earlier games
    (exponentially weighted minutes x points per minute), i.e. no look-ahead."""
    df = hist[hist["counts"]].sort_values(["person_id", "d", "game_id"]).copy()
    g = df.groupby("person_id", sort=False)
    df["m_base"] = g["minutes"].transform(lambda s: s.ewm(halflife=4).mean().shift(1))
    played = df["minutes"].where(df["played"])
    fp = df["dk_fp"].where(df["played"])
    df["_pm"] = played
    df["_fp"] = fp
    g = df.groupby("person_id", sort=False)
    ew_m = g["_pm"].transform(lambda s: s.ewm(halflife=8, ignore_na=True).mean().shift(1))
    ew_f = g["_fp"].transform(lambda s: s.ewm(halflife=8, ignore_na=True).mean().shift(1))
    df["rate_base"] = ew_f / ew_m
    df["n_prior"] = g.cumcount()
    df["pred"] = df["m_base"] * df["rate_base"]
    return df.drop(columns=["_pm", "_fp"])


def fit_factors(hist: pd.DataFrame, lam: float = 50.0) -> dict:
    """Fit situational multipliers; returns coefficients plus an honest backtest."""
    out = {"coef": {f: 0.0 for f in FEATURES}, "used": False, "corr": None}
    if hist.empty or hist["game_id"].nunique() < 300:
        out["note"] = "not enough history yet"
        return out
    refs = official_factors(hist)
    ctx = team_game_context(hist, refs)
    base = baseline_predictions(hist)
    df = base.drop(columns=["home"]).merge(ctx[["game_id", "team"] + FEATURES], on=["game_id", "team"], how="inner")
    df = df[(df["n_prior"] >= 5) & df["played"] & (df["pred"] >= 5)].dropna(subset=["pred"] + FEATURES)
    if len(df) < 3000:
        out["note"] = "not enough player-games yet"
        return out
    y = np.log((df["dk_fp"].to_numpy() + 3) / (df["pred"].to_numpy() + 3))
    X = df[FEATURES].to_numpy(dtype=float)
    order = np.argsort(df["d"].to_numpy())
    cut = order[: int(len(order) * 0.8)]
    test = order[int(len(order) * 0.8):]

    def ridge(Xa, ya):
        # standardise so the penalty treats small-scale features (referee) fairly
        mu = Xa.mean(0)
        sd = Xa.std(0)
        sd[sd == 0] = 1.0
        Xc = (Xa - mu) / sd
        b = np.linalg.solve(Xc.T @ Xc + lam * np.eye(Xa.shape[1]), Xc.T @ (ya - ya.mean()))
        return b / sd, mu

    beta, mu = ridge(X[cut], y[cut])
    pred = df["pred"].to_numpy()
    actual = df["dk_fp"].to_numpy()
    mult = np.exp(np.clip((X[test] - mu) @ beta, -EFFECT_CAP, EFFECT_CAP))
    mae_base = float(np.mean(np.abs(actual[test] - pred[test])))
    mae_adj = float(np.mean(np.abs(actual[test] - pred[test] * mult)))
    out["backtest"] = {"test_games": int(len(test)), "mae_baseline": round(mae_base, 3),
                       "mae_with_factors": round(mae_adj, 3)}
    if mae_adj < mae_base:
        beta, mu = ridge(X, y)
        out["coef"] = {f: float(b) for f, b in zip(FEATURES, beta)}
        out["center"] = {f: float(m) for f, m in zip(FEATURES, mu)}
        out["used"] = True
    out["corr"] = residual_correlation(df.assign(resid=y))
    out["refs"] = {k: round(v[0], 4) for k, v in refs.items() if v[1] >= 10}
    return out


def residual_correlation(df: pd.DataFrame) -> dict:
    """Average correlation of players' over/under-performance with teammates and
    with opponents in the same game (feeds the contest simulator)."""
    d = df[df["pred"] >= 10].copy()
    if d.empty:
        return {}
    d["z"] = (d["resid"] - d["resid"].mean()) / d["resid"].std()
    tg = d.groupby(["game_id", "team"])["z"].agg(S="sum", Q=lambda s: (s * s).sum(), n="count").reset_index()
    pair_team = ((tg["S"] ** 2 - tg["Q"]).sum()) / max((tg["n"] * (tg["n"] - 1)).sum(), 1)
    m = tg.merge(tg, on="game_id", suffixes=("", "_o"))
    m = m[m["team"] < m["team_o"]]
    pair_opp = (m["S"] * m["S_o"]).sum() / max((m["n"] * m["n_o"]).sum(), 1)
    return {"team": round(float(pair_team), 4), "opp": round(float(pair_opp), 4),
            "team_games": int(len(tg))}


def situational_multiplier(features: dict, fit: dict) -> float:
    if not fit or not fit.get("used"):
        return 1.0
    x = sum((features.get(f, 0.0) - fit["center"].get(f, 0.0)) * fit["coef"].get(f, 0.0) for f in FEATURES)
    return float(math.exp(max(-EFFECT_CAP, min(EFFECT_CAP, x))))


# ── teammate with / without ──────────────────────────────────────────────────
def with_without(hist: pd.DataFrame, team: str, player_ids: dict[str, object], absent_ids: list,
                 season: int, shrink: float = 6.0) -> dict[str, tuple[float, float]]:
    """For each available player: (rate multiplier, games of evidence) when the
    absent teammates did not play, vs. games where they did."""
    if not absent_ids:
        return {}
    cur = hist[(hist["team"] == team) & (hist["season"] >= season - 1)]
    if cur.empty:
        return {}
    played_by = {pid: set(cur.loc[(cur["person_id"] == pid) & cur["played"], "game_id"]) for pid in absent_ids}
    team_games = set(cur["game_id"])
    out = {}
    for key, pid in player_ids.items():
        if pid is None:
            continue
        mine = cur[(cur["person_id"] == pid) & cur["played"] & (cur["minutes"] >= 8)]
        if len(mine) < 6:
            continue
        mult, ev = 1.0, 0.0
        for a in absent_ids:
            with_g = mine[mine["game_id"].isin(played_by[a])]
            wo_g = mine[mine["game_id"].isin(team_games - played_by[a])]
            if len(with_g) < 3 or len(wo_g) < 2:
                continue
            r_with = with_g["dk_fp"].sum() / with_g["minutes"].sum()
            r_wo = wo_g["dk_fp"].sum() / wo_g["minutes"].sum()
            w = len(wo_g) / (len(wo_g) + shrink)
            mult *= 1.0 + w * (r_wo / r_with - 1.0)
            ev += len(wo_g)
        if ev:
            out[key] = (min(max(mult, 0.85), 1.30), ev)
    return out


MINUTES_LIMIT_WORDS = ("reconditioning", "minutes restriction", "minute restriction",
                       "minutes limit", "ramp up", "ramping up")


def minutes_limited(reason: str) -> bool:
    r = (reason or "").lower()
    return any(w in r for w in MINUTES_LIMIT_WORDS)
