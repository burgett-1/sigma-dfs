"""Each player's full outcome distribution, learned from history.

Quantile regression on past nights (pipeline/audit.py replays them with only
pre-game information): for every percentile u of DK points (1%, 2.5%, 5%, 10%,
… 50% … 90%, 97.5%, 99%) a separate model says where that percentile sits for a
player, given

  * the model's raw projection (its level sets the overall scale), and
  * tonight's facts that change the *shape* of the outcome: the point spread
    (blowouts), the team total, starting vs. bench, teammates out, how much the
    player's minutes have swung lately, back-to-back, returning from absence, the
    player's own scoring volatility, and his minutes relative to his projection.

A fact is used only if it makes the percentiles more accurate on held-out
nights (forward selection, 2-fold by night). So the spread of outcomes is not a
fixed number: it changes slate to slate with the facts that history shows matter.

From the curve: Proj = the median (half of real results land above it, half
below), floor = 10th percentile, ceiling = 90th, boom/bust = probabilities read
off the curve, and the simulator draws outcomes from the same curve.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

U = [0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7,
     0.75, 0.8, 0.85, 0.9, 0.95, 0.975, 0.99]
U_SAMPLE = [0.0] + U + [0.999]           # sampling grid: 0 at the bottom, far tail at the top
U_SEL = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.975, 0.99]   # quicker grid for feature selection
TAIL = math.log(10) / math.log(2.5)       # exponential-tail step from the 97.5/99% gap to 99.9%
FEATURES = ["f_spread", "f_total", "f_starter", "f_bump", "f_min_cv", "f_few_games", "f_b2b",
            "f_return", "f_cv_player", "f_min_share"]


def dist_features(p: dict, played_yday: set) -> dict:
    """Pre-game facts for one projected player (live rows and backtest rows alike)."""
    sp, tot = p.get("spread"), p.get("implied_total")
    mins = p.get("proj_minutes_raw", p.get("proj_minutes")) or 0.0
    x = p.get("proj_raw", p.get("proj")) or 0.0
    mcv = p.get("min_cv")
    return {"f_spread": abs(float(sp)) if sp is not None and sp == sp else None,
            "f_total": float(tot) if tot else None,
            "f_starter": 1.0 if p.get("is_starter") else 0.0,
            "f_bump": float(p.get("bump") or 0.0),
            "f_min_cv": math.log(max(float(mcv), 0.05)) if mcv is not None and mcv == mcv else None,
            "f_few_games": 1.0 if (p.get("games_cur") or 0) < 5 else 0.0,
            "f_b2b": 1.0 if p.get("team") in played_yday else 0.0,
            "f_return": 1.0 if (p.get("absence_streak") or 0) >= 3 else 0.0,
            "f_cv_player": math.log(max(float(p.get("cv") or 0.32), 0.05)),
            "f_min_share": math.log(max(float(mins), 1.0) / max(float(x), 1.0))}


def _knots(x: np.ndarray) -> list[float]:
    """Knots of the projection-level curve where the data is (log scale)."""
    lx = np.log(np.maximum(x, 1.0))
    return [float(v) for v in np.quantile(lx, [0.1, 0.25, 0.4, 0.55, 0.7, 0.85, 0.95])]


def _basis(x: np.ndarray, knots: list[float]) -> np.ndarray:
    """Piecewise linear in log(projection): follows the data where it is dense and
    extends in a straight line past the last knot (no polynomial swings at the top)."""
    lx = np.log(np.maximum(x, 1.0))
    return np.column_stack([np.ones(len(x)), lx] + [np.maximum(lx - k, 0.0) for k in knots])


def _design(x: np.ndarray, F: pd.DataFrame, feats: list[str], stats: dict, knots: list[float]) -> np.ndarray:
    cols = [_basis(x, knots)]
    for f in feats:
        m, sd = stats[f]
        cols.append(((F[f].astype(float).fillna(m).to_numpy() - m) / sd)[:, None])
    # multiplicative in the projection: every percentile scales with the player's level
    return np.hstack(cols) * np.maximum(x, 0.5)[:, None]


def _qr(A: np.ndarray, y: np.ndarray, u: float, iters: int = 300, eps: float = 1e-4) -> np.ndarray:
    """Linear quantile regression by iteratively reweighted least squares (matches the
    exact linear-programming solution to ~1e-4 on this data, 100x faster)."""
    b = np.linalg.lstsq(A, y, rcond=None)[0]
    ridge = 1e-8 * np.eye(A.shape[1])
    for _ in range(iters):
        res = y - A @ b
        w = np.where(res > 0, u, 1 - u) / np.maximum(np.abs(res), eps)
        nb = np.linalg.solve(A.T @ (A * w[:, None]) + ridge, A.T @ (w * y))
        if np.max(np.abs(nb - b)) < 1e-7:
            return nb
        b = nb
    return b


def pinball(act: np.ndarray, Q: np.ndarray, us) -> float:
    u = np.asarray(us)[None, :]
    d = act[:, None] - Q
    return float(np.mean(np.maximum(u * d, (u - 1) * d)))


def _fit(x, act, F, feats, us):
    stats = {f: (float(F[f].astype(float).mean()), float(F[f].astype(float).std() or 1.0)) for f in feats}
    knots = _knots(x)
    A = _design(x, F, feats, stats, knots)
    return {"feats": feats, "stats": stats, "knots": knots, "us": list(us),
            "coef": [_qr(A, act, u).tolist() for u in us]}


def _predict(m: dict, x: np.ndarray, F: pd.DataFrame) -> np.ndarray:
    A = _design(x, F, m["feats"], m["stats"], m["knots"])
    Q = A @ np.asarray(m["coef"]).T
    Q = np.maximum(Q, 0.0)
    return np.sort(Q, axis=1)              # rearrangement: percentiles never cross


def fit(df: pd.DataFrame, min_rows: int = 3000) -> dict:
    """df: backtest rows with proj (raw model projection), act_fp, date and f_* features."""
    df = df[df["proj"] > 0].reset_index(drop=True)
    if len(df) < min_rows:
        return {"used": False, "note": f"only {len(df)} player-games"}
    x, act = df["proj"].to_numpy(float), df["act_fp"].to_numpy(float)
    F = df.reindex(columns=FEATURES)
    days = np.array(sorted(df["date"].unique()))
    folds = [np.isin(df["date"].to_numpy(), days[k::2]) for k in range(2)]

    def score(feats, us=U_SEL):
        tot = 0.0
        for te in folds:
            m = _fit(x[~te], act[~te], F[~te], feats, us)
            tot += pinball(act[te], _predict(m, x[te], F[te]), us) * te.sum()
        return tot / len(df)

    usable = [f for f in FEATURES if F[f].notna().mean() > 0.5 and F[f].astype(float).std() > 0]
    chosen, best = [], score([])
    trail = [{"features": [], "pinball": round(best, 5)}]
    while True:
        cand = [(score(chosen + [f]), f) for f in usable if f not in chosen]
        if not cand:
            break
        sc, f = min(cand)
        if sc > best * (1 - 0.0002):             # must improve held-out accuracy by >= 0.02%
            break
        chosen.append(f)
        best = sc
        trail.append({"features": list(chosen), "pinball": round(sc, 5)})
    model = _fit(x, act, F, chosen, U)
    # held-out report on the full grid: coverage of each percentile, and vs. no features
    cover, pin_f, pin_0 = np.zeros(len(U)), 0.0, 0.0
    for te in folds:
        m = _fit(x[~te], act[~te], F[~te], chosen, U)
        m0 = _fit(x[~te], act[~te], F[~te], [], U)
        Q, Q0 = _predict(m, x[te], F[te]), _predict(m0, x[te], F[te])
        cover += (act[te][:, None] < Q).sum(axis=0)
        pin_f += pinball(act[te], Q, U) * te.sum()
        pin_0 += pinball(act[te], Q0, U) * te.sum()
    model.update({"used": True, "n": int(len(df)), "nights": int(len(days)), "selection": trail,
                  "held_out": {"pinball_level_only": round(pin_0 / len(df), 4),
                               "pinball_with_facts": round(pin_f / len(df), 4),
                               "share_below": {str(u): round(float(c / len(df)), 4) for u, c in zip(U, cover)}}})
    return model


def curve(x: float, feats: dict, m: dict) -> np.ndarray:
    """Percentiles of DK points at U_SAMPLE for one player."""
    F = pd.DataFrame([{f: feats.get(f) for f in m["feats"]}], columns=m["feats"] or None)
    q = _predict(m, np.array([float(x)]), F if m["feats"] else pd.DataFrame(index=[0]))[0]
    top = q[-1] + TAIL * max(q[-1] - q[-2], 0.0)
    return np.concatenate([[0.0], q, [top]])


def summary(q: np.ndarray, salary: int, boom_mult: float, bust_mult: float) -> dict:
    """Median, mean, sd, floor, ceiling, boom %, bust % from a percentile curve."""
    us = np.asarray(U_SAMPLE)
    fine = np.linspace(0.0005, 0.9995, 2000)
    xs = np.interp(fine, us, q)
    k = salary / 1000.0

    def p_at_least(t):
        if t > q[-1]:
            return 0.0
        return float(1.0 - np.interp(t, q, us, left=0.0)) if t > 0 else 1.0
    return {"median": float(np.interp(0.5, us, q)), "mean": float(xs.mean()), "sd": float(xs.std()),
            "floor": float(np.interp(0.1, us, q)), "ceiling": float(np.interp(0.9, us, q)),
            "boom": 100 * p_at_least(boom_mult * k) if k else 0.0,
            "bust": 100 * (1 - p_at_least(bust_mult * k)) if k else 0.0}
