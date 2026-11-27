"""NFL projected ownership (P3-2.4.5).

Classic: a logit per position group (value, projection, news bump, implied team
total, Questionable), each group normalised to its roster-slot total (QB 100,
DST 100, RB 200 + 100 x FLEX share, WR 300 + ..., TE 100 + ...: 900 in all),
then capped at 80% with the excess re-spread inside the group. Showdown: one
logit over everyone, normalised to 600.

The shape of each group (how concentrated ownership is) is calibrated from the
owner's NFL contest exports: a per-position temperature tau (logit / tau) is
fitted so projected top-1 / top-3 / top-10 shares and HHI match the real ones,
shrunk to 1 with n / (n + 4). Only aggregate figures are read and stored.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from ...ownership import _z, cap_spread
from .. import get

CLASSIC_GROUPS = ("QB", "RB", "WR", "TE", "DST")
BASE_SLOTS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}
TAU_SHRINK = 4.0
TAU_GRID = np.round(np.arange(0.3, 3.01, 0.05), 2)


def group_totals(flex_split: dict | None = None) -> dict[str, float]:
    fs = flex_split or get("NFL")["ownership"]["flex_split"]
    return {p: 100.0 * (n + float(fs.get(p, 0.0))) for p, n in BASE_SLOTS.items()}


def _logit(rows: list[dict], coef: dict) -> np.ndarray:
    value = np.array([float(r.get("value") or 0.0) for r in rows])
    proj = np.array([float(r.get("proj") or 0.0) for r in rows])
    implied = np.array([float(r.get("implied_total") or 0.0) for r in rows])
    bump = np.array([float(r.get("bump") or 0.0) for r in rows])
    q = np.array([1.0 if r.get("status") == "QUESTIONABLE" else 0.0 for r in rows])
    return (coef["value"] * _z(value) + coef["proj"] * _z(proj) + coef["bump"] * bump
            + coef.get("implied", 0.0) * _z(implied) + coef["q"] * q)


def estimate(rows: list[dict], kind: str = "classic", calibration: dict | None = None) -> None:
    """Sets r["own"] (%) on every row; players projected 0 get 0."""
    cfg = get("NFL")["ownership"]
    coef, cap = cfg["coef"], float(cfg.get("cap", 80))
    cal = calibration or {}
    tau = cal.get("tau") or {}
    for r in rows:
        r["own"] = 0.0
    live = [r for r in rows if (r.get("proj") or 0) > 0]
    if not live:
        return
    if kind == "showdown":
        w = np.exp(_logit(live, coef))
        own = cap_spread(w / w.sum() * 600.0, cap)
        for r, o in zip(live, own):
            r["own"] = float(o)
        return
    totals = group_totals(cal.get("flex_split") if cal.get("used") else None)
    for pos in CLASSIC_GROUPS:
        grp = [r for r in live if str(r.get("position", "")).split("/")[0] == pos]
        if not grp:
            continue
        t = float(tau.get(pos, 1.0)) if cal.get("used") else 1.0
        w = np.exp(_logit(grp, coef) / t)
        own = cap_spread(w / w.sum() * totals[pos], cap)
        for r, o in zip(grp, own):
            r["own"] = float(o)


# ── shape calibration (private input, private output; aggregates only) ──────
def shape(values: list[float]) -> dict:
    v = np.sort(np.asarray([x for x in values if x > 0], dtype=float))[::-1]
    if not len(v):
        return {"top1": 0.0, "top3": 0.0, "top10": 0.0, "hhi": 0.0}
    s = v / v.sum()
    return {"top1": float(s[:1].sum()), "top3": float(s[:3].sum()), "top10": float(s[:10].sum()),
            "hhi": float((s ** 2).sum())}


def _shape_loss(a: dict, b: dict) -> float:
    return sum((a[k] - b[k]) ** 2 for k in ("top1", "top3", "top10", "hhi"))


def fit_tau(real_shapes: list[dict], slates: list[list[dict]], pos: str) -> float:
    """1-D grid search of the temperature for one position group."""
    coef = get("NFL")["ownership"]["coef"]
    best, best_loss = 1.0, math.inf
    for t in TAU_GRID:
        loss = 0.0
        for real, rows in zip(real_shapes, slates):
            grp = [r for r in rows if r["position"] == pos and r["proj"] > 0]
            if len(grp) < 3:
                continue
            w = np.exp(_logit(grp, coef) / t)
            loss += _shape_loss(real, shape(list(w)))
        if loss < best_loss:
            best, best_loss = float(t), loss
    return best


def calibrate(contests: list[dict], hist, games=None) -> dict:
    """Fit tau per position and the FLEX split from parsed NFL contest exports
    (contests.parse output with "sport" == "NFL" and a "file_date").

    Each contest is matched to an nflverse week by date; positions come from a
    name join to that week's players (DST by team nickname). The rebuilt slate
    uses each player's season-to-date mean DK points as proj and a salary proxy
    from it (no DK salaries exist for those weeks): an approximation, so tau is
    shrunk to 1 with n / (n + 4)."""
    import pandas as pd

    from ...names import norm_name
    from . import teams as T
    out = {"used": False, "contests": 0, "tau": {}, "flex_split": None, "shapes": {}}
    nfl = [c for c in contests if c.get("sport") == "NFL" and c.get("kind", "classic") == "classic"]
    if not nfl or hist is None or len(hist) == 0:
        out["note"] = "needs NFL classic contest exports and nflverse history"
        return out
    real_by_pos: dict[str, list] = {p: [] for p in CLASSIC_GROUPS}
    slates: list[list[dict]] = []
    flex = {"RB": 0.0, "WR": 0.0, "TE": 0.0}
    used = 0
    for c in nfl:
        wk = _week_for(c.get("file_date"), games if games is not None else hist)
        if wk is None:
            continue
        season, week = wk
        cur = hist[(hist["season"] == season) & (hist["week"] == week)]
        pos_of = {norm_name(str(n)): p for n, p in zip(cur["name"], cur["position"])}
        for t in T.TEAMS:
            pos_of[norm_name(t["nickname"])] = "DST"
        prior = hist[(hist["season"] == season) & (hist["week"] < week)]
        avg = prior.groupby("player_id")["dk_fp"].mean()
        rows = []
        for _, r in cur.drop_duplicates("player_id").iterrows():
            if r["position"] not in CLASSIC_GROUPS:
                continue
            m = float(avg.get(r["player_id"], 0.0))
            if m <= 0:
                continue
            sal = 2500.0 + 280.0 * m                       # salary proxy (documented approximation)
            rows.append({"position": r["position"], "proj": m, "value": m / (sal / 1000.0),
                         "implied_total": (r["total"] - r["spread"]) / 2.0 if pd.notna(r["total"]) else 21.5})
        slates.append(rows)
        own = c.get("own") or {}
        slots = c.get("own_slots") or {}
        groups: dict[str, list] = {p: [] for p in CLASSIC_GROUPS}
        for k, o in own.items():
            p = pos_of.get(k)
            if p in groups:
                groups[p].append(float(o))
                f = (slots.get(k) or {}).get("FLEX")
                if f and p in flex:
                    flex[p] += float(f)
        for p in CLASSIC_GROUPS:
            real_by_pos[p].append(shape(groups[p]))
        used += 1
    if not used:
        out["note"] = "no NFL contest matched an nflverse week"
        return out
    w = used / (used + TAU_SHRINK)
    tau = {}
    for p in CLASSIC_GROUPS:
        t = fit_tau(real_by_pos[p], slates, p)
        tau[p] = round(1.0 + (t - 1.0) * w, 3)
    tot = sum(flex.values())
    out.update(used=True, contests=used, tau=tau, weight=round(w, 3),
               flex_split={p: round(v / tot, 3) for p, v in flex.items()} if tot > 0 else None,
               shapes={p: {k: round(float(np.mean([s[k] for s in v])), 4) for k in ("top1", "top3", "top10", "hhi")}
                       for p, v in real_by_pos.items() if v})
    if out["flex_split"] is None:
        out["flex_split"] = dict(get("NFL")["ownership"]["flex_split"])
    return out


def _week_for(file_date: str | None, sched) -> tuple[int, int] | None:
    """The NFL week a contest export belongs to: the latest week whose games were
    played within the 3 days before the export's date (exports come after the
    slate). sched: nflverse games.csv (season, week, gameday)."""
    if not file_date:
        return None
    import pandas as pd
    try:
        d = pd.Timestamp(file_date)
    except ValueError:
        return None
    if sched is not None and "gameday" in sched:
        g = sched.dropna(subset=["gameday"])
        days = pd.to_datetime(g["gameday"])
        sel = g[(days <= d) & (days >= d - pd.Timedelta(days=3))]
        if not sel.empty:
            r = sel.sort_values(["season", "week"]).iloc[-1]
            return int(r["season"]), int(r["week"])
    return None


def load_calibration(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
