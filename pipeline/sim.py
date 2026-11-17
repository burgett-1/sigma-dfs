"""Contest simulation engine.

1. Player outcomes are simulated jointly, not independently. Each player's score
   shares a game factor (pace/total, both teams), a team factor and an
   individual term. The correlations are measured from the historical box
   scores (`calibrate_correlation`). Scores are right-skewed (gamma), and
   Questionable players can score 0 (did not play).
2. 10,000 unique lineups are built, each optimal for one simulated slate
   (respecting DK rules plus the historical winning-lineup constraints).
3. A field of opponent lineups is sampled from projected ownership.
4. Your 10,000 lineups and the field play 1,000,000 simulated contests.
   Win% = share of contests a lineup finishes first. Lineups are ranked by it.
"""
from __future__ import annotations

import logging
import math
import multiprocessing as mp
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .config import DK_SALARY_CAP, SIM

log = logging.getLogger(__name__)

POS = ("PG", "SG", "SF", "PF", "C")
SLOTS = ("PG", "SG", "SF", "PF", "C", "G", "F", "UTIL")
SLOT_MASK = {  # bitmask of positions allowed in each slot
    "PG": 1, "SG": 2, "SF": 4, "PF": 8, "C": 16, "G": 3, "F": 12, "UTIL": 31,
}


def pos_code(position: str) -> int:
    code = 0
    for p in (position or "").upper().split("/"):
        if p in POS:
            code |= 1 << POS.index(p)
    return code


@lru_cache(maxsize=None)
def can_fill(codes: tuple) -> bool:
    """Can players with these position codes (sorted tuple, <= 8) occupy
    distinct DK slots? Memoised: only a few dozen distinct combos occur."""
    match: dict[int, int] = {}

    def try_player(i: int, seen: set) -> bool:
        for s, slot in enumerate(SLOTS):
            if s in seen or not (codes[i] & SLOT_MASK[slot]):
                continue
            seen.add(s)
            if s not in match or try_player(match[s], seen):
                match[s] = i
                return True
        return False

    return all(try_player(i, set()) for i in range(len(codes)))


@dataclass
class SlatePool:
    ids: np.ndarray        # draftable ids
    names: list
    mu: np.ndarray
    sd: np.ndarray
    sal: np.ndarray
    code: np.ndarray       # position bitmask
    team: np.ndarray       # team index
    game: np.ndarray       # game index
    own: np.ndarray        # projected ownership %
    p_play: np.ndarray     # probability the player suits up
    start: np.ndarray      # game start (epoch seconds) for late-swap slot order
    n_team: int
    n_game: int
    kind: str = "classic"
    cpt_sal: np.ndarray | None = None   # showdown captain salary (1.5x)
    cpt_ids: np.ndarray | None = None   # showdown captain draftable ids


def build_pool(rows: list[dict], kind: str = "classic") -> SlatePool:
    live = [r for r in rows if r.get("proj", 0) >= SIM["min_proj"] and r.get("salary", 0) > 0
            and (pos_code(r.get("position", "")) or kind == "showdown")
            and (kind != "showdown" or r.get("cpt_id"))]
    teams = sorted({r["team_dk"] for r in live})
    games = sorted({r.get("competition_id") or "|".join(sorted((r["team_dk"], r["opp_dk"]))) for r in live},
                   key=str)
    t_idx = {t: i for i, t in enumerate(teams)}
    g_idx = {g: i for i, g in enumerate(games)}
    p_play = []
    for r in live:
        st = r.get("status", "")
        p_play.append(SIM["play_prob"].get(st, 1.0))
    start = []
    for r in live:
        s = r.get("start_utc")
        start.append(s.timestamp() if hasattr(s, "timestamp") else 0.0)
    return SlatePool(
        ids=np.array([r.get("draftable_id") or 0 for r in live], dtype=np.int64),
        names=[r["name"] for r in live],
        mu=np.array([r["proj"] for r in live], dtype=np.float64),
        sd=np.array([max(r.get("std_dev", 0), 0.15 * r["proj"]) for r in live], dtype=np.float64),
        sal=np.array([r["salary"] for r in live], dtype=np.int64),
        code=np.array([pos_code(r["position"]) for r in live], dtype=np.int64),
        team=np.array([t_idx[r["team_dk"]] for r in live], dtype=np.int64),
        game=np.array([g_idx[r.get("competition_id") or "|".join(sorted((r["team_dk"], r["opp_dk"])))]
                       for r in live], dtype=np.int64),
        own=np.array([max(r.get("own", 0.0), 0.05) for r in live], dtype=np.float64),
        p_play=np.array(p_play, dtype=np.float64),
        start=np.array(start, dtype=np.float64),
        n_team=len(teams), n_game=len(games), kind=kind,
        cpt_sal=np.array([r.get("cpt_salary") or int(r["salary"] * 1.5) for r in live], dtype=np.int64),
        cpt_ids=np.array([r.get("cpt_id") or 0 for r in live], dtype=np.int64),
    )


# ─────────────────────────────────────────────────────────────────────────────
# correlated outcome simulation
# ─────────────────────────────────────────────────────────────────────────────
def sample_scores(pool: SlatePool, n: int, rng: np.random.Generator, corr: dict) -> np.ndarray:
    """n x P matrix of simulated DK points (float32)."""
    P = len(pool.mu)
    rho_game = min(max(corr.get("opp", 0.04), 0.0), 0.3)
    rho_team_extra = min(max(corr.get("team", 0.04) - rho_game, 0.0), 0.3)
    a, b = math.sqrt(rho_game), math.sqrt(rho_team_extra)
    c = math.sqrt(max(1e-6, 1.0 - rho_game - rho_team_extra))
    z = rng.standard_normal((n, P), dtype=np.float32) * c
    if a > 0:
        z += a * rng.standard_normal((n, pool.n_game), dtype=np.float32)[:, pool.game]
    if b > 0:
        z += b * rng.standard_normal((n, pool.n_team), dtype=np.float32)[:, pool.team]
    # gamma marginals (right skew) via the Wilson-Hilferty transform
    mu = pool.mu.astype(np.float32)
    k = np.maximum((pool.mu / pool.sd) ** 2, 1.0).astype(np.float32)
    h = (1.0 / (9.0 * k)).astype(np.float32)
    x = mu * np.maximum(1.0 - h + z * np.sqrt(h), 0.0) ** 3
    if (pool.p_play < 1).any():
        sits = rng.random((n, P), dtype=np.float32) > pool.p_play.astype(np.float32)
        x[sits] = 0.0
    return x


# ─────────────────────────────────────────────────────────────────────────────
# lineup optimisation for one simulated slate
# ─────────────────────────────────────────────────────────────────────────────
def best_lineup(score: np.ndarray, pool: SlatePool, rules: dict) -> tuple | None:
    """Near-optimal legal lineup for one score vector: value-ordered greedy fill,
    then best single swaps until no improvement."""
    sal, code, team = pool.sal, pool.code, pool.team
    max_team = rules.get("max_per_team", 4)
    min_sal = rules.get("min_salary", 0)
    min_cost = int(np.sort(sal)[:8].sum() / 8)
    order = np.argsort(-score / sal)
    lu: list[int] = []
    spent = 0
    tcount: dict[int, int] = {}
    for i in order:
        if score[i] <= 0:
            break
        left = 7 - len(lu)
        if spent + sal[i] + left * min_cost > DK_SALARY_CAP:
            continue
        t = int(team[i])
        if tcount.get(t, 0) >= max_team:
            continue
        if not can_fill(tuple(sorted([int(code[j]) for j in lu] + [int(code[i])]))):
            continue
        lu.append(int(i))
        spent += int(sal[i])
        tcount[t] = tcount.get(t, 0) + 1
        if len(lu) == 8:
            break
    if len(lu) < 8:
        return None
    for _ in range(8):
        spent = int(sal[lu].sum())
        tcount = {}
        for j in lu:
            tcount[int(team[j])] = tcount.get(int(team[j]), 0) + 1
        improved = False
        for a_idx in range(8):
            a = lu[a_idx]
            budget = DK_SALARY_CAP - spent + sal[a]
            gain = score - score[a]
            ok = (sal <= budget) & (gain > 1e-6)
            ok[lu] = False
            cand = np.nonzero(ok)[0]
            if not len(cand):
                continue
            rest = [int(code[j]) for k, j in enumerate(lu) if k != a_idx]
            for bb in cand[np.argsort(-gain[cand])][:20]:
                b = int(bb)
                tb = int(team[b])
                if tb != int(team[a]) and tcount.get(tb, 0) >= max_team:
                    continue
                if not can_fill(tuple(sorted(rest + [int(code[b])]))):
                    continue
                spent += int(sal[b] - sal[a])
                tcount[int(team[a])] -= 1
                tcount[tb] = tcount.get(tb, 0) + 1
                lu[a_idx] = b
                improved = True
                break
        if improved:
            continue
        # two-for-two swaps: the move single swaps miss (e.g. stud + punt -> two mid-priced)
        if not _pair_swaps(lu, score, pool, max_team):
            break
    spent = int(sal[lu].sum())
    if spent < min_sal:
        return None
    return tuple(sorted(lu))


def _pair_swaps(lu: list[int], score: np.ndarray, pool: SlatePool, max_team: int, k: int = 24) -> bool:
    """Try replacing two lineup players with the best affordable legal pair.
    Mutates lu; returns True if it improved."""
    sal, code, team = pool.sal, pool.code, pool.team
    spent = int(sal[lu].sum())
    in_lu = np.zeros(len(score), dtype=bool)
    in_lu[lu] = True
    best = None
    best_gain = 1e-6
    for a in range(8):
        for b in range(a + 1, 8):
            pa, pb = lu[a], lu[b]
            budget = DK_SALARY_CAP - spent + sal[pa] + sal[pb]
            base = score[pa] + score[pb]
            ok = (~in_lu) & (sal <= budget)
            cand = np.nonzero(ok)[0]
            if len(cand) < 2:
                continue
            cand = cand[np.argsort(-score[cand])][:k]
            sc = score[cand]
            tot = sc[:, None] + sc[None, :]
            fits = (sal[cand][:, None] + sal[cand][None, :]) <= budget
            np.fill_diagonal(fits, False)
            tot = np.where(fits, tot, -1.0)
            if tot.max() - base <= best_gain:
                continue
            rest = [int(code[j]) for kk, j in enumerate(lu) if kk not in (a, b)]
            flat = np.argsort(-tot, axis=None)
            for f in flat[: 60]:
                i, j = divmod(int(f), len(cand))
                if i >= j or tot[i, j] - base <= best_gain:
                    if tot.flat[f] - base <= best_gain:
                        break
                    continue
                ci, cj = int(cand[i]), int(cand[j])
                if not can_fill(tuple(sorted(rest + [int(code[ci]), int(code[cj])]))):
                    continue
                counts: dict[int, int] = {}
                for kk, jj in enumerate(lu):
                    if kk not in (a, b):
                        counts[int(team[jj])] = counts.get(int(team[jj]), 0) + 1
                counts[int(team[ci])] = counts.get(int(team[ci]), 0) + 1
                counts[int(team[cj])] = counts.get(int(team[cj]), 0) + 1
                if max(counts.values()) > max_team:
                    continue
                best, best_gain = (a, b, ci, cj), tot[i, j] - base
                break
    if best is None:
        return False
    a, b, ci, cj = best
    lu[a], lu[b] = ci, cj
    return True


# Hall's theorem: 8 players fit DK's 8 slots iff, for every set U of positions,
# at least need(U) chosen players can play a position in U, where need(U) =
# number of slots whose eligible positions all lie inside U. (Verified against
# brute-force matching for every position combination in the tests.)
HALL_NEED = {U: sum(1 for sl in SLOTS if (SLOT_MASK[sl] & ~U) == 0) for U in range(1, 31)}


def exact_lineup(score: np.ndarray, pool: SlatePool, rules: dict, start: tuple | None,
                 k: int = 32) -> tuple | None:
    """Exact optimum (HiGHS MIP) over the players who could plausibly be in it:
    top-k by points, top-k by value, top-5 at each position, plus the warm start."""
    import highspy

    cand = set(np.argsort(-score)[:k].tolist()) | set(np.argsort(-score / pool.sal)[:k].tolist())
    for b in range(5):
        m = np.nonzero(pool.code & (1 << b))[0]
        cand |= set(m[np.argsort(-score[m])][:5].tolist())
    if start:
        cand |= set(start)
    cand = np.array(sorted(cand))
    n = len(cand)
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.addVars(n, np.zeros(n), np.ones(n))
    h.changeColsIntegrality(n, np.arange(n, dtype=np.int32),
                            np.array([highspy.HighsVarType.kInteger] * n))

    def row(idx, coef, lo, hi):
        h.addRow(lo, hi, len(idx), np.asarray(idx, dtype=np.int32), np.asarray(coef, dtype=float))

    code = pool.code[cand]
    row(np.arange(n), np.ones(n), 8, 8)
    for U, need in HALL_NEED.items():
        if need:
            idx = np.nonzero(code & U)[0]
            row(idx, np.ones(len(idx)), need, highspy.kHighsInf)
    row(np.arange(n), pool.sal[cand].astype(float), rules.get("min_salary", 0), DK_SALARY_CAP)
    max_team = rules.get("max_per_team", 8)
    for t in set(pool.team[cand].tolist()):
        idx = np.nonzero(pool.team[cand] == t)[0]
        if len(idx) > max_team:
            row(idx, np.ones(len(idx)), 0, max_team)
    max_game = rules.get("max_per_game", 8)
    for g in set(pool.game[cand].tolist()):
        idx = np.nonzero(pool.game[cand] == g)[0]
        if len(idx) > max_game:
            row(idx, np.ones(len(idx)), 0, max_game)
    h.changeColsCost(n, np.arange(n, dtype=np.int32), score[cand].astype(float))
    h.changeObjectiveSense(highspy.ObjSense.kMaximize)
    if start:
        sol = highspy.HighsSolution()
        sol.col_value = [1.0 if c in start else 0.0 for c in cand]
        sol.value_valid = True
        h.setSolution(sol)
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return start
    x = np.asarray(h.getSolution().col_value)
    lu = tuple(sorted(int(c) for c, v in zip(cand, x) if v > 0.5))
    return lu if len(lu) == 8 else start


# ── showdown: 1 captain (1.5x points, 1.5x salary) + 5 UTIL, both teams used ──
def showdown_lineup(score: np.ndarray, pool: SlatePool, rules: dict) -> tuple | None:
    """Exact best showdown lineup for one score vector. Returned as
    (captain, *sorted utils)."""
    import highspy

    P = len(score)
    if P < 6:
        return None
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    n = 2 * P                                   # [0,P) util, [P,2P) captain
    h.addVars(n, np.zeros(n), np.ones(n))
    h.changeColsIntegrality(n, np.arange(n, dtype=np.int32), np.array([highspy.HighsVarType.kInteger] * n))

    def row(idx, coef, lo, hi):
        h.addRow(lo, hi, len(idx), np.asarray(idx, dtype=np.int32), np.asarray(coef, dtype=float))

    row(np.arange(P), np.ones(P), 5, 5)
    row(np.arange(P, n), np.ones(P), 1, 1)
    for i in range(P):
        row([i, P + i], [1, 1], 0, 1)
    row(np.arange(n), np.concatenate([pool.sal, pool.cpt_sal]).astype(float),
        rules.get("min_salary_showdown", 0), DK_SALARY_CAP)
    for t in set(pool.team.tolist()):
        idx = np.nonzero(pool.team == t)[0]
        row(np.concatenate([idx, idx + P]), np.ones(2 * len(idx)), 1, 5)   # both teams represented
    h.changeColsCost(n, np.arange(n, dtype=np.int32), np.concatenate([score, 1.5 * score]).astype(float))
    h.changeObjectiveSense(highspy.ObjSense.kMaximize)
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return None
    x = np.asarray(h.getSolution().col_value)
    utils = sorted(int(i) for i in np.nonzero(x[:P] > 0.5)[0])
    cpt = [int(i) for i in np.nonzero(x[P:] > 0.5)[0]]
    if len(utils) != 5 or len(cpt) != 1:
        return None
    return (cpt[0], *utils)


def _gen_chunk(args) -> list[tuple]:
    pool, rules, corr, n, seed, exact = args
    rng = np.random.default_rng(seed)
    scores = sample_scores(pool, n, rng, corr).astype(np.float64)
    out = []
    for s in scores:
        if pool.kind == "showdown":
            lu = showdown_lineup(s, pool, rules)
            if lu:
                out.append(lu)
            continue
        warm = best_lineup(s, pool, {**rules, "min_salary": 0} if exact else rules)
        lu = exact_lineup(s, pool, rules, warm) if exact and warm else warm
        if lu:
            out.append(lu)
    return out


def generate_lineups(pool: SlatePool, target: int, rules: dict, corr: dict, seed: int = 1,
                     time_limit: float = 240.0, workers: int | None = None, exact: bool = True) -> list[tuple]:
    """Unique lineups, each optimal for some simulated outcome of the slate."""
    workers = workers or min(4, os.cpu_count() or 1)
    found: dict[tuple, int] = {}
    t0 = time.time()
    chunk = 250
    round_ = 0
    ctx = mp.get_context("forkserver" if "forkserver" in mp.get_all_start_methods() else "spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        while len(found) < target and time.time() - t0 < time_limit:
            jobs = [(pool, rules, corr, chunk, seed * 1_000_003 + round_ * 97 + w, exact) for w in range(workers)]
            round_ += 1
            for res in ex.map(_gen_chunk, jobs):
                for lu in res:
                    found[lu] = found.get(lu, 0) + 1
            if round_ > 6 and len(found) < 50:
                break
    # most frequently optimal first, so truncation keeps the strongest
    ranked = sorted(found, key=lambda k: -found[k])
    return ranked[:target]


def sample_field(pool: SlatePool, n: int, rng: np.random.Generator, rules: dict) -> list[tuple]:
    """Opponent lineups built the way the public drafts: ownership-weighted
    picks, salary mostly spent."""
    if pool.kind == "showdown":
        return _sample_field_showdown(pool, n, rng)
    w = np.log(pool.own / 100.0 + 1e-6)
    out = []
    min_cost = int(np.sort(pool.sal)[:8].sum() / 8)
    tries = 0
    while len(out) < n and tries < n * 20:
        tries += 1
        keys = w + rng.gumbel(size=len(w))
        order = np.argsort(-keys)
        lu, spent = [], 0
        for i in order:
            left = 7 - len(lu)
            if spent + pool.sal[i] + left * min_cost > DK_SALARY_CAP:
                continue
            if not can_fill(tuple(sorted([int(pool.code[j]) for j in lu] + [int(pool.code[i])]))):
                continue
            lu.append(int(i))
            spent += int(pool.sal[i])
            if len(lu) == 8:
                break
        if len(lu) == 8 and spent >= SIM["field_min_salary"]:
            out.append(tuple(sorted(lu)))
    return out


def _sample_field_showdown(pool: SlatePool, n: int, rng: np.random.Generator) -> list[tuple]:
    w = np.log(pool.own / 100.0 + 1e-6)
    out = []
    tries = 0
    while len(out) < n and tries < n * 30:
        tries += 1
        keys = w + rng.gumbel(size=len(w))
        order = list(np.argsort(-keys))
        cpt = order[0]
        spent = int(pool.cpt_sal[cpt])
        utils = []
        for i in order[1:]:
            if spent + pool.sal[i] > DK_SALARY_CAP:
                continue
            utils.append(int(i))
            spent += int(pool.sal[i])
            if len(utils) == 5:
                break
        if len(utils) == 5 and len(set(pool.team[[cpt] + utils].tolist())) == 2 and spent >= 45000:
            out.append((int(cpt), *sorted(utils)))
    return out


def lineup_weights(lu: tuple, kind: str) -> list[tuple[int, float]]:
    """(player index, scoring multiplier) for a lineup."""
    if kind == "showdown":
        return [(lu[0], 1.5)] + [(i, 1.0) for i in lu[1:]]
    return [(i, 1.0) for i in lu]


# ─────────────────────────────────────────────────────────────────────────────
# the contest
# ─────────────────────────────────────────────────────────────────────────────
def run_contest(pool: SlatePool, lineups: list[tuple], field: list[tuple], sims: int,
                corr: dict, seed: int = 11, batch: int = 20_000) -> dict:
    """Simulate `sims` contests between your lineups and the field."""
    allu = lineups + field
    # only players who appear in some lineup need simulating
    used = sorted({i for lu in allu for i in lu})
    remap = {p: k for k, p in enumerate(used)}
    kind = pool.kind
    pool = subpool(pool, used)
    P = len(used)
    M = np.zeros((P, len(allu)), dtype=np.float32)
    for j, lu in enumerate(allu):
        for i, w in lineup_weights(lu, kind):
            M[remap[i], j] = w
    L = len(lineups)
    wins = np.zeros(len(allu), dtype=np.float64)
    xs = np.zeros(P, dtype=np.float64)        # sum of player scores
    xtx = np.zeros((P, P), dtype=np.float64)  # sum of player score products
    rng = np.random.default_rng(seed)
    done = 0
    t0 = time.time()
    while done < sims:
        n = min(batch, sims - done)
        X = sample_scores(pool, n, rng, corr)
        S = X @ M
        w = S.argmax(axis=1)
        wins += np.bincount(w, minlength=len(allu))
        xs += X.sum(axis=0, dtype=np.float64)
        xtx += (X.T @ X).astype(np.float64)
        done += n
    # lineup mean and spread follow exactly from the player-level moments
    Ml = M[:, :L].astype(np.float64)
    mean = (xs / sims) @ Ml
    ex2 = np.einsum("pl,pl->l", Ml, (xtx / sims) @ Ml)
    sd = np.sqrt(np.maximum(ex2 - mean ** 2, 0))
    log.info("contest: %d sims x %d lineups (+%d field) in %.1fs", sims, L, len(field), time.time() - t0)
    return {"win": wins[:L] / sims * 100.0, "mean": mean, "sd": sd,
            "field_win": float(wins[L:].sum() / sims * 100.0)}


def subpool(pool: SlatePool, idx: list[int]) -> SlatePool:
    ix = np.asarray(idx)
    return SlatePool(ids=pool.ids[ix], names=[pool.names[i] for i in ix], mu=pool.mu[ix], sd=pool.sd[ix],
                     sal=pool.sal[ix], code=pool.code[ix], team=pool.team[ix], game=pool.game[ix],
                     own=pool.own[ix], p_play=pool.p_play[ix], start=pool.start[ix],
                     n_team=pool.n_team, n_game=pool.n_game, kind=pool.kind,
                     cpt_sal=pool.cpt_sal[ix], cpt_ids=pool.cpt_ids[ix])


def slot_order(lu: tuple, pool: SlatePool) -> list[int] | None:
    """Assign players to PG..UTIL. Later games go to the flex slots (UTIL, F, G)
    so late swap keeps the most options."""
    players = sorted(lu, key=lambda i: -pool.start[i])
    fill_order = ["UTIL", "F", "G", "C", "PF", "SF", "SG", "PG"]
    res: dict[str, int] = {}

    def bt(k: int, used: set) -> bool:
        if k == len(fill_order):
            return True
        slot = fill_order[k]
        for i in players:
            if i in used or not (pool.code[i] & SLOT_MASK[slot]):
                continue
            used.add(i)
            res[slot] = i
            if bt(k + 1, used):
                return True
            used.discard(i)
        return False

    if not bt(0, set()):
        return None
    return [res[s] for s in SLOTS]


def build_slate_lineups(rows: list[dict], rules: dict, corr: dict, n_lineups: int | None = None,
                        sims: int | None = None, field_size: int | None = None, seed: int = 1,
                        exact: bool = True, kind: str = "classic") -> dict:
    n_lineups = n_lineups or SIM["lineups"]
    sims = sims or SIM["sims"]
    field_size = field_size or SIM["field_lineups"]
    pool = build_pool(rows, kind)
    if len(pool.mu) < (6 if kind == "showdown" else 8):
        return {"lineups": [], "sims": 0, "kind": kind}
    t0 = time.time()
    lineups = generate_lineups(pool, n_lineups, rules, corr, seed=seed,
                               time_limit=SIM["generate_time_limit"] if kind == "classic" else 90, exact=exact)
    t1 = time.time()
    field = sample_field(pool, field_size, np.random.default_rng(seed + 5), rules)
    res = run_contest(pool, lineups, field, sims, corr, seed=seed + 9)
    t2 = time.time()
    order = np.argsort(-res["win"], kind="stable")
    out = []
    for rank, j in enumerate(order, 1):
        lu = lineups[j]
        if kind == "showdown":
            ids = [int(pool.cpt_ids[lu[0]])] + [int(pool.ids[i]) for i in lu[1:]]
            proj = 1.5 * pool.mu[lu[0]] + pool.mu[list(lu[1:])].sum()
            salary = int(pool.cpt_sal[lu[0]] + pool.sal[list(lu[1:])].sum())
        else:
            slots = slot_order(lu, pool)
            if slots is None:
                continue
            ids = [int(pool.ids[i]) for i in slots]
            proj = pool.mu[list(lu)].sum()
            salary = int(pool.sal[list(lu)].sum())
        out.append({
            "rank": rank,
            "ids": ids,
            "win": round(float(res["win"][j]), 5),
            "mean": round(float(res["mean"][j]), 2),
            "sd": round(float(res["sd"][j]), 2),
            "proj": round(float(proj), 2),
            "salary": salary,
            "own": round(float(pool.own[list(lu)].sum()), 1),
        })
    log.info("built %d lineups in %.0fs, contest %.0fs", len(lineups), t1 - t0, t2 - t1)
    return {"lineups": out, "sims": sims, "field_size": len(field), "exact": exact, "kind": kind,
            "slots": ["CPT", "UTIL", "UTIL", "UTIL", "UTIL", "UTIL"] if kind == "showdown" else list(SLOTS), "field_win_pct": res["field_win"],
            "generate_seconds": round(t1 - t0, 1), "contest_seconds": round(t2 - t1, 1),
            "correlation": corr, "rules": rules}
