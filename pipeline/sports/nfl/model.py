"""NFL projection model (P3-2.4.3), using pre-game information only.

    team volume (pass / rush attempts: EWMA, scaled to the Vegas implied total,
    game script from the spread) x player shares (targets / carries / attempts:
    EWMA shrunk to a role prior, vacated shares of absent teammates
    redistributed) x efficiency (per-opportunity yards / TDs / catches shrunk to
    the position prior, TDs and yards scaled by the implied total, wind)
    -> stat components -> DK points with the yardage bonuses in expectation.

DST: points allowed ~ Normal(opponent implied total, 9.5) through the buckets,
sacks / INTs / fumble recoveries Poisson with a geometric blend of the
defence's rate and the opponent offence's giveaway rate. K: EWMA points scaled
by the implied total. Outcome curves: ratio quantiles (actual / projected mean)
by position x projection tercile from the backtest (nfl/state/dist.json),
lognormal by position until the first fit.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ...distribution import U_SAMPLE
from .. import get, scoring

HALF_LIFE = 4.0            # games
PRIOR_SEASON_WEIGHT = 0.5
LOOKBACK = 20              # games per player / team
SHARE_K = 3.0              # games of evidence for half weight on a player's own share
EFF_K = {"target": 40.0, "carry": 40.0, "attempt": 150.0}
VOLUME_ELASTICITY = 0.35
SPREAD_PASS, SPREAD_RUSH, SPREAD_CLIP = 0.012, 0.015, 0.10
TD_ELASTICITY, YDS_ELASTICITY = 1.0, 0.5
WIND_FREE, WIND_PER_MPH, WIND_FLOOR = 15.0, 0.01, 0.85
VACATED_SHARE = 0.85
DEFAULT_IMPLIED = 21.5
DST_PA_SD = 9.5
DST_TD_BASE, DST_TD_PER_TAKEAWAY = 0.06, 0.10
DST_SAFETY, DST_BLOCK = 0.03, 0.05
K_ELASTICITY = 0.5
SIGMA = {"pass_yds": (25.0, 0.22), "rush_yds": (12.0, 0.45), "rec_yds": (12.0, 0.50)}
DEFAULT_CV = {"QB": 0.45, "RB": 0.60, "WR": 0.70, "TE": 0.75, "DST": 0.90, "K": 0.55}
FALLBACK_AVG_MULT = 0.9
MIN_GAMES = 2
# role priors (share of team targets / carries / pass attempts) by depth rank
ROLE_PRIOR = {
    "target": {"WR": [0.22, 0.17, 0.11, 0.05], "TE": [0.15, 0.05, 0.02], "RB": [0.10, 0.05, 0.02], "QB": [0.0]},
    "carry": {"RB": [0.50, 0.25, 0.08], "QB": [0.10, 0.01], "WR": [0.01], "TE": [0.0]},
    "attempt": {"QB": [0.97, 0.03]},
}


def _key(season, week) -> int:
    return int(season) * 100 + int(week)


def _weights(seasons: np.ndarray, current: int) -> np.ndarray:
    """EWMA weights for games ordered oldest -> newest (newest weight 1)."""
    n = len(seasons)
    age = np.arange(n - 1, -1, -1, dtype=float)
    w = 0.5 ** (age / HALF_LIFE)
    return w * np.where(seasons < current, PRIOR_SEASON_WEIGHT, 1.0)


def _ewma(values: np.ndarray, seasons: np.ndarray, current: int) -> float:
    if not len(values):
        return float("nan")
    w = _weights(seasons, current)
    return float((w * values).sum() / w.sum())


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bonus_expectation(mu: float, stat: str, threshold: float, pts: float = 3.0) -> float:
    """pts x P(yds >= threshold) with yds ~ Normal(mu, a + b mu)."""
    if mu <= 0:
        return 0.0
    a, b = SIGMA[stat]
    sd = a + b * mu
    return pts * (1.0 - _phi((threshold - mu) / sd))


class Features:
    """Everything the model needs from history before (season, week)."""

    def __init__(self, hist: pd.DataFrame, season: int, week: int):
        cut = _key(season, week)
        h = hist[(hist["season"] * 100 + hist["week"]) < cut]
        h = h[h["season"] >= season - 1]
        self.season = season
        self.h = h.sort_values(["season", "week"])
        tg = self.h[self.h["position"] != "DST"].drop_duplicates(["game_id", "team"])
        self.team_games = {t: g for t, g in tg.groupby("team")}
        self.dst = {t: g for t, g in self.h[self.h["position"] == "DST"].groupby("team")}
        self.players = {pid: g for pid, g in self.h[self.h["position"] != "DST"].groupby("player_id")}
        played = self.h[self.h["position"] != "DST"]
        self.league_points = float(tg["team_points"].dropna().mean()) if tg["team_points"].notna().any() else 22.0
        self.pos_eff = self._position_efficiency(played)

    @staticmethod
    def _position_efficiency(h: pd.DataFrame) -> dict:
        out = {}
        for pos, g in h.groupby("position"):
            t, c, a = g["targets"].sum(), g["carries"].sum(), g["attempts"].sum()
            out[pos] = {
                "catch": g["rec"].sum() / t if t else 0.65, "ypt": g["rec_yds"].sum() / t if t else 7.5,
                "tdpt": g["rec_td"].sum() / t if t else 0.045, "ypc": g["rush_yds"].sum() / c if c else 4.2,
                "tdpc": g["rush_td"].sum() / c if c else 0.03, "ypa": g["pass_yds"].sum() / a if a else 6.8,
                "tdpa": g["pass_td"].sum() / a if a else 0.045, "intpa": g["int"].sum() / a if a else 0.024,
            }
        return out

    def team(self, team: str) -> dict:
        g = self.team_games.get(team)
        if g is None or g.empty:
            return {"pass": 34.0, "rush": 26.0, "tgt_ratio": 0.94, "points": self.league_points, "n": 0}
        g = g.tail(LOOKBACK)
        s = g["season"].to_numpy()
        pa = g["team_pass_att"].to_numpy(float)
        tt = g["team_targets"].to_numpy(float)
        ratio = np.divide(tt, pa, out=np.full_like(tt, 0.94), where=pa > 0)
        pts = g["team_points"].to_numpy(float)
        ok = ~np.isnan(pts)
        return {"pass": _ewma(pa, s, self.season), "rush": _ewma(g["team_rush_att"].to_numpy(float), s, self.season),
                "tgt_ratio": _ewma(ratio, s, self.season),
                "points": _ewma(pts[ok], s[ok], self.season) if ok.any() else self.league_points, "n": len(g)}

    def player(self, pid: str | None) -> dict | None:
        g = self.players.get(pid) if pid else None
        if g is None or g.empty:
            return None
        g = g.tail(LOOKBACK)
        s = g["season"].to_numpy()
        tt, tr, tp = (g[c].to_numpy(float) for c in ("team_targets", "team_rush_att", "team_pass_att"))

        def share(col, tot):
            v = g[col].to_numpy(float)
            return _ewma(np.divide(v, tot, out=np.zeros_like(v), where=tot > 0), s, self.season)
        wt = np.where(s < self.season, PRIOR_SEASON_WEIGHT, 1.0)

        def wsum(col):
            return float((g[col].to_numpy(float) * wt).sum())
        return {
            "n": len(g), "position": g["position"].iloc[-1], "team": g["team"].iloc[-1],
            "target_share": share("targets", tt), "carry_share": share("carries", tr),
            "attempt_share": share("attempts", tp),
            "targets": wsum("targets"), "carries": wsum("carries"), "attempts": wsum("attempts"),
            "rec": wsum("rec"), "rec_yds": wsum("rec_yds"), "rec_td": wsum("rec_td"), "rush_yds": wsum("rush_yds"),
            "rush_td": wsum("rush_td"), "pass_yds": wsum("pass_yds"), "pass_td": wsum("pass_td"), "int": wsum("int"),
            "fum_lost": _ewma(g["fum_lost"].to_numpy(float), s, self.season),
            "two_pt": _ewma(g["two_pt"].to_numpy(float), s, self.season),
            "ret_td": _ewma(g["ret_td"].to_numpy(float), s, self.season),
            "fum_rec_td": _ewma(g["fum_rec_td"].to_numpy(float), s, self.season),
            "dk_fp": _ewma(g["dk_fp"].to_numpy(float), s, self.season),
            "dk_mean": float(g["dk_fp"].mean()),
        }

    def defense(self, team: str) -> dict:
        g = self.dst.get(team)
        if g is None or g.empty:
            return {"sack": 2.4, "int": 0.8, "fum_rec": 0.6, "n": 0, "dk_fp": 6.0}
        g = g.tail(LOOKBACK)
        s = g["season"].to_numpy()
        return {"sack": _ewma(g["sack"].to_numpy(float), s, self.season),
                "int": _ewma(g["int"].to_numpy(float), s, self.season),
                "fum_rec": _ewma(g["fum_rec"].to_numpy(float), s, self.season), "n": len(g),
                "dk_fp": _ewma(g["dk_fp"].to_numpy(float), s, self.season)}

    def offense_giveaways(self, team: str) -> dict:
        """What `team`'s offence gives a defence per game (sacks taken, INTs, fumbles lost)."""
        g = self.dst.get(team)
        if g is None or g.empty:
            return {"sack": 2.4, "int": 0.8, "fum_rec": 0.6}
        g = g.tail(LOOKBACK)
        s = g["season"].to_numpy()
        ints = self.h[(self.h["team"] == team) & (self.h["position"] == "QB")].groupby("game_id")["int"].sum()
        int_rate = float(ints.tail(LOOKBACK).mean()) if len(ints) else 0.8
        return {"sack": _ewma(g["sacks_taken"].to_numpy(float), s, self.season),
                "int": int_rate, "fum_rec": _ewma(g["fum_lost_team"].to_numpy(float), s, self.season)}


def _shrink(own: float, n: float, prior: float, k: float) -> float:
    if own is None or (isinstance(own, float) and math.isnan(own)):
        return prior
    return (n * own + k * prior) / (n + k)


def _prior(kind: str, pos: str, rank: int) -> float:
    lst = ROLE_PRIOR.get(kind, {}).get(pos, [0.0])
    return lst[min(rank, len(lst) - 1)] if rank < len(lst) else lst[-1] * 0.5


def _env(ctx: dict | None, team_pts: float, league: float) -> dict:
    c = ctx or {}
    implied = c.get("implied")
    src = "vegas"
    if implied is None or (isinstance(implied, float) and math.isnan(implied)):
        implied, src = DEFAULT_IMPLIED, "default"
    spread = c.get("spread")
    spread = 0.0 if spread is None or (isinstance(spread, float) and math.isnan(spread)) else float(spread)
    wind = c.get("wind")
    indoor = bool(c.get("indoor")) or str(c.get("roof") or "").lower() in ("dome", "closed")
    wind_mult = 1.0
    if wind is not None and not (isinstance(wind, float) and math.isnan(wind)) and not indoor and wind >= WIND_FREE:
        wind_mult = max(WIND_FLOOR, 1.0 - WIND_PER_MPH * (float(wind) - WIND_FREE))
    return {"implied": float(implied), "spread": spread, "wind": wind, "wind_mult": wind_mult, "source": src,
            "vol": (float(implied) / max(team_pts, 1.0)) ** VOLUME_ELASTICITY,
            "td": (float(implied) / league) ** TD_ELASTICITY, "yds": (float(implied) / league) ** YDS_ELASTICITY,
            "opp_implied": c.get("opp_implied")}


def project(players: list[dict], hist: pd.DataFrame, ctx: dict[str, dict], season: int, week: int,
            dist: dict | None = None) -> list[dict]:
    """Projection rows for slate players.

    players: dicts with dk_player_id, name, team, opp, position (base, e.g. "WR"),
      status ("" / QUESTIONABLE / DOUBTFUL / OUT), dk_avg, player_id (nflverse id
      from data.join_ids, or None).
    ctx: {team: {implied, spread, wind, indoor/roof, opp_implied}} (data.scoreboard).
    Returns one row per player with mean, proj (median), q, std_dev, ... and
    the components."""
    F = Features(hist, season, week)
    cfg = get("NFL")
    not_playing = set(cfg.get("not_playing") or ["OUT", "DOUBTFUL"])
    tables = {p: scoring.table_for("NFL", p) for p in ("QB", "DST", "K")}
    feats = {p["dk_player_id"]: F.player(p.get("player_id")) for p in players}
    # depth ranks within team x position by recent share, then shares (shrunk)
    shares: dict[str, dict] = {}
    for (team, pos), grp in _group(players).items():
        def strength(p):
            f = feats.get(p["dk_player_id"])
            if not f:
                return -1.0
            return {"QB": f["attempt_share"], "RB": f["carry_share"] + f["target_share"]}.get(pos, f["target_share"])
        ranked = sorted(grp, key=lambda p: -strength(p))
        for rank, p in enumerate(ranked):
            f = feats.get(p["dk_player_id"])
            n = f["n"] if f else 0
            shares[p["dk_player_id"]] = {
                "target": _shrink(f["target_share"] if f else None, n, _prior("target", pos, rank), SHARE_K),
                "carry": _shrink(f["carry_share"] if f else None, n, _prior("carry", pos, rank), SHARE_K),
                "attempt": _shrink(f["attempt_share"] if f else None, n, _prior("attempt", pos, rank), SHARE_K),
            }
    _redistribute(players, shares, not_playing)
    rows = []
    for p in players:
        pos = p["position"].split("/")[0]
        team, opp = p["team"], p.get("opp", "")
        tv = F.team(team)
        env = _env(ctx.get(team), tv["points"], F.league_points)
        f = feats.get(p["dk_player_id"])
        status = p.get("status") or ""
        out = {"has_history": bool(f and f["n"] >= MIN_GAMES) or (pos == "DST" and F.defense(team)["n"] >= MIN_GAMES),
               "env_source": env["source"], "implied_total": round(env["implied"], 2), "spread": env["spread"],
               "wind": env["wind"], "targets_share": None, "carries_share": None}
        comp: dict = {}
        if status in not_playing:
            mean = 0.0
        elif pos == "DST":
            mean, comp = _dst(team, opp, F, ctx, tables["DST"])
        elif pos == "K":
            mean = _kicker(f, tv, env) if out["has_history"] else _fallback(p)
        elif not out["has_history"]:
            mean = _fallback(p)
        else:
            sh = shares[p["dk_player_id"]]
            out["targets_share"], out["carries_share"] = round(sh["target"], 4), round(sh["carry"], 4)
            comp = _components(pos, f, sh, tv, env, F.pos_eff.get(pos, F.pos_eff.get("WR", {})))
            mean = scoring.score(comp, tables["QB"]) - _threshold_points(comp, tables["QB"])
            mean += sum(bonus_expectation(comp.get(st, 0.0), st, t["gte"], t["pts"])
                        for t in tables["QB"].get("thresholds") or [] for st in [t["stat"]])
        mean = max(0.0, float(mean)) if status not in not_playing else 0.0
        out.update(_curve(mean, pos, dist, p.get("salary") or 0))
        rows.append({**p, **out, "components": {k: round(float(v), 3) for k, v in comp.items()}})
    return rows


def _group(players):
    out: dict[tuple, list] = {}
    for p in players:
        out.setdefault((p["team"], p["position"].split("/")[0]), []).append(p)
    return out


def _redistribute(players: list[dict], shares: dict, not_playing: set) -> None:
    """Shares of absent players move to active teammates (85% of the vacated share,
    in proportion to their own): targets to WR/TE/RB, carries to RB, attempts to QB."""
    by_team: dict[str, list] = {}
    for p in players:
        by_team.setdefault(p["team"], []).append(p)
    rules = {"target": ("WR", "TE", "RB"), "carry": ("RB",), "attempt": ("QB",)}
    for team, grp in by_team.items():
        out = [p for p in grp if (p.get("status") or "") in not_playing]
        active = [p for p in grp if (p.get("status") or "") not in not_playing]
        for kind, recv_pos in rules.items():
            vac = sum(shares[p["dk_player_id"]][kind] for p in out if p["dk_player_id"] in shares
                      and p["position"].split("/")[0] in recv_pos)
            if vac <= 0:
                continue
            recv = [p for p in active if p["position"].split("/")[0] in recv_pos and p["dk_player_id"] in shares]
            tot = sum(shares[p["dk_player_id"]][kind] for p in recv)
            if tot <= 0:
                continue
            for p in recv:
                s = shares[p["dk_player_id"]]
                s[kind] += VACATED_SHARE * vac * s[kind] / tot
        for p in out:
            if p["dk_player_id"] in shares:
                shares[p["dk_player_id"]] = {k: 0.0 for k in shares[p["dk_player_id"]]}


def _components(pos: str, f: dict, sh: dict, tv: dict, env: dict, prior: dict) -> dict:
    sp = env["spread"]
    pass_mult = 1.0 + max(-SPREAD_CLIP, min(SPREAD_CLIP, SPREAD_PASS * sp))
    rush_mult = 1.0 + max(-SPREAD_CLIP, min(SPREAD_CLIP, -SPREAD_RUSH * sp))
    team_pass = tv["pass"] * env["vol"] * pass_mult
    team_rush = tv["rush"] * env["vol"] * rush_mult
    team_tgt = team_pass * tv["tgt_ratio"]

    def rate(num, den, prior_rate, k):
        return (f[num] + k * prior_rate) / (f[den] + k)
    wind = env["wind_mult"]
    c = {}
    targets = team_tgt * sh["target"]
    carries = team_rush * sh["carry"]
    c["rec"] = targets * rate("rec", "targets", prior.get("catch", 0.65), EFF_K["target"])
    c["rec_yds"] = targets * rate("rec_yds", "targets", prior.get("ypt", 7.5), EFF_K["target"]) * env["yds"] * wind
    c["rec_td"] = targets * rate("rec_td", "targets", prior.get("tdpt", 0.045), EFF_K["target"]) * env["td"] * wind
    c["rush_yds"] = carries * rate("rush_yds", "carries", prior.get("ypc", 4.2), EFF_K["carry"]) * env["yds"]
    c["rush_td"] = carries * rate("rush_td", "carries", prior.get("tdpc", 0.03), EFF_K["carry"]) * env["td"]
    if pos == "QB":
        att = team_pass * sh["attempt"]
        c["pass_yds"] = att * rate("pass_yds", "attempts", prior.get("ypa", 6.8), EFF_K["attempt"]) * env["yds"] * wind
        c["pass_td"] = att * rate("pass_td", "attempts", prior.get("tdpa", 0.045), EFF_K["attempt"]) * env["td"] * wind
        c["int"] = att * rate("int", "attempts", prior.get("intpa", 0.024), EFF_K["attempt"])
        c["attempts"] = att
    c["fum_lost"] = f["fum_lost"]
    c["two_pt"] = f["two_pt"]
    c["ret_td"] = f["ret_td"]
    c["fum_rec_td"] = f["fum_rec_td"]
    c["targets"], c["carries"] = targets, carries
    return c


def _threshold_points(comp: dict, table: dict) -> float:
    """The hard threshold bonuses score() adds for the mean stat line (replaced by
    their expectation)."""
    return sum(float(t["pts"]) for t in table.get("thresholds") or [] if comp.get(t["stat"], 0.0) >= t["gte"])


def dst_mean(opp_implied: float, rates: dict, table: dict) -> tuple[float, dict]:
    pa = scoring.expected_bucket_points(opp_implied, DST_PA_SD, table, "points_allowed")
    td = DST_TD_BASE + DST_TD_PER_TAKEAWAY * (rates["int"] + rates["fum_rec"])
    w = table["per_stat"]
    comp = {"sack": rates["sack"], "int": rates["int"], "fum_rec": rates["fum_rec"], "def_st_td": td,
            "safety": DST_SAFETY, "blocked_kick": DST_BLOCK, "pa_points": pa, "opp_implied": opp_implied}
    mean = (w["sack"] * rates["sack"] + w["int"] * rates["int"] + w["fum_rec"] * rates["fum_rec"]
            + w["def_st_td"] * td + w["safety"] * DST_SAFETY + w["blocked_kick"] * DST_BLOCK + pa)
    return mean, comp


def _dst(team: str, opp: str, F: Features, ctx: dict, table: dict) -> tuple[float, dict]:
    d, o = F.defense(team), F.offense_giveaways(opp)
    rates = {k: math.sqrt(max(d[k], 0.0) * max(o[k], 0.0)) for k in ("sack", "int", "fum_rec")}
    oc = ctx.get(opp) or {}
    opp_implied = oc.get("implied")
    if opp_implied is None or (isinstance(opp_implied, float) and math.isnan(opp_implied)):
        opp_implied = DEFAULT_IMPLIED
    return dst_mean(float(opp_implied), rates, table)


def _kicker(f: dict, tv: dict, env: dict) -> float:
    return f["dk_fp"] * (env["implied"] / max(tv["points"], 1.0)) ** K_ELASTICITY


def _fallback(p: dict) -> float:
    avg = p.get("dk_avg")
    return FALLBACK_AVG_MULT * float(avg) if avg else 0.0


# ── outcome curves ──────────────────────────────────────────────────────────
_ZS = None


def _z_grid():
    global _ZS
    if _ZS is None:
        from statistics import NormalDist
        nd = NormalDist()
        _ZS = np.array([nd.inv_cdf(min(max(u, 1e-4), 1 - 1e-4)) for u in U_SAMPLE])
    return _ZS


def default_ratios(pos: str) -> list[float]:
    """Mean-1 lognormal ratio quantiles at U_SAMPLE for the position's CV."""
    cv = DEFAULT_CV.get(pos, 0.7)
    s2 = math.log(1.0 + cv * cv)
    r = np.exp(-s2 / 2.0 + math.sqrt(s2) * _z_grid())
    r[0] = 0.0
    return [float(x) for x in r]


def ratios_for(pos: str, mean: float, dist: dict | None) -> list[float]:
    d = (dist or {}).get("curves") or {}
    cuts = ((dist or {}).get("terciles") or {}).get(pos)
    if cuts and d:
        t = 0 if mean < cuts[0] else 1 if mean < cuts[1] else 2
        r = d.get(f"{pos}|{t}")
        if r and len(r) == len(U_SAMPLE):
            return r
    return default_ratios(pos)


def curve_stats(q: np.ndarray, salary: int, boom_mult: float, bust_mult: float) -> dict:
    u = np.linspace(0.0005, 0.9995, 2000)
    x = np.interp(u, U_SAMPLE, q)
    k = salary / 1000.0 if salary else 0.0
    return {"mean": float(x.mean()), "std_dev": float(x.std()),
            "boom": float((x >= boom_mult * k).mean()) if k else 0.0,
            "bust": float((x < bust_mult * k).mean()) if k else 0.0}


def _curve(mean: float, pos: str, dist: dict | None, salary: int) -> dict:
    proj_cfg = get("NFL").get("projection") or {}
    if mean <= 0:
        return {"mean": 0.0, "proj": 0.0, "q": [0.0] * len(U_SAMPLE), "std_dev": 0.0, "ceiling": 0.0, "floor": 0.0,
                "boom": 0.0, "bust": 1.0 if salary else 0.0, "value": 0.0}
    q = np.array(ratios_for(pos, mean, dist)) * mean
    q = np.maximum.accumulate(q)
    st = curve_stats(q, salary, proj_cfg.get("boom_multiple", 4.0), proj_cfg.get("bust_multiple", 2.0))
    med = float(np.interp(0.5, U_SAMPLE, q))
    return {"mean": round(mean, 2), "proj": round(med, 2), "q": [round(float(v), 2) for v in q],
            "std_dev": round(st["std_dev"], 2), "ceiling": round(float(np.interp(0.85, U_SAMPLE, q)), 2),
            "floor": round(float(np.interp(0.15, U_SAMPLE, q)), 2), "boom": round(st["boom"], 4),
            "bust": round(st["bust"], 4), "value": round(med / (salary / 1000.0), 3) if salary else 0.0}
