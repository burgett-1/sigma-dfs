"""Walk-forward NFL backtest (P3-2.4.8) -> data/nfl/audit.json, plus the
outcome curves (nfl/state/dist.json) and the correlation fit (nfl/state/corr.json).

Each week is projected from games before it only, with the nflverse closing
spread/total and the players who actually suited up (an "availability oracle",
labelled as such: real slates also carry injury uncertainty). Baselines: the
season-to-date mean DK points (~ DK's AvgPointsPerGame) and the last-3 mean.

Manual (network) run, documented in team_workflow.md P3-3:
    python -m pipeline.sports.nfl.backtest --seasons 2025 2026
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ...distribution import U_SAMPLE
from .. import corr as C
from .. import get
from ..paths import paths
from . import model as M

log = logging.getLogger(__name__)
FIRST_WEEK = 5
MIN_PRIOR_GAMES = 2
MIN_BASELINE = 5.0
SHARES_Q = ("0.1", "0.25", "0.5", "0.75", "0.9")


def _week_slate(hist: pd.DataFrame, season: int, week: int) -> tuple[list[dict], dict, pd.DataFrame]:
    wk = hist[(hist["season"] == season) & (hist["week"] == week)]
    ctx = {}
    for _, r in wk.drop_duplicates("team").iterrows():
        implied = None
        if pd.notna(r["total"]) and pd.notna(r["spread"]):
            implied = (float(r["total"]) - float(r["spread"])) / 2.0
        ctx[r["team"]] = {"implied": implied, "spread": r["spread"], "wind": r["wind"], "roof": r["roof"],
                          "opp": r["opp"]}
    for t, c in ctx.items():
        c["opp_implied"] = (ctx.get(c["opp"]) or {}).get("implied")
    players = [{"dk_player_id": r["player_id"], "name": r["name"], "team": r["team"], "opp": r["opp"],
                "position": r["position"], "status": "", "dk_avg": None, "player_id": r["player_id"], "salary": 0}
               for _, r in wk.iterrows()]
    return players, ctx, wk


def _baselines(hist: pd.DataFrame, season: int, week: int) -> pd.DataFrame:
    h = hist[(hist["season"] == season) & (hist["week"] < week)]
    g = h.sort_values("week").groupby("player_id")["dk_fp"]
    return pd.DataFrame({"n_prior": g.size(), "std_mean": g.mean(), "last3": g.apply(lambda s: s.tail(3).mean())})


def run(hist: pd.DataFrame, seasons: list[int], dist: dict | None = None) -> pd.DataFrame:
    """One row per (player, week) in the evaluation population."""
    out = []
    for season in seasons:
        weeks = sorted(hist.loc[hist["season"] == season, "week"].unique())
        for week in (w for w in weeks if w >= FIRST_WEEK and w <= 18):
            players, ctx, wk = _week_slate(hist, season, int(week))
            rows = M.project(players, hist, ctx, season, int(week), dist)
            base = _baselines(hist, season, int(week))
            for r, (_, a) in zip(rows, wk.iterrows()):
                b = base.loc[r["player_id"]] if r["player_id"] in base.index else None
                out.append({"season": season, "week": int(week), "player_id": r["player_id"],
                            "position": r["position"], "mean": r["mean"], "proj": r["proj"], "q": r["q"],
                            "actual": float(a["dk_fp"]), "n_prior": int(b["n_prior"]) if b is not None else 0,
                            "std_mean": float(b["std_mean"]) if b is not None else np.nan,
                            "last3": float(b["last3"]) if b is not None else np.nan,
                            "pass_yds_mu": r["components"].get("pass_yds"), "rush_yds_mu": r["components"].get("rush_yds"),
                            "rec_yds_mu": r["components"].get("rec_yds"),
                            "pass_yds": float(a["pass_yds"]), "rush_yds": float(a["rush_yds"]),
                            "rec_yds": float(a["rec_yds"]), "points_allowed": a["points_allowed"],
                            "opp_implied": r["components"].get("opp_implied")})
    df = pd.DataFrame(out)
    if df.empty:
        return df
    off = df["position"].isin(M.DEFAULT_CV.keys() - {"DST", "K"})
    keep = (off & (df["n_prior"] >= MIN_PRIOR_GAMES) & (df["std_mean"] >= MIN_BASELINE)) | (df["position"] == "DST")
    return df[keep].reset_index(drop=True)


def _err(pred: pd.Series, act: pd.Series) -> dict:
    ok = pred.notna() & act.notna()
    d = pred[ok] - act[ok]
    if not ok.any():
        return {"n": 0, "mae": None, "bias": None, "corr": None}
    c = float(np.corrcoef(pred[ok], act[ok])[0, 1]) if ok.sum() > 2 and pred[ok].std() > 0 else None
    return {"n": int(ok.sum()), "mae": round(float(d.abs().mean()), 3), "bias": round(float(d.mean()), 3),
            "corr": round(c, 3) if c is not None else None}


def fit_dist(df: pd.DataFrame) -> dict:
    """Ratio quantiles (actual / projected mean) at U_SAMPLE by position x tercile."""
    curves, terciles, counts = {}, {}, {}
    for pos, g in df[df["mean"] > 0].groupby("position"):
        cuts = list(np.quantile(g["mean"], [1 / 3, 2 / 3]))
        terciles[pos] = [round(float(c), 3) for c in cuts]
        t = np.where(g["mean"] < cuts[0], 0, np.where(g["mean"] < cuts[1], 1, 2))
        for k in range(3):
            sub = g[t == k]
            counts[f"{pos}|{k}"] = int(len(sub))
            if len(sub) < 60:
                continue
            ratio = (sub["actual"] / sub["mean"]).clip(lower=0)
            qs = np.quantile(ratio, U_SAMPLE)
            qs[0] = max(0.0, float(ratio.min()))
            curves[f"{pos}|{k}"] = [round(float(v), 4) for v in np.maximum.accumulate(qs)]
    return {"version": 1, "fitted_at": datetime.now(timezone.utc).isoformat(), "u": list(U_SAMPLE),
            "curves": curves, "terciles": terciles, "n": counts}


def _coverage(df: pd.DataFrame) -> dict:
    shares = {}
    for u in SHARES_Q:
        j = U_SAMPLE.index(float(u))
        qv = df["q"].map(lambda q: q[j])
        shares[u] = round(float((df["actual"] < qv).mean()), 4)
    pin = []
    for u in SHARES_Q:
        j = U_SAMPLE.index(float(u))
        qv = df["q"].map(lambda q: q[j]).to_numpy(float)
        a = df["actual"].to_numpy(float)
        pin.append(np.mean(np.maximum(float(u) * (a - qv), (float(u) - 1) * (a - qv))))
    return {"pinball": round(float(np.mean(pin)), 4), "share_below": shares, "n": int(len(df))}


def _bonus_calibration(df: pd.DataFrame) -> dict:
    out = {}
    for stat, thr, pos in (("pass_yds", 300, ("QB",)), ("rush_yds", 100, ("RB", "QB")), ("rec_yds", 100, ("WR", "TE", "RB"))):
        g = df[df["position"].isin(pos) & df[f"{stat}_mu"].notna()]
        if g.empty:
            continue
        p = g[f"{stat}_mu"].map(lambda m: M.bonus_expectation(float(m), stat, thr, 1.0))
        hit = (g[stat] >= thr).astype(float)
        dec = pd.qcut(p.rank(method="first"), 10, labels=False) if len(g) >= 20 else pd.Series(0, index=g.index)
        out[stat] = [{"decile": int(d), "pred": round(float(p[dec == d].mean()), 4),
                      "actual": round(float(hit[dec == d].mean()), 4), "n": int((dec == d).sum())}
                     for d in sorted(dec.unique())]
    return out


def report(df: pd.DataFrame, held_out: pd.DataFrame | None = None) -> dict:
    sc = get("NFL")["scoring"]
    res = {"version": 1, "sport": "NFL", "generated_at": datetime.now(timezone.utc).isoformat(),
           "method": "walk-forward, data before each week only; nflverse closing lines; availability oracle "
                     "(players who actually played)",
           "scoring": {"id": sc["id"], "verified": bool(sc.get("verified")), "source": sc.get("source")},
           "population": {"rows": int(len(df)), "weeks": int(df[["season", "week"]].drop_duplicates().shape[0])
                          if len(df) else 0}}
    if df.empty:
        return res
    by = {}
    for pos, g in df.groupby("position"):
        by[pos] = {"model": _err(g["mean"], g["actual"]), "model_median": _err(g["proj"], g["actual"]),
                   "season_mean": _err(g["std_mean"], g["actual"]), "last3": _err(g["last3"], g["actual"])}
    off = df[df["position"] != "DST"]
    res["dk_points"] = {"overall": {"model": _err(off["mean"], off["actual"]),
                                    "model_median": _err(off["proj"], off["actual"]),
                                    "season_mean": _err(off["std_mean"], off["actual"]),
                                    "last3": _err(off["last3"], off["actual"])},
                        "by_position": by}
    res["bonus_calibration"] = _bonus_calibration(df)
    res["distribution"] = {"held_out": _coverage(held_out if held_out is not None else df),
                           "held_out_def": "curves fitted on the other season, applied to this one"
                           if held_out is not None else "in-sample"}
    d = df[df["position"] == "DST"]
    if len(d):
        bucket = d.apply(lambda r: _bucket(r["points_allowed"]) == _bucket(r["opp_implied"])
                         if pd.notna(r["points_allowed"]) and r["opp_implied"] is not None else np.nan, axis=1)
        res["dst"] = {"mae": _err(d["mean"], d["actual"])["mae"], "bucket_accuracy": round(float(bucket.mean()), 3),
                      "n": int(len(d))}
    return res


def _bucket(v) -> int:
    for i, hi in enumerate((0, 6, 13, 20, 27, 34)):
        if v <= hi:
            return i
    return 6


def run_all(hist: pd.DataFrame, seasons: list[int], write: bool = True) -> dict:
    """Backtest, fit curves on one season and test on the other (both ways), refit
    on everything for live use, fit correlation loadings, and write the outputs."""
    base = run(hist, seasons)
    held = []
    if len(seasons) >= 2:
        for s in seasons:
            dist = fit_dist(base[base["season"] != s])
            part = run(hist, [s], dist)
            held.append(part)
    held_df = pd.concat(held, ignore_index=True) if held else None
    dist = fit_dist(base)
    rep = report(base, held_df)
    fit = C.fit_loadings(hist[hist["season"].isin(seasons)], "NFL")
    rep["correlation_fit"] = {"used": fit["used"], "n": fit["n"], "reason": fit["reason"]}
    if write:
        P = paths("NFL")
        P.state.mkdir(parents=True, exist_ok=True)
        _write(P.state / "dist.json", dist)
        _write(P.state / "corr.json", {"used": fit["used"], "n": fit["n"], "reason": fit["reason"],
                                       "loadings": fit["loadings"], "observed": fit.get("observed"),
                                       "fitted_at": datetime.now(timezone.utc).isoformat()})
        _write(P.root / "audit.json", rep)
    return rep


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=lambda o: None if isinstance(o, float) and math.isnan(o) else o))
    tmp.replace(path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="NFL walk-forward backtest (downloads nflverse unless cached)")
    ap.add_argument("--seasons", type=int, nargs="+", default=[2025, 2026])
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from ... import health
    from . import data as D
    with health.default_scope("NFL"):
        hist = D.refresh_history(sorted(set(a.seasons) | {min(a.seasons) - 1}), max(a.seasons), 6 * 3600)
        rep = run_all(hist, a.seasons, write=not a.no_write)
    print(json.dumps({k: rep.get(k) for k in ("population", "dk_points", "distribution", "dst", "correlation_fit")},
                     indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
