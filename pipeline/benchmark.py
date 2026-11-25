"""Industry benchmark and live accuracy (private, admin only).

* Industry: DailyFantasyFuel's free DraftKings NBA projections (their public
  page; robots.txt allows it; it shows the top 25 players, the rest is their
  paid tier and is not touched). Saved privately before lock each day, never
  republished: it is only a yardstick.
* Live accuracy: every night, OUR published projections (the archived slate
  files) and the industry's are scored against the real box scores.
* Corrections are data-only and need evidence first:
    - a level correction is applied only if, over at least MIN_NIGHTS nights and
      MIN_PLAYERS players at a projection level, real results land below our
      median clearly more (or less) than half the time (sign test, p < 0.01);
    - an industry blend weight is used only if, over at least MIN_NIGHTS nights,
      mixing their number into ours lowers the error on those nights.
  Until then nothing is changed.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime

import numpy as np
import pandas as pd

from .config import ET
from .contests import PRIVATE
from .http import session
from .names import norm_name, team_code

log = logging.getLogger(__name__)

DIR = PRIVATE / "benchmark"
DFF_URL = "https://www.dailyfantasyfuel.com/nba/projections/draftkings"
REPORT = DIR / "accuracy.json"
MIN_NIGHTS = 10
MIN_PLAYERS = 150
BINS = [10, 20, 30, 40, 200]


def parse_dff(page: str) -> list[dict]:
    rows = []
    for tag in re.findall(r"<tr[^>]*data-name=[^>]*>", page):
        d = dict(re.findall(r'data-([a-z_]+)="([^"]*)"', tag))
        try:
            rows.append({"name": d["name"], "team": team_code(d.get("team", "")), "salary": int(d.get("salary") or 0),
                         "proj": float(d["ppg_proj"]), "date": d.get("start_date", ""), "inj": d.get("inj", "")})
        except (KeyError, ValueError):
            continue
    return rows


def snapshot_industry(now: datetime, first_lock: datetime | None) -> int:
    """Save today's industry projections (until the first game locks)."""
    if first_lock and now >= first_lock:
        return 0
    r = session().get(DFF_URL, timeout=20)
    r.raise_for_status()
    rows = [x for x in parse_dff(r.text) if x["date"] == now.astimezone(ET).date().isoformat()]
    if rows:
        DIR.mkdir(parents=True, exist_ok=True)
        (DIR / f"dff_{rows[0]['date']}.json").write_text(json.dumps({"at": now.isoformat(), "players": rows}))
    return len(rows)


def _sign_p(below: int, n: int) -> float:
    """Two-sided sign test p-value (normal approximation, fine for n >= 30)."""
    if n == 0:
        return 1.0
    z = abs(below - n / 2) / math.sqrt(n / 4)
    return math.erfc(z / math.sqrt(2))


def evaluate(our_rows: pd.DataFrame) -> dict:
    """our_rows: day, name, team, proj (our published median), act (real DK points).
    Adds the industry projection where saved. Returns the report + corrections."""
    df = our_rows.copy()
    df["k"] = df["name"].map(norm_name) + "|" + df["team"]
    ind = {}
    for f in DIR.glob("dff_*.json"):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for p in doc["players"]:
            ind[(p["date"], norm_name(p["name"]) + "|" + p["team"])] = p["proj"]
    df["ind"] = [ind.get((str(d), k)) for d, k in zip(df["day"], df["k"])]
    rep = {"nights": int(df["day"].nunique()), "players": int(len(df)), "by_level": [], "industry": None,
           "level_correction": None, "blend_weight": 0.0}
    nights = df["day"].nunique()
    df["bin"] = pd.cut(df["proj"], BINS)
    corr = []
    for b, g in df.groupby("bin", observed=True):
        below = int((g["act"] < g["proj"]).sum())
        n = len(g)
        ratio = float(np.median(g["act"] / g["proj"]))
        p = _sign_p(below, n)
        rep["by_level"].append({"level": str(b), "n": n, "below_pct": round(100 * below / n, 1),
                                "median_error": round(float((g["proj"] - g["act"]).median()), 2), "p_value": round(p, 4),
                                "median_ratio": round(ratio, 3)})
        use = nights >= MIN_NIGHTS and n >= MIN_PLAYERS and p < 0.01
        corr.append({"lo": b.left, "hi": b.right, "ratio": round(ratio, 4) if use else 1.0})
    if any(c["ratio"] != 1.0 for c in corr):
        rep["level_correction"] = corr
    both = df[df["ind"].notna()]
    if len(both):
        rep["industry"] = {"players": int(len(both)), "nights": int(both["day"].nunique()),
                           "ours_mae": round(float((both["proj"] - both["act"]).abs().mean()), 2),
                           "industry_mae": round(float((both["ind"] - both["act"]).abs().mean()), 2),
                           "ours_minus_industry_avg": round(float((both["proj"] - both["ind"]).mean()), 2)}
        if both["day"].nunique() >= MIN_NIGHTS:
            # blend weight chosen on the data: leave-one-night-out, grid 0..1
            grid = np.linspace(0, 1, 21)
            err = np.zeros(len(grid))
            for d in both["day"].unique():
                tr, te = both[both["day"] != d], both[both["day"] == d]
                w = grid[np.argmin([(((1 - g) * tr["proj"] + g * tr["ind"]) - tr["act"]).abs().mean() for g in grid])]
                err += np.array([(((1 - g) * te["proj"] + g * te["ind"]) - te["act"]).abs().sum() if g == w else 0
                                 for g in grid])
            w_all = grid[np.argmin([(((1 - g) * both["proj"] + g * both["ind"]) - both["act"]).abs().mean() for g in grid])]
            cv_mae = err.sum() / len(both)
            if cv_mae < rep["industry"]["ours_mae"]:
                rep["blend_weight"] = round(float(w_all), 2)
            rep["industry"]["blend_cv_mae"] = round(float(cv_mae), 2)
    return rep


def load() -> dict:
    try:
        return json.loads(REPORT.read_text())
    except (OSError, ValueError):
        return {}


def industry_today(day: date) -> dict:
    try:
        doc = json.loads((DIR / f"dff_{day.isoformat()}.json").read_text())
    except (OSError, ValueError):
        return {}
    return {norm_name(p["name"]) + "|" + p["team"]: p["proj"] for p in doc["players"]}
