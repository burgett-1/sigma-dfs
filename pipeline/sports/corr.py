"""Factor-copula correlation (registry `corr.model == "factor"`).

Each slate gets factor instances: `game:<gameKey>` for game-scoped factors and
`team:<factor>:<team>` for team-scoped ones. A player loads on his own game's
and team's instances by position; a loading key "opp:<factor>" attaches him to
the *opponent's* instance instead. With loadings L (players x instances) and
idio_i = sqrt(1 - sum_j L_ij^2), the latent normal is z = F L^T + idio * e, so
corr(z_i, z_j) = (L L^T)_ij. The web engine (engine.js) builds the same matrix.
"""
from __future__ import annotations

import math

import numpy as np

from . import get

MIN_PAIR_N = 300          # team-games per role pair before a fitted loading is trusted
MAX_SUMSQ_FIT = 0.9


def _model(sport: str, reg: dict | None = None) -> dict:
    return get(sport, reg).get("corr") or {}


def instances(players: list[dict], sport: str, reg: dict | None = None, loadings: dict | None = None):
    """(instance keys, L as players x K float64 array, idio array).

    players: dicts with position (base position, e.g. "WR"), team, opp and game
    (any hashable game key). Unknown positions get no loadings (independent)."""
    m = _model(sport, reg)
    scope = {f["name"]: f["scope"] for f in m.get("factors") or []}
    lds = loadings or m.get("loadings") or {}
    keys: dict[str, int] = {}
    entries: list[list[tuple[int, float]]] = []
    for p in players:
        pos = str(p.get("position") or "").split("/")[0].upper()
        row = []
        for name, val in (lds.get(pos) or {}).items():
            opp = name.startswith("opp:")
            f = name[4:] if opp else name
            sc = scope.get(f)
            if sc is None:
                continue
            if sc == "game":
                key = f"game:{p.get('game')}"
            else:
                team = p.get("opp") if opp else p.get("team")
                key = f"team:{f}:{team}"
            if key not in keys:
                keys[key] = len(keys)
            row.append((keys[key], float(val)))
        entries.append(row)
    L = np.zeros((len(players), len(keys)), dtype=np.float64)
    for i, row in enumerate(entries):
        for k, v in row:
            L[i, k] += v
    ss = (L ** 2).sum(axis=1)
    idio = np.sqrt(np.maximum(1.0 - ss, 1e-6))
    return list(keys), L, idio


def implied_corr(players: list[dict], sport: str, reg: dict | None = None, loadings: dict | None = None) -> np.ndarray:
    """Implied latent correlation matrix (unit diagonal)."""
    _, L, _ = instances(players, sport, reg, loadings)
    C = L @ L.T
    np.fill_diagonal(C, 1.0)
    return C


def pair_corr(pos_a: str, pos_b: str, relation: str, sport: str, reg: dict | None = None,
              loadings: dict | None = None) -> float:
    """Implied correlation for two positions related as "same_team", "opp" or "other_game"."""
    a = {"position": pos_a, "team": "A", "opp": "B", "game": "G1"}
    if relation == "same_team":
        b = {"position": pos_b, "team": "A", "opp": "B", "game": "G1"}
    elif relation == "opp":
        b = {"position": pos_b, "team": "B", "opp": "A", "game": "G1"}
    else:
        b = {"position": pos_b, "team": "C", "opp": "D", "game": "G2"}
    return float(implied_corr([a, b], sport, reg, loadings)[0, 1])


def summary(sport: str, reg: dict | None = None, loadings: dict | None = None) -> dict:
    """The headline implied pairs published in meta.json."""
    pairs = {"QB-own WR": ("QB", "WR", "same_team"), "QB-own TE": ("QB", "TE", "same_team"),
             "QB-own RB": ("QB", "RB", "same_team"), "QB-opp WR": ("QB", "WR", "opp"),
             "DST-opp QB": ("DST", "QB", "opp"), "DST-own RB": ("DST", "RB", "same_team"),
             "different games": ("QB", "WR", "other_game")}
    return {k: round(pair_corr(a, b, r, sport, reg, loadings), 4) for k, (a, b, r) in pairs.items()}


# ── fitting from history ─────────────────────────────────────────────────────
# role pairs whose observed residual correlation constrains the loadings:
# (role a, role b, relation, positions a/b)
ROLE_PAIRS = [
    ("QB", "WR1", "same_team", "QB", "WR"), ("QB", "WR2", "same_team", "QB", "WR"),
    ("QB", "TE1", "same_team", "QB", "TE"), ("QB", "RB1", "same_team", "QB", "RB"),
    ("WR1", "WR2", "same_team", "WR", "WR"), ("RB1", "DST", "same_team", "RB", "DST"),
    ("QB", "opp-QB", "opp", "QB", "QB"), ("QB", "opp-WR1", "opp", "QB", "WR"),
    ("DST", "opp-QB", "opp", "DST", "QB"), ("DST", "opp-WR1", "opp", "DST", "WR"),
]


def observed_pairs(df) -> dict:
    """Residual correlations by role pair from a player-week history frame with
    columns season, week, game_id, team, opp, position, dk_fp, targets (and
    player_id). Residual = dk_fp - the player's season-to-date mean (prior weeks)."""
    import pandas as pd
    d = df.copy()
    d = d.sort_values(["season", "week"])
    key = d["player_id"].astype(str)
    grp = d.groupby([d["season"], key])["dk_fp"]
    d["prior_mean"] = grp.transform(lambda s: s.shift(1).expanding().mean())
    d = d[d["prior_mean"].notna()].copy()
    d["resid"] = d["dk_fp"] - d["prior_mean"]
    # roles within each team-game: WR1/WR2 by targets, TE1, RB1 by carries
    roles = []
    for (gid, team), g in d.groupby(["game_id", "team"]):
        r = {}
        for pos, names, col in (("QB", ["QB"], "attempts"), ("WR", ["WR1", "WR2"], "targets"),
                                ("TE", ["TE1"], "targets"), ("RB", ["RB1"], "carries"), ("DST", ["DST"], None)):
            sub = g[g["position"] == pos]
            if col and col in sub:
                sub = sub.sort_values(col, ascending=False)
            for i, role in enumerate(names[:len(sub)]):
                r[role] = float(sub.iloc[i]["resid"])
        roles.append({"game_id": gid, "team": team, "opp": g["opp"].iloc[0], **r})
    R = pd.DataFrame(roles)
    out = {}
    if R.empty:
        return out
    opp = R.set_index(["game_id", "team"])
    for a, b, rel, _, _ in ROLE_PAIRS:
        xs, ys = [], []
        for _, row in R.iterrows():
            va = row.get(a)
            if va is None or (isinstance(va, float) and math.isnan(va)):
                continue
            if rel == "same_team":
                vb = row.get(b)
            else:
                try:
                    vb = opp.loc[(row["game_id"], row["opp"])].get(b.replace("opp-", ""))
                except KeyError:
                    vb = None
            if vb is None or (isinstance(vb, float) and math.isnan(vb)):
                continue
            xs.append(va)
            ys.append(vb)
        if len(xs) >= 3 and np.std(xs) > 0 and np.std(ys) > 0:
            out[f"{a}|{b}"] = {"rho": float(np.corrcoef(xs, ys)[0, 1]), "n": len(xs)}
    return out


def fit_loadings(df, sport: str = "NFL", reg: dict | None = None) -> dict:
    """Weighted least squares of the default loadings' scale per position onto the
    observed role-pair correlations. Accepted only if every pair has n >= 300,
    every sum of squares < 0.9 and every sign matches the default; otherwise the
    registry defaults are kept. Returns {used, loadings, n, reason, observed}."""
    base = _model(sport, reg).get("loadings") or {}
    obs = observed_pairs(df) if df is not None and len(df) else {}
    n_min = min((v["n"] for v in obs.values()), default=0)
    res = {"used": False, "loadings": base, "n": n_min, "observed": obs, "reason": ""}
    if len(obs) < len(ROLE_PAIRS):
        res["reason"] = f"only {len(obs)} of {len(ROLE_PAIRS)} role pairs observed"
        return res
    if n_min < MIN_PAIR_N:
        res["reason"] = f"smallest pair has n={n_min} < {MIN_PAIR_N} team-games"
        return res
    positions = sorted(base)
    # one multiplicative scale per position: rho_ab ~= s_a s_b rho0_ab
    scale = {p: 1.0 for p in positions}
    pairs = [(a, b, rel, pa, pb) for a, b, rel, pa, pb in ROLE_PAIRS]
    for _ in range(200):
        for p in positions:
            num = den = 0.0
            for a, b, rel, pa, pb in pairs:
                o = obs[f"{a}|{b}"]
                r0 = pair_corr(pa, pb, rel, sport, reg)
                if pa != p and pb != p:
                    continue
                other = scale[pb] if pa == p else scale[pa]
                if pa == pb == p:
                    continue
                x = r0 * other
                num += o["n"] * x * o["rho"]
                den += o["n"] * x * x
            if den > 0:
                scale[p] = max(0.0, min(2.0, num / den))
    fitted = {p: {k: v * scale[p] for k, v in base[p].items()} for p in positions}
    for p, ld in fitted.items():
        if sum(v * v for v in ld.values()) >= MAX_SUMSQ_FIT:
            res["reason"] = f"fitted loadings for {p} exceed sum of squares {MAX_SUMSQ_FIT}"
            return res
    for a, b, rel, pa, pb in pairs:
        r_fit = pair_corr(pa, pb, rel, sport, reg, fitted)
        r0 = pair_corr(pa, pb, rel, sport, reg)
        if r0 and np.sign(r_fit) != np.sign(r0):
            res["reason"] = f"fitted sign of {a}|{b} differs from the default"
            return res
    return {"used": True, "loadings": fitted, "n": n_min, "observed": obs, "reason": "fit accepted",
            "scale": {p: round(s, 4) for p, s in scale.items()}}
