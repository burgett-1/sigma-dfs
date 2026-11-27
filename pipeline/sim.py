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
   Win% = share of contests a lineup finishes first (split with any identical
   field lineup). Lineups are ranked by it.
5. Portfolio: lineups are also ordered greedily by how many extra simulated
   contests each adds in which at least one of your picks beats the whole field,
   so "Like best N" builds around the chalk but spreads into the low-owned
   outcomes that actually win contests.

Every roster rule comes from a SlotSpec (pipeline/sports/slots.py, built from the
sport registry). Without one, the NBA spec is used, which reproduces the
original literals exactly (8 slots, PG..UTIL masks, $50k cap). Sports whose
registry kind sets `enforce_rules` also get DraftKings' game/team minimums and
the registry stack rules; sports with a factor correlation model sample with
per-position factor loadings instead of the two-scalar game/team model.
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

from .config import SIM
from .distribution import U_SAMPLE as _U_SAMPLE
from .sports import slots as S
from .sports.slots import SlotSpec

log = logging.getLogger(__name__)

NBA_CLASSIC = S.spec("NBA", "classic")
NBA_SHOWDOWN = S.spec("NBA", "showdown")
# legacy module names, bound to the NBA classic spec (tests and old callers use them)
POS = NBA_CLASSIC.positions
SLOTS = NBA_CLASSIC.labels
SLOT_MASK = dict(zip(NBA_CLASSIC.labels, NBA_CLASSIC.masks))   # bitmask of positions allowed in each slot


def nba_spec(kind: str = "classic") -> SlotSpec:
    return NBA_SHOWDOWN if kind == "showdown" else NBA_CLASSIC


def pos_code(position: str, spec: SlotSpec | None = None) -> int:
    """Position bitmask; unknown positions add nothing (0 = fits no slot)."""
    return S.pos_code(position or "", spec or NBA_CLASSIC)


@lru_cache(maxsize=None)
def can_fill(codes: tuple, masks: tuple | None = None) -> bool:
    """Can players with these position codes (sorted tuple) occupy distinct slots
    (default: DK NBA classic)? Memoised: only a few dozen distinct combos occur."""
    return S.can_fill(codes, masks or NBA_CLASSIC.masks)


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
    q: np.ndarray | None = None         # outcome percentiles per player at distribution.U_SAMPLE
    med: np.ndarray | None = None       # median (what "Proj" shows)
    spec: SlotSpec | None = None        # roster rules (None = NBA for `kind`)
    load: np.ndarray | None = None      # P x K factor loadings (factor correlation model)
    idio: np.ndarray | None = None      # P idiosyncratic scale, sqrt(1 - sum load^2)
    opp: np.ndarray | None = None       # opponent team index (-1 unknown), for stack rules

    def __post_init__(self):
        if self.spec is None:
            self.spec = nba_spec(self.kind)


def _game_key(r: dict):
    return r.get("competition_id") or "|".join(sorted((r["team_dk"], r["opp_dk"])))


def build_pool(rows: list[dict], kind: str = "classic", spec: SlotSpec | None = None,
               loadings: dict | None = None) -> SlatePool:
    """Simulation arrays for the playable players of one slate. `loadings`
    overrides the registry's factor loadings (a fitted set, sports/corr.py)."""
    spec = spec or nba_spec(kind)
    legacy = spec.sport == "NBA"
    min_proj = SIM["min_proj"] if legacy else spec.min_proj
    play_prob = SIM["play_prob"] if legacy else spec.play_prob
    live = [r for r in rows if r.get("mean", r.get("proj", 0)) >= min_proj and r.get("salary", 0) > 0
            and (pos_code(r.get("position", ""), spec) or kind == "showdown")
            and (kind != "showdown" or r.get("cpt_id"))]
    teams = sorted({r["team_dk"] for r in live})
    games = sorted({_game_key(r) for r in live}, key=str)
    t_idx = {t: i for i, t in enumerate(teams)}
    g_idx = {g: i for i, g in enumerate(games)}
    p_play = []
    for r in live:
        st = r.get("status", "")
        p_play.append(play_prob.get(st, 1.0))
    start = []
    for r in live:
        s = r.get("start_utc")
        start.append(s.timestamp() if hasattr(s, "timestamp") else 0.0)
    load = idio = None
    if not legacy and _corr_model(spec.sport) == "factor":
        from .sports import corr as C
        _, load, idio = C.instances([{"position": r.get("position", ""), "team": r["team_dk"],
                                      "opp": r.get("opp_dk", ""), "game": _game_key(r)} for r in live],
                                    spec.sport, loadings=loadings)
    cm = spec.cpt_mult if spec.showdown else 1.5
    return SlatePool(
        ids=np.array([r.get("draftable_id") or 0 for r in live], dtype=np.int64),
        names=[r["name"] for r in live],
        mu=np.array([r.get("mean", r["proj"]) for r in live], dtype=np.float64),
        sd=np.array([max(r.get("std_dev", 0), 0.15 * r.get("mean", r["proj"])) for r in live], dtype=np.float64),
        med=np.array([r["proj"] for r in live], dtype=np.float64),
        q=(np.array([np.asarray(r["q"], dtype=np.float64) for r in live])
           if live and all(r.get("q") is not None for r in live) else None),
        sal=np.array([r["salary"] for r in live], dtype=np.int64),
        code=np.array([pos_code(r["position"], spec) for r in live], dtype=np.int64),
        team=np.array([t_idx[r["team_dk"]] for r in live], dtype=np.int64),
        game=np.array([g_idx[_game_key(r)] for r in live], dtype=np.int64),
        own=np.array([max(r.get("own", 0.0), 0.05) for r in live], dtype=np.float64),
        p_play=np.array(p_play, dtype=np.float64),
        start=np.array(start, dtype=np.float64),
        n_team=len(teams), n_game=len(games), kind=kind,
        cpt_sal=np.array([r.get("cpt_salary") or int(r["salary"] * cm) for r in live], dtype=np.int64),
        cpt_ids=np.array([r.get("cpt_id") or 0 for r in live], dtype=np.int64),
        spec=spec, load=load, idio=idio,
        opp=np.array([t_idx.get(r.get("opp_dk", ""), -1) for r in live], dtype=np.int64),
    )


def _corr_model(sport: str) -> str:
    from . import sports
    return (sports.get(sport).get("corr") or {}).get("model", "game_team")


# ─────────────────────────────────────────────────────────────────────────────
# correlated outcome simulation
# ─────────────────────────────────────────────────────────────────────────────
def _ndtr(z: np.ndarray) -> np.ndarray:
    """Standard normal CDF (Abramowitz-Stegun 7.1.26, error < 2e-7), float32."""
    x = np.abs(z, dtype=np.float32) / np.float32(math.sqrt(2.0))
    t = 1.0 / (1.0 + 0.3275911 * x)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    erf = 1.0 - poly * np.exp(-x * x)
    return (0.5 * (1.0 + np.sign(z) * erf)).astype(np.float32)


def sample_scores(pool: SlatePool, n: int, rng: np.random.Generator, corr: dict) -> np.ndarray:
    """n x P matrix of simulated DK points (float32)."""
    P = len(pool.mu)
    if pool.load is not None:
        return _marginals(pool, _factor_z(pool, n, rng), n, rng)
    rho_game = min(max(corr.get("opp", 0.04), 0.0), 0.3)
    rho_team_extra = min(max(corr.get("team", 0.04) - rho_game, 0.0), 0.3)
    a, b = math.sqrt(rho_game), math.sqrt(rho_team_extra)
    c = math.sqrt(max(1e-6, 1.0 - rho_game - rho_team_extra))
    z = rng.standard_normal((n, P), dtype=np.float32) * c
    if a > 0:
        z += a * rng.standard_normal((n, pool.n_game), dtype=np.float32)[:, pool.game]
    if b > 0:
        z += b * rng.standard_normal((n, pool.n_team), dtype=np.float32)[:, pool.team]
    return _marginals(pool, z, n, rng)


def _factor_z(pool: SlatePool, n: int, rng: np.random.Generator) -> np.ndarray:
    """Latent normals of the factor copula: z = F L^T + idio * e (unit variance)."""
    P, K = pool.load.shape
    z = rng.standard_normal((n, P), dtype=np.float32) * pool.idio.astype(np.float32)
    if K:
        z += rng.standard_normal((n, K), dtype=np.float32) @ pool.load.T.astype(np.float32)
    return z


def _marginals(pool: SlatePool, z: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """Correlated normals -> DK points: each player's learned curve (or gamma), then sit-outs."""
    P = len(pool.mu)
    if pool.q is not None:
        # each player's own outcome distribution, learned from history (distribution.py):
        # correlated normal -> percentile -> DK points, by linear interpolation of the curve
        u = _ndtr(z)
        grid = np.asarray(_U_SAMPLE, dtype=np.float32)
        j = np.clip(np.searchsorted(grid, u, side="right") - 1, 0, len(grid) - 2)
        w = (u - grid[j]) / (grid[j + 1] - grid[j])
        w = np.minimum(w, 1.0)          # beyond the 99.9% point: hold at the far-tail value
        Q = pool.q.astype(np.float32)
        cols = np.arange(P)[None, :]
        x = Q[cols, j] * (1 - w) + Q[cols, j + 1] * w
        if (pool.p_play < 1).any():
            sits = rng.random((n, P), dtype=np.float32) > pool.p_play.astype(np.float32)
            x[sits] = 0.0
        return x.astype(np.float32)
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
def rules_ok(lu, pool: SlatePool, stack: bool = True, partial: bool = False) -> bool:
    """DraftKings game/team minimums and the registry stack rules (only for specs
    with enforce_rules; NBA keeps its original checks). `stack=False` checks only
    what a public lineup must satisfy (games, teams, no DST facing its QB);
    `partial=True` checks an incomplete lineup (only rules it can already break)."""
    spec = pool.spec
    if not spec.enforce_rules:
        return True
    lu = [int(i) for i in lu]
    if partial:
        stack = False
    elif len({int(pool.team[i]) for i in lu}) < spec.min_teams:
        return False
    if spec.showdown:
        return True
    if not partial and len({int(pool.game[i]) for i in lu}) < spec.min_games:
        return False
    sr = spec.stack_rules
    if not sr:
        return True
    qb, dst = spec.bit("QB"), spec.bit("DST")
    catch = spec.bit("WR") | spec.bit("TE")
    skill = catch | spec.bit("RB")
    qbs = [i for i in lu if pool.code[i] & qb]
    if sr.get("no_dst_vs_qb") and dst and pool.opp is not None:
        for d in (i for i in lu if pool.code[i] & dst):
            if any(int(pool.opp[q]) == int(pool.team[d]) for q in qbs):
                return False
    if not stack:
        return True
    m, b = int(sr.get("qb_stack_min", 0)), int(sr.get("bring_back_min", 0))
    for q in qbs:
        if m and sum(1 for j in lu if j != q and pool.code[j] & catch and pool.team[j] == pool.team[q]) < m:
            return False
        if b and pool.opp is not None and \
                sum(1 for j in lu if pool.code[j] & skill and pool.team[j] == pool.opp[q]) < b:
            return False
    return True


def best_lineup(score: np.ndarray, pool: SlatePool, rules: dict) -> tuple | None:
    """Near-optimal legal lineup for one score vector: value-ordered greedy fill,
    then best single swaps until no improvement. For specs with stack rules, a
    greedy lineup that breaks them is rebuilt around the best stacks."""
    if not pool.spec.enforce_rules:
        return _best_lineup(score, pool, rules)
    ok = lambda lu: rules_ok(lu, pool)        # noqa: E731
    lu = _best_lineup(score, pool, rules, rule_ok=ok)
    if lu is not None:
        return lu
    best, best_v = None, -np.inf
    for seed in _stack_seeds(score, pool):
        cand = _best_lineup(score, pool, rules, seed=seed, rule_ok=ok)
        if cand is not None and score[list(cand)].sum() > best_v:
            best, best_v = cand, float(score[list(cand)].sum())
    return best


def _completion_floor(pool: SlatePool, codes: tuple) -> int:
    """Lower bound on the salary still needed to fill the slots these players leave
    open: players are matched to the most restrictive slots first, then each open
    slot is priced at the cheapest pool player it accepts (k-th cheapest when
    several open slots accept the same positions). Memoised per pool."""
    cache = pool.__dict__.setdefault("_floor", {})
    if codes in cache:
        return cache[codes]
    masks = pool.spec.masks
    order = sorted(range(len(masks)), key=lambda s: bin(masks[s]).count("1"))
    match: dict[int, int] = {}

    def try_player(i: int, seen: set) -> bool:
        for s in order:
            if s in seen or not (codes[i] & masks[s]):
                continue
            seen.add(s)
            if s not in match or try_player(match[s], seen):
                match[s] = i
                return True
        return False

    for i in range(len(codes)):
        try_player(i, set())
    need: dict[int, int] = {}
    for s in range(len(masks)):
        if s not in match:
            need[masks[s]] = need.get(masks[s], 0) + 1
    prices = pool.__dict__.setdefault("_prices", {})
    total = 0
    for m, k in need.items():
        if m not in prices:
            prices[m] = np.sort(pool.sal[(pool.code & m) > 0])
        p = prices[m]
        total += int(p[:k].sum()) + (10 ** 7 if len(p) < k else 0)
    cache[codes] = total
    return total


def _stack_seeds(score: np.ndarray, pool: SlatePool, n_qb: int = 4) -> list[list[int]]:
    """Starting players for rule-satisfying greedy fills: each of the top QBs with
    his best same-team pass catchers (and bring-backs when required)."""
    spec, sr = pool.spec, pool.spec.stack_rules or {}
    qb = spec.bit("QB")
    if not qb:
        return [[]]
    catch = spec.bit("WR") | spec.bit("TE")
    skill = catch | spec.bit("RB")
    m, b = int(sr.get("qb_stack_min", 0)), int(sr.get("bring_back_min", 0))
    qbs = [int(i) for i in np.nonzero(pool.code & qb)[0]]
    qbs.sort(key=lambda i: -score[i])
    seeds = []
    for q in qbs[:n_qb]:
        mates = [int(j) for j in np.nonzero(((pool.code & catch) > 0) & (pool.team == pool.team[q]))[0]]
        mates.sort(key=lambda j: -score[j])
        back = [int(j) for j in np.nonzero(((pool.code & skill) > 0) & (pool.team == pool.opp[q]))[0]] \
            if pool.opp is not None else []
        back.sort(key=lambda j: -score[j])
        if len(mates) < m or len(back) < b:
            continue
        seeds.append([q] + mates[:m] + back[:b])
        if m and len(mates) > m:      # also try the stack with the next pass catcher
            seeds.append([q] + mates[1:m + 1] + back[:b])
    return seeds or [[]]


def _best_lineup(score: np.ndarray, pool: SlatePool, rules: dict, seed: list[int] | None = None,
                 rule_ok=None) -> tuple | None:
    sal, code, team = pool.sal, pool.code, pool.team
    spec = pool.spec
    size, cap, masks = spec.size, spec.cap, spec.masks
    max_team = rules.get("max_per_team", 4)
    min_sal = rules.get("min_salary", 0)
    min_cost = int(np.sort(sal)[:size].sum() / size)
    order = np.argsort(-score / sal)
    lu: list[int] = list(seed or [])
    spent = int(sal[lu].sum()) if lu else 0
    tcount: dict[int, int] = {}
    for j in lu:
        tcount[int(team[j])] = tcount.get(int(team[j]), 0) + 1
    if lu and (spent + (size - len(lu)) * min_cost > cap or
               not can_fill(tuple(sorted(int(code[j]) for j in lu)), masks)):
        return None
    for i in order:
        if len(lu) == size:
            break
        if score[i] <= 0:
            break
        if int(i) in lu:
            continue
        left = size - 1 - len(lu)
        if spec.enforce_rules:              # per-slot cheapest completion (positions are not interchangeable)
            if spent + sal[i] + _completion_floor(pool, tuple(sorted([int(code[j]) for j in lu] + [int(code[i])]))) > cap:
                continue
        elif spent + sal[i] + left * min_cost > cap:
            continue
        t = int(team[i])
        if tcount.get(t, 0) >= max_team:
            continue
        if not can_fill(tuple(sorted([int(code[j]) for j in lu] + [int(code[i])])), masks):
            continue
        if rule_ok is not None and not (rule_ok(lu + [int(i)]) if len(lu) == size - 1
                                        else rules_ok(lu + [int(i)], pool, partial=True)):
            continue
        lu.append(int(i))
        spent += int(sal[i])
        tcount[t] = tcount.get(t, 0) + 1
        if len(lu) == size:
            break
    if len(lu) < size:
        return None
    if rule_ok is not None and not rule_ok(lu):
        return None
    for _ in range(8):
        spent = int(sal[lu].sum())
        tcount = {}
        for j in lu:
            tcount[int(team[j])] = tcount.get(int(team[j]), 0) + 1
        improved = False
        for a_idx in range(size):
            a = lu[a_idx]
            budget = cap - spent + sal[a]
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
                if not can_fill(tuple(sorted(rest + [int(code[b])])), masks):
                    continue
                if rule_ok is not None and not rule_ok(lu[:a_idx] + [b] + lu[a_idx + 1:]):
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
        if not _pair_swaps(lu, score, pool, max_team, rule_ok=rule_ok):
            break
    spent = int(sal[lu].sum())
    if spent < min_sal:
        return None
    return tuple(sorted(lu))


def _pair_swaps(lu: list[int], score: np.ndarray, pool: SlatePool, max_team: int, k: int = 24,
                rule_ok=None) -> bool:
    """Try replacing two lineup players with the best affordable legal pair.
    Mutates lu; returns True if it improved."""
    sal, code, team = pool.sal, pool.code, pool.team
    size, cap, masks = pool.spec.size, pool.spec.cap, pool.spec.masks
    spent = int(sal[lu].sum())
    in_lu = np.zeros(len(score), dtype=bool)
    in_lu[lu] = True
    best = None
    best_gain = 1e-6
    for a in range(size):
        for b in range(a + 1, size):
            pa, pb = lu[a], lu[b]
            budget = cap - spent + sal[pa] + sal[pb]
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
                if not can_fill(tuple(sorted(rest + [int(code[ci]), int(code[cj])])), masks):
                    continue
                counts: dict[int, int] = {}
                for kk, jj in enumerate(lu):
                    if kk not in (a, b):
                        counts[int(team[jj])] = counts.get(int(team[jj]), 0) + 1
                counts[int(team[ci])] = counts.get(int(team[ci]), 0) + 1
                counts[int(team[cj])] = counts.get(int(team[cj]), 0) + 1
                if max(counts.values()) > max_team:
                    continue
                if rule_ok is not None:
                    trial = list(lu)
                    trial[a], trial[b] = ci, cj
                    if not rule_ok(trial):
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
HALL_NEED = NBA_CLASSIC.hall_need


def have_highs() -> bool:
    try:
        import highspy  # noqa: F401
        return True
    except ImportError:
        return False


def exact_lineup(score: np.ndarray, pool: SlatePool, rules: dict, start: tuple | None,
                 k: int = 32) -> tuple | None:
    """Exact optimum (HiGHS MIP) over the players who could plausibly be in it:
    top-k by points, top-k by value, top-5 at each position, plus the warm start."""
    import highspy

    spec = pool.spec
    size = spec.size
    cand = set(np.argsort(-score)[:k].tolist()) | set(np.argsort(-score / pool.sal)[:k].tolist())
    for b in range(len(spec.positions)):
        m = np.nonzero(pool.code & (1 << b))[0]
        cand |= set(m[np.argsort(-score[m])][:5].tolist())
    if start:
        cand |= set(start)
    if spec.enforce_rules and spec.stack_rules:
        for seed in _stack_seeds(score, pool, n_qb=6):
            cand |= set(seed)
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
    row(np.arange(n), np.ones(n), size, size)
    for U, need in spec.hall_need.items():
        if need:
            idx = np.nonzero(code & U)[0]
            row(idx, np.ones(len(idx)), need, highspy.kHighsInf)
    row(np.arange(n), pool.sal[cand].astype(float), rules.get("min_salary", 0), spec.cap)
    max_team = rules.get("max_per_team", 8)
    for t in set(pool.team[cand].tolist()):
        idx = np.nonzero(pool.team[cand] == t)[0]
        if len(idx) > max_team:
            row(idx, np.ones(len(idx)), 0, max_team)
    max_game = rules.get("max_per_game", 8)
    if spec.enforce_rules and spec.min_games >= 2:
        max_game = min(max_game, size - 1)
    for g in set(pool.game[cand].tolist()):
        idx = np.nonzero(pool.game[cand] == g)[0]
        if len(idx) > max_game:
            row(idx, np.ones(len(idx)), 0, max_game)
    if spec.enforce_rules and spec.min_teams >= 2:
        for t in set(pool.team[cand].tolist()):
            idx = np.nonzero(pool.team[cand] == t)[0]
            if len(idx) > size - 1:
                row(idx, np.ones(len(idx)), 0, size - 1)
    if spec.enforce_rules and spec.stack_rules:
        _stack_rows(row, cand, pool, highspy.kHighsInf)
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
    return lu if len(lu) == size else start


def _stack_rows(row, cand: np.ndarray, pool: SlatePool, inf: float) -> None:
    """MILP rows for the registry stack rules, over the candidate players.
    QB stack:   sum(same-team WR/TE) - m * x_QB >= 0, one row per QB.
    Bring-back: sum(opponent RB/WR/TE) - b * x_QB >= 0.
    No DST vs QB: x_DST + x_QB <= 1 for each QB facing that DST."""
    spec, sr = pool.spec, pool.spec.stack_rules
    qb, dst = spec.bit("QB"), spec.bit("DST")
    catch = spec.bit("WR") | spec.bit("TE")
    skill = catch | spec.bit("RB")
    code, team = pool.code[cand], pool.team[cand]
    opp = pool.opp[cand] if pool.opp is not None else np.full(len(cand), -1)
    m, b = int(sr.get("qb_stack_min", 0)), int(sr.get("bring_back_min", 0))
    for i in np.nonzero(code & qb)[0]:
        if m:
            mates = np.nonzero(((code & catch) > 0) & (team == team[i]))[0]
            row(np.concatenate([mates, [i]]), np.concatenate([np.ones(len(mates)), [-float(m)]]), 0, inf)
        if b:
            backs = np.nonzero(((code & skill) > 0) & (team == opp[i]))[0]
            row(np.concatenate([backs, [i]]), np.concatenate([np.ones(len(backs)), [-float(b)]]), 0, inf)
        if sr.get("no_dst_vs_qb") and dst:
            for d in np.nonzero(((code & dst) > 0) & (team == opp[i]))[0]:
                row([d, i], [1.0, 1.0], 0, 1)


# ── showdown: 1 captain (1.5x points, 1.5x salary) + 5 UTIL, both teams used ──
def showdown_lineup(score: np.ndarray, pool: SlatePool, rules: dict) -> tuple | None:
    """Exact best showdown lineup for one score vector. Returned as
    (captain, *sorted utils)."""
    import highspy

    spec = pool.spec
    flex = spec.size - 1
    P = len(score)
    if P < spec.size:
        return None
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    n = 2 * P                                   # [0,P) util, [P,2P) captain
    h.addVars(n, np.zeros(n), np.ones(n))
    h.changeColsIntegrality(n, np.arange(n, dtype=np.int32), np.array([highspy.HighsVarType.kInteger] * n))

    def row(idx, coef, lo, hi):
        h.addRow(lo, hi, len(idx), np.asarray(idx, dtype=np.int32), np.asarray(coef, dtype=float))

    row(np.arange(P), np.ones(P), flex, flex)
    row(np.arange(P, n), np.ones(P), 1, 1)
    for i in range(P):
        row([i, P + i], [1, 1], 0, 1)
    row(np.arange(n), np.concatenate([pool.sal, pool.cpt_sal]).astype(float),
        rules.get("min_salary_showdown", 0), spec.cap)
    for t in set(pool.team.tolist()):
        idx = np.nonzero(pool.team == t)[0]
        row(np.concatenate([idx, idx + P]), np.ones(2 * len(idx)), 1, flex)   # both teams represented
    h.changeColsCost(n, np.arange(n, dtype=np.int32), np.concatenate([score, spec.cpt_mult * score]).astype(float))
    h.changeObjectiveSense(highspy.ObjSense.kMaximize)
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return None
    x = np.asarray(h.getSolution().col_value)
    utils = sorted(int(i) for i in np.nonzero(x[:P] > 0.5)[0])
    cpt = [int(i) for i in np.nonzero(x[P:] > 0.5)[0]]
    if len(utils) != flex or len(cpt) != 1:
        return None
    return (cpt[0], *utils)


def _gen_chunk(args) -> list[tuple]:
    pool, rules, corr, n, seed, exact, lev_share, lev_max = args
    rng = np.random.default_rng(seed)
    scores = sample_scores(pool, n, rng, corr).astype(np.float64)
    # leverage candidates: for part of the simulated slates, points from players the
    # field owns heavily are worth less (to win you must beat lineups that share them).
    # These are only candidates; every lineup is then judged by the same contest sim.
    lev = rng.random(n) < lev_share
    lam = rng.uniform(0.0, lev_max, n) * lev
    scores *= 1.0 - lam[:, None] * (pool.own / 100.0)[None, :]
    out = []
    for s in scores:
        if pool.kind == "showdown":
            lu = showdown_lineup(s, pool, rules)
            if lu:
                out.append(lu)
            continue
        warm = best_lineup(s, pool, {**rules, "min_salary": 0} if exact else rules)
        lu = exact_lineup(s, pool, rules, warm) if exact and warm else warm
        if lu is None and pool.spec.enforce_rules and pool.spec.stack_rules and have_highs():
            lu = exact_lineup(s, pool, rules, None)      # greedy could not satisfy the stack rules
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
            jobs = [(pool, rules, corr, chunk, seed * 1_000_003 + round_ * 97 + w, exact,
                     SIM["leverage_share"], SIM["leverage_max"]) for w in range(workers)]
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
    spec = pool.spec
    size, cap, masks = spec.size, spec.cap, spec.masks
    field_min = SIM["field_min_salary"] if spec.sport == "NBA" else spec.field_min_salary
    w = np.log(pool.own / 100.0 + 1e-6)
    out = []
    min_cost = int(np.sort(pool.sal)[:size].sum() / size)
    tries = 0
    while len(out) < n and tries < n * 20:
        tries += 1
        keys = w + rng.gumbel(size=len(w))
        order = np.argsort(-keys)
        lu, spent = [], 0
        for i in order:
            left = size - 1 - len(lu)
            if spent + pool.sal[i] + left * min_cost > cap:
                continue
            if not can_fill(tuple(sorted([int(pool.code[j]) for j in lu] + [int(pool.code[i])])), masks):
                continue
            lu.append(int(i))
            spent += int(pool.sal[i])
            if len(lu) == size:
                break
        if len(lu) == size and spent >= field_min and rules_ok(lu, pool, stack=False):
            out.append(tuple(sorted(lu)))
    return out


FIELD_SPEND_SD = 1500.0    # public showdown spend: cap minus |N(0, 1)| x this (median ~ cap - $1,000)


def _sample_field_showdown(pool: SlatePool, n: int, rng: np.random.Generator) -> list[tuple]:
    if pool.spec.sport != "NBA":
        return _sample_field_showdown_targeted(pool, n, rng)
    spec = pool.spec
    flex, cap = spec.size - 1, spec.cap
    field_min = 45000 if spec.sport == "NBA" else spec.field_min_salary
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
            if spent + pool.sal[i] > cap:
                continue
            utils.append(int(i))
            spent += int(pool.sal[i])
            if len(utils) == flex:
                break
        if len(utils) == flex and len(set(pool.team[[cpt] + utils].tolist())) >= max(2, spec.min_teams) \
                and spent >= field_min:
            out.append((int(cpt), *sorted(utils)))
    return out


def _sample_field_showdown_targeted(pool: SlatePool, n: int, rng: np.random.Generator) -> list[tuple]:
    """Ownership-weighted showdown field (registry sports) that spends like the
    public: each lineup draws a spend target (cap - |N(0,1)| x FIELD_SPEND_SD, at
    least the field minimum) and takes players in ownership-key order only while
    the cap and the target both stay reachable with the slots left. The last pick
    must bring in the second team. Same algorithm as engine.js sampleField."""
    spec = pool.spec
    flex, cap, field_min = spec.size - 1, spec.cap, spec.field_min_salary
    sal = pool.sal.astype(np.int64)
    asc = [int(i) for i in np.argsort(sal, kind="stable")]
    desc = asc[::-1]
    w = np.log(pool.own / 100.0 + 1e-6)

    def extreme(order, r, used):
        tot, k = 0, 0
        for j in order:
            if k == r:
                break
            if j not in used:
                tot += int(sal[j])
                k += 1
        return tot if k == r else None

    out, tries = [], 0
    while len(out) < n and tries < n * 30:
        tries += 1
        keys = w + rng.gumbel(size=len(w))
        target = max(field_min, cap - abs(float(rng.standard_normal())) * FIELD_SPEND_SD)
        order = [int(i) for i in np.argsort(-keys)]
        cpt = order[0]
        spent = int(pool.cpt_sal[cpt])
        if spent > cap:
            continue
        used, utils = {cpt}, []
        for i in order[1:]:
            r = flex - len(utils) - 1
            s2 = spent + int(sal[i])
            u2 = used | {i}
            lo = extreme(asc, r, u2)
            hi = extreme(desc, r, u2)
            if lo is None or s2 + lo > cap or s2 + hi < target:
                continue
            if r == 0 and len({int(pool.team[j]) for j in u2}) < 2:
                continue
            utils.append(i)
            used, spent = u2, s2
            if len(utils) == flex:
                break
        if len(utils) == flex and spent >= field_min and len(set(pool.team[[cpt] + utils].tolist())) >= 2:
            out.append((int(cpt), *sorted(utils)))
    return out


def lineup_weights(lu: tuple, kind: str, cpt_mult: float = 1.5) -> list[tuple[int, float]]:
    """(player index, scoring multiplier) for a lineup."""
    if kind == "showdown":
        return [(lu[0], cpt_mult)] + [(i, 1.0) for i in lu[1:]]
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
    kind, cpt_mult = pool.kind, pool.spec.cpt_mult
    pool = subpool(pool, used)
    P = len(used)
    M = np.zeros((P, len(allu)), dtype=np.float32)
    for j, lu in enumerate(allu):
        for i, w in lineup_weights(lu, kind, cpt_mult):
            M[remap[i], j] = w
    L = len(lineups)
    # duplicates: a field lineup identical to one of yours ties it, so the win is split
    in_field: dict[tuple, int] = {}
    for lu in field:
        in_field[lu] = in_field.get(lu, 0) + 1
    share = np.array([1.0 / (1 + in_field.get(lu, 0)) for lu in lineups])
    # for portfolio selection: in the first `cover_sims` contests, which of your lineups
    # outscore the whole field (bit-packed, sims x lineups)
    cover_sims = min(sims, SIM["portfolio_sims"])
    beats = []
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
        if done < cover_sims and field:
            k = min(n, cover_sims - done)
            fmax = S[:k, L:].max(axis=1)
            beats.append(np.packbits(S[:k, :L] > fmax[:, None], axis=0))
        xs += X.sum(axis=0, dtype=np.float64)
        xtx += (X.T @ X).astype(np.float64)
        done += n
    # lineup mean and spread follow exactly from the player-level moments
    Ml = M[:, :L].astype(np.float64)
    mean = (xs / sims) @ Ml
    ex2 = np.einsum("pl,pl->l", Ml, (xtx / sims) @ Ml)
    sd = np.sqrt(np.maximum(ex2 - mean ** 2, 0))
    log.info("contest: %d sims x %d lineups (+%d field) in %.1fs", sims, L, len(field), time.time() - t0)
    mine = wins[:L] * share
    return {"win": mine / sims * 100.0, "mean": mean, "sd": sd,
            "field_win": float((wins[L:].sum() + (wins[:L] - mine).sum()) / sims * 100.0),
            "dupes": np.array([in_field.get(lu, 0) for lu in lineups]),
            "beats": np.concatenate(beats, axis=0) if beats else None, "cover_sims": cover_sims}


_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _popcount(a: np.ndarray, axis=None):
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(a).sum(axis=axis, dtype=np.int64)
    return _POP8[a].sum(axis=axis, dtype=np.int64)


def portfolio(beats: np.ndarray | None, n_sims: int, k: int) -> tuple[list[int], list[float]]:
    """Greedy portfolio: each pick is the lineup that adds the most simulated contests
    in which at least one of your picks beats the entire field. Diversifies around the
    best lineups exactly as far as it pays (no fixed exposure rules).
    Returns (lineup indices in pick order, cumulative coverage % after each pick)."""
    if beats is None or not beats.size:
        return [], []
    B = np.ascontiguousarray(beats.T)            # lineups x packed sims
    covered = np.zeros(B.shape[1], dtype=np.uint8)
    picks, cum = [], []
    alive = np.ones(B.shape[0], dtype=bool)
    for _ in range(min(k, B.shape[0])):
        gain = _popcount(B & ~covered, axis=1)
        gain[~alive] = -1
        j = int(gain.argmax())
        if gain[j] <= 0:
            break
        picks.append(j)
        alive[j] = False
        covered |= B[j]
        cum.append(100.0 * int(_popcount(covered)) / n_sims)
    return picks, cum


def subpool(pool: SlatePool, idx: list[int]) -> SlatePool:
    ix = np.asarray(idx)
    return SlatePool(ids=pool.ids[ix], names=[pool.names[i] for i in ix], mu=pool.mu[ix], sd=pool.sd[ix],
                     sal=pool.sal[ix], code=pool.code[ix], team=pool.team[ix], game=pool.game[ix],
                     own=pool.own[ix], p_play=pool.p_play[ix], start=pool.start[ix],
                     n_team=pool.n_team, n_game=pool.n_game, kind=pool.kind,
                     cpt_sal=pool.cpt_sal[ix], cpt_ids=pool.cpt_ids[ix],
                     q=pool.q[ix] if pool.q is not None else None,
                     med=pool.med[ix] if pool.med is not None else None, spec=pool.spec,
                     load=_sub_load(pool.load, ix), idio=pool.idio[ix] if pool.idio is not None else None,
                     opp=pool.opp[ix] if pool.opp is not None else None)


def _sub_load(load: np.ndarray | None, ix: np.ndarray) -> np.ndarray | None:
    """Rows for the kept players; factor instances no kept player loads on are dropped
    (so the subpool draws only the factors it needs)."""
    if load is None:
        return None
    sub = load[ix]
    return sub[:, np.any(sub != 0, axis=0)] if sub.size else sub


def slot_order(lu: tuple, pool: SlatePool) -> list[int] | None:
    """Assign players to the spec's slots (NBA: PG..UTIL), filled from the last
    slot backwards so later games go to the flex slots (UTIL, F, G / FLEX) and
    late swap keeps the most options. Slots are indexed by position, not label
    (NFL repeats RB and WR)."""
    players = sorted(lu, key=lambda i: -pool.start[i])
    res = S.assign([int(pool.code[i]) for i in players], pool.spec.masks)
    return None if res is None else [players[k] for k in res]


def build_slate_lineups(rows: list[dict], rules: dict, corr: dict, n_lineups: int | None = None,
                        sims: int | None = None, field_size: int | None = None, seed: int = 1,
                        exact: bool = True, kind: str = "classic", spec: SlotSpec | None = None,
                        loadings: dict | None = None) -> dict:
    n_lineups = n_lineups or SIM["lineups"]
    sims = sims or SIM["sims"]
    field_size = field_size or SIM["field_lineups"]
    pool = build_pool(rows, kind, spec, loadings)
    spec = pool.spec
    if len(pool.mu) < spec.size:
        return {"lineups": [], "sims": 0, "kind": kind}
    t0 = time.time()
    lineups = generate_lineups(pool, n_lineups, rules, corr, seed=seed,
                               time_limit=SIM["generate_time_limit"] if kind == "classic" else 90, exact=exact)
    t1 = time.time()
    field = sample_field(pool, field_size, np.random.default_rng(seed + 5), rules)
    if len(field) < 0.5 * field_size:
        from . import health
        log.warning("simulated field has only %d of %d lineups", len(field), field_size)
        health.fail("Simulation field", f"{len(field)} of {field_size} field lineups ({spec.sport} {kind})")
    res = run_contest(pool, lineups, field, sims, corr, seed=seed + 9)
    t2 = time.time()
    order = np.argsort(-res["win"], kind="stable")
    picks, cover = portfolio(res.get("beats"), res.get("cover_sims", 0), SIM["portfolio_size"])
    pf_rank = {j: r for r, j in enumerate(picks, 1)}
    med = pool.med if pool.med is not None else pool.mu      # lineup Proj = sum of player medians
    out = []
    for rank, j in enumerate(order, 1):
        lu = lineups[j]
        if kind == "showdown":
            ids = [int(pool.cpt_ids[lu[0]])] + [int(pool.ids[i]) for i in lu[1:]]
            proj = spec.cpt_mult * med[lu[0]] + med[list(lu[1:])].sum()
            salary = int(pool.cpt_sal[lu[0]] + pool.sal[list(lu[1:])].sum())
        else:
            slots = slot_order(lu, pool)
            if slots is None:
                continue
            ids = [int(pool.ids[i]) for i in slots]
            proj = med[list(lu)].sum()
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
            "low_owned": int((pool.own[list(lu)] < 10.0).sum()),
            "pf": pf_rank.get(int(j)),            # position in the win-coverage portfolio
            "dupes": int(res["dupes"][j]),
        })
    log.info("built %d lineups in %.0fs, contest %.0fs", len(lineups), t1 - t0, t2 - t1)
    return {"lineups": out, "sims": sims, "field_size": len(field), "exact": exact, "kind": kind,
            "slots": list(spec.labels), "field_win_pct": res["field_win"],
            "generate_seconds": round(t1 - t0, 1), "contest_seconds": round(t2 - t1, 1),
            "correlation": corr, "rules": rules,
            # chance at least one of the first N portfolio lineups beats the whole field
            "portfolio_cover": {str(n): round(cover[n - 1], 3) for n in (1, 5, 20, 50, 150, 500)
                                if len(cover) >= n},
            "portfolio_sims": res.get("cover_sims", 0),
            **({} if spec.sport == "NBA" else {"field_filled": round(len(field) / max(field_size, 1), 4)})}
