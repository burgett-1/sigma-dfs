"""Calibration fitted from the backtest (pipeline/audit.py), so the numbers on the
site match what actually happens:

  * the full distribution of each player's DK points (distribution.py): Proj is
    its median, floor/ceiling its 10th/90th percentiles, boom/bust read off it,
    and its shape changes with tonight's facts where history shows it should;
  * projected minutes: the median real minutes of players given that projection.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import distribution
from .config import STATE_DIR

PATH = STATE_DIR / "calibration.json"


def fit(df: pd.DataFrame, min_rows: int = 3000) -> dict:
    """df: audit rows (raw model projection in `proj`, real `act_fp`, `minutes`,
    `act_min`, `date`, and the f_* pre-game facts)."""
    df = df[df["proj"] > 0]
    if len(df) < min_rows:
        return {"used": False, "note": f"only {len(df)} player-games"}
    cal = {"used": True, "n": int(len(df))}
    qm = pd.qcut(df["minutes"], 14, duplicates="drop")
    gm = df.groupby(qm, observed=True).agg(x=("minutes", "mean"), y=("act_min", "median"))
    cal["minutes"] = {"x": [round(float(v), 3) for v in gm["x"]],
                      "y": [round(float(v), 3) for v in np.maximum.accumulate(gm["y"].to_numpy())]}
    cal["dist"] = distribution.fit(df, min_rows=min_rows)
    return cal


def apply_level(proj, cal: dict):
    """Piecewise-linear level curve; beyond the ends keep the end ratio."""
    xs, ys = np.asarray(cal["proj_x"]), np.asarray(cal["proj_y"])
    p = np.asarray(proj, dtype=float)
    out = np.interp(p, xs, ys)
    hi = p > xs[-1]
    out[hi] = p[hi] * ys[-1] / xs[-1]
    lo = p < xs[0]
    out[lo] = p[lo] * ys[0] / xs[0]
    return out


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    return o


def save(cal: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(_jsonable(cal)))


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}
