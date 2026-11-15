"""DraftKings NBA projection model built from historical box scores.

projection = minutes x fantasy-points-per-minute x usage bump x game environment
             x opponent-position factor x home edge

Minutes and rate come from exponentially weighted recent games, shrunk toward
last season / league priors. Team minutes are re-balanced to 241 after removing
OUT/DOUBTFUL players, so absences automatically flow to teammates.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import MODEL, NOT_PLAYING
from .names import norm_name

log = logging.getLogger(__name__)

_EXCLUDE_REASONS = ("INJURY", "ILLNESS", "PERSONAL", "SUSPEN", "HEALTH", "REST", "SICK")


def pos_group(pos: str) -> str:
    p = (pos or "").upper().split("/")[0]
    if p in ("PG", "SG", "G"):
        return "G"
    if p in ("SF", "PF", "F"):
        return "F"
    if p == "C":
        return "C"
    return ""


def season_of(d: date) -> int:
    return d.year if d.month >= 8 else d.year - 1


@dataclass
class Context:
    """Everything about today's slate the model needs besides history."""
    today: date
    statuses: dict[str, dict] = field(default_factory=dict)   # player_key -> {status, reason}
    odds: dict[str, dict] = field(default_factory=dict)       # team -> {spread, total, implied}
    positions: dict[str, str] = field(default_factory=dict)   # norm name -> DK position
    teams_played_yesterday: set[str] = field(default_factory=set)


# ─────────────────────────────────────────────────────────────────────────────
# history preparation
# ─────────────────────────────────────────────────────────────────────────────
def prepare_history(logs: pd.DataFrame, today: date) -> pd.DataFrame:
    if logs.empty:
        return logs.assign(d=pd.Series(dtype="object"), season=pd.Series(dtype=int),
                           counts=pd.Series(dtype=bool), played=pd.Series(dtype=bool),
                           nkey=pd.Series(dtype=str))
    df = logs.copy()
    df["d"] = pd.to_datetime(df["date"]).dt.date
    df = df[df["d"] < today]
    df["season"] = [season_of(x) for x in df["d"]]
    reason = df["not_playing_reason"].fillna("").astype(str).str.upper()
    excluded = (df["minutes"] <= 0) & reason.str.contains("|".join(_EXCLUDE_REASONS))
    df["counts"] = ~excluded            # game counts toward the minutes average
    df["played"] = df["minutes"] >= 1.0
    df["nkey"] = df["name"].map(norm_name)
    return df.sort_values(["d", "game_id"], ascending=False)


def _decay_weights(n: int, half_life: float) -> np.ndarray:
    return 0.5 ** (np.arange(n) / half_life)


def league_priors(hist: pd.DataFrame, positions: dict[str, str]) -> dict:
    """League-wide FP/min by position group, CV, and points per team game."""
    pri = {"rate": {"G": 0.95, "F": 0.95, "C": 1.05, "": 0.95}, "team_pts": 114.0}
    if hist.empty:
        return pri
    recent = hist[hist["season"] == hist["season"].max()]
    if len(recent) < 2000:
        recent = hist
    pts = recent.drop_duplicates(["game_id", "team"])["team_score"]
    pts = pts[pts > 0]
    if len(pts) >= 20:
        pri["team_pts"] = float(pts.tail(600).mean()) if len(pts) else 114.0
    rot = recent[recent["minutes"] >= 15].copy()
    rot["grp"] = [pos_group(positions.get(k, "")) or pos_group(p)
                  for k, p in zip(rot["nkey"], rot["position"])]
    for g in ("G", "F", "C"):
        sub = rot[rot["grp"] == g]
        if sub["minutes"].sum() > 3000:
            pri["rate"][g] = float(sub["dk_fp"].sum() / sub["minutes"].sum())
    return pri


def opponent_factors(hist: pd.DataFrame, positions: dict[str, str], season: int) -> tuple[dict, dict]:
    """(team -> overall DK FP allowed factor, (team, group) -> positional factor)."""
    cur = hist[(hist["season"] == season) & hist["played"]]
    if cur.empty:
        return {}, {}
    cur = cur.copy()
    cur["grp"] = [pos_group(positions.get(k, "")) or pos_group(p)
                  for k, p in zip(cur["nkey"], cur["position"])]
    per_game = cur.groupby(["opp", "game_id"])["dk_fp"].sum().reset_index()
    league = per_game["dk_fp"].mean()
    overall = {}
    for team, g in per_game.groupby("opp"):
        n = len(g)
        raw = g["dk_fp"].mean() / league if league else 1.0
        w = n / (n + MODEL["opp_shrink_games"])
        overall[team] = 1.0 + (raw - 1.0) * w
    pos = {}
    grp_game = cur[cur["grp"] != ""].groupby(["opp", "game_id", "grp"])["dk_fp"].sum().reset_index()
    league_grp = grp_game.groupby("grp")["dk_fp"].mean().to_dict()
    for (team, grp), g in grp_game.groupby(["opp", "grp"]):
        n = len(g)
        base = league_grp.get(grp) or 0
        if not base:
            continue
        raw = (g["dk_fp"].mean() / base) / overall.get(team, 1.0)
        w = n / (n + MODEL["dvp_shrink_games"])
        pos[(team, grp)] = 1.0 + (raw - 1.0) * w * MODEL["dvp_strength"]
    return overall, pos


# ─────────────────────────────────────────────────────────────────────────────
# per-player baseline
# ─────────────────────────────────────────────────────────────────────────────
def player_baseline(plogs: pd.DataFrame, team_games: list[str], season: int,
                    prior_rate: float, salary: int) -> dict:
    """Minutes, rate, volatility and role from one player's game logs (newest first)."""
    cur = plogs[(plogs["season"] == season) & plogs["counts"]]
    prev = plogs[(plogs["season"] == season - 1) & plogs["counts"]]

    # minutes
    mc = cur["minutes"].to_numpy()[: MODEL["minutes_lookback"]]
    mp = prev["minutes"].to_numpy()[:30]
    n_cur = len(mc)
    m_cur = float(np.average(mc, weights=_decay_weights(n_cur, MODEL["minutes_half_life"]))) if n_cur else None
    m_prev = float(np.average(mp, weights=_decay_weights(len(mp), 10.0))) if len(mp) else None
    k0 = MODEL["minutes_prior_games"]
    if m_cur is not None and m_prev is not None:
        minutes = (n_cur * m_cur + k0 * m_prev) / (n_cur + k0)
    elif m_cur is not None:
        minutes = m_cur
    elif m_prev is not None:
        minutes = m_prev
    else:
        minutes = None

    # fantasy points per minute (played games only)
    pc = cur[cur["played"]].head(MODEL["rate_lookback"])
    pp = prev[prev["played"]].head(60)
    wc = _decay_weights(len(pc), MODEL["rate_half_life"])
    mins_c = float((pc["minutes"].to_numpy() * wc).sum())
    rate_c = float((pc["dk_fp"].to_numpy() * wc).sum() / mins_c) if mins_c > 0 else None
    mins_p = float(pp["minutes"].sum())
    rate_p = float(pp["dk_fp"].sum() / mins_p) if mins_p > 0 else None
    prior = prior_rate
    prior_w = MODEL["rate_prior_minutes"]
    if rate_p is not None and mins_p >= 150:
        # last season is the prior, itself shrunk to the league
        prior = (mins_p * rate_p + prior_w * prior_rate) / (mins_p + prior_w)
    rate = (mins_c * rate_c + prior_w * prior) / (mins_c + prior_w) if rate_c is not None else prior

    # salary-implied minutes for low-sample players
    n_eff = n_cur + 0.5 * min(len(mp), 30)
    fp_sal = salary / 1000.0 * MODEL["rookie_fp_per_k"]
    m_sal = min(fp_sal / max(rate, 0.3), 34.0)
    k_sal = 2.0
    if minutes is None:
        minutes = m_sal
    else:
        minutes = (n_eff * minutes + k_sal * m_sal) / (n_eff + k_sal)

    # volatility (coefficient of variation of DK points in real-minute games)
    vol = pd.concat([pc, pp]).head(25)
    vol = vol[vol["minutes"] >= 10]
    cv_prior = MODEL["cv_prior"]
    if len(vol) >= 3 and vol["dk_fp"].mean() > 0:
        cv_emp = float(vol["dk_fp"].std(ddof=1) / vol["dk_fp"].mean())
        w = len(vol) / (len(vol) + MODEL["cv_shrink_games"])
        cv = w * cv_emp + (1 - w) * cv_prior
    else:
        cv = cv_prior
    cv = min(max(cv, MODEL["cv_min"]), MODEL["cv_max"])

    # role and availability
    last5 = plogs[plogs["season"] == season].head(5)
    start_rate = float(last5["starter"].astype(bool).mean()) if len(last5) else 0.0
    played_ids = set(plogs.loc[plogs["played"], "game_id"])
    recent_team = team_games[:5]
    fresh = (sum(1 for g in recent_team if g in played_ids) / len(recent_team)) if recent_team else 1.0
    streak = 0
    for g in team_games:
        if g in played_ids:
            break
        streak += 1
    return {
        "minutes": float(minutes), "rate": float(rate), "cv": float(cv),
        "start_rate": start_rate, "fresh": fresh, "absence_streak": streak,
        "games_cur": n_cur, "games_prev": len(mp), "has_history": len(plogs) > 0,
        "max_recent_min": float(mc.max()) if n_cur else float(minutes),
    }


# ─────────────────────────────────────────────────────────────────────────────
# team re-balancing
# ─────────────────────────────────────────────────────────────────────────────
def rebalance_minutes(minutes: dict[str, float], caps: dict[str, float],
                      affinity: dict[str, float], target: float) -> dict[str, float]:
    """Scale a team's minutes to `target`, moving most of the change to
    mid-rotation players (weight m*(1-m/48)) and, when filling vacated minutes,
    toward players who share the missing players' positions."""
    out = dict(minutes)
    for _ in range(6):
        total = sum(out.values())
        gap = target - total
        if abs(gap) < 0.05:
            break
        if gap > 0:
            w = {k: v * max(0.05, 1 - v / 48.0) * affinity.get(k, 1.0)
                 for k, v in out.items() if v < caps[k] - 1e-6 and v > 0}
        else:
            w = {k: v * max(0.05, 1 - v / 48.0) for k, v in out.items() if v > 0}
        s = sum(w.values())
        if s <= 0:
            break
        for k, wk in w.items():
            out[k] = min(caps[k], max(0.0, out[k] + gap * wk / s))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# main entry point
# ─────────────────────────────────────────────────────────────────────────────
def project(players: list[dict], logs: pd.DataFrame, ctx: Context) -> list[dict]:
    """players: unique DK draftables for today (see sources.draftkings)."""
    hist = prepare_history(logs, ctx.today)
    season = season_of(ctx.today)
    pri = league_priors(hist, ctx.positions)
    opp_all, opp_pos = opponent_factors(hist, ctx.positions, season)

    # index logs by player
    by_key: dict[str, pd.DataFrame] = {}
    latest_team: dict[str, str] = {}
    by_name_team: dict[tuple[str, str], object] = {}
    by_name: dict[str, list] = {}
    if not hist.empty:
        for pid, g in hist.groupby("person_id", sort=False):
            g = g.sort_values(["d", "game_id"], ascending=False)
            by_key[pid] = g
            nk = g["nkey"].iloc[0]
            t = g["team"].iloc[0]
            latest_team[pid] = t
            by_name_team[(nk, t)] = pid
            by_name.setdefault(nk, []).append((g["d"].iloc[0], pid))
    team_games: dict[str, list[str]] = {}
    if not hist.empty:
        cur = hist[hist["season"] == season]
        for t, g in cur.drop_duplicates(["team", "game_id"]).groupby("team"):
            team_games[t] = list(g.sort_values("d", ascending=False)["game_id"])

    rows = []
    for p in players:
        nk = norm_name(p["name"])
        pid = by_name_team.get((nk, p["team"]))
        if pid is None and nk in by_name:
            pid = max(by_name[nk])[1]  # traded player: most recent history
        plogs = by_key.get(pid, hist.iloc[0:0]) if not hist.empty else hist
        grp = pos_group(p["position"])
        base = player_baseline(plogs, team_games.get(p["team"], []), season,
                               pri["rate"].get(grp, 0.95), p["salary"])
        st = ctx.statuses.get(p["key"], {})
        rows.append({**p, **base, "grp": grp, "person_id": pid,
                     "status": st.get("status", ""), "reason": st.get("reason", ""),
                     "status_source": st.get("source", "")})

    # team-level adjustments
    for team in sorted({r["team"] for r in rows}):
        tr = [r for r in rows if r["team"] == team]
        out = [r for r in tr if r["status"] in NOT_PLAYING]
        avail = [r for r in tr if r["status"] not in NOT_PLAYING]
        for r in avail:
            if r["absence_streak"] >= MODEL["return_absence_games"] and r["has_history"]:
                r["minutes"] *= MODEL["return_minutes_mult"]
        # vacated minutes go preferentially to the same position group
        missing_grp: dict[str, float] = {}
        for r in out:
            missing_grp[r["grp"]] = missing_grp.get(r["grp"], 0.0) + r["minutes"] * r["fresh"]
        miss_total = sum(missing_grp.values()) or 1.0
        affinity = {r["key"]: 1.0 + 1.5 * missing_grp.get(r["grp"], 0.0) / miss_total for r in avail}
        mins = {r["key"]: r["minutes"] for r in avail}
        caps = {r["key"]: min(MODEL["max_player_minutes"], max(r["max_recent_min"] + 8.0, r["minutes"] + 6.0))
                for r in avail}
        target = MODEL["team_minutes"]
        if sum(caps.values()) < target:
            target = sum(caps.values())
        if avail:
            new_mins = rebalance_minutes(mins, caps, affinity, target)
        else:
            new_mins = {}

        # blowout risk: starters sit late, bench soaks up the minutes
        o = ctx.odds.get(team) or {}
        spread = o.get("spread")
        if spread is not None and new_mins:
            excess = max(0.0, abs(spread) - MODEL["blowout_free_points"])
            cut = 0.0
            for r in avail:
                if new_mins[r["key"]] >= 28:
                    d = new_mins[r["key"]] * MODEL["blowout_per_point"] * excess
                    new_mins[r["key"]] -= d
                    cut += d
            bench = [r for r in avail if 4 <= new_mins[r["key"]] < 24]
            bw = sum(new_mins[r["key"]] for r in bench)
            for r in bench:
                new_mins[r["key"]] += cut * new_mins[r["key"]] / bw if bw else 0
        if team in ctx.teams_played_yesterday:
            for r in avail:
                if new_mins.get(r["key"], 0) >= 30:
                    new_mins[r["key"]] *= MODEL["back_to_back_mult"]

        # usage bump from rotation players who are out (only recent absences count)
        vol_out = sum(r["minutes"] * r["rate"] * r["fresh"] for r in out)
        vol_all = vol_out + sum(r["minutes"] * r["rate"] for r in avail)
        u_out = vol_out / vol_all if vol_all > 0 else 0.0
        rates = [r["rate"] for r in avail if r["minutes"] >= 10] or [1.0]
        r_mean = float(np.mean(rates))

        # game environment
        implied = o.get("implied")
        if implied:
            env = 1.0 + MODEL["vegas_elasticity"] * (implied / pri["team_pts"] - 1.0)
            env_src = "vegas"
        else:
            env = opp_all.get(p_opp(tr), 1.0)
            env_src = "opponent"

        for r in tr:
            r["env"] = env
            r["env_source"] = env_src
            r["implied_total"] = implied
            r["spread"] = spread
            if r["status"] in NOT_PLAYING:
                r["proj_minutes"] = 0.0
                r["bump"] = 0.0
                r["dvp"] = 1.0
                r["proj"] = 0.0
                continue
            m = new_mins.get(r["key"], r["minutes"])
            bump = min(MODEL["usage_bump_cap"],
                       MODEL["usage_bump_beta"] * u_out * math.sqrt(max(r["rate"], 0.1) / r_mean))
            dvp = opp_pos.get((r["opp"], r["grp"]), 1.0)
            home = 1.0 + (MODEL["home_edge"] if r["home"] else -MODEL["home_edge"])
            r["proj_minutes"] = m
            r["bump"] = bump
            r["dvp"] = dvp
            r["proj"] = m * r["rate"] * (1 + bump) * env * dvp * home

    for r in rows:
        finish(r)
    return rows


def p_opp(team_rows: list[dict]) -> str:
    return team_rows[0]["opp"] if team_rows else ""


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def finish(r: dict) -> None:
    """Derived distribution columns the optimizer reads."""
    proj = max(0.0, r["proj"])
    r["proj"] = proj
    sd = proj * r["cv"]
    r["std_dev"] = sd
    r["ceiling"] = proj + 1.2816 * sd
    r["floor"] = max(0.0, proj - 1.2816 * sd)
    r["value"] = proj / (r["salary"] / 1000.0) if r["salary"] else 0.0
    r["fpts_min"] = proj / r["proj_minutes"] if r.get("proj_minutes") else 0.0
    k = r["salary"] / 1000.0
    if sd > 0:
        r["boom"] = 100.0 * (1.0 - _norm_cdf((MODEL["boom_multiple"] * k - proj) / sd))
        r["bust"] = 100.0 * _norm_cdf((MODEL["bust_multiple"] * k - proj) / sd)
    else:
        r["boom"] = 0.0
        r["bust"] = 100.0 if proj < MODEL["bust_multiple"] * k else 0.0
    r["starting"] = r["start_rate"] >= 0.5 and proj > 0


def yesterday(d: date) -> date:
    return d - timedelta(days=1)
