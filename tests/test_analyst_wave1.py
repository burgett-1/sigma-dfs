"""Analyst (Phase 3, Wave 1): independent checks of the multi-sport framework and NFL.

Everything here is written from DraftKings' published NFL rules and hand-computed
box scores, not from the code under test:
- `DKFile` / `classic_violations` / `showdown_violations` are an independent DK
  legality validator that reads the raw salary CSV (Roster Position semantics).
- Scoring cases are worked by hand from real nflverse lines (fixture rows) with
  DK's rules: 0.04/pass yd, 4/pass TD, -1/INT, 0.1/rush+rec yd, 6/TD, 1/rec,
  -1/fumble lost, 2/2-pt, +3 at 300 pass / 100 rush / 100 rec yds; DST 1/sack,
  2/INT, 2/fumble rec, 2/safety, 2/block, 6/TD, points-allowed buckets.
Real public DK files (lobby, salary CSVs) are used only when ANALYST_DK_DIR points
at a folder holding them (skip otherwise); nothing real is committed here. All
ids below are synthetic.
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pipeline import config, sim
from pipeline.sports import scoring, slots
from pipeline.sources import draftkings as dk
from tests.fixtures.nfl import synthetic_nfl as N

FIX = Path(__file__).parent / "fixtures" / "nfl"
DK_DIR = Path(os.environ.get("ANALYST_DK_DIR", "/nonexistent"))
CLASSIC_ORDER = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]
CAP = 50_000


@pytest.fixture(autouse=True)
def _nfl_health_scope():
    """These tests call NFL code directly; keep their health notes out of NBA's
    global dict (the runner does the same with health.scope)."""
    from pipeline import health
    before = dict(health._status)
    with health.scope("NFL"):
        yield
    assert health._status == before, "an NFL check wrote to NBA's global health"


# ─────────────────────────────────────────────────────────────────────────────
# Independent DraftKings validator (reads the raw CSV only)
# ─────────────────────────────────────────────────────────────────────────────
class DKFile:
    """id -> {pos, slots (Roster Position tokens), salary, team, game, name}."""

    def __init__(self, text: str):
        self.by_id: dict[int, dict] = {}
        rd = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        for r in rd:
            if not (r.get("ID") or "").strip().isdigit():
                continue
            gi = (r.get("Game Info") or "").split(" ")[0]
            away, _, home = gi.partition("@")
            team = r["TeamAbbrev"].strip()
            self.by_id[int(r["ID"])] = {
                "pos": r["Position"].strip(), "slots": set(r["Roster Position"].strip().split("/")),
                "salary": int(r["Salary"]), "team": team, "game": gi, "name": r["Name"].strip(),
                "opp": home if team == away else away}

    def person(self, i):
        p = self.by_id[i]
        return (p["name"], p["team"])


def classic_violations(ids, f: DKFile, stack: bool = True) -> list[str]:
    v = []
    ids = [int(i) for i in ids]
    if len(ids) != 9:
        return [f"size {len(ids)}"]
    if len(set(ids)) != 9:
        v.append("duplicate id")
    if any(i not in f.by_id for i in ids):
        return v + ["id not in CSV"]
    P = [f.by_id[i] for i in ids]
    if len({f.person(i) for i in ids}) != 9:
        v.append("same player twice")
    for p, slot in zip(P, CLASSIC_ORDER):
        if slot not in p["slots"]:
            v.append(f"{p['pos']} in {slot} (eligible {sorted(p['slots'])})")
        if slot == "FLEX" and p["pos"] not in ("RB", "WR", "TE"):
            v.append("FLEX not RB/WR/TE")
    if [p["pos"] for p in P].count("QB") != 1 or [p["pos"] for p in P].count("DST") != 1:
        v.append("QB/DST count")
    if any(p["pos"] == "K" for p in P):
        v.append("K in classic")
    if sum(p["salary"] for p in P) > CAP:
        v.append("over cap")
    if len({p["game"] for p in P}) < 2:
        v.append("one game")
    qb = next((p for p in P if p["pos"] == "QB"), None)
    dst = next((p for p in P if p["pos"] == "DST"), None)
    if qb and dst and dst["team"] == qb["opp"]:
        v.append("DST faces own QB")
    if stack and qb and not any(p["pos"] in ("WR", "TE") and p["team"] == qb["team"] for p in P):
        v.append("QB not stacked")
    return v


def showdown_violations(ids, f: DKFile) -> list[str]:
    v = []
    ids = [int(i) for i in ids]
    if len(ids) != 6 or len(set(ids)) != 6:
        return [f"size/dupes {ids}"]
    if any(i not in f.by_id for i in ids):
        return ["id not in CSV"]
    P = [f.by_id[i] for i in ids]
    if "CPT" not in P[0]["slots"] or any("CPT" in p["slots"] or "FLEX" not in p["slots"] for p in P[1:]):
        v.append("slot rows")
    if len({f.person(i) for i in ids}) != 6:
        v.append("captain also in FLEX")
    flex_sal = {f.person(i): p["salary"] for i, p in f.by_id.items() if "FLEX" in p["slots"]}
    if P[0]["salary"] != round(1.5 * flex_sal.get(f.person(ids[0]), -1)):
        v.append("CPT salary != 1.5x FLEX")
    if sum(p["salary"] for p in P) > CAP:
        v.append("over cap")
    if len({p["team"] for p in P}) < 2:
        v.append("one team")
    return v


def test_validator_self_check():
    """The validator itself flags each rule on hand-built lineups (synthetic CSV)."""
    f = DKFile(N.salary_csv("classic"))
    by = {}
    for i, p in f.by_id.items():
        by.setdefault((p["team"], p["pos"]), []).append(i)
    good = [by[("BUF", "QB")][0], by[("KC", "RB")][2], by[("DAL", "RB")][2], by[("BUF", "WR")][0],
            by[("KC", "WR")][1], by[("SF", "WR")][2], by[("PHI", "TE")][1], by[("SEA", "WR")][4], by[("SF", "DST")][0]]
    assert classic_violations(good, f) == [], classic_violations(good, f)
    bad_flex = good[:7] + [by[("SEA", "QB")][1], good[8]]
    assert any("FLEX" in x for x in classic_violations(bad_flex, f))
    faces = good[:8] + [by[("MIA", "DST")][0]]                       # MIA plays BUF's QB
    assert "DST faces own QB" in classic_violations(faces, f)
    unstacked = [by[("BUF", "QB")][0]] + good[1:3] + [by[("KC", "WR")][0], by[("KC", "WR")][1],
                                                      by[("SF", "WR")][2], by[("PHI", "TE")][1], good[7], good[8]]
    assert "QB not stacked" in classic_violations(unstacked, f)
    sd = DKFile(N.salary_csv("showdown"))
    cpt = next(i for i, p in sd.by_id.items() if "CPT" in p["slots"] and p["pos"] == "QB")
    cand = [i for i, p in sd.by_id.items() if "FLEX" in p["slots"] and sd.person(i) != sd.person(cpt)
            and p["pos"] in ("WR", "TE")]
    flex = [i for i in cand if sd.by_id[i]["team"] == "MIA"][-3:] + [i for i in cand if sd.by_id[i]["team"] == "BUF"][-2:]
    assert len({sd.by_id[i]["team"] for i in flex}) == 2
    assert showdown_violations([cpt] + flex, sd) == []
    twin = next(i for i, p in sd.by_id.items() if "FLEX" in p["slots"] and sd.person(i) == sd.person(cpt))
    assert "captain also in FLEX" in showdown_violations([cpt, twin] + flex[:4], sd)


# ─────────────────────────────────────────────────────────────────────────────
# Projected synthetic slate (same jobs the scheduler runs)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def projected(tmp_path_factory):
    from pipeline.sports.nfl import data as D, runner as R
    tmp = tmp_path_factory.mktemp("analyst-nfl")
    old = config.DATA_DIR
    config.DATA_DIR = tmp
    try:
        D.save_history(N.history())
        r = R.NFLRunner(get_json=lambda u: N.lobby() if "lobby" in u else {"events": [], "injuries": []},
                        get_text=N.fake_dk_text)
        r.lobby(N.NOW)
        r.salaries(N.NOW)
        r.ctx = N.context()
        r.project(N.NOW)
        return {k: [dict(x) for x in v] for k, v in r.rows.items()}, r.rules()
    finally:
        config.DATA_DIR = old


def test_classic_fast_lineups_field_and_greedy_are_dk_legal(projected, monkeypatch):
    rows, rules = projected
    f = DKFile(N.salary_csv("classic"))
    monkeypatch.setitem(config.SIM, "portfolio_sims", 2000)
    sp = slots.spec("NFL", "classic")
    res = sim.build_slate_lineups([dict(r) for r in rows[900001]], rules, {}, n_lineups=200, sims=5000,
                                  field_size=300, seed=5, exact=False, kind="classic", spec=sp)
    assert res["slots"] == CLASSIC_ORDER and len(res["lineups"]) >= 50
    bad = [(lu["ids"], classic_violations(lu["ids"], f)) for lu in res["lineups"] if classic_violations(lu["ids"], f)]
    assert not bad, bad[:3]
    assert all(lu["salary"] == sum(f.by_id[i]["salary"] for i in lu["ids"]) for lu in res["lineups"])
    pool = sim.build_pool([dict(r) for r in rows[900001]], "classic", sp)
    field = sim.sample_field(pool, 400, np.random.default_rng(8), rules)
    fb = [classic_violations([int(pool.ids[i]) for i in sim.slot_order(lu, pool)], f, stack=False) for lu in field]
    assert len(field) >= 200 and not any(fb), [x for x in fb if x][:3]
    for s in sim.sample_scores(pool, 80, np.random.default_rng(9), {}).astype(float):
        lu = sim.best_lineup(s, pool, rules)
        assert lu is not None
        assert classic_violations([int(pool.ids[i]) for i in sim.slot_order(lu, pool)], f) == []


def test_opt_rates_per_position_totals(projected):
    """Each optimal lineup has exactly one QB and one DST, two-three RB, etc."""
    from pipeline import ownership
    rows, rules = projected
    work = [dict(r) for r in rows[900001]]
    ownership.optimal_rates(work, "classic", rules, None, sims=150, spec=slots.spec("NFL", "classic"))
    tot = {}
    for r in work:
        tot[r["position"]] = tot.get(r["position"], 0) + r["opt_pct"]
    assert abs(sum(tot.values()) - 900) < 1e-6
    assert abs(tot["QB"] - 100) < 1e-6 and abs(tot["DST"] - 100) < 1e-6
    assert 200 - 1e-6 <= tot["RB"] <= 300 + 1e-6 and 300 - 1e-6 <= tot["WR"] <= 400 + 1e-6
    assert 100 - 1e-6 <= tot["TE"] <= 200 + 1e-6
    assert abs(tot["RB"] + tot["WR"] + tot["TE"] - 700) < 1e-6


def test_exact_classic_and_showdown_legal(projected, monkeypatch):
    pytest.importorskip("highspy")
    rows, rules = projected
    monkeypatch.setitem(config.SIM, "portfolio_sims", 2000)
    f, sdf = DKFile(N.salary_csv("classic")), DKFile(N.salary_csv("showdown"))
    res = sim.build_slate_lineups([dict(r) for r in rows[900001]], rules, {}, n_lineups=120, sims=4000,
                                  field_size=200, exact=True, kind="classic", spec=slots.spec("NFL", "classic"))
    assert res["lineups"] and not [classic_violations(lu["ids"], f) for lu in res["lineups"]
                                   if classic_violations(lu["ids"], f)]
    sd_rows = [dict(r) for r in rows[900002]]
    for r in sd_rows:
        if r["position"] in ("K", "DST") and r["team_dk"] == "BUF":
            r["mean"] = r["proj"] = 30.0
    res = sim.build_slate_lineups(sd_rows, rules, {}, n_lineups=100, sims=4000, field_size=200, exact=True,
                                  kind="showdown", spec=slots.spec("NFL", "showdown"))
    assert res["lineups"] and not [showdown_violations(lu["ids"], sdf) for lu in res["lineups"]
                                   if showdown_violations(lu["ids"], sdf)]
    used = {sdf.by_id[i]["pos"] for lu in res["lineups"] for i in lu["ids"]}
    assert {"K", "DST"} <= used


def test_showdown_captain_scores_one_and_a_half(projected):
    """run_contest weights the CPT 1.5x: lineup mean = 1.5 mu_cpt + sum mu_flex."""
    rows, _ = projected
    pool = sim.build_pool([dict(r) for r in rows[900002]], "showdown", slots.spec("NFL", "showdown"))
    pool.q = None
    pool.p_play[:] = 1.0
    order = list(np.argsort(-pool.mu)[:6])
    lu = tuple(int(i) for i in order)
    res = sim.run_contest(pool, [lu], [], 40_000, {}, seed=3)
    want = 1.5 * pool.mu[lu[0]] + pool.mu[list(lu[1:])].sum()
    assert abs(res["mean"][0] / want - 1) < 0.02, (res["mean"][0], want)
    assert sim.lineup_weights(lu, "showdown", pool.spec.cpt_mult)[0] == (lu[0], 1.5)


# ─────────────────────────────────────────────────────────────────────────────
# Roster Position parsing: multi-position, fail-closed, malformed rows
# ─────────────────────────────────────────────────────────────────────────────
HEAD = "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame,Status"
GI = "MIA@BUF 11/29/2026 01:00PM ET"


def _slate(kind="classic"):
    return dk.Slate(draft_group_id=990001, name="x", start_utc=dk.parse_dt("2026-11-29T18:00:00Z"), game_count=1,
                    featured=False, kind=kind, contest_type=21)


def _row(pos, name, i, rp, sal, team="BUF", gi=GI, st=""):
    return f"{pos},{name} ({i}),{name},{i},{rp},{sal},{gi},{team},5.0,{st}"


def test_roster_position_eligibility_cases():
    rows = [
        _row("RB/WR", "Dual A", 91000001, "RB/WR/FLEX", 5000),       # multi-position: RB, WR and FLEX
        _row("WR", "NoFlex B", 91000002, "WR", 4000),                  # missing FLEX token -> fail closed
        _row("K", "Kick C", 91000003, "K", 4500),                      # K in classic -> excluded
        _row("XX", "Odd D", 91000004, "XX/FLEX", 3000),               # unknown token -> excluded
        _row("TE", "Late E", 91000005, "TE/FLEX", 3500, gi="Postponed"),  # unparsable Game Info
        _row("DST", "Bills ", 91000006, "DST", 3000),                  # DK pads DST names
        _row("QB", "Q F", 91000007, "QB", 6000, team="MIA", st="IR"),
        _row("WR", "Q G", 91000008, "WR/FLEX", 6000, team="MIA", st="Q"),
        "WR,Short Row (91000009),Short Row,91000009",                   # short row: skipped
        _row("WR", "Bad Id", "abc", "WR/FLEX", 4000),                  # non-numeric id: skipped
    ]
    s = dk.load_salary_csv(_slate(), "﻿" + "\r\n".join([HEAD] + rows) + "\r\n", "NFL")
    by = {p["name"]: p for p in s.players}
    assert set(by) == {"Dual A", "Bills", "Q F", "Q G"}, set(by)
    assert sorted(by["Dual A"]["eligible"]) == ["FLEX", "RB", "WR"]
    sp = slots.spec("NFL", "classic")
    assert by["Dual A"]["pos_code"] == sp.bit("RB") | sp.bit("WR")
    assert by["Bills"]["position"] == "DST" and by["Bills"]["team_dk"] == "BUF"
    assert by["Q F"]["dk_status"] == "OUT" and by["Q G"]["dk_status"] == "QUESTIONABLE"
    assert s.excluded.get("eligibility") == 3 and s.excluded.get("game_info") == 1, s.excluded
    # CRLF/BOM parse == plain LF parse
    s2 = dk.load_salary_csv(_slate(), "\n".join([HEAD] + rows) + "\n", "NFL")
    assert [p["draftable_id"] for p in s2.players] == [p["draftable_id"] for p in s.players]


def test_multi_position_player_fills_either_slot():
    """A RB/WR player is placed in a slot he is eligible for (RB, WR or FLEX)."""
    sp = slots.spec("NFL", "classic")
    code = sp.bit("RB") | sp.bit("WR")
    others = [sp.bit("QB"), sp.bit("RB"), sp.bit("RB"), sp.bit("WR"), sp.bit("WR"), sp.bit("TE"), sp.bit("TE"),
              sp.bit("DST")]
    res = slots.assign([code] + others, sp.masks)
    assert res is not None
    slot_of_dual = sp.labels[res.index(0)] if 0 in res else None
    assert slot_of_dual in ("WR", "FLEX", "RB"), slot_of_dual
    assert slots.assign([sp.bit("QB")] * 2 + others[1:], sp.masks) is None   # two QBs never fit


def test_empty_and_header_only_csvs():
    with pytest.raises(ValueError):
        dk.load_salary_csv(_slate(), "", "NFL")
    s = dk.load_salary_csv(_slate(), HEAD + "\n", "NFL")
    assert s.players == []


def test_unpopulated_group_skipped(monkeypatch):
    monkeypatch.setattr(dk, "get_text", lambda url: HEAD + "\n")
    with pytest.raises(ValueError):
        dk.load_draftables(_slate(), "NFL")


# ─────────────────────────────────────────────────────────────────────────────
# Scoring: hand-computed real box scores (nflverse 2026 W10 fixture rows)
# ─────────────────────────────────────────────────────────────────────────────
HAND = {   # name: DK points worked by hand from the stat line
    "Jared Goff": 25.42,          # 303*.04=12.12 +3x4 -2x1 +3 rush*.1=.3 +3 (300 bonus)
    "Jahmyr Gibbs": 47.20,        # 153 rush 15.3 +2 TD 12 +7 rec +99 yd 9.9 +3 (rush 100; rec 99 no bonus)
    "Isaiah Likely": 30.70,       # 9 rec +127 yd 12.7 +TD 6 +3 (rec 100)
    "Jayden Daniels": 22.76,      # 249*.04=9.96 +12 -2 +2.8
    "J.J. McCarthy": 18.12,       # 293 -> 11.72 +4 -1 +3.4 (no bonus at 293)
    "Malik Nabers": 8.40,         # 5 rec +4.4 -1 fumble lost
    "Cam Skattebo": 27.20,        # 7.8 +12 +3 +4.4
    "Tommy DeVito": 8.14,         # 3.24 +4 -1 +1.9
    "Joshua Dobbs": 0.08,         # 0.28 -0.2
    "Lan Larison": 9.70,          # 0.2 +2 +1.5 +6
    "Stefon Diggs": 20.30,        # 6 +8.3 +6
}
DST_HAND = {"DET": 11.0,          # 5 sacks +5, 1 INT +2, NE scored 10 -> +4
            "NE": 2.0,            # 2 sacks, 2 INT +4, DET scored 35 -> -4
            "NYG": 5.0,           # 1 sack, 2 INT +4, WAS scored 24 -> 0
            "WAS": 5.0}           # 1 sack, 1 INT +2, 1 fum rec +2, NYG scored 21 -> 0


def test_real_box_scores_hand_computed():
    from pipeline.sports.nfl import data as D
    h = D.build_history(pd.read_csv(FIX / "nflverse_player_week_sample.csv"),
                        pd.read_csv(FIX / "nflverse_team_week_sample.csv"),
                        pd.read_csv(FIX / "nflverse_games_sample.csv"))
    got = {r["name"]: r["dk_fp"] for _, r in h[h.position != "DST"].iterrows()}
    for name, pts in HAND.items():
        assert abs(got[name] - pts) < 0.005, (name, got[name], pts)
    dst = {r["team"]: r["dk_fp"] for _, r in h[h.position == "DST"].iterrows()}
    assert dst == DST_HAND, dst


OFF = scoring.table_for("NFL", "QB")
DST = scoring.table_for("NFL", "DST")


@pytest.mark.parametrize("line,pts", [
    ({"pass_yds": 299}, 11.96), ({"pass_yds": 300}, 15.0), ({"pass_yds": 450, "pass_td": 5, "int": 3}, 38.0),
    ({"rush_yds": 99}, 9.9), ({"rush_yds": 100}, 13.0), ({"rec": 1, "rec_yds": 99}, 10.9), ({"rec_yds": 100}, 13.0),
    ({"rush_yds": 100, "rec_yds": 100}, 26.0), ({"pass_yds": 300, "rush_yds": 100}, 28.0),
    ({"two_pt": 3}, 6.0), ({"fum_lost": 2}, -2.0), ({"ret_td": 1}, 6.0), ({"fum_rec_td": 1}, 6.0),
    ({"rush_yds": -5}, -0.5), ({"pass_yds": 310, "int": 1, "fum_lost": 1, "two_pt": 1}, 15.4),
])
def test_offense_hand_cases(line, pts):
    assert scoring.score(line, OFF) == pytest.approx(pts, abs=0.005)


@pytest.mark.parametrize("pa,pts", [(0, 10), (1, 7), (6, 7), (7, 4), (13, 4), (14, 1), (20, 1), (21, 0), (27, 0),
                                    (28, -1), (34, -1), (35, -4), (60, -4)])
def test_dst_points_allowed_buckets(pa, pts):
    assert scoring.score({"points_allowed": pa}, DST) == pts


def test_dst_categories_hand():
    line = {"sack": 3, "int": 2, "fum_rec": 1, "safety": 1, "blocked_kick": 1, "def_st_td": 2, "points_allowed": 3}
    assert scoring.score(line, DST) == 3 + 4 + 2 + 2 + 2 + 12 + 7


# ─────────────────────────────────────────────────────────────────────────────
# Ownership per position
# ─────────────────────────────────────────────────────────────────────────────
def test_ownership_totals_by_position(projected):
    from pipeline import sports
    from pipeline.sports.nfl import ownership as O
    rows, _ = projected
    fs = sports.get("NFL")["ownership"]["flex_split"]
    work = [dict(r) for r in rows[900001]]
    O.estimate(work, "classic")
    tot = {}
    for r in work:
        tot[r["position"]] = tot.get(r["position"], 0) + r["own"]
    assert abs(sum(tot.values()) - 900) < 0.5
    want = {"QB": 100, "DST": 100, "RB": 200 + 100 * fs["RB"], "WR": 300 + 100 * fs["WR"], "TE": 100 + 100 * fs["TE"]}
    for k, v in want.items():
        assert abs(tot[k] - v) < 0.5, (k, tot[k], v)
    assert max(r["own"] for r in work) <= 80 + 1e-9
    sd = [dict(r) for r in rows[900002]]
    O.estimate(sd, "showdown")
    assert abs(sum(r["own"] for r in sd) - 600) < 0.5


def test_ownership_out_players_zero_and_tiny_group():
    from pipeline.sports.nfl import ownership as O
    rows = [{"position": "QB", "proj": 20.0, "value": 3.0, "salary": 7000, "status": ""},
            {"position": "QB", "proj": 0.0, "value": 0.0, "salary": 6000, "status": "OUT"},
            {"position": "DST", "proj": 8.0, "value": 2.5, "salary": 3000, "status": ""}]
    O.estimate(rows, "classic")              # one live QB and one DST: must terminate
    assert rows[1]["own"] == 0.0
    assert all(np.isfinite(r["own"]) for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler: gating, budget, churn
# ─────────────────────────────────────────────────────────────────────────────
class FakeRunner:
    def __init__(self, groups=0, next_lock=None, dur=None, clock=None):
        self.groups, self.next_lock, self.calls = groups, next_lock, []
        self.dur, self.clock = dur or {}, clock
        self.empty = 0

    def status(self, now):
        return {"groups": self.groups, "next_lock": self.next_lock, "in_progress": False,
                "last_group_seen_at": None, "empty_polls": self.empty, "slates": self.groups}

    def opt_candidates(self, now):
        return []

    def opt_needed(self, dg):
        return False

    def dg_index(self):
        return {}

    def _job(self, name, now):
        self.calls.append((name, now))
        if self.clock is not None:
            self.clock.t += self.dur.get(name, 1.0)
        if name == "lobby" and not self.groups:
            self.empty += 1
        return {"changed": False}

    def __getattr__(self, name):
        if name in ("lobby", "context", "project", "history", "audit"):
            return lambda now: self._job(name, now)
        if name == "salaries":
            return lambda now, ttl_s=None: self._job(name, now)
        raise AttributeError(name)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_off_season_lobby_once_per_day_and_nothing_else(tmp_path):
    from pipeline.scheduler import Scheduler
    fr = FakeRunner()
    s = Scheduler(["NFL"], data_dir=tmp_path, runners={"NFL": fr})
    t = datetime(2027, 6, 1, 12, tzinfo=timezone.utc)
    for k in range(0, 48 * 60, 15):                    # every 15 min for two days
        s.run_due(t + timedelta(minutes=k), 600.0)
    names = [c[0] for c in fr.calls]
    assert set(names) <= {"lobby", "project"}, set(names)
    assert names.count("lobby") == 2, names.count("lobby")
    doc = json.loads((tmp_path / "sports.json").read_text())
    nfl = next(x for x in doc["sports"] if x["code"] == "NFL")
    assert nfl["status"] == "off_season" and nfl["season_note"]


def test_in_season_empty_lobby_backs_off_to_six_hours(tmp_path):
    from pipeline.scheduler import Scheduler
    fr = FakeRunner()
    s = Scheduler(["NFL"], data_dir=tmp_path, runners={"NFL": fr})
    t = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    for k in range(0, 24 * 60, 5):
        s.run_due(t + timedelta(minutes=k), 600.0)
    lob = [c[1] for c in fr.calls if c[0] == "lobby"]
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(lob, lob[1:])]
    assert gaps[-1] >= 6.0 - 1e-9 and len(lob) <= 3 + 24 / 6 + 1, (len(lob), gaps)
    nfl = next(x for x in json.loads((tmp_path / "sports.json").read_text())["sports"] if x["code"] == "NFL")
    assert nfl["status"] == "no_slate"


def test_budget_is_respected_when_durations_match_the_ewma(tmp_path):
    """Steady state: real durations equal to the learned EWMA never exceed t_left."""
    from pipeline.scheduler import Scheduler
    clock = Clock()
    lock = datetime(2026, 11, 29, 18, tzinfo=timezone.utc)
    dur = {"lobby": 4.0, "salaries": 7.0, "context": 5.0, "project": 9.0, "history": 14.0}
    fr = FakeRunner(groups=2, next_lock=lock, dur=dur, clock=clock)
    s = Scheduler(["NFL"], data_dir=tmp_path, runners={"NFL": fr}, clock=clock)
    t = datetime(2026, 11, 29, 16, tzinfo=timezone.utc)
    over = []
    for k in range(120):
        start = clock.t
        budget = [12.0, 20.0, 35.0][k % 3]
        s.run_due(t + timedelta(minutes=k), budget, None)
        if k > 10:
            over.append(clock.t - start - budget)
    assert max(over) <= 1e-9, max(over)


def test_no_change_means_no_publish_trigger(tmp_path):
    """D3: a pass in which nothing changed returns changed=False (no extra publish)."""
    from pipeline.scheduler import Scheduler
    fr = FakeRunner(groups=1, next_lock=datetime(2026, 11, 29, 18, tzinfo=timezone.utc))
    s = Scheduler(["NFL"], data_dir=tmp_path, runners={"NFL": fr})
    t = datetime(2026, 11, 26, 15, tzinfo=timezone.utc)
    s.run_due(t, 600.0)                                    # first pass writes sports.json
    assert s.run_due(t + timedelta(minutes=1), 600.0)["changed"] is False
    assert s.run_due(t + timedelta(hours=2), 600.0)["changed"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Malformed feeds, privacy, kill switch default
# ─────────────────────────────────────────────────────────────────────────────
def test_garbage_nflverse_response_does_not_wipe_history():
    from pipeline.sports.nfl import data as D, runner as R
    D.save_history(N.history())
    r = R.NFLRunner(get_json=lambda u: {}, get_text=lambda u: "<html>rate limited</html>")
    before = len(D.load_history())
    r.history(N.NOW)
    assert before > 0
    assert len(D.load_history()) == before, "saved history replaced"
    assert r.hist is not None and len(r.hist) > 0, "in-memory history emptied by a garbage feed"


def test_conftest_default_is_nba_only_and_production_default_includes_nfl():
    from pipeline import sports
    assert os.environ.get("SIGMA_SPORTS") == "NBA" and sports.enabled() == ["NBA"]
    assert "NFL" in sports.enabled(env="")                   # what the Mac runs with no override


def test_published_nfl_files_hold_no_contest_identifiers(projected):
    """Runner + contest history outputs: no fake usernames, entry ids or contest ids."""
    from pipeline import contest_history as H
    from pipeline import contests as C
    root = Path(config.DATA_DIR)
    c = C.parse(N.contest_export(60))
    c.update(id="888123456", file_date="2026-11-30")
    out = root / "nfl" / "contest_history.json"
    H.write_public([c], root / "snaps", out, sport="NFL",
                   context=H.nfl_context(N.history(), pd.DataFrame([{"season": 2026, "week": 12,
                                                                      "gameday": "2026-11-29"}])))
    blob = "\n".join(p.read_text(errors="ignore") for p in root.rglob("*") if p.is_file() and p.suffix in (".json", ".csv"))
    assert "fakeuser" not in blob.lower() and "888123456" not in blob
    assert not any(str(9_000_000 + k) in blob for k in range(60))


# ─────────────────────────────────────────────────────────────────────────────
# Real public DK files (skip unless ANALYST_DK_DIR is set)
# ─────────────────────────────────────────────────────────────────────────────
def _real(name):
    p = DK_DIR / name
    if not p.exists():
        pytest.skip(f"{name} not available")
    return p


def test_real_nfl_lobby_matches_independent_filter():
    lob = json.loads(_real("lobby_NFL.json").read_text())
    now = datetime(2026, 11, 26, 21, tzinfo=timezone.utc)
    skip = ("tiers", "snake", "pick6", "in-game", "best ball", "arcade", "single stat")
    want = {}
    for g in lob["DraftGroups"]:
        tag = " ".join(str(g.get(k) or "") for k in ("DraftGroupTag", "ContestStartTimeSuffix", "GameType",
                                                     "ContestTypeName")).lower()
        start = datetime.fromisoformat(g["StartDate"][:19]).replace(tzinfo=timezone.utc)
        if g.get("GameTypeId") in (1, 96) and not any(w in tag for w in skip) and \
                now - timedelta(hours=12) <= start < now + timedelta(hours=144):
            want[g["DraftGroupId"]] = ("classic" if g["GameTypeId"] == 1 else "showdown",
                                       21 if g["GameTypeId"] == 1 else 96)
    import pipeline.sources.draftkings as dkm
    old = dkm.get_json
    dkm.get_json = lambda url: lob
    try:
        got = {s.draft_group_id: (s.kind, s.contest_type) for s in dk.list_draft_groups(now, "NFL")}
    finally:
        dkm.get_json = old
    assert got == want and got


def test_real_nfl_classic_csv_every_player_parsed():
    text = _real("sal_21_157066.csv").read_text(encoding="utf-8-sig")
    f = DKFile(text)
    s = dk.load_salary_csv(_slate(), text, "NFL")
    assert not s.excluded and len(s.players) == len(f.by_id)
    for p in s.players:
        raw = f.by_id[p["draftable_id"]]
        assert p["position"] == raw["pos"] and set(p["eligible"]) == raw["slots"]
        assert p["salary"] == raw["salary"] and p["name"] == raw["name"]
    assert len({g["competition_id"] for g in s.games}) == len({r["game"] for r in f.by_id.values()})


def test_real_nfl_showdown_csv_captain_rows():
    text = _real("sal_96_157076.csv").read_text(encoding="utf-8-sig")
    f = DKFile(text)
    s = dk.load_salary_csv(_slate("showdown"), text, "NFL")
    assert s.kind == "showdown" and not s.excluded
    assert len(s.players) * 2 == len(f.by_id)
    for p in s.players:
        assert "CPT" in f.by_id[p["cpt_id"]]["slots"] and "FLEX" in f.by_id[p["draftable_id"]]["slots"]
        assert p["cpt_salary"] == f.by_id[p["cpt_id"]]["salary"] == round(1.5 * p["salary"])
    assert {"K", "DST"} <= {p["position"] for p in s.players}


def test_one_garbage_nflverse_response_does_not_poison_the_cache():
    """A single bad 200 response (HTML error page) for a past season must not stop
    later refreshes from rebuilding history once nflverse answers properly."""
    from pipeline.sports.nfl import data as D
    good_pw = (FIX / "nflverse_player_week_sample.csv").read_text()
    good_tw = (FIX / "nflverse_team_week_sample.csv").read_text()
    good_gm = (FIX / "nflverse_games_sample.csv").read_text()

    def good(url):
        return good_gm if "games.csv" in url else good_tw if "stats_team" in url else good_pw
    calls = []

    def bad_once(url):
        calls.append(url)
        if "2025" in url and len([c for c in calls if "2025" in c]) == 1:
            return "<html>503</html>"
        return good(url)
    D.refresh_history([2025, 2026], 2026, 0.0, bad_once)          # first refresh hits the bad page
    hist = D.refresh_history([2025, 2026], 2026, 0.0, bad_once)   # nflverse is fine again
    import shutil
    shutil.rmtree(D.paths("NFL").cache)
    clean = D.refresh_history([2025, 2026], 2026, 0.0, good)
    assert len(hist) == len(clean), f"past season dropped for good: {len(hist)} rows vs {len(clean)}"


# ═════════════════════════════════════════════════════════════════════════════
# Retest 1 (P3-5 Wave 1 Round 1 fixes F1–F6)
# ═════════════════════════════════════════════════════════════════════════════
def _nflverse_good(url):
    if "games.csv" in url:
        return (FIX / "nflverse_games_sample.csv").read_text()
    return (FIX / ("nflverse_team_week_sample.csv" if "stats_team" in url else "nflverse_player_week_sample.csv")).read_text()


def test_f1_poisoned_cache_on_disk_heals_and_bad_body_never_cached():
    from pipeline.sports.nfl import data as D
    cache = D.paths("NFL").cache
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "stats_player_week_2025.csv").write_text("<html>old poison</html>")
    h = D.refresh_history([2025, 2026], 2026, 0.0, _nflverse_good)
    assert "<html>" not in (cache / "stats_player_week_2025.csv").read_text()
    assert len(h) == 224
    # a bad body for a past season with no valid cache: season skipped, health fail, nothing cached
    (cache / "stats_player_week_2025.csv").unlink()
    from pipeline import health
    bad = lambda u: "<html>503</html>" if "2025" in u else _nflverse_good(u)   # noqa: E731
    h2 = D.refresh_history([2025, 2026], 2026, 0.0, bad)
    assert health.snapshot("NFL")["nflverse"]["ok"] is False
    only = D.refresh_history([2026], 2026, 0.0, _nflverse_good)
    assert len(h2) == len(only) > 0 and not (cache / "stats_player_week_2025.csv").exists()
    # a bad current-season body falls back to the valid cached copy; with no cache it raises
    assert len(D.refresh_history([2026], 2026, 0.0, lambda u: "<html>503</html>" if "2026" in u else _nflverse_good(u))) > 0
    for f in cache.glob("stats_*_2026.csv"):
        f.unlink()
    with pytest.raises(Exception):
        D.refresh_history([2025, 2026], 2026, 0.0, lambda u: "<html>503</html>" if "2026" in u else _nflverse_good(u))


def test_f1_truncated_body_is_rejected_and_cached_truncation_refetched():
    """R1-B2 (Retest 1b): a body cut mid-file is rejected and never cached; a truncated
    past-season file already in the cache is replaced on the next read. A cut exactly at a
    row boundary still validates (documented residual)."""
    from pipeline.sports.nfl import data as D
    full = _nflverse_good("stats_player")
    cut = full[: len(full) // 3]                                   # header + some rows + a partial row
    assert not D.PLAYER_WEEK_OK(cut)
    cache = D.paths("NFL").cache / "stats_player_week_2024.csv"
    with pytest.raises(Exception):
        D.cached_text("u", cache.name, None, lambda u: cut, D.PLAYER_WEEK_OK)
    assert not cache.exists()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(cut)                                          # cached before the fix
    assert D.cached_text("u", cache.name, None, lambda u: full, D.PLAYER_WEEK_OK) == full
    assert cache.read_text() == full
    boundary = full[: full.rfind("\n", 0, len(full) // 3) + 1]     # whole rows only
    assert D.PLAYER_WEEK_OK(boundary)                               # residual: accepted


def test_f4_return_fumble_counts():
    from pipeline.sports.nfl import data as D
    pw = pd.read_csv(FIX / "nflverse_player_week_sample.csv")
    i = pw.index[pw.player_display_name == "Stefon Diggs"][0]
    pw.loc[i, "fumbles_lost_total"] = 1                             # e.g. a muffed punt; rush/rec/sack columns 0
    h = D.build_history(pw, pd.read_csv(FIX / "nflverse_team_week_sample.csv"), pd.read_csv(FIX / "nflverse_games_sample.csv"))
    assert h.loc[h.name == "Stefon Diggs", "dk_fp"].iloc[0] == pytest.approx(19.30)


def test_f2_synthetic_showdown_field_full_legal_and_spends(projected):
    rows, rules = projected
    sdf = DKFile(N.salary_csv("showdown"))
    pool = sim.build_pool([dict(r) for r in rows[900002]], "showdown", slots.spec("NFL", "showdown"))
    for n in (500, 2000):
        field = sim.sample_field(pool, n, np.random.default_rng(5), rules)
        assert len(field) >= 0.95 * n, (n, len(field))
        ids = [[int(pool.cpt_ids[lu[0]])] + [int(pool.ids[i]) for i in lu[1:]] for lu in field]
        assert not [v for x in ids for v in showdown_violations(x, sdf)]
        sal = [sum(sdf.by_id[i]["salary"] for i in x) for x in ids]
        assert np.median(sal) >= 47_000 and min(sal) >= 45_000


class AuditRunner(FakeRunner):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.audits = []

    def audit(self, now):
        self.audits.append(now)
        return {"changed": True}


def _live_like(s, start, hours, nba_lock=None):
    """60 s loops; scheduler with t_left 50 s; the 600 s background block every 10th loop."""
    for k in range(hours * 60):
        t = start + timedelta(minutes=k)
        s.run_due(t, 50.0, nba_lock)
        if k % 10 == 0:
            s.run_background(t, nba_lock)


def test_f6_weekly_audit_runs_once_at_production_cadence(tmp_path):
    from pipeline.scheduler import Scheduler
    fr = AuditRunner(groups=1, next_lock=datetime(2026, 12, 6, 18, tzinfo=timezone.utc))
    s = Scheduler(["NFL"], data_dir=tmp_path, runners={"NFL": fr})
    tue = datetime(2026, 12, 1, 15, 30, tzinfo=timezone.utc)            # Tuesday 10:30 ET
    _live_like(s, tue, 8)
    assert len(fr.audits) == 1                                          # first run seeds immediately
    fr.next_lock = datetime(2026, 12, 13, 18, tzinfo=timezone.utc)      # runner.status reports upcoming locks only
    _live_like(s, tue + timedelta(days=7), 8)                           # next Tuesday: once more
    assert len(fr.audits) == 2


def test_f6_audit_respects_lock_guards(tmp_path):
    from pipeline.scheduler import Scheduler
    tue = datetime(2026, 12, 1, 15, 30, tzinfo=timezone.utc)
    fr = AuditRunner(groups=1, next_lock=datetime(2026, 12, 6, 18, tzinfo=timezone.utc))
    s = Scheduler(["NFL"], data_dir=tmp_path / "a", runners={"NFL": fr})
    assert s.run_background(tue, tue + timedelta(minutes=19))["ran"] == []      # NBA locks in 19 min
    fr2 = AuditRunner(groups=1, next_lock=tue + timedelta(hours=2, minutes=59))
    s2 = Scheduler(["NFL"], data_dir=tmp_path / "b", runners={"NFL": fr2})
    assert s2.run_background(tue, None)["ran"] == []                           # NFL locks in < 3 h
    assert fr.audits == [] and fr2.audits == []


def test_f6_failed_first_audit_is_retried_soon(tmp_path):
    """A failed audit (e.g. no history yet on a fresh Mac) must not count as this week's run."""
    from pipeline.scheduler import Scheduler

    class Failing(AuditRunner):
        def audit(self, now):
            self.audits.append(now)
            if len(self.audits) == 1:
                raise KeyError("season")
            return {"changed": True}
    fr = Failing(groups=1, next_lock=datetime(2026, 12, 6, 18, tzinfo=timezone.utc))
    s = Scheduler(["NFL"], data_dir=tmp_path, runners={"NFL": fr})
    thu = datetime(2026, 11, 26, 15, tzinfo=timezone.utc)
    _live_like(s, thu, 24)
    assert len(fr.audits) >= 2, f"audit retried {len(fr.audits) - 1} times in 24 h after a failure"


def test_r1_new_opt_slate_uses_sport_level_ewma(tmp_path):
    from pipeline.scheduler import Job, Scheduler
    s = Scheduler(["NFL"], data_dir=tmp_path, runners={"NFL": FakeRunner()})
    st = {"jobs": {"NFL:opt": {"ewma": 6.3}}}
    assert s.expected(st, Job("NFL", "opt", 5.0, 100.0, 123456)) == 6.3


def test_r2_nfl_parse_outside_any_scope_stays_out_of_nba_health():
    from pipeline import health
    saved, health._active[:] = list(health._active), []           # leave the autouse scope for this check
    try:
        before = dict(health._status)
        dk.load_salary_csv(_slate(), "\n".join([HEAD, _row("K", "Kick C", 91000003, "K", 4500)]) + "\n", "NFL")
        assert health._status == before
    finally:
        health._active[:] = saved


def test_f2_underfilled_field_is_flagged(projected, monkeypatch):
    pytest.importorskip("highspy")
    from pipeline import health
    rows, rules = projected
    sp = dataclasses.replace(slots.spec("NFL", "showdown"), field_min_salary=50_001)      # impossible floor
    monkeypatch.setitem(config.SIM, "portfolio_sims", 500)
    res = sim.build_slate_lineups([dict(r) for r in rows[900002]], rules, {}, n_lineups=20, sims=500, field_size=200,
                                  exact=False, kind="showdown", spec=sp)
    assert res.get("field_filled") is not None and res["field_filled"] < 0.5, res.get("field_filled")
    assert health.snapshot("NFL").get("Simulation field", {}).get("ok") is False


def test_r1b1_failing_or_skipped_audit_retries_hourly_then_weekly(tmp_path):
    """Retest 1b: failures/skips back off ~1 h (not a week); after a success, at most weekly."""
    from pipeline.scheduler import Scheduler

    class Flaky(AuditRunner):
        def audit(self, now):
            self.audits.append(now)
            k = len(self.audits)
            if k <= 3:
                raise KeyError("season")
            if k <= 5:
                return {"changed": False, "skipped": "no NFL history yet"}
            return {"changed": True}
    fr = Flaky(groups=1, next_lock=datetime(2026, 12, 13, 18, tzinfo=timezone.utc))
    s = Scheduler(["NFL"], data_dir=tmp_path, runners={"NFL": fr})
    thu = datetime(2026, 11, 26, 15, tzinfo=timezone.utc)
    _live_like(s, thu, 24)
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(fr.audits, fr.audits[1:])]
    assert len(fr.audits) == 6, fr.audits                  # 3 errors + 2 skips, then one success
    assert all(1.0 <= g <= 1.2 for g in gaps), gaps         # ~hourly (600 s block granularity)
    _live_like(s, thu + timedelta(days=1), 24 * 4)          # Fri..Mon: no more runs within the week
    assert len(fr.audits) == 6
