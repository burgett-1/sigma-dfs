"""Slate-level estimates: ownership %, optimal-lineup rate (opt%) and leverage.

There is no free ownership feed, so ownership is modelled from what drives
it in practice: value (pts per $1K), raw projection and salary tier. DK
classic lineups have 8 slots, so ownership over a slate sums to ~800%.
"""
from __future__ import annotations

import numpy as np

from .config import DK_ROSTER_SLOTS, DK_SLOT_OK, MODEL


def _z(x: np.ndarray) -> np.ndarray:
    sd = x.std()
    return (x - x.mean()) / sd if sd > 0 else np.zeros_like(x)


def estimate_ownership(rows: list[dict], roster_size: int = 8, own_cal: dict | None = None) -> None:
    live = [r for r in rows if r["proj"] > 0]
    for r in rows:
        r["own"] = 0.0
    if not live:
        return
    value = np.array([r["value"] for r in live])
    proj = np.array([r["proj"] for r in live])
    q = np.array([1.0 if r.get("status") == "QUESTIONABLE" else 0.0 for r in live])
    bump = np.array([r.get("bump", 0.0) for r in live])
    logit = 1.55 * _z(value) + 0.85 * _z(proj) + 2.0 * bump - 0.5 * q
    w = np.exp(logit)
    total = 100.0 * roster_size          # each roster spot adds 100% of ownership
    own = w / w.sum() * total
    # small correction learned from your own DraftKings contest exports (real %Drafted
    # vs this model), shrunk toward no change while the sample is small (contests.py)
    if own_cal is None:
        from . import contests
        own_cal = contests.load_own_calibration()
    if own_cal.get("used") and own_cal.get("slope"):
        own = np.exp(float(own_cal.get("intercept", 0.0)) + float(own_cal["slope"]) * np.log(own + 0.5)) - 0.5
        own = np.maximum(own, 0.01)
        own = own / own.sum() * total
    # a single player cannot exceed ~80% and a few rounds of capping keep the total at 800
    for _ in range(25):
        over = own > 80.0 + 1e-9
        if not over.any():
            break
        extra = (own[over] - 80.0).sum()
        own[over] = 80.0
        own[~over] += extra * own[~over] / own[~over].sum()
    for r, o in zip(live, own):
        r["own"] = float(o)


def _assignable(pos_lists: list[set[str]]) -> bool:
    """Can these 8 players fill PG/SG/SF/PF/C/G/F/UTIL? (small bipartite match)"""
    match: dict[int, int] = {}

    def try_slot(i: int, seen: set[int]) -> bool:
        for s, slot in enumerate(DK_ROSTER_SLOTS):
            if s in seen or not (pos_lists[i] & DK_SLOT_OK[slot]):
                continue
            seen.add(s)
            if s not in match or try_slot(match[s], seen):
                match[s] = i
                return True
        return False

    return all(try_slot(i, set()) for i in range(len(pos_lists)))


def optimal_rates(rows: list[dict], kind: str = "classic", rules: dict | None = None,
                  corr: dict | None = None, sims: int | None = None, seed: int = 7) -> None:
    """opt% = share of simulated slates in which the player is in the best lineup;
    leverage = opt% - projected ownership (negative = more popular than he is optimal).

    Uses the same engine as lineup building: correlated outcomes drawn from each
    player's learned distribution (questionable players sometimes sit), the slate's
    DraftKings rules (classic positions / showdown captain), solved per simulation.
    Every player gets a value, including those never optimal (opt 0, leverage -own)."""
    from . import sim
    from .config import SIM
    rules = rules or {}
    corr = corr or SIM["default_corr"]
    sims = sims or MODEL["opt_sims"]
    for r in rows:
        r["opt_pct"] = 0.0
        r["lev"] = -float(r.get("own", 0.0))
    pool = sim.build_pool(rows, kind)
    if len(pool.mu) < (6 if kind == "showdown" else 8):
        return
    rng = np.random.default_rng(seed)
    counts = np.zeros(len(pool.mu))
    done = 0
    for start in range(0, sims, 250):
        X = sim.sample_scores(pool, min(250, sims - start), rng, corr).astype(np.float64)
        for score in X:
            lu = sim.showdown_lineup(score, pool, rules) if kind == "showdown" else sim.best_lineup(score, pool, rules)
            if not lu:
                continue
            counts[list(lu)] += 1
            done += 1
    if not done:
        return
    pct = {int(i): 100.0 * c / done for i, c in zip(pool.ids, counts)}
    for r in rows:
        r["opt_pct"] = float(pct.get(int(r.get("draftable_id") or 0), 0.0))
        r["lev"] = r["opt_pct"] - float(r.get("own", 0.0))
