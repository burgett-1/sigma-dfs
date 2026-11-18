"""Slate-level estimates: ownership %, optimal-lineup rate (opt%) and leverage.

There is no free ownership feed, so ownership is modelled from what drives
it in practice: value (pts per $1K), raw projection and salary tier. DK
classic lineups have 8 slots, so ownership over a slate sums to ~800%.
"""
from __future__ import annotations

import numpy as np

from .config import DK_ROSTER_SLOTS, DK_SALARY_CAP, DK_SLOT_OK, MODEL


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


def optimal_rates(rows: list[dict], sims: int | None = None, seed: int = 7) -> None:
    """Fraction of simulated slates in which each player is in the best lineup
    (greedy value fill + one-swap improvement — fast and close to optimal)."""
    sims = sims or MODEL["opt_sims"]
    live = [r for r in rows if r["proj"] > 0 and r["salary"] > 0]
    for r in rows:
        r["opt_pct"] = 0.0
        r["lev"] = 0.0
    if len(live) < 8:
        return
    rng = np.random.default_rng(seed)
    n = len(live)
    mu = np.array([r["proj"] for r in live])
    sd = np.array([r["std_dev"] for r in live])
    sal = np.array([r["salary"] for r in live])
    pos = [set(r["position"].split("/")) for r in live]
    counts = np.zeros(n)
    min_sal = sal.min()
    done = 0
    for _ in range(sims):
        score = np.maximum(0, rng.normal(mu, sd))
        order = np.argsort(-score / sal)  # fill by value, then upgrade with swaps
        lu: list[int] = []
        spent = 0
        for i in order:
            left = 8 - len(lu) - 1
            if spent + sal[i] + left * min_sal > DK_SALARY_CAP:
                continue
            if not _assignable([pos[j] for j in lu] + [pos[i]]):
                continue
            lu.append(i)
            spent += sal[i]
            if len(lu) == 8:
                break
        if len(lu) < 8:
            continue
        # one pass of best single swaps
        improved = True
        passes = 0
        while improved and passes < 6:
            improved = False
            passes += 1
            for a_idx in range(8):
                a = lu[a_idx]
                budget = DK_SALARY_CAP - spent + sal[a]
                gain = score - score[a]
                ok = (sal <= budget) & (gain > 1e-9)
                ok[lu] = False
                cand = np.nonzero(ok)[0]
                if not len(cand):
                    continue
                for b in cand[np.argsort(-gain[cand])][:12]:
                    trial = [pos[j] for j in lu]
                    trial[a_idx] = pos[b]
                    if _assignable(trial):
                        spent += sal[b] - sal[a]
                        lu[a_idx] = int(b)
                        improved = True
                        break
        counts[lu] += 1
        done += 1
    if done == 0:
        return
    for r, c in zip(live, counts):
        r["opt_pct"] = float(100.0 * c / done)
        r["lev"] = r["opt_pct"] - r["own"]
