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

from .config import DK_SCORING, MODEL, NOT_PLAYING
from . import calibration, factors
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
    lineups: dict[str, dict] = field(default_factory=dict)    # team -> {status, starters}
    factors: dict = field(default_factory=dict)               # fitted adjustments (factors.py)
    game_context: dict[str, dict] = field(default_factory=dict)  # team -> rest/travel/ref features
    props: dict[str, dict] = field(default_factory=dict)         # norm name -> {stat: {mean, line, ...}}
    calibration: dict = field(default_factory=dict)              # fitted by calibration.fit (backtest)


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

    # per-minute rate of each box-score stat (for blending in betting-market props)
    stat_rates = {}
    pool = pd.concat([pc, pp.head(30)])
    pm = float(pool["minutes"].sum())
    if pm > 0:
        for st in STAT_KEYS:
            if st in pool.columns:
                stat_rates[st] = float(pool[st].sum() / pm)

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

    # minutes as a starter vs off the bench (role changes move minutes a lot)
    role = pd.concat([cur.head(25), prev.head(25)])
    w_role = np.concatenate([_decay_weights(len(cur.head(25)), 6.0), 0.5 * _decay_weights(len(prev.head(25)), 6.0)])
    st_mask = role["starter"].astype(bool).to_numpy()
    mins_role = role["minutes"].to_numpy()
    n_start = float(w_role[st_mask].sum())
    n_bench = float(w_role[~st_mask].sum())
    m_start = float((mins_role[st_mask] * w_role[st_mask]).sum() / n_start) if n_start > 0 else None
    m_bench = float((mins_role[~st_mask] * w_role[~st_mask]).sum() / n_bench) if n_bench > 0 else None

    # role and availability
    last10 = plogs[(plogs["season"] == season) & plogs["counts"]].head(10)
    if len(last10) < 3:
        last10 = plogs[plogs["counts"]].head(10)
    start_rate = float(last10["starter"].astype(bool).mean()) if len(last10) else 0.0
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
        "stat_rates": stat_rates,
        "m_start": m_start, "n_start": n_start, "m_bench": m_bench, "n_bench": n_bench,
    }


def role_minutes(r: dict, is_starter: bool) -> float:
    """Minutes given today's role, shrunk toward the overall average when the
    player has little history in that role."""
    m = r["minutes"]
    k = 3.0
    if is_starter:
        prior = max(m, 0.5 * m + 14.0)          # a bench player promoted to the five
        n, mr = r.get("n_start") or 0.0, r.get("m_start")
    else:
        prior = min(m, 0.75 * m + 2.0)          # a usual starter coming off the bench
        n, mr = r.get("n_bench") or 0.0, r.get("m_bench")
    if mr is None or n <= 0:
        return prior
    return (n * mr + k * prior) / (n + k)


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
    global _CAL
    _CAL = ctx.calibration or {}
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
    from .sources.lineups import project_starters
    for team in sorted({r["team"] for r in rows}):
        tr = [r for r in rows if r["team"] == team]
        out = [r for r in tr if r["status"] in NOT_PLAYING]
        avail = [r for r in tr if r["status"] not in NOT_PLAYING]

        # starting five: team-announced (NBA.com) or Sigma's projection
        info = ctx.lineups.get(team)
        if info and info.get("starters"):
            five = {norm_name(n) for n in info["starters"]}
            lu_status = info.get("status", "expected")
        else:
            five = {norm_name(n) for n in project_starters(tr)}
            lu_status = "projected"
        for r in tr:
            nk = norm_name(r["name"])
            r["is_starter"] = nk in five
            r["starter_status"] = lu_status if r["is_starter"] else ""
            usual = r["start_rate"] >= 0.6
            r["unexpected"] = ""
            if lu_status in ("confirmed", "expected") and r["status"] not in NOT_PLAYING:
                if r["is_starter"] and r["start_rate"] < 0.3 and r["has_history"]:
                    r["unexpected"] = "starting"
                elif not r["is_starter"] and usual:
                    r["unexpected"] = "benched"
            if r["status"] not in NOT_PLAYING and r["has_history"]:
                r["minutes"] = role_minutes(r, r["is_starter"])
        for r in avail:
            r["restricted"] = False
            if r["absence_streak"] >= MODEL["return_absence_games"] and r["has_history"]:
                r["minutes"] *= MODEL["return_minutes_mult"]
            if factors.minutes_limited(r.get("reason", "")):
                r["minutes"] *= MODEL["restricted_minutes_mult"]
                r["restricted"] = True
        # vacated minutes go preferentially to the same position group
        missing_grp: dict[str, float] = {}
        for r in out:
            missing_grp[r["grp"]] = missing_grp.get(r["grp"], 0.0) + r["minutes"] * r["fresh"]
        miss_total = sum(missing_grp.values()) or 1.0
        affinity = {r["key"]: 1.0 + 1.5 * missing_grp.get(r["grp"], 0.0) / miss_total for r in avail}
        mins = {r["key"]: r["minutes"] for r in avail}
        caps = {r["key"]: (r["minutes"] if r["restricted"] else
                           min(MODEL["max_player_minutes"], max(r["max_recent_min"] + 8.0, r["minutes"] + 6.0)))
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
        fit = ctx.factors or {}
        if team in ctx.teams_played_yesterday and not fit.get("used"):
            for r in avail:
                if new_mins.get(r["key"], 0) >= 30:
                    new_mins[r["key"]] *= MODEL["back_to_back_mult"]

        # usage bump from rotation players who are out (only recent absences count)
        vol_out = sum(r["minutes"] * r["rate"] * r["fresh"] for r in out)
        vol_all = vol_out + sum(r["minutes"] * r["rate"] for r in avail)
        u_out = vol_out / vol_all if vol_all > 0 else 0.0
        rates = [r["rate"] for r in avail if r["minutes"] >= 10] or [1.0]
        r_mean = float(np.mean(rates))
        # how each teammate actually produced when these players sat
        fresh_out = [r["person_id"] for r in out if r["fresh"] > 0 and r["person_id"] is not None]
        ww = factors.with_without(hist, team, {r["key"]: r["person_id"] for r in avail}, fresh_out,
                                  season) if fresh_out and not hist.empty else {}
        situ = factors.situational_multiplier(ctx.game_context.get(team, {}), fit)

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
            r["ww_evidence"] = 0.0
            if r["key"] in ww and ww[r["key"]][1] >= 3:
                spec, ev = ww[r["key"]]
                bump = 0.5 * bump + 0.5 * (spec - 1.0)
                r["ww_evidence"] = ev
            dvp = opp_pos.get((r["opp"], r["grp"]), 1.0)
            if fit.get("used"):
                home = 1.0   # home court is one of the fitted situational factors
            else:
                home = 1.0 + (MODEL["home_edge"] if r["home"] else -MODEL["home_edge"])
            r["proj_minutes"] = m
            r["bump"] = bump
            r["dvp"] = dvp
            r["situational"] = situ
            r["proj"] = m * r["rate"] * (1 + bump) * env * dvp * home * situ
            r["proj_raw"] = r["proj"]
            if _CAL.get("used"):          # backtest-fitted level correction (calibration.py)
                r["proj"] = float(calibration.apply_level([r["proj"]], _CAL)[0])
                if _CAL.get("min_x"):     # minutes too: real minutes vs projected, by level
                    r["proj_minutes"] = float(calibration.apply_level(
                        [m], {"proj_x": _CAL["min_x"], "proj_y": _CAL["min_y"]})[0])
            r["proj_model"] = r["proj"]
            apply_market(r, ctx.props.get(norm_name(r["name"])))

    for r in rows:
        finish(r)
    return rows


STAT_KEYS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")


def apply_market(r: dict, props: dict | None) -> None:
    """Blend sharp-book player props into the projection.

    The model's projection is split into expected box-score stats (the player's
    per-minute rates, scaled to his projected DK points). For every stat the
    market prices, the projection moves MODEL["market_weight"] of the way to the
    market's de-vigged expectation, valued at DraftKings scoring."""
    r["market_stats"] = 0
    r["market_delta"] = 0.0
    if not props or r.get("proj", 0) <= 0:
        return
    rates = r.get("stat_rates") or {}
    m = r.get("proj_minutes") or 0
    raw = sum(DK_SCORING[s] * rates.get(s, 0) * m for s in STAT_KEYS)
    scale = r["proj"] / raw if raw > 0 else 1.0
    delta, n = 0.0, 0
    for s in STAT_KEYS:
        mk = props.get(s)
        if not mk or s not in rates:
            continue
        model_s = rates[s] * m * scale
        delta += DK_SCORING[s] * (mk["mean"] - model_s)
        n += 1
    if n:
        w = MODEL["market_weight"]
        r["market_delta"] = round(w * delta, 2)
        r["market_stats"] = n
        r["proj"] = max(0.0, r["proj"] + w * delta)


def p_opp(team_rows: list[dict]) -> str:
    return team_rows[0]["opp"] if team_rows else ""


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


_CAL: dict = {}


def finish(r: dict) -> None:
    """Derived distribution columns the optimizer reads.

    With a fitted calibration (the normal case once history exists), the spread
    of outcomes comes from the backtest and floor/ceiling/boom/bust from the same
    right-skewed gamma distribution the simulator uses. Without one, a plain
    normal approximation around the player's own volatility is used."""
    proj = max(0.0, r["proj"])
    r["proj"] = proj
    k = r["salary"] / 1000.0
    r["value"] = proj / k if r["salary"] else 0.0
    r["fpts_min"] = proj / r["proj_minutes"] if r.get("proj_minutes") else 0.0
    r["starting"] = bool(r.get("is_starter", r["start_rate"] >= 0.5)) and proj > 0
    if proj <= 0:
        r.update(std_dev=0.0, ceiling=0.0, floor=0.0, boom=0.0, bust=100.0 if k else 0.0)
        return
    if _CAL.get("used"):
        cv = calibration.cv_for(proj, _CAL, r.get("cv"))
        r["cv_final"] = cv
        r["std_dev"] = proj * cv
        r["floor"] = calibration.quantile(proj, cv, 0.10)
        r["ceiling"] = calibration.quantile(proj, cv, 0.90)
        r["boom"] = 100.0 * calibration.prob_at_least(proj, cv, MODEL["boom_multiple"] * k)
        r["bust"] = 100.0 * (1.0 - calibration.prob_at_least(proj, cv, MODEL["bust_multiple"] * k))
        return
    sd = proj * r["cv"]
    r["std_dev"] = sd
    r["ceiling"] = proj + 1.2816 * sd
    r["floor"] = max(0.0, proj - 1.2816 * sd)
    r["boom"] = 100.0 * (1.0 - _norm_cdf((MODEL["boom_multiple"] * k - proj) / sd))
    r["bust"] = 100.0 * _norm_cdf((MODEL["bust_multiple"] * k - proj) / sd)


def yesterday(d: date) -> date:
    return d - timedelta(days=1)
