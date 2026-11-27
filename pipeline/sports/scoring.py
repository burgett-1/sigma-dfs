"""Table-driven DraftKings scoring (registry `scoring.tables`).

A table has any of:
  per_stat   {stat: points per unit}
  thresholds [{stat, gte, pts}]                 bonus when stat >= gte (independent)
  buckets    [{stat, ranges: [[lo, hi, pts]]}]  points by range (hi null = open)
  multi_cat  [{stats, gte, min_count, pts}]     e.g. NBA double/triple double
Missing stats count as 0. Results are rounded to 2 dp.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import get


def table_for(sport: str, position: str | None = None, reg: dict | None = None) -> dict:
    sc = get(sport, reg)["scoring"]
    tables = sc.get("tables") or {}
    name = (sc.get("position_tables") or {}).get((position or "").upper(), sc.get("default_table"))
    if name not in tables:
        raise KeyError(f"{sport} has no scoring table {name!r}")
    return tables[name]


def _col(stats, k):
    if isinstance(stats, pd.DataFrame):
        return stats[k].astype(float).fillna(0.0) if k in stats else pd.Series(0.0, index=stats.index)
    v = stats.get(k, 0) if isinstance(stats, dict) else 0
    return float(v or 0)


def score(stats, table: dict):
    """DK points for one stat line (dict -> float) or many (DataFrame -> Series)."""
    frame = isinstance(stats, pd.DataFrame)
    total = pd.Series(0.0, index=stats.index) if frame else 0.0
    for k, w in (table.get("per_stat") or {}).items():
        total = total + _col(stats, k) * float(w)
    for t in table.get("thresholds") or []:
        v = _col(stats, t["stat"])
        total = total + ((v >= t["gte"]) * float(t["pts"]) if frame else (float(t["pts"]) if v >= t["gte"] else 0.0))
    for b in table.get("buckets") or []:
        v = _col(stats, b["stat"])
        if frame:
            add = pd.Series(0.0, index=stats.index)
            for lo, hi, pts in b["ranges"]:
                sel = (v >= lo) & ((v <= hi) if hi is not None else True)
                add = add.where(~sel, float(pts))
            total = total + add
        else:
            for lo, hi, pts in b["ranges"]:
                if v >= lo and (hi is None or v <= hi):
                    total += float(pts)
                    break
    for m in table.get("multi_cat") or []:
        if frame:
            cnt = sum((_col(stats, k) >= m["gte"]).astype(int) for k in m["stats"])
            total = total + (cnt >= m["min_count"]) * float(m["pts"])
        else:
            cnt = sum(1 for k in m["stats"] if _col(stats, k) >= m["gte"])
            total += float(m["pts"]) if cnt >= m["min_count"] else 0.0
    if frame:
        return total.round(2)
    return round(float(total), 2)


def bucket_points(value: float, table: dict, stat: str) -> float:
    """Points for one bucketed stat (e.g. DST points allowed)."""
    for b in table.get("buckets") or []:
        if b["stat"] != stat:
            continue
        for lo, hi, pts in b["ranges"]:
            if value >= lo and (hi is None or value <= hi):
                return float(pts)
    return 0.0


def expected_bucket_points(mu: float, sd: float, table: dict, stat: str) -> float:
    """E[bucket points] for an integer stat ~ Normal(mu, sd), integrated per integer
    value (continuity-corrected) over 0..max(80, mu + 6 sd)."""
    hi = int(max(80, mu + 6 * sd))
    x = np.arange(0, hi + 1)
    from math import erf, sqrt
    cdf = np.array([0.5 * (1 + erf((v + 0.5 - mu) / (sd * sqrt(2)))) for v in x])
    p = np.diff(np.concatenate([[0.0], cdf]))
    p[-1] += 1.0 - cdf[-1]
    pts = np.array([bucket_points(float(v), table, stat) for v in x])
    return float((p * pts).sum())
