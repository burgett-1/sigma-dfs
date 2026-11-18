"""Calibration fitted from the backtest (pipeline/audit.py), so the numbers on the
site match what actually happens:

  * projection level: real DK points vs projected, by projection range (a monotone
    curve; e.g. if players projected ~34 really average ~37, they're moved up)
  * outcome spread: how widely real results vary around the projection at each
    projection level (low projections vary much more, relatively)

Floor / ceiling / boom / bust are then computed from the same right-skewed (gamma)
distribution the contest simulator uses, so a "20% boom" really booms ~20% of the time.
"""
from __future__ import annotations

import json
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

from .config import STATE_DIR

_N = NormalDist()
PATH = STATE_DIR / "calibration.json"


def fit(df: pd.DataFrame, min_rows: int = 1500) -> dict:
    """df: audit rows with proj (model, before market), act_fp, cv_player."""
    df = df[df["proj"] > 0]
    if len(df) < min_rows:
        return {"used": False, "note": f"only {len(df)} player-games"}
    # 1) projection level: mean actual per projection decile, forced monotone
    q = pd.qcut(df["proj"], 12, duplicates="drop")
    g = df.groupby(q, observed=True).agg(x=("proj", "mean"), y=("act_fp", "mean"), n=("proj", "size"))
    xs, ys = g["x"].to_numpy(), np.maximum.accumulate(g["y"].to_numpy())
    cal = {"used": True, "n": int(len(df)), "proj_x": [round(float(v), 3) for v in xs],
           "proj_y": [round(float(v), 3) for v in ys]}
    # minutes: mean real minutes per projected-minutes bin (coach's-decision DNPs count as 0)
    if "minutes" in df and "act_min" in df:
        qm = pd.qcut(df["minutes"], 12, duplicates="drop")
        gm = df.groupby(qm, observed=True).agg(x=("minutes", "mean"), y=("act_min", "mean"))
        cal["min_x"] = [round(float(v), 3) for v in gm["x"]]
        cal["min_y"] = [round(float(v), 3) for v in np.maximum.accumulate(gm["y"].to_numpy())]
    # 2) spread: std of actual / calibrated projection by level; fit log(cv) = a + b log(proj)
    pc = apply_level(df["proj"].to_numpy(), cal)
    ratio = df["act_fp"].to_numpy() / pc
    q2 = pd.qcut(pc, 10, duplicates="drop")
    gb = pd.DataFrame({"pc": pc, "r": ratio, "cvp": df["cv_player"].to_numpy()}).groupby(q2, observed=True)
    tab = gb.agg(x=("pc", "mean"), cv=("r", "std"), cvp=("cvp", "mean"))
    tab = tab[tab["x"] >= 6]
    b, a = np.polyfit(np.log(tab["x"]), np.log(tab["cv"]), 1)
    cal.update({"cv_a": round(float(a), 4), "cv_b": round(float(b), 4),
                "cv_table": [[round(float(x), 1), round(float(c), 3)] for x, c in zip(tab["x"], tab["cv"])],
                "cv_player_mean": round(float(df["cv_player"].mean()), 4)})
    return cal


def apply_level(proj, cal: dict):
    xs, ys = np.asarray(cal["proj_x"]), np.asarray(cal["proj_y"])
    p = np.asarray(proj, dtype=float)
    out = np.interp(p, xs, ys)
    hi = p > xs[-1]
    out[hi] = p[hi] * ys[-1] / xs[-1]                 # beyond the data: keep the last ratio
    lo = p < xs[0]
    out[lo] = p[lo] * ys[0] / xs[0]
    return out


def cv_for(proj: float, cal: dict, player_cv: float | None = None) -> float:
    """Outcome spread (sd / mean) for a projection, nudged by the player's own volatility."""
    base = math.exp(cal["cv_a"]) * max(proj, 3.0) ** cal["cv_b"]
    if player_cv and cal.get("cv_player_mean"):
        base *= min(1.25, max(0.8, math.sqrt(player_cv / cal["cv_player_mean"])))
    return float(min(max(base, 0.2), 2.0))


# ── gamma (Wilson-Hilferty) helpers, same shape as the simulator ─────────────
def _h(cv: float) -> float:
    k = max(1.0 / (cv * cv), 0.25)
    return 1.0 / (9.0 * k)


def quantile(mean: float, cv: float, p: float) -> float:
    h = _h(cv)
    return mean * max(1 - h + _N.inv_cdf(p) * math.sqrt(h), 0.0) ** 3


def prob_at_least(mean: float, cv: float, t: float) -> float:
    if mean <= 0:
        return 0.0
    h = _h(cv)
    z = ((max(t, 0) / mean) ** (1 / 3) - (1 - h)) / math.sqrt(h)
    return 1 - _N.cdf(z)


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}
