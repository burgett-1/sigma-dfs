"""Backtest audit of every projection column against what actually happened.

For a sample of past game nights it re-runs the projection model using only what
was known before tip-off (earlier box scores; who sat; who started; the closing
spread and total), then compares with the real box scores:

  minutes, DK points, points per minute: bias, mean absolute error, correlation
  std dev / floor / ceiling: are 10% of results below the floor and 10% above
  the ceiling, and is (actual - projection) / std dev spread like a unit normal?
  boom / bust: do players given X% boom actually boom about X% of the time?

`python -m pipeline.audit [DATA_DIR] [n_nights]` prints a report and writes
audit.json (also used to calibrate the spread of outcomes).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date

import numpy as np
import pandas as pd

from . import history, model
from .config import MODEL, NOT_PLAYING
from .names import player_key

log = logging.getLogger(__name__)


def _salary_proxy(prior_fp: float) -> int:
    # only used where salary enters the model (rookie prior); no real salary needed
    return int(min(11000, max(3000, round((3000 + 190 * prior_fp) / 100) * 100)))


def nights(logs: pd.DataFrame, n: int) -> list[date]:
    days = sorted(set(pd.to_datetime(logs["date"]).dt.date))
    days = [d for d in days if (d.month, d.day) not in ((10, 21), (10, 22), (10, 23))]
    days = days[20:]                      # need some history before the first audited night
    if len(days) <= n:
        return days
    idx = np.linspace(0, len(days) - 1, n).round().astype(int)
    return [days[i] for i in idx]


def replay_night(logs: pd.DataFrame, day: date) -> list[dict]:
    tonight = logs[logs["date"].astype(str) == day.isoformat()]
    if tonight.empty:
        return []
    prior = logs[logs["date"].astype(str) < day.isoformat()]
    avg_fp = prior[prior["minutes"] > 0].groupby("person_id")["dk_fp"].mean()
    players, statuses, lineups, odds = [], {}, {}, {}
    for r in tonight.itertuples(index=False):
        key = player_key(r.name, r.team)
        players.append({"draftable_id": r.person_id, "dk_player_id": r.person_id, "name": r.name,
                        "position": "", "salary": _salary_proxy(float(avg_fp.get(r.person_id, 15.0))),
                        "team_dk": r.team, "team": r.team, "opp_dk": r.opp, "opp": r.opp, "home": bool(r.home),
                        "competition_id": r.game_id, "start_utc": None, "dk_status": "", "key": key})
        if r.status == "INACTIVE" or (r.minutes <= 0 and "COACH" not in str(r.not_playing_reason)):
            statuses[key] = {"status": "OUT", "reason": str(r.not_playing_reason), "source": "box"}
        if bool(r.starter):
            lineups.setdefault(r.team, {"status": "confirmed", "starters": []})["starters"].append(r.name)
        try:
            sp, tot = float(r.spread), float(r.total)
            odds[r.team] = {"spread": sp, "total": tot, "implied": tot / 2 - sp / 2}
        except (TypeError, ValueError):
            pass
    ctx = model.Context(today=day, statuses=statuses, odds=odds, lineups=lineups)
    rows = model.project(players, prior, ctx)
    actual = {(r.person_id): r for r in tonight.itertuples(index=False)}
    out = []
    for p in rows:
        a = actual.get(p["dk_player_id"])
        if a is None or p["status"] in NOT_PLAYING:
            continue
        # available but didn't play (coach's decision) counts as 0, like it would in a lineup
        if a.minutes <= 0 and "COACH" not in str(a.not_playing_reason):
            continue
        out.append({"date": day.isoformat(), "name": p["name"], "proj": p["proj"], "minutes": p["proj_minutes"],
                    "fppm": p["fpts_min"], "sd": p["std_dev"], "floor": p["floor"], "ceiling": p["ceiling"],
                    "salary": p["salary"], "boom": p["boom"], "bust": p["bust"],
                    "act_fp": float(a.dk_fp), "act_min": float(a.minutes), "starter": bool(a.starter),
                    "proj_raw": p.get("proj_raw", p["proj"]), "cv_player": p.get("cv", 0.32)})
    return out


def summarise(rows: list[dict]) -> dict:
    df = pd.DataFrame(rows)
    df = df[df["proj"] > 0]
    z = (df["act_fp"] - df["proj"]) / df["sd"].where(df["sd"] > 0)
    act_ppm = df["act_fp"] / df["act_min"].clip(lower=1)
    k = df["salary"] / 1000

    def stats(pred, act):
        return {"bias": round(float((pred - act).mean()), 3), "mae": round(float((pred - act).abs().mean()), 3),
                "corr": round(float(np.corrcoef(pred, act)[0, 1]), 3)}

    boom_hit = df["act_fp"] >= MODEL["boom_multiple"] * k
    bust_hit = df["act_fp"] < MODEL["bust_multiple"] * k
    bins = [0, 5, 15, 30, 50, 101]
    boom_cal = [{"predicted": f"{a}-{b}%", "n": int(m.sum()), "mean_pred": round(float(df.loc[m, "boom"].mean()), 1),
                 "actual": round(100 * float(boom_hit[m].mean()), 1)}
                for a, b in zip(bins[:-1], bins[1:]) if (m := (df["boom"] >= a) & (df["boom"] < b)).sum() > 30]
    bust_cal = [{"predicted": f"{a}-{b}%", "n": int(m.sum()), "mean_pred": round(float(df.loc[m, "bust"].mean()), 1),
                 "actual": round(100 * float(bust_hit[m].mean()), 1)}
                for a, b in zip(bins[:-1], bins[1:]) if (m := (df["bust"] >= a) & (df["bust"] < b)).sum() > 30]
    # quantiles of the actual outcome relative to the projection (for the floor/ceiling definition)
    ratio = df["act_fp"] / df["proj"]
    return {
        "player_games": int(len(df)), "nights": int(df["date"].nunique()),
        "dk_points": stats(df["proj"], df["act_fp"]),
        "minutes": stats(df["minutes"], df["act_min"]),
        "fppm": stats(df["fppm"], act_ppm[df["act_min"] >= 10].reindex(df.index).fillna(df["fppm"])),
        "below_floor_pct": round(100 * float((df["act_fp"] < df["floor"]).mean()), 1),
        "above_ceiling_pct": round(100 * float((df["act_fp"] > df["ceiling"]).mean()), 1),
        "z_mean": round(float(z.mean()), 3), "z_sd": round(float(z.std()), 3),
        "outcome_ratio_p10": round(float(ratio.quantile(0.10)), 3),
        "outcome_ratio_p90": round(float(ratio.quantile(0.90)), 3),
        "boom_calibration": boom_cal, "bust_calibration": bust_cal,
    }


def recalibrated(rows: list[dict], cal: dict) -> list[dict]:
    """The same audit rows with the calibration applied (to score it on held-out nights)."""
    from . import calibration
    out = []
    for r in rows:
        r = dict(r)
        if r["proj"] > 0:
            pc = float(calibration.apply_level(np.array([r["proj"]]), cal)[0])
            cv = calibration.cv_for(pc, cal, r["cv_player"])
            k = r["salary"] / 1000
            r.update(proj=pc, sd=pc * cv, floor=calibration.quantile(pc, cv, 0.10),
                     ceiling=calibration.quantile(pc, cv, 0.90),
                     boom=100 * calibration.prob_at_least(pc, cv, MODEL["boom_multiple"] * k),
                     bust=100 * (1 - calibration.prob_at_least(pc, cv, MODEL["bust_multiple"] * k)))
        out.append(r)
    return out


def calibrate(logs: pd.DataFrame, n: int = 40) -> tuple[dict, dict]:
    """Replay n past nights, fit the calibration, and report it honestly: fitted on half
    the nights, scored on the other half, before and after."""
    from . import calibration
    rows = []
    for d in nights(logs, n):
        try:
            rows += replay_night(logs, d)
        except Exception as e:  # noqa: BLE001
            log.warning("audit night %s failed: %s", d, e)
    if not rows:
        return {"used": False, "note": "no nights to audit"}, {}
    days = sorted({r["date"] for r in rows})
    a, b = set(days[0::2]), set(days[1::2])
    train = [r for r in rows if r["date"] in a]
    test = [r for r in rows if r["date"] in b]
    report = {"nights": len(days), "player_games": len(rows)}
    half = calibration.fit(pd.DataFrame(train), min_rows=500)
    if half.get("used") and test:
        report["held_out_before"] = summarise(test)
        report["held_out_after"] = summarise(recalibrated(test, half))
    cal = calibration.fit(pd.DataFrame(rows))
    return cal, report


def run(data_dir: str | None = None, n: int = 40) -> dict:
    if data_dir:
        history.HISTORY_DIR = __import__("pathlib").Path(data_dir) / "history"
    logs = history.load()
    rows = []
    for d in nights(logs, n):
        try:
            rows += replay_night(logs, d)
        except Exception as e:  # noqa: BLE001
            log.warning("audit night %s failed: %s", d, e)
    rep = summarise(rows)
    return rep


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    rep = run(sys.argv[1] if len(sys.argv) > 1 else None, int(sys.argv[2]) if len(sys.argv) > 2 else 40)
    print(json.dumps(rep, indent=1))
